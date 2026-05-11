"""One-shot backfill for D-024 tier (a) enrichment fields.

Re-fetches the Met Open Access API JSON for every existing ``met`` row in
the artworks table and UPDATEs only the seven new columns added by
migration 0002_met_enrichment.sql.

Design decisions
----------------
* **Preserves embeddings.** We never touch the ``embedding`` column —
  re-embedding 100 records would take minutes and is entirely unnecessary
  for a pure metadata update.
* **Idempotent.** Running the backfill twice produces the same result.
  The UPDATE is unconditional (last-write-wins), so re-running after a
  partial failure is safe and correct.
* **Resilient.** If the Met API 404s or errors on a record, that record is
  skipped and counted; the rest continue.
* **Polite.** Uses the same ``DEFAULT_REQUEST_DELAY_S`` floor as ingest and
  the same ``_IMAGE_CDN_HEADERS``-free path (backfill only hits the
  collection API, never image CDN).

Public API
----------
``backfill_met_enrichment(pool, ...)`` — async, returns ``BackfillStats``.
``extract_enrichment_fields(raw)`` — pure function, unit-testable.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import httpx

from ml.ingest.met_db import (
    DEFAULT_BASE_URL,
    DEFAULT_HTTP_TIMEOUT_S,
    DEFAULT_MAX_RETRIES,
    DEFAULT_REQUEST_DELAY_S,
    DEFAULT_USER_AGENT,
    MET_SOURCE_SLUG,
    _get_with_retry,
)

logger = logging.getLogger("ml.ingest.met_backfill")

# UPDATE only the seven enrichment columns; leave embedding and everything
# else untouched.
_BACKFILL_SQL = """
UPDATE artworks SET
    artist_bio          = $1,
    credit_line         = $2,
    dimensions          = $3,
    dynasty             = $4,
    object_wikidata_url = $5,
    date_begin          = $6,
    date_end            = $7,
    updated_at          = now()
WHERE source = $8 AND source_id = $9
"""


# ----------------------------------------------------------------- stats

@dataclass
class BackfillStats:
    """Outcome counters for a backfill run."""

    total_rows: int = 0
    fetched: int = 0
    updated: int = 0
    skipped_fetch_error: int = 0
    last_source_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "fetched": self.fetched,
            "updated": self.updated,
            "skipped_fetch_error": self.skipped_fetch_error,
        }


# ----------------------------------------------------------------- field extractor

def extract_enrichment_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract the seven D-024 enrichment fields from a raw Met API payload.

    Empty strings are normalised to None — the Met API commonly returns
    empty string instead of absent key for optional fields.

    Pure function — no I/O. Unit-testable in isolation.
    """
    def _str_or_none(key: str) -> str | None:
        val = raw.get(key)
        return str(val) if val and str(val).strip() else None

    def _int_or_none(key: str) -> int | None:
        val = raw.get(key)
        if val is None or val == "" or val == 0:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    return {
        "artist_bio": _str_or_none("artistDisplayBio"),
        "credit_line": _str_or_none("creditLine"),
        "dimensions": _str_or_none("dimensions"),
        "dynasty": _str_or_none("dynasty"),
        "object_wikidata_url": _str_or_none("objectWikidata_URL"),
        "date_begin": _int_or_none("objectBeginDate"),
        "date_end": _int_or_none("objectEndDate"),
    }


# ----------------------------------------------------------------- main

async def backfill_met_enrichment(
    *,
    pool: asyncpg.Pool,
    base_url: str = DEFAULT_BASE_URL,
    user_agent: str = DEFAULT_USER_AGENT,
    request_delay: float = DEFAULT_REQUEST_DELAY_S,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
    max_retries: int = DEFAULT_MAX_RETRIES,
    source: str = MET_SOURCE_SLUG,
) -> BackfillStats:
    """Re-fetch Met API records and UPDATE the seven enrichment columns.

    Parameters
    ----------
    pool:
        asyncpg connection pool pointing at the artworks DB.
    base_url:
        Met collection API root (default: live Met API).
    user_agent:
        Polite UA string for the collection API (NOT the image CDN).
    request_delay:
        Seconds to sleep between successive Met API requests.
    timeout:
        Per-request HTTP timeout in seconds.
    max_retries:
        Retry cap for transient HTTP failures.
    source:
        ``source`` column value to filter — always ``'met'`` in practice,
        parameterised for testability.

    Returns
    -------
    BackfillStats
    """
    stats = BackfillStats()

    # 1. Load all existing Met source_ids from the DB.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_id FROM artworks WHERE source = $1 ORDER BY source_id",
            source,
        )
    source_ids = [r["source_id"] for r in rows]
    stats.total_rows = len(source_ids)
    logger.info(
        "backfill: found %d %s rows to enrich", stats.total_rows, source
    )

    if not source_ids:
        return stats

    # 2. For each source_id, fetch Met JSON and UPDATE.
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
    ) as client:
        for source_id in source_ids:
            if request_delay > 0:
                await asyncio.sleep(request_delay)

            url = f"{base_url.rstrip('/')}/objects/{source_id}"
            try:
                raw = await _get_with_retry(
                    client, url, max_retries=max_retries
                )
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "backfill: met %s fetch failed: %s — skipping",
                    source_id,
                    exc,
                )
                stats.skipped_fetch_error += 1
                continue

            stats.fetched += 1
            ef = extract_enrichment_fields(raw)

            async with pool.acquire() as conn:
                await conn.execute(
                    _BACKFILL_SQL,
                    ef["artist_bio"],
                    ef["credit_line"],
                    ef["dimensions"],
                    ef["dynasty"],
                    ef["object_wikidata_url"],
                    ef["date_begin"],
                    ef["date_end"],
                    source,
                    source_id,
                )
            stats.updated += 1
            stats.last_source_ids.append(source_id)
            logger.info(
                "backfill: updated met:%s artist_bio=%r credit_line=%r "
                "dimensions=%r dynasty=%r wikidata=%r dates=%s–%s",
                source_id,
                (ef["artist_bio"] or "")[:60],
                (ef["credit_line"] or "")[:60],
                (ef["dimensions"] or "")[:40],
                ef["dynasty"],
                ef["object_wikidata_url"],
                ef["date_begin"],
                ef["date_end"],
            )

    logger.info(
        "backfill done: total=%d fetched=%d updated=%d skipped=%d",
        stats.total_rows,
        stats.fetched,
        stats.updated,
        stats.skipped_fetch_error,
    )
    return stats


__all__ = [
    "BackfillStats",
    "backfill_met_enrichment",
    "extract_enrichment_fields",
]
