"""Unit tests for the Met → DB ingest mapper.

These tests cover the **pure** field-mapping function only — no network,
no embedder, no Postgres. Live ingestion against the Met API or a real
database is skipped here; smoke testing is a manual operator step.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from ml.ingest.met_db import (
    MET_LICENSE,
    MET_LICENSE_URL,
    MET_MUSEUM_NAME,
    MET_SOURCE_SLUG,
    format_pgvector,
    map_met_record,
)
from ml.ingest.met_backfill import extract_enrichment_fields


def _met_painting_payload(**overrides):
    base = {
        "objectID": 436532,
        "isPublicDomain": True,
        "primaryImage": "https://images.metmuseum.org/CRDImages/ep/original/full.jpg",
        "primaryImageSmall": "https://images.metmuseum.org/CRDImages/ep/web-large/small.jpg",
        "classification": "Paintings",
        "title": "Wheat Field with Cypresses",
        "artistDisplayName": "Vincent van Gogh",
        "artistDisplayBio": "Dutch, Zundert 1853–1890 Auvers-sur-Oise",
        "objectDate": "1889",
        "medium": "Oil on canvas",
        "culture": "",
        "period": "",
        "department": "European Paintings",
        "objectName": "Painting",
        "objectURL": "https://www.metmuseum.org/art/collection/search/436532",
        "tags": [{"term": "Landscapes"}, {"term": "Trees"}],
        "creditLine": "Purchase, The Annenberg Foundation Gift, 1993",
        "dimensions": "28 7/8 x 36 1/2 in. (73.2 x 92.7 cm)",
        "dynasty": "",
        "objectWikidata_URL": "https://www.wikidata.org/wiki/Q18689539",
        "objectBeginDate": 1889,
        "objectEndDate": 1889,
    }
    base.update(overrides)
    return base


def test_map_met_record_painting_happy_path():
    raw = _met_painting_payload()
    out = map_met_record(raw)

    assert out is not None
    # Canonical id + dedicated source/source_id columns (idempotency key).
    assert out["id"] == "met:436532"
    assert out["source"] == MET_SOURCE_SLUG
    assert out["source_id"] == "436532"

    # Canonical column names — must match 0001_init.sql.
    assert out["title"] == "Wheat Field with Cypresses"
    assert out["artist"] == "Vincent van Gogh"
    assert out["date"] == "1889"
    assert out["medium"] == "Oil on canvas"
    assert out["museum"] == MET_MUSEUM_NAME
    assert out["image_url"].endswith("/full.jpg")
    assert out["source_url"].endswith("/436532")
    assert out["is_public_domain"] is True

    # Non-canonical fields live in raw_metadata (the safety valve).
    assert out["raw_metadata"]["thumbnail_url"].endswith("/small.jpg")
    assert out["raw_metadata"]["license"] == MET_LICENSE
    assert out["raw_metadata"]["license_url"] == MET_LICENSE_URL
    assert out["raw_metadata"]["met"]["objectID"] == 436532

    # Tags include department, classification, objectName, and Met "tags".
    assert "European Paintings" in out["tags"]
    assert "Paintings" in out["tags"]
    assert "Landscapes" in out["tags"]


def test_skips_non_public_domain():
    raw = _met_painting_payload(isPublicDomain=False)
    assert map_met_record(raw) is None


def test_skips_no_primary_image():
    raw = _met_painting_payload(primaryImage="")
    assert map_met_record(raw) is None


def test_skips_missing_object_id():
    raw = _met_painting_payload()
    raw.pop("objectID")
    assert map_met_record(raw) is None


@pytest.mark.parametrize(
    "classification,object_name",
    [
        ("Drawings", "Drawing"),
        ("Photographs", "Photograph"),
        ("Prints", "Print"),
    ],
)
def test_skips_non_painting_non_sculpture(classification, object_name):
    raw = _met_painting_payload(classification=classification, objectName=object_name)
    assert map_met_record(raw) is None


@pytest.mark.parametrize(
    "classification",
    ["Sculpture", "Statue", "Bust", "Relief", "Painting"],
)
def test_accepts_painting_or_sculpture_classification(classification):
    raw = _met_painting_payload(classification=classification)
    out = map_met_record(raw)
    assert out is not None
    assert classification in out["tags"] or classification.lower() in (
        t.lower() for t in out["tags"]
    )


def test_handles_missing_optional_fields():
    raw = _met_painting_payload(
        title="",
        artistDisplayName="",
        medium="",
        objectDate="",
        objectURL="",
        primaryImageSmall="",
    )
    out = map_met_record(raw)
    assert out is not None
    assert out["title"] is None
    assert out["artist"] is None
    assert out["medium"] is None
    assert out["date"] is None
    # source_url falls back to a constructed museum URL keyed on objectID.
    assert "436532" in out["source_url"]
    assert out["raw_metadata"]["thumbnail_url"] is None


def test_format_pgvector_produces_pgvector_text_format():
    vec = np.array([0.1, -0.5, 0.0, 1.0], dtype=np.float32)
    text = format_pgvector(vec)
    assert text.startswith("[") and text.endswith("]")
    parts = text[1:-1].split(",")
    assert len(parts) == 4
    parsed = [float(p) for p in parts]
    assert math.isclose(parsed[0], 0.1, rel_tol=1e-5)
    assert math.isclose(parsed[1], -0.5, rel_tol=1e-5)


def test_format_pgvector_rejects_2d():
    with pytest.raises(ValueError):
        format_pgvector(np.zeros((2, 3), dtype=np.float32))


def test_format_pgvector_accepts_list():
    text = format_pgvector([0.0, 1.0, -1.0])
    assert text == "[0,1,-1]"


# ----------------------------------------------------------------- D-024 enrichment field tests


def test_map_met_record_enrichment_fields_populated():
    """D-024 tier (a): all seven enrichment fields are extracted from a full payload."""
    raw = _met_painting_payload()
    out = map_met_record(raw)
    assert out is not None
    assert out["artist_bio"] == "Dutch, Zundert 1853–1890 Auvers-sur-Oise"
    assert out["credit_line"] == "Purchase, The Annenberg Foundation Gift, 1993"
    assert out["dimensions"] == "28 7/8 x 36 1/2 in. (73.2 x 92.7 cm)"
    assert out["dynasty"] is None  # empty string → None
    assert out["object_wikidata_url"] == "https://www.wikidata.org/wiki/Q18689539"
    assert out["date_begin"] == 1889
    assert out["date_end"] == 1889


def test_map_met_record_empty_string_enrichment_fields_become_none():
    """Empty-string Met fields normalize to None, not empty string."""
    raw = _met_painting_payload(
        artistDisplayBio="",
        creditLine="",
        dimensions="",
        dynasty="",
        objectWikidata_URL="",
    )
    out = map_met_record(raw)
    assert out is not None
    assert out["artist_bio"] is None
    assert out["credit_line"] is None
    assert out["dimensions"] is None
    assert out["dynasty"] is None
    assert out["object_wikidata_url"] is None


def test_map_met_record_absent_enrichment_fields_become_none():
    """Fields missing entirely from the Met response → None (not KeyError)."""
    raw = _met_painting_payload()
    for key in ("artistDisplayBio", "creditLine", "dimensions", "dynasty",
                "objectWikidata_URL", "objectBeginDate", "objectEndDate"):
        raw.pop(key, None)
    out = map_met_record(raw)
    assert out is not None
    assert out["artist_bio"] is None
    assert out["credit_line"] is None
    assert out["dimensions"] is None
    assert out["dynasty"] is None
    assert out["object_wikidata_url"] is None
    assert out["date_begin"] is None
    assert out["date_end"] is None


def test_map_met_record_dynasty_populated_for_ancient_art():
    """Dynasty field is populated when present (Egyptian-style record)."""
    raw = _met_painting_payload(
        classification="Sculpture",
        dynasty="Dynasty 18",
        period="New Kingdom",
        artistDisplayBio="",
    )
    out = map_met_record(raw)
    assert out is not None
    assert out["dynasty"] == "Dynasty 18"
    assert out["artist_bio"] is None  # ancient art often has no bio


def test_map_met_record_zero_dates_become_none():
    """objectBeginDate/objectEndDate of 0 (Met placeholder) → None."""
    raw = _met_painting_payload(objectBeginDate=0, objectEndDate=0)
    out = map_met_record(raw)
    assert out is not None
    assert out["date_begin"] is None
    assert out["date_end"] is None


# ----------------------------------------------------------------- extract_enrichment_fields tests


def _met_api_response(**overrides):
    """Minimal Met API response for extract_enrichment_fields tests."""
    base = {
        "objectID": 436105,
        "artistDisplayBio": "French, Paris 1748–1825 Brussels",
        "creditLine": "Catharine Lorillard Wolfe Collection, Wolfe Fund, 1931",
        "dimensions": "51 x 77 1/4 in. (129.5 x 196.2 cm)",
        "dynasty": "",
        "objectWikidata_URL": "https://www.wikidata.org/wiki/Q1752990",
        "objectBeginDate": 1787,
        "objectEndDate": 1787,
    }
    base.update(overrides)
    return base


def test_extract_enrichment_fields_happy_path():
    raw = _met_api_response()
    ef = extract_enrichment_fields(raw)
    assert ef["artist_bio"] == "French, Paris 1748–1825 Brussels"
    assert ef["credit_line"] == "Catharine Lorillard Wolfe Collection, Wolfe Fund, 1931"
    assert ef["dimensions"] == "51 x 77 1/4 in. (129.5 x 196.2 cm)"
    assert ef["dynasty"] is None
    assert ef["object_wikidata_url"] == "https://www.wikidata.org/wiki/Q1752990"
    assert ef["date_begin"] == 1787
    assert ef["date_end"] == 1787


def test_extract_enrichment_fields_empty_strings_to_none():
    raw = _met_api_response(
        artistDisplayBio="",
        creditLine="  ",
        dimensions="",
        objectWikidata_URL="",
    )
    ef = extract_enrichment_fields(raw)
    assert ef["artist_bio"] is None
    assert ef["credit_line"] is None
    assert ef["dimensions"] is None
    assert ef["object_wikidata_url"] is None


def test_extract_enrichment_fields_zero_dates_to_none():
    raw = _met_api_response(objectBeginDate=0, objectEndDate=0)
    ef = extract_enrichment_fields(raw)
    assert ef["date_begin"] is None
    assert ef["date_end"] is None


def test_extract_enrichment_fields_idempotent():
    """Calling extract_enrichment_fields twice yields identical output."""
    raw = _met_api_response()
    ef1 = extract_enrichment_fields(raw)
    ef2 = extract_enrichment_fields(raw)
    assert ef1 == ef2


def test_extract_enrichment_fields_absent_keys():
    """Missing keys don't raise — returns None for each absent field."""
    ef = extract_enrichment_fields({})
    assert ef == {
        "artist_bio": None,
        "credit_line": None,
        "dimensions": None,
        "dynasty": None,
        "object_wikidata_url": None,
        "date_begin": None,
        "date_end": None,
    }
