"""v3 Met ingest via the Hugging Face ``metmuseum/openaccess`` dataset.

Companion to ``ml.ingest.met_csv`` (v2 CSV-dump path) and ``ml.ingest.met_db``
(v1 live-API path).

Key advantage
-------------
The HF dataset includes ``primaryImage`` and ``primaryImageSmall`` columns
directly — the Met API is bypassed entirely. No /objects/{id} requests, no
IP-ban risk (Azure egress IP bans only affect the Collection API, not the CDN).

Pipeline
--------
1. Stream rows from ``metmuseum/openaccess`` HF dataset via the ``datasets``
   library (``streaming=True`` — no full download required).
2. Pre-filter: ``isPublicDomain == True`` (accept bool or "True" string).
3. Convert row to the Met API JSON shape via :func:`convert_hf_row`:
   - Coerce ``objectID`` (str → int), ``isPublicDomain`` (str → bool),
     ``objectBeginDate`` / ``objectEndDate`` (str → int or None).
   - Parse ``tags`` from JSON string → list-of-dicts (or [] on failure).
   All other field names already match the API response 1:1.
4. Call :func:`~ml.ingest.met_db.map_met_record` to produce a canonical row.
5. Download ``mapped["image_url"]`` (``primaryImage``) from Met CDN using the
   browser-like ``_IMAGE_CDN_HEADERS`` (CDN returns 406 for plain API headers).
6. Embed with SigLIP via :meth:`~ml.embeddings.Embedder.embed_bytes`.
7. UPSERT into ``artworks`` via the same SQL as v1/v2.

Hard rules
----------
* Image bytes live in memory only (D-012 / hard rule #3 — never on disk).
* All preprocessing through ``ml.imageops.prepare_for_embedding`` (hard rule #4)
  via the embedder seam.
* Output rows are identical to v1/v2 — same UPSERT SQL, same idempotency key.
* CDN requests are rate-limited by ``image_delay`` (default 0.1 s / 10 img/s).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

import asyncpg
import httpx
import numpy as np

from ml.embeddings import get_embedder
from ml.ingest.met_db import (
    DEFAULT_BATCH_COMMIT_SIZE,
    DEFAULT_HTTP_TIMEOUT_S,
    MET_SOURCE_SLUG,
    _IMAGE_CDN_HEADERS,
    _INSERT_SQL,
    format_pgvector,
    map_met_record,
)

logger = logging.getLogger("ml.ingest.met_hf")


# ----------------------------------------------------------------- constants

HF_DATASET_NAME = "metmuseum/openaccess"

DEFAULT_IMAGE_DELAY_S = 0.1
DEFAULT_CONSECUTIVE_CDN_FAIL_LIMIT = 50

PROGRESS_LOG_INTERVAL = 1000

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Safari/605.1.15"
)


# ----------------------------------------------------------------- exceptions


class MetCDNCircuitBreakerError(RuntimeError):
    """Raised when ``DEFAULT_CONSECUTIVE_CDN_FAIL_LIMIT`` consecutive Met CDN
    image downloads fail, indicating a CDN-level block or unreachable host.

    Unlike the API circuit breaker in ``met_csv``, this triggers on HTTP
    errors from ``images.metmuseum.org`` rather than 403s from the Collection
    API. Recovery is usually transient: wait a few minutes and retry.
    """


# ----------------------------------------------------------------- stats


@dataclass
class HFIngestStats:
    """Outcome counters for a HF-path ingest run.

    ``hf_rows_*`` counters track the raw HF row pipeline before
    ``map_met_record()`` is called.  ``skipped_*`` counters track post-map
    rejections consistent with v1/v2 stat shapes.
    """

    hf_rows_total: int = 0
    hf_rows_accepted: int = 0
    hf_rows_skipped_filter: int = 0
    hf_rows_skipped_no_image: int = 0
    fetched_images: int = 0
    skipped_filter: int = 0       # map_met_record rejection
    skipped_existing: int = 0
    skipped_image_error: int = 0
    skipped_embed_error: int = 0
    skipped_db_error: int = 0
    inserted: int = 0
    updated: int = 0
    last_object_ids: list[int] = field(default_factory=list)

    @property
    def total_persisted(self) -> int:
        return self.inserted + self.updated

    def as_dict(self) -> dict[str, Any]:
        return {
            "hf_rows_total": self.hf_rows_total,
            "hf_rows_accepted": self.hf_rows_accepted,
            "hf_rows_skipped_filter": self.hf_rows_skipped_filter,
            "hf_rows_skipped_no_image": self.hf_rows_skipped_no_image,
            "fetched_images": self.fetched_images,
            "skipped_filter": self.skipped_filter,
            "skipped_existing": self.skipped_existing,
            "skipped_image_error": self.skipped_image_error,
            "skipped_embed_error": self.skipped_embed_error,
            "skipped_db_error": self.skipped_db_error,
            "inserted": self.inserted,
            "updated": self.updated,
            "total_persisted": self.total_persisted,
        }


# ----------------------------------------------------------------- row conversion


def _row_passes_hf_filter(row: dict[str, Any]) -> bool:
    """Return True iff the HF row's ``isPublicDomain`` is truthy.

    Accepts both the bool ``True`` and the string ``"True"`` (both appear in
    the HF dataset depending on how it was converted from the original CSV).

    Pure function — no I/O, fully unit-testable.
    """
    val = row.get("isPublicDomain")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() == "true"
    return False


def _parse_int_or_none(val: Any) -> int | None:
    """Coerce a string (or int) to int; return None on empty / unconvertible."""
    if val is None:
        return None
    if isinstance(val, int):
        return val if val != 0 else None
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def convert_hf_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw HF dataset row to a :func:`map_met_record`-compatible dict.

    The HF ``metmuseum/openaccess`` CSV has field names that match the Met
    ``/objects/{id}`` API JSON response exactly — with three exceptions that
    this function normalises:

    1. ``objectID`` is a string in the CSV → coerced to ``int``.
    2. ``isPublicDomain`` is the string ``"True"``/``"False"`` → coerced to
       ``bool``.
    3. ``tags`` is a JSON-serialised string of the list-of-dicts the API
       returns → parsed via ``json.loads()``.
    4. ``objectBeginDate`` / ``objectEndDate`` are strings → coerced to ``int``
       (or ``None`` for empty/zero values that ``map_met_record`` normalises).

    All other fields pass through unchanged.

    Pure function — no I/O.
    """
    out = dict(row)

    # 1. objectID: string → int
    raw_id = out.get("objectID")
    if raw_id is not None and not isinstance(raw_id, int):
        try:
            out["objectID"] = int(str(raw_id).strip())
        except (ValueError, TypeError):
            pass  # leave as-is; map_met_record will return None for bad ID

    # 2. isPublicDomain: string → bool
    ipd = out.get("isPublicDomain")
    if isinstance(ipd, str):
        out["isPublicDomain"] = ipd.strip().lower() == "true"

    # 3. tags: JSON string → list
    tags_raw = out.get("tags")
    if isinstance(tags_raw, str):
        stripped = tags_raw.strip()
        if not stripped or stripped.lower() == "null":
            out["tags"] = []
        else:
            try:
                parsed = json.loads(stripped)
                out["tags"] = parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, ValueError):
                out["tags"] = []
    elif not isinstance(tags_raw, list):
        out["tags"] = []

    # 4. objectBeginDate / objectEndDate: string → int (or None)
    for key in ("objectBeginDate", "objectEndDate"):
        val = out.get(key)
        if val is not None and not isinstance(val, int):
            s = str(val).strip()
            if not s:
                out[key] = None
            else:
                try:
                    out[key] = int(s)
                except ValueError:
                    out[key] = None

    return out


# ----------------------------------------------------------------- HTTP helpers


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes:
    """Download Met CDN image into memory only (D-012, hard rule #3).

    The Met image CDN (``images.metmuseum.org``) returns 406 for plain
    JSON-API ``User-Agent`` headers. We use browser-like headers from
    ``_IMAGE_CDN_HEADERS`` — same as ``met_db`` and ``met_csv``.

    Never written to disk. Never logged. Caller is responsible for discarding
    the bytes once embedding is complete.
    """
    resp = await client.get(url, headers=_IMAGE_CDN_HEADERS)
    resp.raise_for_status()
    return resp.content


# ----------------------------------------------------------------- DB helpers


async def _load_existing_source_ids(pool: asyncpg.Pool, source: str) -> set[str]:
    """Return source_ids already in DB for this source (resume-skip-existing)."""
    rows = await pool.fetch(
        "SELECT source_id FROM artworks WHERE source = $1",
        source,
    )
    return {r["source_id"] for r in rows}


async def _commit_batch(
    pool: asyncpg.Pool,
    batch: list[dict[str, Any]],
    *,
    stats: HFIngestStats,
) -> None:
    """Single-transaction UPSERT for a batch of embedded rows.

    Mirrors ``met_csv._commit_batch`` exactly — same SQL, same stats counting.
    """
    if not batch:
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            for row in batch:
                vec_text = format_pgvector(row["embedding"])
                try:
                    record = await conn.fetchrow(
                        _INSERT_SQL,
                        row["id"],
                        row["source"],
                        row["source_id"],
                        row["title"],
                        row["artist"],
                        row["date"],
                        row["medium"],
                        row["culture"],
                        row["period"],
                        row["museum"],
                        row["source_url"],
                        row["image_url"],
                        row["tags"],
                        row["is_public_domain"],
                        json.dumps(row["raw_metadata"]),
                        vec_text,
                        row.get("artist_bio"),
                        row.get("credit_line"),
                        row.get("dimensions"),
                        row.get("dynasty"),
                        row.get("object_wikidata_url"),
                        row.get("date_begin"),
                        row.get("date_end"),
                    )
                except Exception:  # noqa: BLE001
                    stats.skipped_db_error += 1
                    logger.exception(
                        "DB upsert failed for %s; rolling back batch",
                        row.get("id"),
                    )
                    raise
                if record and record["inserted"]:
                    stats.inserted += 1
                    logger.info(
                        "inserted %s title=%r",
                        row["id"],
                        (row.get("title") or "")[:80],
                    )
                else:
                    stats.updated += 1
                    logger.info(
                        "updated %s title=%r",
                        row["id"],
                        (row.get("title") or "")[:80],
                    )


# ----------------------------------------------------------------- dataset loader


def _iter_hf_rows(
    hf_dataset_name: str,
    offset: int = 0,
) -> Iterable[dict[str, Any]]:
    """Yield raw HF rows from the ``metmuseum/openaccess`` streaming dataset.

    Uses ``datasets.load_dataset`` with ``streaming=True`` so no full download
    is required before iteration begins.  The ``offset`` parameter skips the
    first N rows of the stream (for resuming partial runs).

    Raises ``ImportError`` if the ``datasets`` package is not installed.
    """
    try:
        from datasets import load_dataset  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "The 'datasets' package is required for the met-hf ingest adapter. "
            "Install it with: pip install 'datasets>=2.14'"
        ) from exc

    ds = load_dataset(hf_dataset_name, split="train", streaming=True)
    if offset:
        ds = ds.skip(offset)
    yield from ds


# ----------------------------------------------------------------- main


async def ingest_met_hf_to_db(
    pool: asyncpg.Pool | None,
    *,
    rows: Iterable[dict[str, Any]] | None = None,
    limit: int = 0,
    offset: int = 0,
    batch_commit_size: int = DEFAULT_BATCH_COMMIT_SIZE,
    embedder: Any | None = None,
    image_delay: float = DEFAULT_IMAGE_DELAY_S,
    request_delay: float | None = None,
    circuit_breaker_threshold: int = DEFAULT_CONSECUTIVE_CDN_FAIL_LIMIT,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
    dry_run: bool = False,
    resume_skip_existing: bool = False,
    hf_dataset_name: str = HF_DATASET_NAME,
    max_records: int | None = None,
) -> HFIngestStats:
    """v3 Met ingest: HF dataset → Met CDN image download → SigLIP embed → UPSERT.

    Parameters
    ----------
    pool:
        asyncpg pool. Required unless ``dry_run=True``.
    rows:
        An iterable of raw HF row dicts. When ``None`` (the default), rows are
        streamed from the HF dataset named by ``hf_dataset_name``. Tests pass
        an explicit list here so no network access is needed.
    limit:
        Max records to successfully ingest (post-embed). ``0`` / ``None``
        means unlimited.
    offset:
        Skip the first N rows of the HF stream before filtering. Only
        meaningful when ``rows=None``; ignored for an explicit ``rows`` list.
    batch_commit_size:
        DB transaction granularity (rows per txn). Default 50.
    embedder:
        Object exposing ``embed_bytes(bytes) -> np.ndarray``. Defaults to the
        project default via ``get_embedder()``.
    image_delay:
        Floor (seconds) between successive Met CDN image downloads. Default
        0.1 s (≈10 img/s). Polite and well within CDN limits.
    request_delay:
        Alias for ``image_delay``; if set, takes precedence. Provided for
        CLI flag consistency with other ingest adapters.
    circuit_breaker_threshold:
        Abort with :exc:`MetCDNCircuitBreakerError` after this many consecutive
        CDN image download failures. Default 50.
    timeout:
        HTTP request timeout in seconds.
    dry_run:
        Skip DB writes; still downloads images and embeds end-to-end.
    resume_skip_existing:
        On startup, load the set of ``source_id`` values already in DB for
        ``source='met'`` and skip those records (same D-060 pattern as v2).
    hf_dataset_name:
        HF dataset identifier. Override for testing with a local fixture.
    max_records:
        Stop after processing this many rows from the HF stream (before
        filtering). Useful for ``--max-records 20`` smoke runs.

    Returns
    -------
    HFIngestStats
    """
    if circuit_breaker_threshold <= 0:
        raise ValueError("circuit_breaker_threshold must be > 0")
    if not dry_run and pool is None:
        raise ValueError("pool is required when dry_run is False")

    effective_delay = request_delay if request_delay is not None else image_delay
    unlimited = limit is None or limit <= 0

    stats = HFIngestStats()
    embedder = embedder or get_embedder()

    skip_existing_ids: set[str] = set()
    if resume_skip_existing and pool is not None:
        skip_existing_ids = await _load_existing_source_ids(pool, MET_SOURCE_SLUG)
        logger.info(
            "resume-skip-existing: found %d existing met source_ids; skipping them",
            len(skip_existing_ids),
        )

    row_source: Iterable[dict[str, Any]]
    if rows is not None:
        row_source = rows
    else:
        row_source = _iter_hf_rows(hf_dataset_name, offset=offset)

    ingested_so_far = 0
    consecutive_cdn_failures = 0
    commit_batch: list[dict[str, Any]] = []

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    ) as client:

        scanned = 0
        for hf_row in row_source:
            if max_records is not None and scanned >= max_records:
                break
            scanned += 1

            stats.hf_rows_total += 1

            if stats.hf_rows_total % PROGRESS_LOG_INTERVAL == 0:
                logger.info(
                    "met-hf progress: scanned=%d accepted=%d skipped_filter=%d "
                    "ingested=%d cdn_errors=%d",
                    stats.hf_rows_total,
                    stats.hf_rows_accepted,
                    stats.hf_rows_skipped_filter,
                    ingested_so_far,
                    stats.skipped_image_error,
                )

            # ---- isPublicDomain pre-filter ----
            if not _row_passes_hf_filter(hf_row):
                stats.hf_rows_skipped_filter += 1
                continue
            stats.hf_rows_accepted += 1

            # ---- convert to API shape ----
            api_dict = convert_hf_row(hf_row)

            # ---- map_met_record filter ----
            mapped = map_met_record(api_dict)
            if mapped is None:
                stats.skipped_filter += 1
                continue

            # ---- resume-skip-existing ----
            source_id = mapped["source_id"]
            if skip_existing_ids and source_id in skip_existing_ids:
                stats.skipped_existing += 1
                continue

            # ---- check limit ----
            if not unlimited and ingested_so_far >= limit:
                break

            # ---- CDN image download ----
            img_url = mapped["image_url"]
            if not img_url:
                stats.hf_rows_skipped_no_image += 1
                continue

            if effective_delay > 0:
                await asyncio.sleep(effective_delay)

            try:
                img_bytes = await _download_image(client, img_url)
                stats.fetched_images += 1
                consecutive_cdn_failures = 0
            except httpx.HTTPError as exc:
                consecutive_cdn_failures += 1
                logger.warning(
                    "met-hf CDN image download failed for %s (%s): %s "
                    "[consecutive_failures=%d]",
                    source_id,
                    img_url,
                    exc,
                    consecutive_cdn_failures,
                )
                stats.skipped_image_error += 1
                if consecutive_cdn_failures >= circuit_breaker_threshold:
                    raise MetCDNCircuitBreakerError(
                        f"Circuit breaker tripped: {consecutive_cdn_failures} consecutive "
                        f"Met CDN image download failures (last source_id={source_id}). "
                        "The CDN may be blocking the egress IP or be temporarily "
                        "unreachable. Wait a few minutes and retry."
                    ) from exc
                continue

            # ---- embed ----
            try:
                vec = await asyncio.to_thread(embedder.embed_bytes, img_bytes)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "met-hf embed failed for %s: %s", source_id, exc
                )
                stats.skipped_embed_error += 1
                continue
            finally:
                img_bytes = None  # type: ignore[assignment]  # release memory

            norm = float(np.linalg.norm(np.asarray(vec)))
            mapped["embedding"] = vec
            stats.last_object_ids.append(int(source_id))
            logger.info(
                "embed met:%s title=%r vec_norm=%.4f",
                source_id,
                (mapped.get("title") or "")[:80],
                norm,
            )
            ingested_so_far += 1

            if dry_run:
                stats.inserted += 1
                continue

            commit_batch.append(mapped)
            if len(commit_batch) >= batch_commit_size:
                await _commit_batch(pool, commit_batch, stats=stats)
                commit_batch.clear()

        # Flush trailing batch.
        if commit_batch and not dry_run:
            await _commit_batch(pool, commit_batch, stats=stats)
            commit_batch.clear()

    logger.info(
        "met-hf done: hf_rows_total=%d accepted=%d skipped_filter=%d "
        "fetched_images=%d ingested=%d inserted=%d updated=%d "
        "skipped_image=%d skipped_embed=%d skipped_db=%d",
        stats.hf_rows_total,
        stats.hf_rows_accepted,
        stats.hf_rows_skipped_filter,
        stats.fetched_images,
        ingested_so_far,
        stats.inserted,
        stats.updated,
        stats.skipped_image_error,
        stats.skipped_embed_error,
        stats.skipped_db_error,
    )
    return stats


# ----------------------------------------------------------------- public API

__all__ = [
    "DEFAULT_CONSECUTIVE_CDN_FAIL_LIMIT",
    "DEFAULT_IMAGE_DELAY_S",
    "HF_DATASET_NAME",
    "HFIngestStats",
    "MetCDNCircuitBreakerError",
    "_row_passes_hf_filter",
    "convert_hf_row",
    "ingest_met_hf_to_db",
]
