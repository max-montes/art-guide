"""Unit tests for the AIC → DB ingest mapper.

These tests cover the pure field-mapping function only — no network, no
embedder, no Postgres. Mirrors the discipline of `test_met_db_ingest.py`.
"""

from __future__ import annotations

import pytest

from ml.ingest.aic_db import (
    AIC_DESCRIPTION_LICENSE,
    AIC_IIIF_BASE,
    AIC_LICENSE,
    AIC_LICENSE_URL,
    AIC_MUSEUM_NAME,
    AIC_SOURCE_SLUG,
    _accept_artwork_type,
    _build_iiif_url,
    _build_tags,
    _coerce_int_date,
    map_aic_record,
)


def _aic_painting_payload(**overrides):
    """Realistic AIC `/artworks/{id}` payload (Monet *Water Lilies*, 16568)."""
    base = {
        "id": 16568,
        "title": "Water Lilies",
        "artist_title": "Claude Monet",
        "artist_display": "Claude Monet (French, 1840\u20131926)",
        "date_display": "1906",
        "date_start": 1906,
        "date_end": 1906,
        "medium_display": "Oil on canvas",
        "dimensions": "89.9 \u00d7 94.1 cm (35 3/8 \u00d7 37 1/16 in.)",
        "credit_line": "Mr. and Mrs. Martin A. Ryerson Collection",
        "image_id": "3c27b499-af56-f0d5-93b5-a7f2f1ad5813",
        "alt_image_ids": [],
        "is_public_domain": True,
        "place_of_origin": "France",
        "department_title": "Painting and Sculpture of Europe",
        "classification_title": "oil on canvas",
        "classification_titles": ["oil on canvas", "paint", "oil paintings (visual works)"],
        "artwork_type_title": "Painting",
        "style_title": "Impressionism",
        "style_titles": ["Impressionism", "20th Century"],
        "subject_titles": ["landscapes", "Nymphaea"],
        "thumbnail": {"width": 8808, "height": 8460, "alt_text": "Painting of a pond..."},
        "api_link": "https://api.artic.edu/api/v1/artworks/16568",
        "main_reference_number": "1933.1157",
        "fiscal_year": 1933,
        "description": "<p>Little did Claude Monet know...</p>",
        "provenance_text": "The artist (d. 1926); sold to Durand-Ruel...",
        "publication_history": None,
        "exhibition_history": None,
        "inscriptions": "Inscribed lower right: Claude Monet 1906",
        "material_titles": ["oil paint (paint)", "canvas"],
        "technique_titles": ["oil painting"],
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------- happy path


def test_map_aic_record_basic():
    raw = _aic_painting_payload()
    out = map_aic_record(raw)

    assert out is not None
    # Canonical id + source/source_id idempotency key.
    assert out["id"] == "aic:16568"
    assert out["source"] == AIC_SOURCE_SLUG
    assert out["source_id"] == "16568"

    # Direct field mappings.
    assert out["title"] == "Water Lilies"
    assert out["artist"] == "Claude Monet"
    assert out["date"] == "1906"
    assert out["medium"] == "Oil on canvas"
    assert out["museum"] == AIC_MUSEUM_NAME
    assert out["source_url"] == "https://www.artic.edu/artworks/16568"
    assert out["is_public_domain"] is True

    # D-024 enrichment fields populated from AIC analogs.
    assert out["artist_bio"] == "Claude Monet (French, 1840\u20131926)"
    assert out["credit_line"] == "Mr. and Mrs. Martin A. Ryerson Collection"
    assert out["dimensions"] == "89.9 \u00d7 94.1 cm (35 3/8 \u00d7 37 1/16 in.)"
    assert out["date_begin"] == 1906
    assert out["date_end"] == 1906

    # AIC has no dynasty / wikidata / period analog — must be None.
    assert out["dynasty"] is None
    assert out["object_wikidata_url"] is None
    assert out["period"] is None

    # culture proxied from place_of_origin.
    assert out["culture"] == "France"


# ----------------------------------------------------------------- IIIF URL


def test_map_aic_record_iiif_url():
    """IIIF URL is built from image_id using AIC's recommended 843px width."""
    raw = _aic_painting_payload()
    out = map_aic_record(raw)
    assert out is not None
    expected = (
        f"{AIC_IIIF_BASE}/3c27b499-af56-f0d5-93b5-a7f2f1ad5813"
        "/full/843,/0/default.jpg"
    )
    assert out["image_url"] == expected


def test_build_iiif_url_explicit():
    """Direct unit test of the IIIF URL builder — pins the exact spec."""
    url = _build_iiif_url("abc-123")
    assert url == "https://www.artic.edu/iiif/2/abc-123/full/843,/0/default.jpg"


def test_iiif_base_pinned_to_v2():
    """The IIIF API version is locked to v2 (current AIC default).

    If AIC migrates to v3, the URL spec changes (e.g. `/v3/`), and we
    must update both _build_iiif_url and this test together.
    """
    assert AIC_IIIF_BASE == "https://www.artic.edu/iiif/2"


# ----------------------------------------------------------------- missing fields


def test_map_aic_record_missing_fields():
    """All optional fields can be missing/empty/null — no crash, gracefully None."""
    raw = _aic_painting_payload(
        title=None,
        artist_title=None,
        artist_display=None,
        date_display=None,
        date_start=None,
        date_end=None,
        medium_display=None,
        dimensions=None,
        credit_line=None,
        place_of_origin=None,
        style_title=None,
        style_titles=[],
        subject_titles=[],
        description=None,
        provenance_text=None,
        inscriptions=None,
    )
    out = map_aic_record(raw)
    assert out is not None
    assert out["title"] is None
    assert out["artist"] is None
    assert out["artist_bio"] is None
    assert out["date"] is None
    assert out["date_begin"] is None
    assert out["date_end"] is None
    assert out["medium"] is None
    assert out["dimensions"] is None
    assert out["credit_line"] is None
    assert out["culture"] is None
    # IIIF URL still built from image_id — required field, still present.
    assert out["image_url"].endswith("/full/843,/0/default.jpg")


def test_map_aic_record_zero_dates_become_none():
    """date_start / date_end of 0 (AIC placeholder) → None."""
    raw = _aic_painting_payload(date_start=0, date_end=0)
    out = map_aic_record(raw)
    assert out is not None
    assert out["date_begin"] is None
    assert out["date_end"] is None


def test_map_aic_record_negative_dates_preserved_for_bce():
    """BCE dates come back as negative integers (e.g. -1069 for Egyptian art)."""
    raw = _aic_painting_payload(date_start=-1069, date_end=-945)
    out = map_aic_record(raw)
    assert out is not None
    assert out["date_begin"] == -1069
    assert out["date_end"] == -945


# ----------------------------------------------------------------- filters


def test_skips_non_public_domain():
    raw = _aic_painting_payload(is_public_domain=False)
    assert map_aic_record(raw) is None


def test_skips_missing_image_id():
    raw = _aic_painting_payload(image_id=None)
    assert map_aic_record(raw) is None
    raw2 = _aic_painting_payload(image_id="")
    assert map_aic_record(raw2) is None


def test_skips_missing_id():
    raw = _aic_painting_payload()
    raw.pop("id")
    assert map_aic_record(raw) is None


@pytest.mark.parametrize(
    "artwork_type",
    [
        "Painting", "Sculpture", "Print", "Photograph", "Drawing",
        "Drawing and Watercolor", "Vessel", "Textile", "Coin",
        "Costume and Accessories", "Book", "Manuscript",
    ],
)
def test_accepts_project_approved_types(artwork_type):
    raw = _aic_painting_payload(artwork_type_title=artwork_type)
    assert map_aic_record(raw) is not None


@pytest.mark.parametrize(
    "artwork_type",
    [
        "Audio",  # AIC has audio-tour records that aren't artworks per se
        "Video",
        "Reproduction",
        "Performance",
    ],
)
def test_skips_non_artwork_types(artwork_type):
    raw = _aic_painting_payload(
        artwork_type_title=artwork_type,
        classification_title="audio",
    )
    assert map_aic_record(raw) is None


def test_classification_fallback_when_type_missing():
    """If artwork_type_title is null but classification names a paintingy thing,
    we still accept the record (AIC has older catalog entries with null type)."""
    raw = _aic_painting_payload(
        artwork_type_title=None,
        classification_title="painting",
    )
    out = map_aic_record(raw)
    assert out is not None


def test_no_classification_no_type_rejected():
    raw = _aic_painting_payload(
        artwork_type_title=None,
        classification_title=None,
    )
    assert map_aic_record(raw) is None


# ----------------------------------------------------------------- tags


def test_tags_merge_and_dedup():
    raw = _aic_painting_payload(
        department_title="Painting and Sculpture of Europe",
        artwork_type_title="Painting",
        classification_title="oil on canvas",
        classification_titles=["oil on canvas", "paint"],  # dup with classification_title
        style_titles=["Impressionism"],
        subject_titles=["landscapes"],
        place_of_origin="France",
    )
    out = map_aic_record(raw)
    assert out is not None
    tags = out["tags"]
    # All distinct categories present.
    assert "Painting and Sculpture of Europe" in tags
    assert "Painting" in tags
    assert "oil on canvas" in tags
    assert "paint" in tags
    assert "Impressionism" in tags
    assert "landscapes" in tags
    assert "France" in tags
    # Dedup: "oil on canvas" appears at most once.
    assert tags.count("oil on canvas") == 1


def test_build_tags_handles_none_values():
    raw = {
        "department_title": None,
        "artwork_type_title": "Print",
        "classification_title": None,
        "classification_titles": None,
        "style_title": None,
        "style_titles": None,
        "subject_titles": None,
        "place_of_origin": None,
    }
    tags = _build_tags(raw)
    assert tags == ["Print"]


# ----------------------------------------------------------------- raw_metadata


def test_raw_metadata_captures_description_and_license():
    raw = _aic_painting_payload()
    out = map_aic_record(raw)
    assert out is not None
    rm = out["raw_metadata"]
    # Original payload preserved forward-compat.
    assert rm[AIC_SOURCE_SLUG]["id"] == 16568
    # CC0 license for non-description metadata.
    assert rm["license"] == AIC_LICENSE
    assert rm["license_url"] == AIC_LICENSE_URL
    # CC-BY-4.0 license tracked separately for the description field.
    assert rm["description_license"] == AIC_DESCRIPTION_LICENSE
    assert rm["description_html"] == "<p>Little did Claude Monet know...</p>"
    # IIIF base + image_id captured for future tier-(b) needs.
    assert rm["iiif_base_url"] == AIC_IIIF_BASE
    assert rm["image_id"] == "3c27b499-af56-f0d5-93b5-a7f2f1ad5813"
    # Accession number preserved.
    assert rm["accession_number"] == "1933.1157"


# ----------------------------------------------------------------- helpers


def test_coerce_int_date_handles_placeholders():
    assert _coerce_int_date(None) is None
    assert _coerce_int_date(0) is None
    assert _coerce_int_date("") is None
    assert _coerce_int_date(1906) == 1906
    assert _coerce_int_date(-1069) == -1069
    assert _coerce_int_date("invalid") is None


def test_accept_artwork_type_strictness():
    assert _accept_artwork_type({"artwork_type_title": "Painting"}) is True
    assert _accept_artwork_type({"artwork_type_title": "Performance"}) is False
    assert _accept_artwork_type({
        "artwork_type_title": None,
        "classification_title": "sculpture",
    }) is True
    assert _accept_artwork_type({}) is False


# ---------------------------------------------- resume-skip-existing (PR-?)
#
# These tests cover the `--resume-skip-existing` flag wired through
# `ingest_aic_to_db`. They monkeypatch the listing fetcher + image
# download to keep the test purely in-process (no network, no DB).

class _FakeEmbedder:
    """Embedder stub that records every call. Returns a deterministic vec."""

    def __init__(self):
        self.calls: list[bytes] = []

    def embed_bytes(self, img_bytes: bytes):
        import numpy as np
        self.calls.append(img_bytes)
        return np.ones(768, dtype="float32")


def _aic_listing_page(records: list[dict]) -> dict:
    return {
        "pagination": {"total": len(records), "total_pages": 1, "current_page": 1},
        "data": records,
    }


def test_resume_skip_existing_skips_known_ids_no_image_fetch(monkeypatch):
    """When source_id is in the skip set, embed + image download must not run."""
    import asyncio
    from ml.ingest import aic_db

    rec_a = _aic_painting_payload(id=111, image_id="aaa")
    rec_b = _aic_painting_payload(id=222, image_id="bbb")
    rec_c = _aic_painting_payload(id=333, image_id="ccc")

    async def fake_list_artworks(client, base_url, page, *, page_size, stats):
        return _aic_listing_page([rec_a, rec_b, rec_c])

    image_calls: list[str] = []

    async def fake_download_image(client, url):
        image_calls.append(url)
        return b"\x00\x01\x02"

    async def fake_load_existing(pool, source):
        assert source == aic_db.AIC_SOURCE_SLUG
        return {"111", "333"}  # skip A + C, keep B

    monkeypatch.setattr(aic_db, "_list_artworks", fake_list_artworks)
    monkeypatch.setattr(aic_db, "_download_image", fake_download_image)
    monkeypatch.setattr(aic_db, "_load_existing_source_ids", fake_load_existing)

    embedder = _FakeEmbedder()
    sentinel_pool = object()  # non-None so resume branch loads; dry_run skips writes

    stats = asyncio.run(aic_db.ingest_aic_to_db(
        pool=sentinel_pool,  # type: ignore[arg-type]
        limit=0,
        request_delay=0.0,
        embedder=embedder,
        dry_run=True,
        resume_skip_existing=True,
    ))

    assert stats.skipped_existing == 2
    assert len(embedder.calls) == 1  # only record B embedded
    assert image_calls == [aic_db._build_iiif_url("bbb")]
    assert stats.as_dict()["skipped_existing"] == 2


def test_resume_skip_existing_off_by_default_embeds_everything(monkeypatch):
    """Flag defaults to False — every accepted record gets embedded."""
    import asyncio
    from ml.ingest import aic_db

    rec_a = _aic_painting_payload(id=111, image_id="aaa")
    rec_b = _aic_painting_payload(id=222, image_id="bbb")

    async def fake_list_artworks(client, base_url, page, *, page_size, stats):
        return _aic_listing_page([rec_a, rec_b])

    async def fake_download_image(client, url):
        return b"\x00"

    def boom_loader(*a, **kw):  # would-be loader must NOT be called
        raise AssertionError("_load_existing_source_ids called with flag off")

    monkeypatch.setattr(aic_db, "_list_artworks", fake_list_artworks)
    monkeypatch.setattr(aic_db, "_download_image", fake_download_image)
    monkeypatch.setattr(aic_db, "_load_existing_source_ids", boom_loader)

    embedder = _FakeEmbedder()

    stats = asyncio.run(aic_db.ingest_aic_to_db(
        pool=None,
        limit=0,
        request_delay=0.0,
        embedder=embedder,
        dry_run=True,
        # resume_skip_existing omitted → defaults to False
    ))

    assert stats.skipped_existing == 0
    assert len(embedder.calls) == 2


def test_resume_skip_existing_empty_set_embeds_everything(monkeypatch):
    """Flag on but DB already drained: skip set empty → everything embeds."""
    import asyncio
    from ml.ingest import aic_db

    rec_a = _aic_painting_payload(id=111, image_id="aaa")

    async def fake_list_artworks(client, base_url, page, *, page_size, stats):
        return _aic_listing_page([rec_a])

    async def fake_download_image(client, url):
        return b"\x00"

    async def fake_load_existing(pool, source):
        return set()

    monkeypatch.setattr(aic_db, "_list_artworks", fake_list_artworks)
    monkeypatch.setattr(aic_db, "_download_image", fake_download_image)
    monkeypatch.setattr(aic_db, "_load_existing_source_ids", fake_load_existing)

    embedder = _FakeEmbedder()

    stats = asyncio.run(aic_db.ingest_aic_to_db(
        pool=object(),  # type: ignore[arg-type]
        limit=0,
        request_delay=0.0,
        embedder=embedder,
        dry_run=True,
        resume_skip_existing=True,
    ))

    assert stats.skipped_existing == 0
    assert len(embedder.calls) == 1

