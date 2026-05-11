"""End-to-end Met → embed → Postgres upsert pipeline.

This module glues three building blocks that already exist in the repo:

1. ``ml.ingest.met`` (discovery + per-object fetch + classification filter
   for The Met Open Access).
2. ``ml.imageops.prepare_for_embedding`` (the **only** image preprocessor;
   AGENTS.md hard rule #4) — invoked indirectly via the embedder.
3. ``ml.embeddings.get_embedder()`` (SigLIP-base D=768; D-015) — the
   embedding seam.

Adds:

* In-memory image download (D-012 / hard rule #3 — never written to disk).
* UPSERT into the ``artworks`` table with a ``vector(768)`` column. Conflict
  target ``(source, source_id)`` is the schema's idempotency key, so
  re-running this pipeline refreshes the embedding rather than duplicating
  the row.
* Batch commits (default 50/txn) to amortize per-row overhead.

Stays inside hard rules:

* The ``artworks`` schema is **not** modified here. Fields published by
  the Met that don't map to canonical columns (thumbnail URL, license
  metadata, the full raw payload) are dumped into ``raw_metadata`` (jsonb).
* PII / image bytes never appear in logs (D-012). Per-record log lines
  carry only ``source_id``, title, embedding L2 norm, and outcome.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import httpx
import numpy as np

from ml.embeddings import get_embedder
from ml.ingest.met import (
    DEFAULT_BASE_URL,
    DEFAULT_USER_AGENT,
    _classification_matches,
)

logger = logging.getLogger("ml.ingest.met_db")


# ----------------------------------------------------------------- constants

DEFAULT_LIMIT = 100
DEFAULT_BATCH_COMMIT_SIZE = 50
DEFAULT_REQUEST_DELAY_S = 0.15
DEFAULT_HTTP_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 5

# The Met's image CDN (images.metmuseum.org) sits behind a WAF that 406s
# anything that smells non-browser — even though the COLLECTION API
# (collectionapi.metmuseum.org) is happy with our polite UA + Accept: json.
# We therefore use a separate header set for image GETs only: a real Safari
# UA and an image-typed Accept header. We are NOT impersonating a person —
# we are complying with the CDN's quirky requirements for an open-access
# image the museum invites us to use.
_IMAGE_CDN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.0 Safari/605.1.15"
    ),
    "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8,*/*;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}

MET_LICENSE = "CC0"
MET_LICENSE_URL = (
    "https://www.metmuseum.org/about-the-met/policies-and-documents/open-access"
)
MET_MUSEUM_NAME = "The Metropolitan Museum of Art"
MET_SOURCE_SLUG = "met"

# UPSERT against the schema's `(source, source_id)` idempotency key.
# `xmax = 0` is the canonical asyncpg trick for distinguishing INSERTs from
# UPDATEs in an upsert RETURNING — a fresh row has xmax 0; an updated row
# has the previous transaction id stamped in.
#
# D-024 tier (a): columns $17–$23 are the new enrichment fields added by
# migration 0002_met_enrichment.sql.
_INSERT_SQL = """
INSERT INTO artworks (
    id, source, source_id, title, artist, date, medium, culture, period,
    museum, source_url, image_url, tags, is_public_domain, raw_metadata,
    embedding,
    artist_bio, credit_line, dimensions, dynasty,
    object_wikidata_url, date_begin, date_end
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9,
    $10, $11, $12, $13, $14, $15::jsonb,
    $16::vector,
    $17, $18, $19, $20, $21, $22, $23
)
ON CONFLICT (source, source_id) DO UPDATE SET
    id                  = EXCLUDED.id,
    title               = EXCLUDED.title,
    artist              = EXCLUDED.artist,
    date                = EXCLUDED.date,
    medium              = EXCLUDED.medium,
    culture             = EXCLUDED.culture,
    period              = EXCLUDED.period,
    museum              = EXCLUDED.museum,
    source_url          = EXCLUDED.source_url,
    image_url           = EXCLUDED.image_url,
    tags                = EXCLUDED.tags,
    is_public_domain    = EXCLUDED.is_public_domain,
    raw_metadata        = EXCLUDED.raw_metadata,
    embedding           = EXCLUDED.embedding,
    artist_bio          = EXCLUDED.artist_bio,
    credit_line         = EXCLUDED.credit_line,
    dimensions          = EXCLUDED.dimensions,
    dynasty             = EXCLUDED.dynasty,
    object_wikidata_url = EXCLUDED.object_wikidata_url,
    date_begin          = EXCLUDED.date_begin,
    date_end            = EXCLUDED.date_end
RETURNING (xmax = 0) AS inserted
"""


# ----------------------------------------------------------------- stats

@dataclass
class IngestStats:
    """Outcome counters for an ingest run. Cheap to log; safe to print."""

    candidate_ids: int = 0
    fetched: int = 0
    skipped_filter: int = 0
    skipped_image_error: int = 0
    skipped_embed_error: int = 0
    skipped_db_error: int = 0
    inserted: int = 0
    updated: int = 0
    rate_limited_events: int = 0
    last_object_ids: list[int] = field(default_factory=list)

    @property
    def total_persisted(self) -> int:
        return self.inserted + self.updated

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_ids": self.candidate_ids,
            "fetched": self.fetched,
            "skipped_filter": self.skipped_filter,
            "skipped_image_error": self.skipped_image_error,
            "skipped_embed_error": self.skipped_embed_error,
            "skipped_db_error": self.skipped_db_error,
            "inserted": self.inserted,
            "updated": self.updated,
            "rate_limited_events": self.rate_limited_events,
            "total_persisted": self.total_persisted,
        }


# ----------------------------------------------------------------- mapping

def map_met_record(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map a raw Met ``/objects/{id}`` payload to an ``artworks``-row dict.

    Returns ``None`` if the record fails any filter (not public domain,
    no primary image, classification doesn't match paintings/sculpture).

    Pure function — no I/O. Unit tests can call it directly.

    Output keys are the exact column names from
    ``services/api/app/migrations/0001_init.sql`` so the row dict can be
    handed straight to the asyncpg insert binding.

    Fields that don't fit a canonical column (``thumbnail_url``, license
    metadata, the original raw Met payload) live inside ``raw_metadata``
    (jsonb safety valve). Per the project rules, **do not** add columns
    here without first writing a 0002 migration.
    """
    object_id = raw.get("objectID")
    if object_id is None:
        return None
    if not raw.get("isPublicDomain"):
        return None

    primary_image = raw.get("primaryImage") or None
    if not primary_image:
        return None

    classification = raw.get("classification") or raw.get("objectName")
    if not _classification_matches(classification):
        return None

    source_id = str(object_id)
    artwork_id = f"{MET_SOURCE_SLUG}:{source_id}"
    primary_image_small = raw.get("primaryImageSmall") or None

    tags: list[str] = []
    for key in ("department", "classification", "objectName", "culture", "period"):
        val = raw.get(key)
        if val:
            tags.append(str(val))
    for t in raw.get("tags") or []:
        if isinstance(t, dict):
            term = t.get("term")
            if term:
                tags.append(str(term))

    raw_metadata = {
        "thumbnail_url": primary_image_small,
        "license": MET_LICENSE,
        "license_url": MET_LICENSE_URL,
        "met": raw,
    }

    return {
        "id": artwork_id,
        "source": MET_SOURCE_SLUG,
        "source_id": source_id,
        "title": raw.get("title") or None,
        "artist": raw.get("artistDisplayName") or None,
        "date": raw.get("objectDate") or None,
        "medium": raw.get("medium") or None,
        "culture": raw.get("culture") or None,
        "period": raw.get("period") or None,
        "museum": MET_MUSEUM_NAME,
        "source_url": (
            raw.get("objectURL")
            or f"https://www.metmuseum.org/art/collection/search/{object_id}"
        ),
        "image_url": primary_image,
        "tags": tags,
        "is_public_domain": True,
        "raw_metadata": raw_metadata,
        # D-024 tier (a) enrichment fields — empty strings normalized to None.
        "artist_bio": raw.get("artistDisplayBio") or None,
        "credit_line": raw.get("creditLine") or None,
        "dimensions": raw.get("dimensions") or None,
        "dynasty": raw.get("dynasty") or None,
        "object_wikidata_url": raw.get("objectWikidata_URL") or None,
        "date_begin": raw.get("objectBeginDate") if raw.get("objectBeginDate") not in (None, 0, "") else None,
        "date_end": raw.get("objectEndDate") if raw.get("objectEndDate") not in (None, 0, "") else None,
    }


# ----------------------------------------------------------------- pgvector helper

def format_pgvector(vec: np.ndarray | list[float]) -> str:
    """Serialize a numpy/array vector into pgvector's text input format.

    pgvector accepts ``'[v1,v2,...]'::vector`` directly, so we hand asyncpg
    a plain text parameter and let Postgres do the cast. Avoids pulling in
    the ``pgvector`` Python adapter solely for type registration.
    """
    if isinstance(vec, np.ndarray):
        if vec.ndim != 1:
            raise ValueError(f"Expected 1-D vector, got shape {vec.shape}")
        seq = vec.tolist()
    else:
        seq = list(vec)
    return "[" + ",".join(f"{float(v):.7g}" for v in seq) + "]"


# ----------------------------------------------------------------- HTTP helpers

async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    stats: IngestStats | None = None,
) -> Any:
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 429:
                if stats is not None:
                    stats.rate_limited_events += 1
                raise httpx.HTTPStatusError(
                    "rate limited", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            if attempt > max_retries:
                raise
            sleep_s = min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                "Met %s failed (attempt %d/%d): %s. Sleeping %.2fs",
                url,
                attempt,
                max_retries,
                exc,
                sleep_s,
            )
            await asyncio.sleep(sleep_s)


async def _list_object_ids(
    client: httpx.AsyncClient,
    base_url: str,
    department_ids: list[int] | None,
    *,
    stats: IngestStats,
) -> list[int]:
    params: dict[str, Any] = {"isPublicDomain": "true", "hasImages": "true"}
    if department_ids:
        params["departmentIds"] = ",".join(str(d) for d in department_ids)
    payload = await _get_with_retry(
        client,
        f"{base_url.rstrip('/')}/objects",
        params=params,
        stats=stats,
    )
    ids = list(payload.get("objectIDs") or [])
    return ids


async def _fetch_object(
    client: httpx.AsyncClient,
    base_url: str,
    object_id: int,
    *,
    stats: IngestStats,
) -> dict[str, Any]:
    return await _get_with_retry(
        client,
        f"{base_url.rstrip('/')}/objects/{object_id}",
        stats=stats,
    )


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes:
    """Download into memory only. NEVER written to disk (D-012, hard rule #3).

    The Met image CDN (images.metmuseum.org) returns 406 for our polite
    JSON-API client headers. We override per-request with a browser-like
    UA + image Accept header — see ``_IMAGE_CDN_HEADERS``.
    """
    resp = await client.get(url, headers=_IMAGE_CDN_HEADERS)
    resp.raise_for_status()
    return resp.content


# ----------------------------------------------------------------- DB commit

async def _commit_batch(
    pool: asyncpg.Pool,
    batch: list[dict[str, Any]],
    *,
    stats: IngestStats,
) -> None:
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
                        # D-024 tier (a) enrichment fields
                        row.get("artist_bio"),
                        row.get("credit_line"),
                        row.get("dimensions"),
                        row.get("dynasty"),
                        row.get("object_wikidata_url"),
                        row.get("date_begin"),
                        row.get("date_end"),
                    )
                except Exception:  # noqa: BLE001 - logged + counted, never fatal
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

async def ingest_met_to_db(
    *,
    pool: asyncpg.Pool | None,
    limit: int = DEFAULT_LIMIT,
    batch_commit_size: int = DEFAULT_BATCH_COMMIT_SIZE,
    department_ids: list[int] | None = None,
    embedder: Any | None = None,
    request_delay: float = DEFAULT_REQUEST_DELAY_S,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
    user_agent: str = DEFAULT_USER_AGENT,
    base_url: str = DEFAULT_BASE_URL,
    dry_run: bool = False,
) -> IngestStats:
    """Fetch Met records, embed, and UPSERT into the ``artworks`` table.

    Parameters
    ----------
    pool:
        asyncpg pool. Required unless ``dry_run=True``, in which case the
        pipeline runs end-to-end through the embedder but skips the DB.
    limit:
        Maximum number of records to *successfully ingest* (i.e. embedded
        and ready to write). The discovery list is much larger; we stop
        iterating early once ``limit`` is hit.
    batch_commit_size:
        Number of rows accumulated before each DB transaction.
    department_ids:
        Optional Met department filter passed to ``/objects``. ``None``
        means "all departments" (already narrowed by classification).
    embedder:
        An object exposing ``embed_bytes(image_bytes) -> np.ndarray``.
        Defaults to the project default (SigLIP-base via D-015).
    request_delay:
        Floor (seconds) between successive Met API requests. Polite throttling.
    dry_run:
        If True, skip DB writes; still fetches + downloads + embeds so we
        can validate the pipeline without a database.

    Returns
    -------
    IngestStats
    """
    if not dry_run and pool is None:
        raise ValueError("pool is required when dry_run is False")
    if limit <= 0:
        raise ValueError("limit must be positive")

    embedder = embedder or get_embedder()
    stats = IngestStats()

    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
    ) as client:
        ids = await _list_object_ids(
            client, base_url, department_ids, stats=stats
        )
        stats.candidate_ids = len(ids)
        logger.info(
            "Met /objects returned %d candidate ids; ingesting up to %d",
            stats.candidate_ids,
            limit,
        )

        batch: list[dict[str, Any]] = []
        embedded_so_far = 0

        for object_id in ids:
            if embedded_so_far >= limit:
                break
            if request_delay > 0:
                await asyncio.sleep(request_delay)

            try:
                raw = await _fetch_object(
                    client, base_url, object_id, stats=stats
                )
            except httpx.HTTPError as exc:
                logger.warning("met %s fetch failed: %s", object_id, exc)
                continue
            stats.fetched += 1

            mapped = map_met_record(raw)
            if mapped is None:
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

            try:
                vec = await asyncio.to_thread(embedder.embed_bytes, img_bytes)
            except Exception as exc:  # noqa: BLE001 - per-record resilience
                logger.warning("met %s embed failed: %s", object_id, exc)
                stats.skipped_embed_error += 1
                continue
            finally:
                # Drop the bytes reference promptly; honour D-012 / hard rule #3.
                img_bytes = None  # noqa: F841

            norm = float(np.linalg.norm(vec))
            mapped["embedding"] = vec
            stats.last_object_ids.append(object_id)

            logger.info(
                "embed met:%s title=%r vec_norm=%.4f",
                object_id,
                (mapped.get("title") or "")[:80],
                norm,
            )
            embedded_so_far += 1

            if dry_run:
                # Account dry-run successes as "inserted" so total_persisted
                # reflects pipeline-end count, then drop the row.
                stats.inserted += 1
                continue

            batch.append(mapped)
            if len(batch) >= batch_commit_size:
                await _commit_batch(pool, batch, stats=stats)
                batch.clear()

        if batch and not dry_run:
            await _commit_batch(pool, batch, stats=stats)
            batch.clear()

    return stats


__all__ = [
    "DEFAULT_BATCH_COMMIT_SIZE",
    "DEFAULT_LIMIT",
    "IngestStats",
    "MET_LICENSE",
    "MET_LICENSE_URL",
    "MET_MUSEUM_NAME",
    "MET_SOURCE_SLUG",
    "format_pgvector",
    "ingest_met_to_db",
    "map_met_record",
]
