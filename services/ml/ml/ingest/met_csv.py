"""v2 Met ingest via the published CSV dump.

Companion to ``ml.ingest.met_db`` (the API-discovery path). The two adapters
emit IDENTICAL rows (same canonical id, same shared :func:`map_met_record`,
same UPSERT, same enrichment fields) but reach them via different paths:

* ``met_db`` walks the live ``/objects`` endpoint, fetching every candidate
  id sequentially. Used for incremental syncs and live re-ingest.
* ``met_csv`` (this module) reads ``metmuseum/openaccess`` ``MetObjects.csv``
  from disk (downloaded once, cached for 7 days), applies a CSV-side
  pre-filter (PD + classification denylist) BEFORE any network call, then
  fetches the full ``/objects/{id}`` JSON to map + embed. Used for bulk
  catalog rebuilds where the savings come from
    (a) skipping ``/objects`` listing pagination entirely,
    (b) skipping ``/objects/{id}`` calls on records we'd reject anyway,
    (c) batched MPS embedding (see :meth:`ml.embeddings._HFEmbedder.embed_batch`).

Why we still hit ``/objects/{id}`` per record
---------------------------------------------
The CSV does not contain the ``primaryImage`` URL (only ``Link Resource``,
the human-facing object page) and the Met image CDN URL is NOT derivable
from CSV-published fields. We therefore call ``/objects/{id}`` per accepted
record, exactly as ``met_db`` does. The dump still saves wall time because
the CSV pre-filter eliminates ~50% of candidate ids before any API request
(non-PD + denylisted classifications).

Hard rules
----------
* Image bytes live in memory only (D-012 / hard rule #3 — never on disk,
  never logged).
* All preprocessing goes through ``ml.imageops.prepare_for_embedding`` via
  the embedder seam (hard rule #4 — no parallel pipeline).
* No schema changes; output rows are identical to ``met_db`` and idempotent
  via ``ON CONFLICT (source, source_id)``.
* ``met_db.py``, ``aic_db.py``, ``rijks_db.py`` are NOT modified — they
  remain the live-sync path.

CSV cache
---------
Downloaded once to ``~/.cache/art-guide/met-objects.csv`` (override via
``ART_GUIDE_CACHE_DIR``). The Git LFS URL
``https://media.githubusercontent.com/media/metmuseum/openaccess/refs/heads/master/MetObjects.csv``
serves the actual CSV bytes (not the LFS pointer) without requiring
``git-lfs`` to be installed. Refresh policy: re-download if the cached
file is older than 7 days.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import random
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import asyncpg
import httpx
import numpy as np

from ml.embeddings import get_embedder
from ml.ingest.met import DEFAULT_BASE_URL, DEFAULT_USER_AGENT
from ml.ingest.met_db import (
    DEFAULT_BATCH_COMMIT_SIZE,
    DEFAULT_HTTP_TIMEOUT_S,
    DEFAULT_MAX_RETRIES,
    DEFAULT_REQUEST_DELAY_S,
    MET_SOURCE_SLUG,
    _IMAGE_CDN_HEADERS,
    _INSERT_SQL,
    format_pgvector,
    map_met_record,
)

logger = logging.getLogger("ml.ingest.met_csv")


# ----------------------------------------------------------------- exceptions


class MetAPIBannedError(RuntimeError):
    """Raised when the Met API returns enough consecutive 403s to indicate
    an IP-level ban rather than a per-record permission issue.

    This is a fast-fail safeguard: the job exits with a clear error instead
    of burning hours scanning ~250K CSV records and writing 0 rows.

    Recovery: wait for the Azure Container Apps egress IP to exit the Met
    penalty box (typically 1–3 hours), then re-run the job.  Verify with::

        curl -s -o /dev/null -w "%{http_code}" \\
            https://collectionapi.metmuseum.org/public/collection/v1/objects/1

    Expect HTTP 200 before restarting.
    """


# ----------------------------------------------------------------- constants

MET_CSV_URL = (
    "https://media.githubusercontent.com/media/metmuseum/openaccess/"
    "refs/heads/master/MetObjects.csv"
)
DEFAULT_CACHE_MAX_AGE_DAYS = 7
DEFAULT_EMBED_BATCH_SIZE = 8

# Circuit-breaker threshold: if this many consecutive /objects/{id} calls
# return 403, abort the run immediately via MetAPIBannedError.  A ban hits
# every single ID instantly; a legitimate per-record 403 (retired/private
# object) would be isolated with 200s around it.
DEFAULT_CONSECUTIVE_403_LIMIT = 50

# Classification denylist — applied case-insensitively. Records whose
# `Classification` (or fallback `Object Name`) contain any of these tokens
# are skipped. These are the categories the user explicitly excluded for v2:
# coins (low retrieval value, dominate the dataset), books / manuscripts
# (text-heavy, not what the iOS user is photographing), ephemera (programs,
# tickets, etc. — not artworks per the product definition).
DEFAULT_DENYLIST_SUBSTRINGS: frozenset[str] = frozenset({
    "ephemera",
})
DEFAULT_DENYLIST_EXACT: frozenset[str] = frozenset({
    "coins",
    "coin",
    "books",
    "book",
    "manuscripts",
    "manuscript",
})


# ----------------------------------------------------------------- stats

@dataclass
class CSVIngestStats:
    """Outcome counters for a CSV dump ingest. Same shape as the API-path
    ``IngestStats`` plus a ``csv_rows_total`` so the operator can see how
    many CSV rows were scanned vs. how many cleared the pre-filter.
    """

    csv_rows_total: int = 0
    csv_rows_accepted: int = 0
    csv_rows_skipped_filter: int = 0
    fetched: int = 0
    skipped_filter: int = 0  # post-API map_met_record rejection
    skipped_existing: int = 0  # rows already in DB (resume-skip-existing path)
    skipped_image_error: int = 0
    skipped_embed_error: int = 0
    skipped_db_error: int = 0
    skipped_api_error: int = 0  # /objects/{id} fetch failures
    inserted: int = 0
    updated: int = 0
    rate_limited_events: int = 0
    last_object_ids: list[int] = field(default_factory=list)

    @property
    def total_persisted(self) -> int:
        return self.inserted + self.updated

    def as_dict(self) -> dict[str, Any]:
        return {
            "csv_rows_total": self.csv_rows_total,
            "csv_rows_accepted": self.csv_rows_accepted,
            "csv_rows_skipped_filter": self.csv_rows_skipped_filter,
            "fetched": self.fetched,
            "skipped_filter": self.skipped_filter,
            "skipped_existing": self.skipped_existing,
            "skipped_image_error": self.skipped_image_error,
            "skipped_embed_error": self.skipped_embed_error,
            "skipped_db_error": self.skipped_db_error,
            "skipped_api_error": self.skipped_api_error,
            "inserted": self.inserted,
            "updated": self.updated,
            "rate_limited_events": self.rate_limited_events,
            "total_persisted": self.total_persisted,
        }


# ----------------------------------------------------------------- cache mgmt

def get_cache_dir() -> Path:
    """Return the on-disk cache root for museum dumps.

    Respects ``ART_GUIDE_CACHE_DIR`` for test isolation; defaults to
    ``~/.cache/art-guide/``.
    """
    env = os.environ.get("ART_GUIDE_CACHE_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".cache" / "art-guide"


def _csv_cache_path() -> Path:
    return get_cache_dir() / "met-objects.csv"


def _is_fresh(path: Path, max_age_days: int) -> bool:
    if not path.exists():
        return False
    if max_age_days <= 0:
        return True
    age_s = time.time() - path.stat().st_mtime
    return age_s < (max_age_days * 86400)


def ensure_met_csv(
    *,
    cache_path: Path | None = None,
    csv_url: str = MET_CSV_URL,
    max_age_days: int = DEFAULT_CACHE_MAX_AGE_DAYS,
    timeout_s: float = 600.0,
    force_refresh: bool = False,
) -> Path:
    """Ensure the Met CSV is available locally; download if missing/stale.

    Returns the on-disk path to the CSV. Streams the download to a sibling
    ``.partial`` file then atomically renames so a Ctrl-C mid-download
    cannot leave a half-written cache that subsequent runs would parse as
    truncated.
    """
    path = cache_path or _csv_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if not force_refresh and _is_fresh(path, max_age_days):
        logger.info(
            "Met CSV cache hit at %s (age=%.1f days, max=%d)",
            path,
            (time.time() - path.stat().st_mtime) / 86400.0,
            max_age_days,
        )
        return path

    logger.info("Downloading Met CSV from %s → %s", csv_url, path)
    tmp = path.with_suffix(path.suffix + ".partial")
    if tmp.exists():
        tmp.unlink()

    with httpx.stream("GET", csv_url, timeout=timeout_s, follow_redirects=True) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            bytes_written = 0
            for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                fh.write(chunk)
                bytes_written += len(chunk)
    shutil.move(str(tmp), str(path))
    logger.info("Wrote %.1f MB to %s", bytes_written / (1024 * 1024), path)
    return path


# ----------------------------------------------------------------- CSV filter

def _strip_bom(s: str) -> str:
    """The CSV header's first column has a UTF-8 BOM. Strip it so callers
    can access ``row["Object Number"]`` instead of ``row["\ufeffObject Number"]``."""
    return s.lstrip("\ufeff")


def _row_passes_filter(
    row: dict[str, str],
    *,
    denylist_substrings: frozenset[str] = DEFAULT_DENYLIST_SUBSTRINGS,
    denylist_exact: frozenset[str] = DEFAULT_DENYLIST_EXACT,
) -> bool:
    """Apply the CSV-side pre-filter.

    Returns True if the row should advance to the per-id API fetch.
    Reject conditions:
      * Not public domain (``Is Public Domain != 'True'``).
      * No ``Link Resource`` (no canonical object page).
      * ``Classification`` (case-insensitive) is an exact match or
        substring match against the denylist.

    Pure function — no I/O, fully unit-testable.
    """
    if (row.get("Is Public Domain") or "").strip().lower() != "true":
        return False
    if not (row.get("Link Resource") or "").strip():
        return False

    classification = (row.get("Classification") or "").strip().lower()
    object_name = (row.get("Object Name") or "").strip().lower()

    for token in denylist_exact:
        if classification == token or object_name == token:
            return False
    for needle in denylist_substrings:
        if needle in classification or needle in object_name:
            return False

    return True


def iter_csv_rows(
    csv_path: Path,
    *,
    max_records: int | None = None,
    denylist_substrings: frozenset[str] = DEFAULT_DENYLIST_SUBSTRINGS,
    denylist_exact: frozenset[str] = DEFAULT_DENYLIST_EXACT,
    stats: CSVIngestStats | None = None,
) -> Iterator[tuple[int, dict[str, str]]]:
    """Yield ``(object_id, row_dict)`` for every accepted CSV row.

    Rows that fail the filter are silently skipped (counter bumped on
    ``stats`` when provided). ``max_records`` caps the YIELDED count, not
    the scanned count.
    """
    yielded = 0
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return
        header = [_strip_bom(h) for h in header]

        for raw in reader:
            if stats is not None:
                stats.csv_rows_total += 1
            if len(raw) != len(header):
                # Defensive: a malformed line shouldn't kill the run.
                if stats is not None:
                    stats.csv_rows_skipped_filter += 1
                continue
            row = dict(zip(header, raw))

            if not _row_passes_filter(
                row,
                denylist_substrings=denylist_substrings,
                denylist_exact=denylist_exact,
            ):
                if stats is not None:
                    stats.csv_rows_skipped_filter += 1
                continue

            object_id_raw = (row.get("Object ID") or "").strip()
            if not object_id_raw:
                if stats is not None:
                    stats.csv_rows_skipped_filter += 1
                continue
            try:
                object_id = int(object_id_raw)
            except ValueError:
                if stats is not None:
                    stats.csv_rows_skipped_filter += 1
                continue

            if stats is not None:
                stats.csv_rows_accepted += 1
            yield object_id, row
            yielded += 1
            if max_records is not None and yielded >= max_records:
                return


# ----------------------------------------------------------------- HTTP

async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    stats: CSVIngestStats | None = None,
) -> Any:
    """JSON GET with the same retry semantics as ``met_db._get_with_retry``.

    Retries on 429 / 5xx / network errors. Skips immediately on permanent
    4xx (403/404). Per-record 403/404 are common for retired ids and
    waste the polite request budget if retried (lesson from D-053).
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = await client.get(url)
            if resp.status_code == 429:
                if stats is not None:
                    stats.rate_limited_events += 1
                raise httpx.HTTPStatusError(
                    "rate limited", request=resp.request, response=resp
                )
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                resp.raise_for_status()
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500 and status != 429:
                raise
            if attempt > max_retries:
                raise
            sleep_s = min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                "Met %s failed (attempt %d/%d): %s. Sleeping %.2fs",
                url, attempt, max_retries, exc, sleep_s,
            )
            await asyncio.sleep(sleep_s)
        except (httpx.HTTPError, ValueError) as exc:
            if attempt > max_retries:
                raise
            sleep_s = min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                "Met %s failed (attempt %d/%d): %s. Sleeping %.2fs",
                url, attempt, max_retries, exc, sleep_s,
            )
            await asyncio.sleep(sleep_s)


async def _probe_met_api(
    client: httpx.AsyncClient,
    base_url: str,
    probe_id: int = 1,
) -> None:
    """Make a single cheap probe request to confirm the Met API is reachable.

    Raises ``MetAPIBannedError`` immediately if the probe returns 403,
    rather than letting the caller discover it record-by-record over hours.
    Use a well-known public-domain object (default: object 1, which is
    reliably indexed) so the probe itself is representative.
    """
    url = f"{base_url.rstrip('/')}/objects/{probe_id}"
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("Met API probe failed (network): %s", exc)
        return  # network blip — let the main loop handle it
    if resp.status_code == 403:
        raise MetAPIBannedError(
            f"Met API probe returned 403 for {url}. "
            "The Azure Container Apps egress IP is likely in the Met penalty "
            "box. Wait 1–3 hours and verify with: "
            f"curl -s -o /dev/null -w '%{{http_code}}' {url}"
        )
    if resp.status_code not in (200, 404):
        # 404 = object not found but API is up; non-403 4xx/5xx = warn only.
        logger.warning("Met API probe returned HTTP %d for %s", resp.status_code, url)


async def _fetch_object_json(
    client: httpx.AsyncClient,
    base_url: str,
    object_id: int,
    *,
    stats: CSVIngestStats,
) -> dict[str, Any]:
    return await _get_with_retry(
        client,
        f"{base_url.rstrip('/')}/objects/{object_id}",
        stats=stats,
    )


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes:
    """In-memory download (D-012). Uses the Met image CDN headers because
    ``images.metmuseum.org`` 406s plain JSON-API headers (see met_db)."""
    resp = await client.get(url, headers=_IMAGE_CDN_HEADERS)
    resp.raise_for_status()
    return resp.content


# ----------------------------------------------------------------- DB helpers

async def _load_existing_source_ids(
    pool: asyncpg.Pool, source: str
) -> set[str]:
    """Return the set of ``source_id`` values already present for ``source``.

    Used by the ``--resume-skip-existing`` path so a restarted run skips
    records it already embedded on a previous invocation, instead of
    re-fetching + re-embedding them only to UPDATE the row. For ~250K Met
    rows this is <5 MB in memory — trivial.
    """
    rows = await pool.fetch(
        "SELECT source_id FROM artworks WHERE source = $1",
        source,
    )
    return {r["source_id"] for r in rows}


# ----------------------------------------------------------------- DB commit

async def _commit_batch(
    pool: asyncpg.Pool,
    batch: list[dict[str, Any]],
    *,
    stats: CSVIngestStats,
) -> None:
    """Single-transaction UPSERT for a batch of mapped rows. Mirrors
    ``met_db._commit_batch`` byte-for-byte except for the stats type.
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
                    outcome = "inserted"
                else:
                    stats.updated += 1
                    outcome = "updated"
                logger.info(
                    "%s %s title=%r",
                    outcome,
                    row["id"],
                    (row.get("title") or "")[:80],
                )


# ----------------------------------------------------------------- main

async def ingest_met_csv_to_db(
    *,
    pool: asyncpg.Pool | None,
    csv_path: Path | None = None,
    limit: int | None = 0,
    max_records: int | None = None,
    batch_commit_size: int = DEFAULT_BATCH_COMMIT_SIZE,
    embed_batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
    embedder: Any | None = None,
    request_delay: float = DEFAULT_REQUEST_DELAY_S,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
    user_agent: str = DEFAULT_USER_AGENT,
    base_url: str = DEFAULT_BASE_URL,
    cache_max_age_days: int = DEFAULT_CACHE_MAX_AGE_DAYS,
    force_refresh_csv: bool = False,
    dry_run: bool = False,
    resume_skip_existing: bool = False,
) -> CSVIngestStats:
    """v2 Met ingest path: CSV dump → filter → /objects/{id} → batch embed → UPSERT.

    Parameters
    ----------
    pool:
        asyncpg pool. Required unless ``dry_run=True``.
    csv_path:
        Override the on-disk CSV path (for tests). Defaults to the cache.
    limit:
        Max records to successfully ingest (post-embed). ``0`` / ``None``
        means unlimited.
    max_records:
        Stop reading the CSV after this many YIELDED rows (i.e. after the
        CSV-side filter). Useful for ``--max-records 20`` smoke runs.
    batch_commit_size:
        DB transaction granularity (rows per txn). Default 50.
    embed_batch_size:
        Number of images fed to SigLIP in one forward pass. Default 8 (the
        ``DEFAULT_EMBED_BATCH_SIZE`` constant; can also be set globally via
        ``ART_GUIDE_EMBED_BATCH``). On M-series MPS, batch=8 gives roughly
        a 4-5x throughput multiplier vs. sequential embeds.
    embedder:
        An object exposing ``embed_batch(list[bytes]) -> list[np.ndarray]``.
        Defaults to the project default via ``get_embedder()``.
    request_delay:
        Floor (seconds) between successive Met API calls (per replica).
        Same default as ``met_db`` — 0.15s ≈ 6 req/s/replica well under
        the published 80 req/s aggregate cap.
    cache_max_age_days:
        Refresh the cached CSV if older than this many days.
    force_refresh_csv:
        Always re-download, regardless of cache age.
    dry_run:
        Skip DB writes; still fetches + downloads + embeds end-to-end.
    resume_skip_existing:
        On startup, query the DB for the set of ``source_id`` values already
        present for ``source='met'`` and skip per-record API + image fetches
        for IDs already in the DB. Use when restarting an interrupted run so
        the adapter adds genuinely new rows instead of re-embedding existing
        ones. Same pattern as ``aic_db`` / ``rijks_db`` (D-060).

    Returns
    -------
    CSVIngestStats
    """
    if not dry_run and pool is None:
        raise ValueError("pool is required when dry_run is False")

    unlimited = limit is None or limit <= 0
    csv_path = csv_path or ensure_met_csv(
        max_age_days=cache_max_age_days,
        force_refresh=force_refresh_csv,
    )
    if not csv_path.exists():
        raise FileNotFoundError(f"Met CSV not found at {csv_path}")

    skip_existing_ids: set[str] = set()
    if resume_skip_existing and pool is not None:
        skip_existing_ids = await _load_existing_source_ids(pool, MET_SOURCE_SLUG)
        logger.info(
            "resume-skip-existing: found %d existing met source_ids in DB; "
            "these will be skipped",
            len(skip_existing_ids),
        )
    elif resume_skip_existing:
        logger.warning(
            "resume-skip-existing: pool is None (dry_run?), skip set empty"
        )

    embedder = embedder or get_embedder()
    stats = CSVIngestStats()

    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
    ) as client:

        # Upfront probe: fail fast if the Azure egress IP is already banned.
        # Avoids burning hours scanning 250K CSV records with 0 writes.
        await _probe_met_api(client, base_url)

        # Pending batch of (mapped_row, image_bytes) tuples waiting for the
        # next embed call. Once we hit ``embed_batch_size``, we run a single
        # SigLIP forward pass against all of them.
        embed_pending: list[tuple[dict[str, Any], bytes]] = []
        commit_batch: list[dict[str, Any]] = []
        embedded_so_far = 0
        consecutive_403s = 0  # circuit-breaker counter

        async def _flush_embed_batch() -> None:
            """Embed all pending images in a single forward pass, then
            attach the resulting vectors to the mapped rows and add them
            to the commit batch.
            """
            nonlocal embedded_so_far
            if not embed_pending:
                return
            images = [b for _, b in embed_pending]
            try:
                vecs = await asyncio.to_thread(embedder.embed_batch, images)
            except Exception as exc:  # noqa: BLE001 - per-batch resilience
                logger.warning(
                    "Met CSV batch embed failed for %d images: %s; "
                    "falling back to per-record embed",
                    len(images), exc,
                )
                # Fall back: try one at a time so a single bad image
                # doesn't kill the whole batch.
                vecs = []
                for img in images:
                    try:
                        vec = await asyncio.to_thread(embedder.embed_bytes, img)
                        vecs.append(vec)
                    except Exception as exc2:  # noqa: BLE001
                        logger.warning("met csv per-image embed failed: %s", exc2)
                        vecs.append(None)

            for (mapped, _img), vec in zip(embed_pending, vecs):
                if vec is None:
                    stats.skipped_embed_error += 1
                    continue
                norm = float(np.linalg.norm(vec))
                mapped["embedding"] = vec
                stats.last_object_ids.append(int(mapped["source_id"]))
                logger.info(
                    "embed met:%s title=%r vec_norm=%.4f",
                    mapped["source_id"],
                    (mapped.get("title") or "")[:80],
                    norm,
                )
                embedded_so_far += 1
                if dry_run:
                    stats.inserted += 1
                    continue
                commit_batch.append(mapped)

            embed_pending.clear()

            if commit_batch and (
                len(commit_batch) >= batch_commit_size
                or (not unlimited and embedded_so_far >= limit)
            ):
                await _commit_batch(pool, commit_batch, stats=stats)
                commit_batch.clear()

        for object_id, _csv_row in iter_csv_rows(
            csv_path,
            max_records=max_records,
            stats=stats,
        ):
            if not unlimited and embedded_so_far >= limit:
                break

            # resume-skip-existing: bypass API + image fetch for IDs already
            # present in the DB (same pattern as aic_db / rijks_db, D-060).
            if skip_existing_ids and str(object_id) in skip_existing_ids:
                stats.skipped_existing += 1
                continue

            if request_delay > 0:
                await asyncio.sleep(request_delay)

            try:
                raw = await _fetch_object_json(
                    client, base_url, object_id, stats=stats
                )
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code == 403:
                    consecutive_403s += 1
                    if consecutive_403s >= DEFAULT_CONSECUTIVE_403_LIMIT:
                        raise MetAPIBannedError(
                            f"Circuit breaker tripped: {consecutive_403s} consecutive "
                            "403s from the Met API (last object_id "
                            f"{object_id}). Azure egress IP is likely IP-banned. "
                            "Wait 1–3 hours, verify HTTP 200 from the ACA egress IP, "
                            "then restart the job."
                        ) from exc
                else:
                    consecutive_403s = 0
                logger.warning("met %s /objects fetch failed: %s", object_id, exc)
                stats.skipped_api_error += 1
                continue
            except httpx.HTTPError as exc:
                consecutive_403s = 0
                logger.warning("met %s /objects fetch failed: %s", object_id, exc)
                stats.skipped_api_error += 1
                continue
            consecutive_403s = 0  # reset on success
            stats.fetched += 1

            mapped = map_met_record(raw)
            if mapped is None:
                # Post-API filter rejection (e.g. CSV said PD but API now
                # returns isPublicDomain=False, or primaryImage is empty).
                stats.skipped_filter += 1
                continue

            try:
                img_bytes = await _download_image(client, mapped["image_url"])
            except httpx.HTTPError as exc:
                logger.warning(
                    "met %s image download failed: %s", object_id, exc
                )
                stats.skipped_image_error += 1
                continue

            embed_pending.append((mapped, img_bytes))
            # Drop the local reference; the tuple in embed_pending owns
            # the bytes until the batch flushes.
            img_bytes = None  # noqa: F841

            if len(embed_pending) >= embed_batch_size:
                await _flush_embed_batch()

        # Drain any trailing partial batch.
        await _flush_embed_batch()
        if commit_batch and not dry_run:
            await _commit_batch(pool, commit_batch, stats=stats)
            commit_batch.clear()

    return stats


__all__ = [
    "CSVIngestStats",
    "DEFAULT_CACHE_MAX_AGE_DAYS",
    "DEFAULT_CONSECUTIVE_403_LIMIT",
    "DEFAULT_DENYLIST_EXACT",
    "DEFAULT_DENYLIST_SUBSTRINGS",
    "DEFAULT_EMBED_BATCH_SIZE",
    "MetAPIBannedError",
    "MET_CSV_URL",
    "ensure_met_csv",
    "get_cache_dir",
    "ingest_met_csv_to_db",
    "iter_csv_rows",
]
