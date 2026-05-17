"""End-to-end Rijksmuseum (OAI-PMH / EDM) → embed → Postgres upsert pipeline.

Mirrors ``ml.ingest.met_db`` (see that module for the full design rationale
and hard-rule context). Differences from the Met adapter:

* **Discovery** uses the Rijks OAI-PMH ``ListRecords`` verb with
  ``metadataPrefix=edm`` against curated sets (default: paintings +
  sculptures). One HTTP call returns 50 fully-hydrated records plus all
  inline Concept/Agent entities — no per-record metadata follow-ups.
* **Per-record metadata** is parsed from the OAI XML in memory using
  ``xml.etree.ElementTree`` (already in the stdlib — no lxml dependency).
* **Image URL** is read directly from ``<edm:isShownBy>`` (IIIF endpoint
  at ``iiif.micr.io``). Records without an image are skipped at mapping
  time (``map_rijks_record`` returns ``None``), same convention as Met.
* **Mapper is pure** — ``map_rijks_record(raw_dict)`` accepts a pre-parsed
  dict (the form produced by ``_parse_record``) and contains every
  filter rule + field assignment. This is the only code path covered by
  unit tests; the live OAI loop is exercised by ``--dry-run`` smoke test.

Stays inside the AGENTS.md hard rules:

* **Image bytes never touch disk** — downloaded into memory, embedded,
  dereferenced in a ``finally`` block (D-012 / hard rule #3).
* **Single image pipeline** — embedding goes through
  ``ml.embeddings.get_embedder()`` which routes through
  ``ml.imageops.prepare_for_embedding`` (hard rule #4).
* **Catalog over model** — this is a new source adapter, not a model
  change (hard rule #5).
* **Same canonical schema** — ``artworks`` table is not modified; all
  Rijks-specific text (descriptions, Iconclass codes, set IDs) lives in
  the ``raw_metadata`` jsonb column (museum-ingest-loop skill rule).

Field-mapping decisions and corpus-scope analysis are documented in
``docs/rijks-ingest-field-audit.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import httpx
import numpy as np

from ml.embeddings import get_embedder
from ml.ingest.met_db import format_pgvector

logger = logging.getLogger("ml.ingest.rijks_db")


# ----------------------------------------------------------------- constants

DEFAULT_OAI_BASE_URL = "https://data.rijksmuseum.nl/oai"
DEFAULT_USER_AGENT = "art-guide-ml/0.1 (+https://github.com/maxmontes/art-guide)"

# Sets to harvest by default. setSpec values from the Rijks OAI ListSets verb.
#   261208 → "schilderijen" (paintings — 4,916 records as of 2026-05-17)
#   26126  → "beeldhouwwerken" (sculptures — 2,468 records as of 2026-05-17)
DEFAULT_SET_SPECS: tuple[str, ...] = ("261208", "26126")

# Conservative single-worker rate, per the prod-catalog-ingest decision tree's
# "unspecified / no published limit" row. Rijks publishes no per-IP cap.
DEFAULT_REQUEST_DELAY_S = 0.2          # ~5 req/s to data.rijksmuseum.nl
DEFAULT_BATCH_COMMIT_SIZE = 50
DEFAULT_HTTP_TIMEOUT_S = 60.0           # ListRecords payloads can be ~MB-sized
DEFAULT_MAX_RETRIES = 5

RIJKS_SOURCE_SLUG = "rijks"
RIJKS_MUSEUM_NAME = "Rijksmuseum"
RIJKS_LICENSE = "CC0"
RIJKS_LICENSE_URL = "https://creativecommons.org/publicdomain/zero/1.0/"

# Public-domain rights values accepted as "is_public_domain=true". Anything
# else triggers the mapper to return None (skip).
_PUBLIC_DOMAIN_RIGHTS = {
    "http://creativecommons.org/publicdomain/mark/1.0/",
    "https://creativecommons.org/publicdomain/mark/1.0/",
    "http://creativecommons.org/publicdomain/zero/1.0/",
    "https://creativecommons.org/publicdomain/zero/1.0/",
}

# XML namespace map for ElementTree. ALL namespaces present in Rijks EDM
# responses must be listed — ElementTree raises SyntaxError otherwise.
_NS = {
    "oai":     "http://www.openarchives.org/OAI/2.0/",
    "rdf":     "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "edm":     "http://www.europeana.eu/schemas/edm/",
    "dc":      "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "ore":     "http://www.openarchives.org/ore/terms/",
    "skos":    "http://www.w3.org/2004/02/skos/core#",
    "owl":     "http://www.w3.org/2002/07/owl#",
    "rdaGr2":  "http://rdvocab.info/ElementsGr2/",
    "svcs":    "http://rdfs.org/sioc/services#",
    "wgs84":   "http://www.w3.org/2003/01/geo/wgs84_pos#",
    "foaf":    "http://xmlns.com/foaf/0.1/",
}
_RDF_RESOURCE = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}resource"
_RDF_ABOUT    = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"
_XML_LANG     = "{http://www.w3.org/XML/1998/namespace}lang"

# Same UPSERT shape and column list as met_db._INSERT_SQL — schema is shared
# across all sources. See met_db.py for the rationale on (source, source_id)
# being the idempotency key and the ``xmax = 0`` insert/update discriminator.
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
    """Outcome counters for a Rijks ingest run."""

    candidate_records: int = 0
    fetched_pages: int = 0
    fetched_records: int = 0
    skipped_filter: int = 0
    skipped_existing: int = 0  # rows already in DB (resume-skip-existing path)
    skipped_image_error: int = 0
    skipped_embed_error: int = 0
    skipped_db_error: int = 0
    inserted: int = 0
    updated: int = 0
    rate_limited_events: int = 0
    last_record_ids: list[str] = field(default_factory=list)

    @property
    def total_persisted(self) -> int:
        return self.inserted + self.updated

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_records": self.candidate_records,
            "fetched_pages": self.fetched_pages,
            "fetched_records": self.fetched_records,
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


# ----------------------------------------------------------------- date parsing

_YEAR_TOKEN = re.compile(r"\b(\d{3,4})\b")


def _parse_year_range(date_str: str | None) -> tuple[int | None, int | None]:
    """Best-effort min/max year extraction from a Rijks ``dcterms:created`` string.

    Patterns seen in the field audit (docs/rijks-ingest-field-audit.md §4):

    * ``"1642"``                  → (1642, 1642)
    * ``"c. 1500"`` / ``"ca. 1500"`` → (1500, 1500)
    * ``"c. 1735 - c. 1745"``     → (1735, 1745)
    * ``"618 - 906"``             → (618, 906)
    * ``"1500 - before 1537"``    → (1500, 1537)

    Strategy: extract all 3-4 digit year tokens; return (min, max). Returns
    (None, None) when nothing parseable is present. Good enough for the
    retrieval-side range filter; date_str itself is still emitted as the
    user-facing ``date`` field.
    """
    if not date_str:
        return (None, None)
    years = [int(y) for y in _YEAR_TOKEN.findall(date_str)]
    # Reject obvious noise (e.g. inventory numbers > 3000) by sanity bound.
    years = [y for y in years if 1 <= y <= 2200]
    if not years:
        return (None, None)
    return (min(years), max(years))


# ----------------------------------------------------------------- XML helpers

def _localname(el: ET.Element) -> str:
    """Strip the namespace prefix from an element tag."""
    return el.tag.split("}", 1)[-1]


def _text_by_lang(
    elems: list[ET.Element],
    *,
    lang_preference: tuple[str, ...] = ("en", "nl"),
) -> str | None:
    """Return the first non-empty text content, preferring the given languages.

    Rijks EDM emits the same field with multiple ``xml:lang`` tags; we
    pick the first that matches our preference order, then fall back to
    any non-empty value. Note: ``xml:lang`` is sometimes mislabeled (the
    "en" entry may carry Dutch text), so this is a preference, not a
    correctness guarantee — see field audit §6 gotcha #1.
    """
    by_lang = {(e.get(_XML_LANG) or ""): (e.text or "").strip()
               for e in elems if (e.text or "").strip()}
    for lang in lang_preference:
        if by_lang.get(lang):
            return by_lang[lang]
    for v in by_lang.values():
        return v
    return None


def _parse_record(record_el: ET.Element) -> dict[str, Any] | None:
    """Parse one ``<oai:record>`` element into a flat dict.

    This is the boundary between XML and the rest of the pipeline. The
    dict it returns is what ``map_rijks_record`` consumes, so unit tests
    can construct synthetic input by hand without an XML round-trip.

    Returns ``None`` if the record has no ``<edm:ProvidedCHO>`` (e.g.
    OAI deleted-record markers).
    """
    cho = record_el.find(".//edm:ProvidedCHO", _NS)
    if cho is None:
        return None
    about = cho.get(_RDF_ABOUT) or ""
    if not about:
        return None
    numeric_id = about.rsplit("/", 1)[-1]

    rdf_root = record_el.find(
        ".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF"
    )
    # Inline Concept resolution map: URI -> {"en": "...", "nl": "..."}
    concepts: dict[str, dict[str, str]] = {}
    for c in rdf_root.findall("skos:Concept", _NS):
        cid = c.get(_RDF_ABOUT)
        if not cid:
            continue
        concepts[cid] = {
            (l.get(_XML_LANG) or ""): (l.text or "").strip()
            for l in c.findall("skos:prefLabel", _NS)
            if (l.text or "").strip()
        }

    # Inline Agent resolution map
    agents: dict[str, dict[str, Any]] = {}
    for a in rdf_root.findall("edm:Agent", _NS):
        aid = a.get(_RDF_ABOUT)
        if not aid:
            continue
        names = {
            (l.get(_XML_LANG) or ""): (l.text or "").strip()
            for l in a.findall("skos:prefLabel", _NS)
            if (l.text or "").strip()
        }
        dob_el = a.find("rdaGr2:dateOfBirth", _NS)
        dod_el = a.find("rdaGr2:dateOfDeath", _NS)
        wikidata_urls = [
            (s.get(_RDF_RESOURCE) or "")
            for s in a.findall("owl:sameAs", _NS)
            if "wikidata.org" in (s.get(_RDF_RESOURCE) or "")
        ]
        agents[aid] = {
            "names": names,
            "birth": (dob_el.text or "").strip() if dob_el is not None else None,
            "death": (dod_el.text or "").strip() if dod_el is not None else None,
            "wikidata_urls": wikidata_urls,
        }

    def _resolve_concept_label(uri: str | None) -> str | None:
        if not uri:
            return None
        labels = concepts.get(uri, {})
        return labels.get("en") or labels.get("nl") or None

    # Pull aggregation-level fields (rights, image)
    rights_uri: str | None = None
    image_url: str | None = None
    source_url: str | None = None
    agg = record_el.find(".//ore:Aggregation", _NS)
    if agg is not None:
        for child in agg:
            tag = _localname(child)
            if tag == "rights":
                rights_uri = child.get(_RDF_RESOURCE)
            elif tag in ("isShownBy", "object") and image_url is None:
                wr = child.find("edm:WebResource", _NS)
                if wr is not None and wr.get(_RDF_ABOUT):
                    image_url = wr.get(_RDF_ABOUT)
                else:
                    image_url = child.get(_RDF_RESOURCE) or image_url
            elif tag == "isShownAt" and source_url is None:
                wr = child.find("edm:WebResource", _NS)
                if wr is not None and wr.get(_RDF_ABOUT):
                    source_url = wr.get(_RDF_ABOUT)
                else:
                    source_url = child.get(_RDF_RESOURCE) or source_url

    # ProvidedCHO-level fields
    titles = cho.findall("dc:title", _NS)
    descriptions = cho.findall("dc:description", _NS)
    extents = cho.findall("dcterms:extent", _NS)
    medium_refs = [e.get(_RDF_RESOURCE) for e in cho.findall("dcterms:medium", _NS)]
    type_refs   = [e.get(_RDF_RESOURCE) for e in cho.findall("dc:type", _NS)]
    creator_refs = [e.get(_RDF_RESOURCE) for e in cho.findall("dc:creator", _NS)]
    subject_refs = [e.get(_RDF_RESOURCE) for e in cho.findall("dc:subject", _NS)]
    created_els = cho.findall("dcterms:created", _NS)
    identifier_els = cho.findall("dc:identifier", _NS)
    set_refs = [e.get(_RDF_RESOURCE) for e in cho.findall("dcterms:isPartOf", _NS)]
    provenance_els = cho.findall("dcterms:provenance", _NS)
    alternative_els = cho.findall("dcterms:alternative", _NS)

    iconclass_codes: list[str] = []
    for s in subject_refs:
        if s and "iconclass.org/" in s:
            iconclass_codes.append(s.rsplit("/", 1)[-1])

    return {
        "numeric_id": numeric_id,
        "about": about,
        "museum_id": (identifier_els[0].text or "").strip() if identifier_els else None,
        "title_en": _text_by_lang(titles),
        "title_nl": _text_by_lang(titles, lang_preference=("nl",)),
        "description_en": _text_by_lang(descriptions, lang_preference=("en",)),
        "description_nl": _text_by_lang(descriptions, lang_preference=("nl",)),
        "extent_en": _text_by_lang(extents),
        "medium_labels": [
            label for label in (_resolve_concept_label(r) for r in medium_refs)
            if label
        ],
        "type_labels": [
            label for label in (_resolve_concept_label(r) for r in type_refs)
            if label
        ],
        "subject_labels": [
            label for label in (_resolve_concept_label(r) for r in subject_refs)
            if label
        ],
        "creator_uris": [r for r in creator_refs if r],
        "creator_agents": [agents.get(r, {}) for r in creator_refs if r],
        "created": _text_by_lang(created_els),
        "set_ids": [r for r in set_refs if r],
        "provenance_en": _text_by_lang(provenance_els, lang_preference=("en",)),
        "alternative_titles": [
            {"lang": (e.get(_XML_LANG) or ""), "value": (e.text or "").strip()}
            for e in alternative_els
            if (e.text or "").strip()
        ],
        "iconclass_codes": iconclass_codes,
        "image_url": image_url,
        "source_url_from_record": source_url,
        "rights_uri": rights_uri,
    }


def _iter_records_from_xml(xml_bytes: bytes) -> list[dict[str, Any]]:
    """Parse a ListRecords OAI response into a list of pre-mapped record dicts."""
    root = ET.fromstring(xml_bytes)
    out: list[dict[str, Any]] = []
    for rec in root.findall(".//oai:record", _NS):
        # Skip deleted-record markers (header has status="deleted")
        header = rec.find("oai:header", _NS)
        if header is not None and header.get("status") == "deleted":
            continue
        parsed = _parse_record(rec)
        if parsed is not None:
            out.append(parsed)
    return out


def _resumption_token(xml_bytes: bytes) -> tuple[str | None, int | None]:
    """Extract (resumptionToken, completeListSize) from a ListRecords payload."""
    root = ET.fromstring(xml_bytes)
    rt = root.find(".//oai:resumptionToken", _NS)
    if rt is None:
        return (None, None)
    size_attr = rt.get("completeListSize")
    size = int(size_attr) if size_attr and size_attr.isdigit() else None
    token = (rt.text or "").strip() or None
    return (token, size)


# ----------------------------------------------------------------- mapping

# Cleanup regex for Rijks-style HTML/MD pseudo-markup that sometimes
# appears inside provenance / description text.
_PSEUDO_HTML = re.compile(r"</?(em|i|b|strong|sub|sup)>", re.IGNORECASE)
_WHITESPACE_RUN = re.compile(r"\s+")


def _clean_text(s: str | None, *, max_len: int | None = None) -> str | None:
    if s is None:
        return None
    s = _PSEUDO_HTML.sub("", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    if not s:
        return None
    if max_len is not None and len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s


def _artist_bio_from_agent(agent: dict[str, Any]) -> str | None:
    """Synthesise ``"YYYY – YYYY"`` (or ``"ca. YYYY – YYYY"``) from agent dates.

    Rijks gives ``rdaGr2:dateOfBirth`` / ``dateOfDeath`` as structured
    strings (``"1606-07-15"``, ``"ca. 1592 - ca. 1593"``, ``"na 1666-08-17"``).
    Met by contrast packs the same info into a free-text
    ``artistDisplayBio`` like ``"French, Paris 1748–1825 Brussels"``.
    We don't have a clean nationality field in Rijks EDM, so the bio is
    dates-only — still useful to the LLM for "this 17th-century painter…"
    framing.
    """
    birth = (agent.get("birth") or "").strip()
    death = (agent.get("death") or "").strip()
    if not birth and not death:
        return None
    b_years = _YEAR_TOKEN.findall(birth)
    d_years = _YEAR_TOKEN.findall(death)
    b = b_years[0] if b_years else None
    d = d_years[-1] if d_years else None  # range end for "1928-03-03 - 1928-03-30"
    if not b and not d:
        return None
    b_pref = "ca. " if (b and (birth.lower().startswith(("ca", "c.", "circa")))) else ""
    d_pref = "ca. " if (d and (death.lower().startswith(("ca", "c.", "circa")))) else ""
    return f"{b_pref}{b or '?'} – {d_pref}{d or '?'}"


def map_rijks_record(parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Map a parsed Rijks record dict to an ``artworks``-row dict.

    Pure function — no I/O. Returns ``None`` for records that fail a
    project filter (no image, not public domain). Output keys are the
    exact column names from ``services/api/app/migrations/0001_init.sql``
    + the D-024 enrichment columns.

    See ``docs/rijks-ingest-field-audit.md`` for the field-by-field
    derivation and the rationale behind each fallback.
    """
    if not parsed:
        return None

    # Filter 1: must be public domain.
    if parsed.get("rights_uri") not in _PUBLIC_DOMAIN_RIGHTS:
        return None

    # Filter 2: must have an image URL.
    image_url = parsed.get("image_url")
    if not image_url:
        return None

    numeric_id = parsed.get("numeric_id")
    if not numeric_id:
        return None

    artwork_id = f"{RIJKS_SOURCE_SLUG}:{numeric_id}"
    museum_id = parsed.get("museum_id")

    # source_url synthesis — <edm:isShownAt> is absent in all sampled records.
    source_url = (
        parsed.get("source_url_from_record")
        or (f"https://www.rijksmuseum.nl/en/collection/{museum_id}" if museum_id else None)
        or f"https://id.rijksmuseum.nl/{numeric_id}"
    )

    # Title: en pref, else nl. Both come from _text_by_lang already.
    title = parsed.get("title_en") or parsed.get("title_nl")

    # Artist: comma-join EN prefLabels of resolved creator Agents.
    artist_names: list[str] = []
    for agent in parsed.get("creator_agents", []) or []:
        names = agent.get("names") or {}
        n = names.get("en") or names.get("nl")
        if n:
            artist_names.append(n)
    artist = ", ".join(artist_names) if artist_names else None

    # Artist bio: only synthesised when there's exactly one creator with dates.
    artist_bio: str | None = None
    creator_agents = parsed.get("creator_agents") or []
    if len(creator_agents) == 1:
        artist_bio = _artist_bio_from_agent(creator_agents[0])

    # Medium: comma-join resolved EN labels.
    medium_labels = parsed.get("medium_labels") or []
    medium = ", ".join(medium_labels) if medium_labels else None

    # Tags: dedup union of type + subject labels.
    seen_tags: set[str] = set()
    tags: list[str] = []
    for label in (parsed.get("type_labels") or []) + (parsed.get("subject_labels") or []):
        if label and label not in seen_tags:
            seen_tags.add(label)
            tags.append(label)

    # Dates.
    created = parsed.get("created")
    date_begin, date_end = _parse_year_range(created)

    # credit_line ← Rijks dcterms:provenance (semantic mismatch w/ Met — see audit §3).
    credit_line = _clean_text(parsed.get("provenance_en"), max_len=600)

    # dimensions ← Rijks dcterms:extent
    dimensions = _clean_text(parsed.get("extent_en"), max_len=400)

    # object_wikidata_url: Rijks EDM (per the audit) does not put a wikidata
    # sameAs on the object itself in the records sampled. Leave null.
    object_wikidata_url = None

    # raw_metadata: keep license info + Rijks-specific text that doesn't fit
    # the canonical schema. ``rijks`` key holds the source-specific blob.
    raw_metadata = {
        "license": RIJKS_LICENSE,
        "license_url": RIJKS_LICENSE_URL,
        "rijks": {
            "museum_id": museum_id,
            "set_ids": parsed.get("set_ids") or [],
            "description_en": parsed.get("description_en"),
            "description_nl": parsed.get("description_nl"),
            "alternative_titles": parsed.get("alternative_titles") or [],
            "iconclass_codes": parsed.get("iconclass_codes") or [],
            "agent_wikidata_urls": [
                u for agent in creator_agents for u in (agent.get("wikidata_urls") or [])
            ],
        },
    }

    return {
        "id": artwork_id,
        "source": RIJKS_SOURCE_SLUG,
        "source_id": str(numeric_id),
        "title": title,
        "artist": artist,
        "date": created,
        "medium": medium,
        "culture": None,
        "period": None,
        "museum": RIJKS_MUSEUM_NAME,
        "source_url": source_url,
        "image_url": image_url,
        "tags": tags,
        "is_public_domain": True,
        "raw_metadata": raw_metadata,
        "artist_bio": artist_bio,
        "credit_line": credit_line,
        "dimensions": dimensions,
        "dynasty": None,
        "object_wikidata_url": object_wikidata_url,
        "date_begin": date_begin,
        "date_end": date_end,
    }


# ----------------------------------------------------------------- HTTP helpers

async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    stats: IngestStats | None = None,
) -> bytes:
    """GET (returning raw bytes) with retries on transient errors only.

    Same retry policy as ``met_db._get_with_retry``: retry on 429, 5xx,
    and network errors; bail immediately on permanent 4xx (no retry storm
    on retired or restricted records). See met_db for the rationale.
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
            return resp.content
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500 and status != 429:
                raise
            if attempt > max_retries:
                raise
            sleep_s = min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                "Rijks %s failed (attempt %d/%d): %s. Sleeping %.2fs",
                url, attempt, max_retries, exc, sleep_s,
            )
            await asyncio.sleep(sleep_s)
        except (httpx.HTTPError, ValueError) as exc:
            if attempt > max_retries:
                raise
            sleep_s = min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                "Rijks %s failed (attempt %d/%d): %s. Sleeping %.2fs",
                url, attempt, max_retries, exc, sleep_s,
            )
            await asyncio.sleep(sleep_s)


async def _list_records_page(
    client: httpx.AsyncClient,
    base_url: str,
    *,
    set_spec: str | None,
    resumption_token: str | None,
    stats: IngestStats,
) -> bytes:
    """One ``ListRecords`` HTTP call. With a resumption token, ``set`` and
    ``metadataPrefix`` are NOT sent (OAI-PMH spec requirement).
    """
    if resumption_token:
        params: dict[str, Any] = {
            "verb": "ListRecords",
            "resumptionToken": resumption_token,
        }
    else:
        params = {"verb": "ListRecords", "metadataPrefix": "edm"}
        if set_spec:
            params["set"] = set_spec
    return await _get_with_retry(client, base_url, params=params, stats=stats)


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes:
    """Download image bytes into memory. Never written to disk (D-012)."""
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.content


# ----------------------------------------------------------------- DB commit

async def _load_existing_source_ids(
    pool: asyncpg.Pool, source: str
) -> set[str]:
    """Return the set of `source_id` values already present for ``source``.

    Used by the ``--resume-skip-existing`` path. See aic_db._load_existing_source_ids
    for the matched-pair version and the rationale.
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
                    outcome, row["id"], (row.get("title") or "")[:80],
                )


# ----------------------------------------------------------------- main

async def ingest_rijks_to_db(
    *,
    pool: asyncpg.Pool | None,
    limit: int | None = None,
    batch_commit_size: int = DEFAULT_BATCH_COMMIT_SIZE,
    set_specs: tuple[str, ...] | list[str] = DEFAULT_SET_SPECS,
    embedder: Any | None = None,
    request_delay: float = DEFAULT_REQUEST_DELAY_S,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
    user_agent: str = DEFAULT_USER_AGENT,
    base_url: str = DEFAULT_OAI_BASE_URL,
    dry_run: bool = False,
    resume_skip_existing: bool = False,
) -> IngestStats:
    """Harvest the configured Rijks sets, embed, and UPSERT into ``artworks``.

    Parameters
    ----------
    pool:
        asyncpg pool. Required unless ``dry_run=True``.
    limit:
        Max number of records to successfully ingest. ``None`` or ``<=0``
        means unbounded (process the entire set list). Default unbounded
        because the Rijks corpus is small (~7K candidates).
    batch_commit_size:
        Rows per DB transaction.
    set_specs:
        Iterable of Rijks setSpec values to harvest sequentially. Defaults
        to ``("261208", "26126")`` = paintings + sculptures.
    embedder:
        Object exposing ``embed_bytes(image_bytes) -> np.ndarray``. Defaults
        to the project default (SigLIP-base via D-015).
    request_delay:
        Floor (seconds) between successive OAI HTTP requests. Default
        ``0.2`` (≈5 req/s) — conservative since Rijks publishes no per-IP cap.
    dry_run:
        If True, skip DB writes; still fetches + downloads + embeds end-to-end.
    resume_skip_existing:
        If True (and ``pool`` is not None), load the set of ``source_id``
        values already present in ``artworks`` for source ``rijks`` at
        startup and skip any record from the OAI listing whose
        ``source_id`` is in that set — *before* the per-record image
        download/embed. Default False (preserves prior behavior).

    Returns
    -------
    IngestStats
    """
    if not dry_run and pool is None:
        raise ValueError("pool is required when dry_run is False")

    unlimited = limit is None or limit <= 0
    embedder = embedder or get_embedder()
    stats = IngestStats()

    skip_existing_ids: set[str] = set()
    if resume_skip_existing and pool is not None:
        skip_existing_ids = await _load_existing_source_ids(pool, RIJKS_SOURCE_SLUG)
        logger.info(
            "resume-skip-existing: found %d existing %s source_ids in DB; "
            "will skip those",
            len(skip_existing_ids),
            RIJKS_SOURCE_SLUG,
        )
    elif resume_skip_existing:
        logger.info(
            "resume-skip-existing: pool is None (dry_run?), skip set empty"
        )

    headers = {"User-Agent": user_agent, "Accept": "application/xml"}
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
    ) as client:

        batch: list[dict[str, Any]] = []
        embedded_so_far = 0
        stop_outer = False

        for set_spec in set_specs:
            if stop_outer:
                break
            logger.info("Rijks ingest: starting set %s", set_spec)
            resumption_token: str | None = None
            while True:
                if not unlimited and embedded_so_far >= limit:
                    stop_outer = True
                    break
                if request_delay > 0:
                    await asyncio.sleep(request_delay)
                try:
                    xml_bytes = await _list_records_page(
                        client, base_url,
                        set_spec=set_spec,
                        resumption_token=resumption_token,
                        stats=stats,
                    )
                except httpx.HTTPError as exc:
                    logger.warning(
                        "Rijks ListRecords (set=%s, token=%s) failed: %s",
                        set_spec, (resumption_token or "<initial>")[:20], exc,
                    )
                    break

                stats.fetched_pages += 1
                records = _iter_records_from_xml(xml_bytes)
                stats.fetched_records += len(records)
                next_token, complete_size = _resumption_token(xml_bytes)

                if resumption_token is None and complete_size:
                    stats.candidate_records += complete_size
                    logger.info(
                        "Rijks set %s: completeListSize=%d", set_spec, complete_size,
                    )

                for parsed in records:
                    if not unlimited and embedded_so_far >= limit:
                        stop_outer = True
                        break

                    mapped = map_rijks_record(parsed)
                    if mapped is None:
                        stats.skipped_filter += 1
                        continue

                    if mapped["source_id"] in skip_existing_ids:
                        # Already in DB from a prior run — don't re-fetch its
                        # image or re-embed. OAI page already paid for.
                        stats.skipped_existing += 1
                        continue

                    try:
                        img_bytes = await _download_image(client, mapped["image_url"])
                    except httpx.HTTPError as exc:
                        logger.warning(
                            "Rijks %s image download failed: %s",
                            mapped["id"], exc,
                        )
                        stats.skipped_image_error += 1
                        continue

                    try:
                        vec = await asyncio.to_thread(
                            embedder.embed_bytes, img_bytes
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Rijks %s embed failed: %s", mapped["id"], exc,
                        )
                        stats.skipped_embed_error += 1
                        continue
                    finally:
                        img_bytes = None  # noqa: F841 - D-012 / hard rule #3

                    norm = float(np.linalg.norm(vec))
                    mapped["embedding"] = vec
                    stats.last_record_ids.append(mapped["id"])

                    logger.info(
                        "embed %s title=%r vec_norm=%.4f",
                        mapped["id"],
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

                if stop_outer or not next_token:
                    break
                resumption_token = next_token

        if batch and not dry_run:
            await _commit_batch(pool, batch, stats=stats)
            batch.clear()

    return stats
