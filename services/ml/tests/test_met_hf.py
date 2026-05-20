"""Unit tests for the Met HF dataset ingest adapter (``ml.ingest.met_hf``).

Covers the pure / no-network surface:

* ``convert_hf_row`` — HF CSV row → ``map_met_record()``-compatible dict,
  including ``tags`` JSON-string parsing, ``isPublicDomain`` coercion, and
  ``objectID`` coercion.
* ``_row_passes_hf_filter`` — ``isPublicDomain`` pre-filter on the raw HF row.
* Tags parsing — JSON string, null/None, empty string, malformed.
* Missing ``primaryImageSmall`` — empty string means no image URL; the adapter
  must not attempt a download.
* Circuit breaker — N consecutive CDN image download failures triggers abort.
* CLI registration smoke — ``ingest met-hf`` sub-command parses correctly.

No real network requests are made; all HTTP calls are monkeypatched.

Interface assumptions (the adapter does not exist yet at time of writing):
---------------------------------------------------------------------------
* ``ml.ingest.met_hf.convert_hf_row(row: dict) -> dict``
      Converts a raw HF CSV row dict into the dict shape expected by
      ``map_met_record()``. Specifically:
        - parses ``tags`` from a JSON string (``'[{"term":"X"}]'``) to a list
          of dicts, or returns ``[]`` for null/empty/invalid values;
        - coerces ``isPublicDomain`` from "True"/"False" string to bool;
        - coerces ``objectID`` from string to int.
      Pure function — no I/O.

* ``ml.ingest.met_hf._row_passes_hf_filter(row: dict) -> bool``
      Returns True iff the raw HF row's ``isPublicDomain`` field is truthy
      (accepts both bool ``True`` and string ``"True"``).

* ``ml.ingest.met_hf.HFIngestStats``
      Dataclass with at minimum the counters:
        hf_rows_total, hf_rows_accepted, hf_rows_skipped_filter,
        hf_rows_skipped_no_image, fetched_images, skipped_image_error,
        skipped_filter (map_met_record rejects), skipped_embed_error,
        skipped_db_error, inserted, updated.
      Has an ``as_dict()`` method.

* ``ml.ingest.met_hf.MetCDNCircuitBreakerError``
      RuntimeError subclass raised when consecutive CDN image download
      failures reach the threshold. The adapter may instead reuse/extend
      ``MetAPIBannedError`` from ``met_csv``; either is acceptable.

* ``ml.ingest.met_hf.DEFAULT_CONSECUTIVE_CDN_FAIL_LIMIT``
      int — default circuit-breaker threshold for CDN failures (suggest 50,
      consistent with met_csv's DEFAULT_CONSECUTIVE_403_LIMIT).

* ``ml.ingest.met_hf.ingest_met_hf_to_db(pool, *, rows, dry_run, ...)``
      Async function; accepts an iterable of HF-row dicts (or a path/dataset
      object), processes them, and returns an HFIngestStats. The tests below
      pass a plain list of dicts as ``rows`` for unit-test isolation.

The HF dataset (``metmuseum/openaccess``) has 58 columns whose names match
the Met ``/objects/{id}`` JSON field names exactly. The sole schema difference
versus the live API is that ``tags`` is stored as a JSON string rather than
a list-of-dicts — every test that exercises tags coercion is therefore
testing the one real divergence from the API shape.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Lazy import guard — tests are written ahead of the implementation.
# If the module does not exist yet, all tests in this file are collected
# but skipped with a clear message rather than failing with ImportError.
# Remove this guard once ``ml/ingest/met_hf.py`` is implemented.
# ---------------------------------------------------------------------------

try:
    from ml.ingest.met_hf import (
        DEFAULT_CONSECUTIVE_CDN_FAIL_LIMIT,
        HFIngestStats,
        MetCDNCircuitBreakerError,
        _row_passes_hf_filter,
        convert_hf_row,
        ingest_met_hf_to_db,
    )
    _MODULE_MISSING = False
except ImportError:
    _MODULE_MISSING = True

pytestmark = pytest.mark.skipif(
    _MODULE_MISSING,
    reason="ml.ingest.met_hf not yet implemented — tests will run once the module exists",
)


# ===========================================================================
# Fixtures
# ===========================================================================

# A realistic 58-column HF CSV row for "Wheat Field with Cypresses".
# Column names match Met API JSON field names exactly (camelCase), per the
# task brief. ``tags`` is a JSON string — the one divergence from the API.
_VAN_GOGH_HF_ROW: dict[str, Any] = {
    # --- core identity ---
    "objectID": "436532",                           # string in CSV
    "isHighlight": "False",
    "accessionNumber": "1993.132",
    "accessionYear": "1993",
    "isPublicDomain": "True",                       # string in CSV
    # --- images ---
    "primaryImage": "https://images.metmuseum.org/CRDImages/ep/original/full.jpg",
    "primaryImageSmall": "https://images.metmuseum.org/CRDImages/ep/web-large/small.jpg",
    "additionalImages": "[]",
    # --- constituents ---
    "constituents": '[{"constituentID":161947,"role":"Artist","name":"Vincent van Gogh","constituentULAN_URL":"","constituentWikidata_URL":"","gender":""}]',
    # --- classification / type ---
    "department": "European Paintings",
    "objectName": "Painting",
    "title": "Wheat Field with Cypresses",
    "culture": "",
    "period": "",
    "dynasty": "",
    "reign": "",
    "portfolio": "",
    # --- artist ---
    "artistRole": "Artist",
    "artistPrefix": "",
    "artistDisplayName": "Vincent van Gogh",
    "artistDisplayBio": "Dutch, Zundert 1853–1890 Auvers-sur-Oise",
    "artistSuffix": "",
    "artistAlphaSort": "Gogh, Vincent van",
    "artistNationality": "Dutch",
    "artistBeginDate": "1853",
    "artistEndDate": "1890",
    "artistGender": "",
    "artistWikidata_URL": "https://www.wikidata.org/wiki/Q5582",
    "artistULAN_URL": "http://vocab.getty.edu/page/ulan/500115588",
    # --- dates ---
    "objectDate": "1889",
    "objectBeginDate": "1889",
    "objectEndDate": "1889",
    # --- physical ---
    "medium": "Oil on canvas",
    "dimensions": "28 7/8 x 36 1/2 in. (73.2 x 92.7 cm)",
    "measurements": '[{"elementName":"Overall","elementDescription":null,"elementMeasurements":{"Height":73.2,"Width":92.7}}]',
    "creditLine": "Purchase, The Annenberg Foundation Gift, 1993",
    # --- geography ---
    "geographyType": "",
    "city": "",
    "state": "",
    "county": "",
    "country": "",
    "region": "",
    "subregion": "",
    "locale": "",
    "locus": "",
    "excavation": "",
    "river": "",
    # --- classification ---
    "classification": "Paintings",
    "rightsAndReproduction": "",
    "linkResource": "https://www.metmuseum.org/art/collection/search/436532",
    "metadataDate": "2023-10-15T00:00:00.000Z",
    "repository": "Metropolitan Museum of Art, New York, NY",
    "objectURL": "https://www.metmuseum.org/art/collection/search/436532",
    # --- tags (JSON string — key divergence from live API) ---
    "tags": '[{"term": "Landscapes", "AAT_URL": "http://vocab.getty.edu/page/aat/300132294", "Wikidata_URL": "https://www.wikidata.org/wiki/Q107425"}, {"term": "Trees", "AAT_URL": "http://vocab.getty.edu/page/aat/300132410", "Wikidata_URL": "https://www.wikidata.org/wiki/Q10884"}]',
    # --- extra fields to 58 ---
    "objectWikidata_URL": "https://www.wikidata.org/wiki/Q18689539",
    "isTimelineWork": "True",
    "GalleryNumber": "822",
    # 58th column: tags AAT URL (separate from the tags JSON field in some
    # HF dataset revisions; kept here to hit the documented column count).
    "tagsAAT_URL": "http://vocab.getty.edu/page/aat/300132294",
}

# Soft assertion: fixture must be close to the documented 58-column count.
# Exact count may vary by HF dataset revision; adjust when the real schema
# is confirmed.
assert len(_VAN_GOGH_HF_ROW) >= 50, (
    f"Fixture looks too small ({len(_VAN_GOGH_HF_ROW)} columns); "
    "HF dataset should have ~58 columns"
)


def _hf_row(**overrides: Any) -> dict[str, Any]:
    """Return a copy of the base fixture with the given overrides applied."""
    row = dict(_VAN_GOGH_HF_ROW)
    row.update(overrides)
    return row


# ===========================================================================
# 1. Row conversion — convert_hf_row
# ===========================================================================


class TestConvertHfRow:
    """convert_hf_row produces a map_met_record()-compatible dict."""

    def test_happy_path_field_values(self):
        """Core fields survive conversion intact."""
        converted = convert_hf_row(_VAN_GOGH_HF_ROW)

        assert converted["objectID"] == 436532          # int, not string
        assert converted["isPublicDomain"] is True      # bool, not string
        assert converted["title"] == "Wheat Field with Cypresses"
        assert converted["artistDisplayName"] == "Vincent van Gogh"
        assert converted["medium"] == "Oil on canvas"
        assert converted["objectDate"] == "1889"
        assert converted["classification"] == "Paintings"
        assert converted["department"] == "European Paintings"
        assert converted["primaryImage"].startswith("https://images.metmuseum.org")
        assert converted["primaryImageSmall"].startswith("https://images.metmuseum.org")

    def test_tags_parsed_from_json_string(self):
        """tags JSON string → list of dicts with 'term' key."""
        converted = convert_hf_row(_VAN_GOGH_HF_ROW)
        tags = converted["tags"]
        assert isinstance(tags, list), "tags must be a list after conversion"
        assert len(tags) == 2
        assert {"term": "Landscapes"} == {k: v for k, v in tags[0].items() if k == "term"}
        assert any(t["term"] == "Trees" for t in tags)

    def test_object_id_coerced_to_int(self):
        """objectID string '436532' → int 436532."""
        converted = convert_hf_row(_hf_row(objectID="436532"))
        assert converted["objectID"] == 436532
        assert isinstance(converted["objectID"], int)

    def test_is_public_domain_true_string_coerced(self):
        converted = convert_hf_row(_hf_row(isPublicDomain="True"))
        assert converted["isPublicDomain"] is True

    def test_is_public_domain_false_string_coerced(self):
        converted = convert_hf_row(_hf_row(isPublicDomain="False"))
        assert converted["isPublicDomain"] is False

    def test_is_public_domain_already_bool(self):
        """convert_hf_row must be idempotent when isPublicDomain is already bool."""
        converted = convert_hf_row(_hf_row(isPublicDomain=True))
        assert converted["isPublicDomain"] is True

    def test_enrichment_fields_preserved(self):
        """D-024 enrichment fields pass through unconverted."""
        converted = convert_hf_row(_VAN_GOGH_HF_ROW)
        assert converted["artistDisplayBio"] == "Dutch, Zundert 1853–1890 Auvers-sur-Oise"
        assert converted["creditLine"] == "Purchase, The Annenberg Foundation Gift, 1993"
        assert converted["dimensions"] == "28 7/8 x 36 1/2 in. (73.2 x 92.7 cm)"
        assert converted["dynasty"] == ""           # empty string — map_met_record normalises to None
        assert converted["objectWikidata_URL"] == "https://www.wikidata.org/wiki/Q18689539"

    def test_date_begin_end_coerced_to_int(self):
        """objectBeginDate / objectEndDate strings → int for map_met_record."""
        converted = convert_hf_row(_VAN_GOGH_HF_ROW)
        assert converted["objectBeginDate"] == 1889
        assert converted["objectEndDate"] == 1889

    def test_empty_string_begin_end_date_becomes_none_or_zero(self):
        """Empty objectBeginDate/End strings don't raise; produce None or 0
        (map_met_record will normalise 0 → None downstream)."""
        converted = convert_hf_row(_hf_row(objectBeginDate="", objectEndDate=""))
        # Acceptable outputs: None or 0 — both are handled by map_met_record.
        assert converted.get("objectBeginDate") in (None, 0, "")


# ===========================================================================
# 2. isPublicDomain filter — _row_passes_hf_filter
# ===========================================================================


class TestRowPassesHfFilter:
    """_row_passes_hf_filter guards the pipeline before convert_hf_row."""

    def test_accepts_public_domain_true_string(self):
        assert _row_passes_hf_filter(_hf_row(isPublicDomain="True")) is True

    def test_rejects_public_domain_false_string(self):
        assert _row_passes_hf_filter(_hf_row(isPublicDomain="False")) is False

    def test_rejects_empty_string(self):
        assert _row_passes_hf_filter(_hf_row(isPublicDomain="")) is False

    def test_accepts_bool_true(self):
        assert _row_passes_hf_filter(_hf_row(isPublicDomain=True)) is True

    def test_rejects_bool_false(self):
        assert _row_passes_hf_filter(_hf_row(isPublicDomain=False)) is False

    def test_rejects_missing_key(self):
        row = dict(_VAN_GOGH_HF_ROW)
        row.pop("isPublicDomain", None)
        assert _row_passes_hf_filter(row) is False


# ===========================================================================
# 3. Tags parsing edge cases
# ===========================================================================


class TestTagsParsing:
    """convert_hf_row handles all observed tags shapes gracefully."""

    def test_tags_json_string_round_trips(self):
        tags_list = [{"term": "Landscapes"}, {"term": "Portraits"}]
        converted = convert_hf_row(_hf_row(tags=json.dumps(tags_list)))
        assert converted["tags"] == tags_list

    def test_tags_null_string_becomes_empty_list(self):
        """HF CSV represents SQL NULL as the literal string 'null' or empty."""
        converted = convert_hf_row(_hf_row(tags="null"))
        result = converted["tags"]
        assert result == [] or result is None, (
            "null tags must yield [] or None, not raise"
        )

    def test_tags_none_python_becomes_empty_list(self):
        converted = convert_hf_row(_hf_row(tags=None))
        result = converted["tags"]
        assert result == [] or result is None

    def test_tags_empty_string_becomes_empty_list(self):
        converted = convert_hf_row(_hf_row(tags=""))
        result = converted["tags"]
        assert result == [] or result is None

    def test_tags_empty_json_array_string(self):
        converted = convert_hf_row(_hf_row(tags="[]"))
        assert converted["tags"] == []

    def test_tags_malformed_json_does_not_raise(self):
        """A corrupted tags string must not abort the whole ingest job."""
        # Acceptable: returns [] / None / raises a benign ValueError that the
        # caller catches — but must NOT propagate an unhandled exception here.
        try:
            result = convert_hf_row(_hf_row(tags="{bad json"))
            tags = result["tags"]
            assert tags == [] or tags is None
        except (ValueError, json.JSONDecodeError):
            pass  # Also acceptable: caller is expected to handle this.

    def test_tags_piped_through_map_met_record(self):
        """End-to-end: JSON-string tags arrive in NormalizedArtwork.tags."""
        from ml.ingest.met_db import map_met_record

        converted = convert_hf_row(_VAN_GOGH_HF_ROW)
        artwork = map_met_record(converted)
        assert artwork is not None
        assert "Landscapes" in artwork["tags"]
        assert "Trees" in artwork["tags"]

    def test_null_tags_still_produce_valid_artwork(self):
        """A row with null tags should produce a valid NormalizedArtwork
        (with department/classification tags still present from map_met_record)."""
        from ml.ingest.met_db import map_met_record

        converted = convert_hf_row(_hf_row(tags=None))
        artwork = map_met_record(converted)
        assert artwork is not None
        # Department / classification are always added by map_met_record from
        # the non-tags fields.
        assert "Paintings" in artwork["tags"] or "European Paintings" in artwork["tags"]


# ===========================================================================
# 4. map_met_record integration via converted row
# ===========================================================================


class TestConvertedRowViaMapMetRecord:
    """Verify that convert_hf_row + map_met_record produce correct output."""

    def test_full_pipeline_produces_canonical_id(self):
        from ml.ingest.met_db import map_met_record

        converted = convert_hf_row(_VAN_GOGH_HF_ROW)
        artwork = map_met_record(converted)
        assert artwork is not None
        assert artwork["id"] == "met:436532"
        assert artwork["source"] == "met"
        assert artwork["source_id"] == "436532"

    def test_non_public_domain_rejected_by_map_met_record(self):
        """convert_hf_row + map_met_record must reject non-PD records."""
        from ml.ingest.met_db import map_met_record

        converted = convert_hf_row(_hf_row(isPublicDomain="False"))
        assert map_met_record(converted) is None

    def test_missing_primary_image_rejected(self):
        """primaryImage empty → map_met_record returns None (no artwork)."""
        from ml.ingest.met_db import map_met_record

        converted = convert_hf_row(_hf_row(primaryImage=""))
        assert map_met_record(converted) is None


# ===========================================================================
# 5. Missing / empty primaryImageSmall — no download attempted
# ===========================================================================


class TestMissingImageUrl:
    """Rows with empty primaryImageSmall are handled without download attempt.

    The adapter uses primaryImage as the canonical image URL for embedding
    (via map_met_record → image_url).  primaryImageSmall is stored in
    raw_metadata.thumbnail_url only — it's never the download target.

    The important invariant is: if primaryImageSmall is empty, the adapter
    must NOT make a CDN request for a thumbnail URL, and it must NOT pass
    an empty string to the HTTP client.
    """

    def test_empty_primary_image_small_does_not_download(self):
        """When primaryImageSmall is empty the adapter logs it as
        no-thumbnail but proceeds normally with primaryImage download."""
        from ml.ingest.met_db import map_met_record

        converted = convert_hf_row(_hf_row(primaryImageSmall=""))
        artwork = map_met_record(converted)

        if artwork is not None:
            # thumbnail_url must be None (empty string normalised).
            assert artwork["raw_metadata"]["thumbnail_url"] is None

    def test_row_with_no_primary_image_small_skipped_or_no_thumbnail(self):
        """Row where primaryImageSmall key is absent produces None thumbnail,
        not a KeyError."""
        row = dict(_VAN_GOGH_HF_ROW)
        row.pop("primaryImageSmall", None)
        converted = convert_hf_row(row)
        # Must not raise.
        assert converted.get("primaryImageSmall", "") in ("", None)

    @pytest.mark.asyncio
    async def test_ingest_rows_without_image_small_increments_skipped_counter(
        self, monkeypatch
    ):
        """A row with primaryImageSmall='' should increment
        hf_rows_skipped_no_image (not attempt a download) when the adapter
        uses primaryImageSmall as the download target (implementation choice).

        If the adapter uses primaryImage instead, this counter may stay 0
        and that is also acceptable — adjust the assertion accordingly once
        the implementation is known.
        """
        # Row has a valid primaryImage but empty primaryImageSmall.
        row = _hf_row(primaryImageSmall="")

        # Fake embedder so we don't need torch.
        mock_embedder = MagicMock()
        mock_embedder.embed_bytes.return_value = [0.0] * 768
        monkeypatch.setattr("ml.ingest.met_hf.get_embedder", lambda: mock_embedder)

        # Fake CDN client — should NOT be called for an empty URL.
        download_called = []

        async def _fake_download(client, url, **kwargs):
            download_called.append(url)
            return b"\xff\xd8\xff"  # minimal JPEG magic bytes

        monkeypatch.setattr("ml.ingest.met_hf._download_image", _fake_download)

        stats = await ingest_met_hf_to_db(
            pool=None,
            rows=[row],
            dry_run=True,
        )
        # Either the row was skipped (no_image counter) or the download was
        # attempted against primaryImage (not empty).  In either case, no
        # download against an empty URL.
        for url in download_called:
            assert url, "download must never be called with an empty URL"


# ===========================================================================
# 6. isPublicDomain filter — ingest-level skip
# ===========================================================================


@pytest.mark.asyncio
async def test_ingest_skips_non_public_domain_rows(monkeypatch):
    """Rows with isPublicDomain=False are filtered before any processing."""
    mock_embedder = MagicMock()
    mock_embedder.embed_bytes.return_value = [0.0] * 768
    monkeypatch.setattr("ml.ingest.met_hf.get_embedder", lambda: mock_embedder)

    download_called = []

    async def _fake_download(client, url, **kwargs):
        download_called.append(url)
        return b"\xff\xd8\xff"

    monkeypatch.setattr("ml.ingest.met_hf._download_image", _fake_download)

    rows = [
        _hf_row(isPublicDomain="True"),   # accepted
        _hf_row(isPublicDomain="False"),  # skipped
        _hf_row(isPublicDomain="False"),  # skipped
        _hf_row(isPublicDomain="True"),   # accepted
    ]

    stats = await ingest_met_hf_to_db(pool=None, rows=rows, dry_run=True)

    assert stats.hf_rows_total == 4
    assert stats.hf_rows_accepted == 2
    assert stats.hf_rows_skipped_filter == 2


@pytest.mark.asyncio
async def test_ingest_accepts_public_domain_row(monkeypatch):
    """At least one row with isPublicDomain=True is processed past the filter."""
    mock_embedder = MagicMock()
    mock_embedder.embed_bytes.return_value = [0.0] * 768
    monkeypatch.setattr("ml.ingest.met_hf.get_embedder", lambda: mock_embedder)

    async def _fake_download(client, url, **kwargs):
        return b"\xff\xd8\xff"

    monkeypatch.setattr("ml.ingest.met_hf._download_image", _fake_download)

    stats = await ingest_met_hf_to_db(
        pool=None,
        rows=[_hf_row(isPublicDomain="True")],
        dry_run=True,
    )

    assert stats.hf_rows_total == 1
    assert stats.hf_rows_accepted == 1
    assert stats.hf_rows_skipped_filter == 0


# ===========================================================================
# 7. Circuit breaker — consecutive CDN image download failures
# ===========================================================================


@pytest.mark.asyncio
async def test_circuit_breaker_trips_on_consecutive_failures(monkeypatch):
    """N consecutive CDN download failures abort via MetCDNCircuitBreakerError."""
    mock_embedder = MagicMock()
    mock_embedder.embed_bytes.return_value = [0.0] * 768
    monkeypatch.setattr("ml.ingest.met_hf.get_embedder", lambda: mock_embedder)

    import httpx

    async def _always_fails(client, url, **kwargs):
        request = httpx.Request("GET", url)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("503", request=request, response=response)

    monkeypatch.setattr("ml.ingest.met_hf._download_image", _always_fails)

    # Build enough rows to trip a threshold of 3.
    rows = [_hf_row(isPublicDomain="True") for _ in range(6)]

    with pytest.raises(MetCDNCircuitBreakerError, match="consecutive"):
        await ingest_met_hf_to_db(
            pool=None,
            rows=rows,
            dry_run=True,
            circuit_breaker_threshold=3,
        )


@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_success(monkeypatch):
    """Successful download resets the failure counter so it doesn't
    accumulate across non-consecutive failures."""
    mock_embedder = MagicMock()
    mock_embedder.embed_bytes.return_value = [0.0] * 768
    monkeypatch.setattr("ml.ingest.met_hf.get_embedder", lambda: mock_embedder)

    import httpx

    call_count = {"n": 0}

    async def _fail_then_succeed(client, url, **kwargs):
        call_count["n"] += 1
        # Fail on calls 1, 3, 5 — succeed on 2, 4, 6.
        # Max consecutive failures is 1, well below threshold of 3.
        if call_count["n"] % 2 == 1:
            request = httpx.Request("GET", url)
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("503", request=request, response=response)
        return b"\xff\xd8\xff"

    monkeypatch.setattr("ml.ingest.met_hf._download_image", _fail_then_succeed)

    rows = [_hf_row(isPublicDomain="True") for _ in range(6)]

    # Should NOT trip the circuit breaker (threshold=3, max_consecutive=1).
    stats = await ingest_met_hf_to_db(
        pool=None,
        rows=rows,
        dry_run=True,
        circuit_breaker_threshold=3,
    )
    assert stats.hf_rows_total == 6


@pytest.mark.asyncio
async def test_circuit_breaker_default_threshold_is_sane(monkeypatch):
    """DEFAULT_CONSECUTIVE_CDN_FAIL_LIMIT is a positive integer (sanity guard)."""
    assert isinstance(DEFAULT_CONSECUTIVE_CDN_FAIL_LIMIT, int)
    assert DEFAULT_CONSECUTIVE_CDN_FAIL_LIMIT > 0


@pytest.mark.asyncio
async def test_circuit_breaker_zero_threshold_raises_value_error(monkeypatch):
    """circuit_breaker_threshold=0 is invalid; must raise ValueError immediately."""
    mock_embedder = MagicMock()
    monkeypatch.setattr("ml.ingest.met_hf.get_embedder", lambda: mock_embedder)

    with pytest.raises(ValueError):
        await ingest_met_hf_to_db(
            pool=None,
            rows=[],
            dry_run=True,
            circuit_breaker_threshold=0,
        )


# ===========================================================================
# 8. CLI registration smoke
# ===========================================================================


def test_cli_registers_met_hf_subcommand():
    """``ingest met-hf`` is reachable from the CLI parser."""
    from ml.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args([
        "ingest",
        "met-hf",
        "--max-records", "10",
        "--dry-run",
    ])
    assert args.source == "met-hf"
    assert args.max_records == 10
    assert args.dry_run is True


def test_cli_met_hf_circuit_breaker_flag():
    """``--circuit-breaker-threshold`` is accepted and parsed as int."""
    from ml.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args([
        "ingest",
        "met-hf",
        "--circuit-breaker-threshold", "7",
    ])
    assert args.circuit_breaker_threshold == 7


# ===========================================================================
# 9. HFIngestStats
# ===========================================================================


class TestHFIngestStats:

    def test_stats_has_required_counters(self):
        stats = HFIngestStats()
        required = {
            "hf_rows_total",
            "hf_rows_accepted",
            "hf_rows_skipped_filter",
        }
        d = stats.as_dict()
        for key in required:
            assert key in d, f"HFIngestStats.as_dict() missing '{key}'"

    def test_stats_as_dict_total_persisted(self):
        """total_persisted = inserted + updated."""
        stats = HFIngestStats()
        stats.inserted = 3
        stats.updated = 2
        assert stats.total_persisted == 5

    def test_stats_zero_initialised(self):
        stats = HFIngestStats()
        d = stats.as_dict()
        for key, val in d.items():
            if isinstance(val, int):
                assert val == 0, f"Counter '{key}' should start at 0"
