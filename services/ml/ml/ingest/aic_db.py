"""End-to-end AIC (Art Institute of Chicago) → embed → Postgres upsert pipeline.

Mirrors the structure of ``ml.ingest.met_db`` (the reference adapter — read
its module docstring first if you haven't). The substantive differences from
the Met adapter are noted inline; everything that doesn't differ is left in
the same shape so the two adapters can be maintained as a matched pair.

Key AIC-specific design choices (see ``docs/aic-ingest-field-audit.md``):

* **No auth.** Anonymous client throttled to 60 req/min/IP. Send the courtesy
  ``AIC-User-Agent`` header but no key.
* **Single worker, ~1 req/s.** AIC docs explicitly say "no parallel
  scrapers, 1 second between requests." Default ``request_delay = 1.05``.
* **Listing endpoint, no per-id detail call.** ``/api/v1/artworks?page=N&limit=100&fields=...``
  returns full records when ``fields=`` is passed. We never hit
  ``/artworks/{id}`` in the steady-state walk — half the request budget of
  the Met adapter.
* **Wide classification filter.** AIC's strength is non-Western and
  decorative arts; we accept any record with a project-approved
  ``artwork_type_title``. The Met adapter is paintings + sculpture only.
* **IIIF image URLs.** ``https://www.artic.edu/iiif/2/{image_id}/full/843,/0/default.jpg``.
  No special CDN headers needed (unlike Met's ``images.metmuseum.org`` WAF).
* **CC0 metadata; CC-BY-4.0 ``description``.** The ``description`` field is
  captured forward-compat to ``raw_metadata.description_html`` but is NOT
  added to canonical columns or ``grounded_fields`` in this PR.

Stays inside hard rules:

* The ``artworks`` schema is **not** modified here. Anything AIC publishes
  that doesn't map to a canonical column lands in ``raw_metadata`` jsonb.
* Image bytes live in memory and die in the same iteration (D-012 / hard
  rule #3 — never written to disk, never logged).
* All preprocessing goes through ``ml.embeddings.get_embedder()`` →
  ``ml.imageops.prepare_for_embedding`` (the single image pipeline,
  hard rule #4). No source-specific processor.
* ``(source, source_id)`` ON CONFLICT upsert makes re-runs safe.
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
from ml.ingest.met_db import format_pgvector

logger = logging.getLogger("ml.ingest.aic_db")


# ----------------------------------------------------------------- constants

DEFAULT_LIMIT = 100
DEFAULT_BATCH_COMMIT_SIZE = 50
# AIC docs cap anonymous clients at 60 req/min ≈ 1.0 req/s. 1.05s delay
# = ~57 req/min, comfortable safety margin under the cap.
DEFAULT_REQUEST_DELAY_S = 1.05
DEFAULT_HTTP_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_PAGE_SIZE = 100  # max per AIC docs

AIC_BASE_URL = "https://api.artic.edu/api/v1"
AIC_IIIF_BASE = "https://www.artic.edu/iiif/2"
AIC_IMAGE_SIZE = "843,"  # AIC's recommended cached width

# Courtesy header. AIC docs request this so they can contact us if our
# traffic causes problems; not used for auth or rate-limit tiers.
DEFAULT_USER_AGENT = (
    "art-guide/0.1 (+https://github.com/max-montes/art-guide; "
    "ingest@art-guide.local) python-httpx"
)
DEFAULT_AIC_USER_AGENT = (
    "art-guide (+https://github.com/max-montes/art-guide; "
    "ingest@art-guide.local)"
)

AIC_LICENSE = "CC0"
AIC_LICENSE_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
AIC_DESCRIPTION_LICENSE = "CC-BY-4.0"
AIC_DESCRIPTION_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
AIC_MUSEUM_NAME = "The Art Institute of Chicago"
AIC_SOURCE_SLUG = "aic"

# Fields requested from the AIC listing endpoint. Listed explicitly to
# (a) cut payload size dramatically and (b) document the dependency surface.
# Adding a field here without updating `map_aic_record` is a no-op.
_AIC_LIST_FIELDS = ",".join([
    "id",
    "title",
    "artist_title",
    "artist_display",
    "date_display",
    "date_start",
    "date_end",
    "medium_display",
    "dimensions",
    "credit_line",
    "image_id",
    "alt_image_ids",
    "is_public_domain",
    "place_of_origin",
    "department_title",
    "classification_title",
    "classification_titles",
    "artwork_type_title",
    "style_title",
    "style_titles",
    "subject_titles",
    "thumbnail",
    "api_link",
    "main_reference_number",
    "fiscal_year",
    "description",
    "provenance_text",
    "publication_history",
    "exhibition_history",
    "inscriptions",
    "material_titles",
    "technique_titles",
])

# Project-approved artwork types. Wider than the Met adapter's paintings +
# sculpture filter — AIC's strength is non-Western, decorative arts, prints,
# and antiquities, which the user explicitly wants for catalog coverage
# (per AGENTS.md hard rule #5: "Catalog over model").
_AIC_ACCEPTED_TYPES = frozenset({
    "Painting",
    "Sculpture",
    "Print",
    "Drawing and Watercolor",
    "Drawing",
    "Photograph",
    "Mixed Media",
    "Vessel",
    "Textile",
    "Furniture",
    "Costume and Accessories",
    "Decorative Arts",
    "Architectural Drawing",
    "Architecture",
    "Coin",
    "Mask",
    "Book",
    "Manuscript",
})


# Same column set + ordering as ``met_db.py::_INSERT_SQL``. Kept verbatim so
# the asyncpg call signature matches (one less footgun to maintain).
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
    """Outcome counters for an AIC ingest run.

    Same shape as ``met_db.IngestStats`` plus ``pages_walked`` so the operator
    can see how far through the listing endpoint we are at any moment.
    """

    candidate_ids: int = 0  # PD records seen via listing (post non-PD skip)
    pages_walked: int = 0
    fetched: int = 0
    skipped_filter: int = 0
    skipped_existing: int = 0  # rows already in DB (resume-skip-existing path)
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
            "pages_walked": self.pages_walked,
            "fetched": self.fetched,
            "skipped_filter": self.skipped_filter,
            "skipped_existing": self.skipped_existing,
            "skipped_image_error": self.skipped_image_error,
            "skipped_embed_error": self.skipped_embed_error,
            "skipped_db_error": self.skipped_db_error,
            "inserted": self.inserted,
            "updated": self.updated,
            "rate_limited_events": self.rate_limited_events,
            "total_persisted": self.total_persisted,
        }


# ----------------------------------------------------------------- mapping

def _build_iiif_url(image_id: str) -> str:
    """Construct an AIC IIIF URL at the recommended 843px width.

    Per AIC docs (`#image-sizes`), 843px is the most commonly-cached width
    on their CDN, so this gives us the best cache hit rate and minimises
    server load. The IIIF spec also makes the parameter set explicit:
    ``/full/{w},/0/default.jpg`` = full region, scale to w pixels wide,
    no rotation, JPEG output.
    """
    return f"{AIC_IIIF_BASE}/{image_id}/full/{AIC_IMAGE_SIZE}/0/default.jpg"


def _accept_artwork_type(raw: dict[str, Any]) -> bool:
    """Project-level type filter.

    Wider than the Met adapter — see module docstring. We accept a record
    if its ``artwork_type_title`` is in the project-approved set, OR if
    ``artwork_type_title`` is None/missing but the record looks like one
    of the accepted types via its ``classification_title``.
    """
    art_type = raw.get("artwork_type_title")
    if isinstance(art_type, str) and art_type in _AIC_ACCEPTED_TYPES:
        return True
    # Fallback: some PD records have a usable classification_title but a
    # null artwork_type_title (e.g. older catalog entries).
    classification = raw.get("classification_title")
    if isinstance(classification, str):
        cl_lower = classification.lower()
        # Match obvious paintings / sculpture / prints by classification
        # alone — these are the high-signal types we'd never want to drop
        # on a typing technicality.
        for token in ("painting", "sculpture", "print", "drawing", "photograph",
                      "papyrus", "manuscript", "textile", "vessel"):
            if token in cl_lower:
                return True
    return False


def _build_tags(raw: dict[str, Any]) -> list[str]:
    """Collect source-specific facets into the canonical ``tags`` list.

    Order: department, artwork type, classification, classification_titles,
    style, subject, place_of_origin. Dedup preserving first occurrence
    (Python 3.7+ dict ordering is stable).
    """
    candidates: list[str] = []

    def _push(v: Any) -> None:
        if isinstance(v, str) and v:
            candidates.append(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item:
                    candidates.append(item)

    _push(raw.get("department_title"))
    _push(raw.get("artwork_type_title"))
    _push(raw.get("classification_title"))
    _push(raw.get("classification_titles"))
    _push(raw.get("style_title"))
    _push(raw.get("style_titles"))
    _push(raw.get("subject_titles"))
    _push(raw.get("place_of_origin"))

    # Dedup preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for t in candidates:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _coerce_int_date(value: Any) -> int | None:
    """AIC returns date_start / date_end as integers; 0 is a placeholder."""
    if value is None or value == 0 or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def map_aic_record(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map a raw AIC artwork payload to an ``artworks``-row dict.

    Returns ``None`` if the record fails any filter (not public domain,
    no image, classification type not in the project-approved set).

    Pure function — no I/O. Unit-tested in isolation.
    """
    object_id = raw.get("id")
    if object_id is None:
        return None
    if not raw.get("is_public_domain"):
        return None

    image_id = raw.get("image_id")
    if not image_id or not isinstance(image_id, str):
        return None

    if not _accept_artwork_type(raw):
        return None

    source_id = str(object_id)
    artwork_id = f"{AIC_SOURCE_SLUG}:{source_id}"

    raw_metadata = {
        "iiif_base_url": AIC_IIIF_BASE,
        "image_id": image_id,
        "alt_image_ids": raw.get("alt_image_ids") or [],
        "thumbnail": raw.get("thumbnail"),
        "license": AIC_LICENSE,
        "license_url": AIC_LICENSE_URL,
        # The `description` field is CC-BY-4.0, not CC0 like everything else.
        # We store the prose forward-compat but DO NOT add it to the
        # grounded_fields allow-list in this PR — that's a separate
        # decision coordinated with backend-engineer (see audit doc §5.1).
        "description_license": AIC_DESCRIPTION_LICENSE,
        "description_license_url": AIC_DESCRIPTION_LICENSE_URL,
        "description_html": raw.get("description"),
        "api_link": raw.get("api_link"),
        "accession_number": raw.get("main_reference_number"),
        "fiscal_year": raw.get("fiscal_year"),
        "provenance_text": raw.get("provenance_text"),
        "publication_history": raw.get("publication_history"),
        "exhibition_history": raw.get("exhibition_history"),
        "inscriptions": raw.get("inscriptions"),
        "material_titles": raw.get("material_titles") or [],
        "technique_titles": raw.get("technique_titles") or [],
        AIC_SOURCE_SLUG: raw,
    }

    return {
        "id": artwork_id,
        "source": AIC_SOURCE_SLUG,
        "source_id": source_id,
        "title": raw.get("title") or None,
        "artist": raw.get("artist_title") or None,
        "date": raw.get("date_display") or None,
        "medium": raw.get("medium_display") or None,
        # AIC has no `culture` field; place_of_origin is the best proxy.
        "culture": raw.get("place_of_origin") or None,
        # AIC has no `period` analog; date_display already covers it.
        "period": None,
        "museum": AIC_MUSEUM_NAME,
        "source_url": f"https://www.artic.edu/artworks/{object_id}",
        "image_url": _build_iiif_url(image_id),
        "tags": _build_tags(raw),
        "is_public_domain": True,
        "raw_metadata": raw_metadata,
        # D-024 tier (a) enrichment columns (shared schema with Met).
        # AIC's artist_display is the equivalent of Met's artistDisplayBio
        # ("Claude Monet (French, 1840–1926)").
        "artist_bio": raw.get("artist_display") or None,
        "credit_line": raw.get("credit_line") or None,
        "dimensions": raw.get("dimensions") or None,
        # AIC has no dedicated dynasty column. Dynasty info, when present,
        # appears in date_display and style_titles (already in tags).
        "dynasty": None,
        # AIC does not expose Wikidata IDs in artwork records.
        "object_wikidata_url": None,
        "date_begin": _coerce_int_date(raw.get("date_start")),
        "date_end": _coerce_int_date(raw.get("date_end")),
    }


# ----------------------------------------------------------------- HTTP helpers

async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    stats: IngestStats | None = None,
) -> Any:
    """GET with exponential backoff for transient errors only.

    Identical retry semantics to ``met_db._get_with_retry``:
      * retry 429 / 5xx / network errors
      * bail immediately on permanent 4xx (403/404)

    AIC uses standard HTTP semantics — 429 (with no ``Retry-After`` from
    casual inspection) for rate-limit, 404 for retired ids, 200 for
    everything else. We have not observed a 403-storm scenario like Met's
    IP throttle, but the retry shape is the same defensive code regardless.
    """
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
                "AIC %s failed (attempt %d/%d): %s. Sleeping %.2fs",
                url, attempt, max_retries, exc, sleep_s,
            )
            await asyncio.sleep(sleep_s)
        except (httpx.HTTPError, ValueError) as exc:
            if attempt > max_retries:
                raise
            sleep_s = min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                "AIC %s failed (attempt %d/%d): %s. Sleeping %.2fs",
                url, attempt, max_retries, exc, sleep_s,
            )
            await asyncio.sleep(sleep_s)


async def _list_artworks(
    client: httpx.AsyncClient,
    base_url: str,
    page: int,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    stats: IngestStats,
) -> dict[str, Any]:
    """Fetch one page from ``/api/v1/artworks`` with full fields.

    Returns the parsed JSON dict (caller pulls ``data`` and ``pagination``).
    We pass ``fields=`` to request exactly what ``map_aic_record`` needs —
    cuts payload size dramatically and avoids transferring the dozens of
    AIC-internal Elasticsearch fields we don't use.

    No separate ``_fetch_object(id)`` call is needed: the listing endpoint
    returns full records when ``fields=`` is passed. This halves the
    request budget vs. the Met adapter.
    """
    url = f"{base_url.rstrip('/')}/artworks"
    params: dict[str, Any] = {
        "page": page,
        "limit": page_size,
        "fields": _AIC_LIST_FIELDS,
    }
    return await _get_with_retry(client, url, params=params, stats=stats)


async def _fetch_object(
    client: httpx.AsyncClient,
    base_url: str,
    object_id: int,
    *,
    stats: IngestStats,
) -> dict[str, Any]:
    """Fetch a single artwork by id.

    Not used in the steady-state walk — the listing endpoint already
    returns full records. Kept available for ad-hoc reingest / backfill /
    debugging of a specific artwork id.
    """
    url = f"{base_url.rstrip('/')}/artworks/{object_id}"
    payload = await _get_with_retry(
        client, url, params={"fields": _AIC_LIST_FIELDS}, stats=stats,
    )
    return payload.get("data") or {}


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes:
    """Download into memory only. NEVER written to disk (D-012, hard rule #3).

    AIC's IIIF CDN (``www.artic.edu/iiif/2``) is Cloudflare-fronted and
    accepts our default polite UA / Accept headers without complaint
    (unlike Met's ``images.metmuseum.org`` WAF). No header override needed.
    """
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.content


# ----------------------------------------------------------------- DB commit

async def _load_existing_source_ids(
    pool: asyncpg.Pool, source: str
) -> set[str]:
    """Return the set of `source_id` values already present for ``source``.

    Used by the ``--resume-skip-existing`` path so a restarted run skips
    records it already embedded on a previous invocation, instead of
    re-fetching + re-embedding them only to UPDATE the row. For ~50K rows
    this is <2 MB in memory — trivial.
    """
    rows = await pool.fetch(
        "SELECT source_id FROM artworks WHERE source = $1",
        source,
    )
    return {r["source_id"] for r in rows}


async def _commit_batch(
    pool: asyncpg.Pool,
    batch: list[dict[str, Any]],
    *,
    stats: IngestStats,
) -> None:
    """UPSERT a batch in a single transaction. Identical to met_db's version."""
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
                except Exception:  # noqa: BLE001 - logged + counted
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

async def ingest_aic_to_db(
    *,
    pool: asyncpg.Pool | None,
    limit: int | None = DEFAULT_LIMIT,
    batch_commit_size: int = DEFAULT_BATCH_COMMIT_SIZE,
    embedder: Any | None = None,
    request_delay: float = DEFAULT_REQUEST_DELAY_S,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
    user_agent: str = DEFAULT_USER_AGENT,
    aic_user_agent: str = DEFAULT_AIC_USER_AGENT,
    base_url: str = AIC_BASE_URL,
    page_size: int = DEFAULT_PAGE_SIZE,
    start_page: int = 1,
    dry_run: bool = False,
    resume_skip_existing: bool = False,
) -> IngestStats:
    """Walk the AIC listing endpoint, embed PD records, UPSERT into ``artworks``.

    Single-worker pattern (no sharding) per AIC's published guidance: "no
    multiple scrapers in parallel, 1 second between requests". The request
    budget is split between listing pages (every ~100 records) and image
    downloads (every accepted record). Aggregate rate is bounded by
    ``request_delay`` between successive API or image requests.

    Parameters
    ----------
    pool:
        asyncpg pool. Required unless ``dry_run=True``.
    limit:
        Maximum number of records to *successfully embed*. ``None`` or
        ``<= 0`` means "no limit — walk every page in the listing."
    batch_commit_size:
        Rows per DB transaction. Default 50.
    embedder:
        An object exposing ``embed_bytes(image_bytes) -> np.ndarray``.
        Defaults to SigLIP-base via the project ``get_embedder()`` seam.
    request_delay:
        Floor (seconds) between any two outgoing HTTP requests (API or
        image). Default 1.05s to stay safely under AIC's 1 req/s cap.
    user_agent:
        Standard ``User-Agent`` header.
    aic_user_agent:
        Courtesy ``AIC-User-Agent`` header per AIC docs.
    base_url:
        AIC API base URL. Override for testing.
    page_size:
        Records per listing page. Max 100 per AIC docs.
    start_page:
        First page to fetch. Use to resume after a restart (the listing
        is sorted by ``updated_at desc`` so resuming mid-walk is best-effort,
        but idempotent upsert makes any overlap safe).
    dry_run:
        Skip DB writes; still fetches + downloads + embeds.
    resume_skip_existing:
        If True (and ``pool`` is not None), load the set of ``source_id``
        values already present in ``artworks`` for source ``aic`` at
        startup and skip any record from the listing whose ``source_id``
        is in that set — *before* the per-record image download/embed.
        Lets an overnight re-run pick up only genuinely new records
        instead of re-embedding the existing catalog. Default False
        (preserves prior behavior).

    Returns
    -------
    IngestStats
    """
    if not dry_run and pool is None:
        raise ValueError("pool is required when dry_run is False")
    if page_size < 1 or page_size > 100:
        raise ValueError("page_size must be in [1, 100] per AIC docs")
    if start_page < 1:
        raise ValueError("start_page must be >= 1")

    unlimited = limit is None or limit <= 0

    embedder = embedder or get_embedder()
    stats = IngestStats()

    skip_existing_ids: set[str] = set()
    if resume_skip_existing and pool is not None:
        skip_existing_ids = await _load_existing_source_ids(pool, AIC_SOURCE_SLUG)
        logger.info(
            "resume-skip-existing: found %d existing %s source_ids in DB; "
            "will skip those",
            len(skip_existing_ids),
            AIC_SOURCE_SLUG,
        )
    elif resume_skip_existing:
        logger.info(
            "resume-skip-existing: pool is None (dry_run?), skip set empty"
        )

    headers = {
        "User-Agent": user_agent,
        "AIC-User-Agent": aic_user_agent,
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
    ) as client:
        page = start_page
        total_pages: int | None = None
        batch: list[dict[str, Any]] = []
        embedded_so_far = 0

        while True:
            if not unlimited and embedded_so_far >= limit:
                break

            if request_delay > 0:
                await asyncio.sleep(request_delay)

            try:
                payload = await _list_artworks(
                    client, base_url, page, page_size=page_size, stats=stats,
                )
            except httpx.HTTPError as exc:
                logger.warning("AIC listing page=%d failed: %s", page, exc)
                # Stop the walk on a hard listing failure; the operator
                # can restart with --start-page=<page> after fixing.
                break

            stats.pages_walked += 1

            # Pagination block tells us when to stop.
            pagination = payload.get("pagination") or {}
            if total_pages is None:
                total_pages = pagination.get("total_pages")
                logger.info(
                    "AIC listing: total=%s total_pages=%s starting at page=%d",
                    pagination.get("total"), total_pages, start_page,
                )

            data = payload.get("data") or []
            if not data:
                logger.info("AIC listing page=%d returned no data; stopping", page)
                break

            for raw in data:
                if not unlimited and embedded_so_far >= limit:
                    break

                mapped = map_aic_record(raw)
                if mapped is None:
                    stats.skipped_filter += 1
                    continue

                if mapped["source_id"] in skip_existing_ids:
                    # Already in DB from a prior run — don't re-fetch its
                    # image or re-embed. Listing API call cost is already paid.
                    stats.skipped_existing += 1
                    continue

                stats.candidate_ids += 1
                stats.fetched += 1

                # Image download counts against the same rate budget as
                # API calls (AIC docs treat them as one IP-throttled bucket).
                if request_delay > 0:
                    await asyncio.sleep(request_delay)

                try:
                    img_bytes = await _download_image(client, mapped["image_url"])
                except httpx.HTTPError as exc:
                    logger.warning(
                        "aic %s image download failed: %s",
                        mapped["source_id"], exc,
                    )
                    stats.skipped_image_error += 1
                    continue

                try:
                    vec = await asyncio.to_thread(embedder.embed_bytes, img_bytes)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "aic %s embed failed: %s",
                        mapped["source_id"], exc,
                    )
                    stats.skipped_embed_error += 1
                    continue
                finally:
                    img_bytes = None  # noqa: F841 - D-012 / hard rule #3

                norm = float(np.linalg.norm(vec))
                mapped["embedding"] = vec
                stats.last_object_ids.append(int(mapped["source_id"]))

                logger.info(
                    "embed aic:%s title=%r vec_norm=%.4f",
                    mapped["source_id"],
                    (mapped.get("title") or "")[:80],
                    norm,
                )
                embedded_so_far += 1

                if dry_run:
                    stats.inserted += 1
                    continue

                batch.append(mapped)
                if len(batch) >= batch_commit_size:
                    await _commit_batch(pool, batch, stats=stats)
                    batch.clear()

            # Advance pagination.
            if total_pages is not None and page >= total_pages:
                logger.info(
                    "AIC listing: reached last page %d/%d; stopping",
                    page, total_pages,
                )
                break
            page += 1

        if batch and not dry_run:
            await _commit_batch(pool, batch, stats=stats)
            batch.clear()

    return stats


__all__ = [
    "AIC_BASE_URL",
    "AIC_IIIF_BASE",
    "AIC_LICENSE",
    "AIC_LICENSE_URL",
    "AIC_DESCRIPTION_LICENSE",
    "AIC_MUSEUM_NAME",
    "AIC_SOURCE_SLUG",
    "DEFAULT_BATCH_COMMIT_SIZE",
    "DEFAULT_LIMIT",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_REQUEST_DELAY_S",
    "IngestStats",
    "ingest_aic_to_db",
    "map_aic_record",
]
