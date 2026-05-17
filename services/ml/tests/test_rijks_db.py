"""Unit tests for the Rijksmuseum → DB ingest mapper.

Covers the **pure** field-mapping pipeline only — no network, no embedder,
no Postgres. Mirrors the discipline of ``test_met_db_ingest.py`` and
``test_aic_db.py``. Live OAI-PMH ingestion is smoke-tested manually via
``art-guide-ml ingest rijks --dry-run --limit 5``.

Fixtures are tiny synthetic OAI EDM XML payloads modelled after real
records sampled during the field audit (docs/rijks-ingest-field-audit.md
§3). Each fixture is the minimal subset of EDM markup needed to exercise
one branch of the mapper.
"""

from __future__ import annotations

import pytest

from ml.ingest.rijks_db import (
    RIJKS_LICENSE,
    RIJKS_LICENSE_URL,
    RIJKS_MUSEUM_NAME,
    RIJKS_SOURCE_SLUG,
    _artist_bio_from_agent,
    _clean_text,
    _iter_records_from_xml,
    _parse_year_range,
    _resumption_token,
    map_rijks_record,
)


# --------------------------------------------------------- helpers / fixtures

def _envelope(records_xml: str, *, resumption: str | None = None,
              complete_size: int | None = None) -> bytes:
    """Wrap one or more ``<record>`` blocks in a ListRecords OAI envelope."""
    if resumption is not None:
        size_attr = f' completeListSize="{complete_size}"' if complete_size else ""
        rt = f'<resumptionToken{size_attr}>{resumption}</resumptionToken>'
    else:
        rt = ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-05-17T04:00:00Z</responseDate>
  <ListRecords>
    {records_xml}
    {rt}
  </ListRecords>
</OAI-PMH>
""".encode("utf-8")


# Realistic, slightly trimmed record for The Night Watch.
_NIGHT_WATCH_RECORD = """
<record xmlns="http://www.openarchives.org/OAI/2.0/">
  <header>
    <identifier>https://id.rijksmuseum.nl/200107928</identifier>
    <datestamp>2024-09-27T07:20:23Z</datestamp>
    <setSpec>261208</setSpec>
  </header>
  <metadata>
    <rdf:RDF
        xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns:edm="http://www.europeana.eu/schemas/edm/"
        xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:dcterms="http://purl.org/dc/terms/"
        xmlns:ore="http://www.openarchives.org/ore/terms/"
        xmlns:skos="http://www.w3.org/2004/02/skos/core#"
        xmlns:owl="http://www.w3.org/2002/07/owl#"
        xmlns:rdaGr2="http://rdvocab.info/ElementsGr2/">
      <ore:Aggregation rdf:about="https://id.rijksmuseum.nl/200107928#aggregation">
        <edm:aggregatedCHO>
          <edm:ProvidedCHO rdf:about="https://id.rijksmuseum.nl/200107928">
            <dc:creator rdf:resource="https://id.rijksmuseum.nl/2103429"/>
            <dc:description xml:lang="en">Rembrandt's largest, most famous canvas was made for the Arquebusiers guild hall.</dc:description>
            <dc:description xml:lang="nl">Rembrandts beroemdste en grootste doek werd gemaakt voor de Kloveniersdoelen.</dc:description>
            <dc:identifier>SK-C-5</dc:identifier>
            <dc:title xml:lang="en">The Night Watch</dc:title>
            <dc:title xml:lang="nl">De Nachtwacht</dc:title>
            <dc:type rdf:resource="https://id.rijksmuseum.nl/2208"/>
            <dc:subject rdf:resource="https://id.rijksmuseum.nl/221306"/>
            <dcterms:created xml:lang="en">1642</dcterms:created>
            <dcterms:created xml:lang="nl">1642</dcterms:created>
            <dcterms:extent xml:lang="en">height 379.5 cm x width 453.5 cm</dcterms:extent>
            <dcterms:extent xml:lang="nl">hoogte 379,5 cm x breedte 453,5 cm</dcterms:extent>
            <dcterms:medium rdf:resource="https://id.rijksmuseum.nl/22010"/>
            <dcterms:medium rdf:resource="https://id.rijksmuseum.nl/2209"/>
            <dcterms:isPartOf rdf:resource="https://id.rijksmuseum.nl/260212"/>
            <dcterms:provenance xml:lang="en">Commissioned by or for the sitters for the great hall of the Kloveniersdoelen.</dcterms:provenance>
          </edm:ProvidedCHO>
        </edm:aggregatedCHO>
        <edm:provider>Rijksmuseum</edm:provider>
        <edm:rights rdf:resource="http://creativecommons.org/publicdomain/mark/1.0/"/>
        <edm:dataProvider>Rijksmuseum</edm:dataProvider>
        <edm:isShownBy>
          <edm:WebResource rdf:about="https://iiif.micr.io/PJEZO/full/max/0/default.jpg"/>
        </edm:isShownBy>
        <edm:object>
          <edm:WebResource rdf:about="https://iiif.micr.io/PJEZO/full/max/0/default.jpg"/>
        </edm:object>
      </ore:Aggregation>
      <edm:Agent rdf:about="https://id.rijksmuseum.nl/2103429">
        <rdaGr2:dateOfBirth xml:lang="nl">1606-07-15</rdaGr2:dateOfBirth>
        <rdaGr2:dateOfDeath xml:lang="nl">1669-10-08</rdaGr2:dateOfDeath>
        <skos:prefLabel xml:lang="en">Rembrandt van Rijn</skos:prefLabel>
        <skos:prefLabel xml:lang="nl">Rembrandt van Rijn</skos:prefLabel>
        <owl:sameAs rdf:resource="http://www.wikidata.org/entity/Q5598"/>
      </edm:Agent>
      <skos:Concept rdf:about="https://id.rijksmuseum.nl/22010">
        <skos:prefLabel xml:lang="en">canvas</skos:prefLabel>
        <skos:prefLabel xml:lang="nl">doek</skos:prefLabel>
      </skos:Concept>
      <skos:Concept rdf:about="https://id.rijksmuseum.nl/2209">
        <skos:prefLabel xml:lang="en">oil paint (paint)</skos:prefLabel>
        <skos:prefLabel xml:lang="nl">olieverf</skos:prefLabel>
      </skos:Concept>
      <skos:Concept rdf:about="https://id.rijksmuseum.nl/2208">
        <skos:prefLabel xml:lang="en">painting</skos:prefLabel>
        <skos:prefLabel xml:lang="nl">schilderij</skos:prefLabel>
      </skos:Concept>
      <skos:Concept rdf:about="https://id.rijksmuseum.nl/221306">
        <skos:prefLabel xml:lang="en">weapons</skos:prefLabel>
      </skos:Concept>
    </rdf:RDF>
  </metadata>
</record>
"""

# Record with no image (skipped at mapping time).
_NO_IMAGE_RECORD = """
<record xmlns="http://www.openarchives.org/OAI/2.0/">
  <header>
    <identifier>https://id.rijksmuseum.nl/200104600</identifier>
    <datestamp>2024-08-30T14:20:05Z</datestamp>
    <setSpec>261208</setSpec>
  </header>
  <metadata>
    <rdf:RDF
        xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns:edm="http://www.europeana.eu/schemas/edm/"
        xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:dcterms="http://purl.org/dc/terms/"
        xmlns:ore="http://www.openarchives.org/ore/terms/">
      <ore:Aggregation rdf:about="https://id.rijksmuseum.nl/200104600#aggregation">
        <edm:aggregatedCHO>
          <edm:ProvidedCHO rdf:about="https://id.rijksmuseum.nl/200104600">
            <dc:identifier>SK-A-1490</dc:identifier>
            <dc:title xml:lang="nl">Apen in een kooi</dc:title>
            <dcterms:created xml:lang="en">1884</dcterms:created>
          </edm:ProvidedCHO>
        </edm:aggregatedCHO>
        <edm:rights rdf:resource="http://creativecommons.org/publicdomain/mark/1.0/"/>
      </ore:Aggregation>
    </rdf:RDF>
  </metadata>
</record>
"""

# Anonymous, sparsely-populated record (no creator, no description).
_ANONYMOUS_RECORD = """
<record xmlns="http://www.openarchives.org/OAI/2.0/">
  <header>
    <identifier>https://id.rijksmuseum.nl/200107774</identifier>
    <datestamp>2024-08-30T14:20:05Z</datestamp>
    <setSpec>261208</setSpec>
  </header>
  <metadata>
    <rdf:RDF
        xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns:edm="http://www.europeana.eu/schemas/edm/"
        xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:dcterms="http://purl.org/dc/terms/"
        xmlns:ore="http://www.openarchives.org/ore/terms/"
        xmlns:skos="http://www.w3.org/2004/02/skos/core#">
      <ore:Aggregation rdf:about="https://id.rijksmuseum.nl/200107774#aggregation">
        <edm:aggregatedCHO>
          <edm:ProvidedCHO rdf:about="https://id.rijksmuseum.nl/200107774">
            <dc:identifier>SK-A-3901</dc:identifier>
            <dc:title xml:lang="en">The Tree of Jesse</dc:title>
            <dc:type rdf:resource="https://id.rijksmuseum.nl/2208"/>
            <dcterms:created xml:lang="en">c. 1500</dcterms:created>
            <dcterms:medium rdf:resource="https://id.rijksmuseum.nl/2209"/>
          </edm:ProvidedCHO>
        </edm:aggregatedCHO>
        <edm:rights rdf:resource="http://creativecommons.org/publicdomain/mark/1.0/"/>
        <edm:isShownBy>
          <edm:WebResource rdf:about="https://iiif.micr.io/LcfFk/full/max/0/default.jpg"/>
        </edm:isShownBy>
      </ore:Aggregation>
      <skos:Concept rdf:about="https://id.rijksmuseum.nl/2208">
        <skos:prefLabel xml:lang="en">painting</skos:prefLabel>
      </skos:Concept>
      <skos:Concept rdf:about="https://id.rijksmuseum.nl/2209">
        <skos:prefLabel xml:lang="en">oil paint (paint)</skos:prefLabel>
      </skos:Concept>
    </rdf:RDF>
  </metadata>
</record>
"""

# Non-public-domain record (filter should reject).
_RESTRICTED_RECORD = """
<record xmlns="http://www.openarchives.org/OAI/2.0/">
  <header>
    <identifier>https://id.rijksmuseum.nl/300000001</identifier>
    <datestamp>2024-08-30T14:20:05Z</datestamp>
    <setSpec>261208</setSpec>
  </header>
  <metadata>
    <rdf:RDF
        xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns:edm="http://www.europeana.eu/schemas/edm/"
        xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:ore="http://www.openarchives.org/ore/terms/">
      <ore:Aggregation rdf:about="https://id.rijksmuseum.nl/300000001#aggregation">
        <edm:aggregatedCHO>
          <edm:ProvidedCHO rdf:about="https://id.rijksmuseum.nl/300000001">
            <dc:title xml:lang="en">Restricted Modern Work</dc:title>
          </edm:ProvidedCHO>
        </edm:aggregatedCHO>
        <edm:rights rdf:resource="http://rightsstatements.org/vocab/InC/1.0/"/>
        <edm:isShownBy>
          <edm:WebResource rdf:about="https://iiif.micr.io/zzzz/full/max/0/default.jpg"/>
        </edm:isShownBy>
      </ore:Aggregation>
    </rdf:RDF>
  </metadata>
</record>
"""


# --------------------------------------------------------- date / text helpers

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1642",             (1642, 1642)),
        ("c. 1500",          (1500, 1500)),
        ("ca. 1500",         (1500, 1500)),
        ("c. 1735 - c. 1745", (1735, 1745)),
        ("618 - 906",        (618,  906)),
        ("1500 - before 1537", (1500, 1537)),
        ("",                 (None, None)),
        (None,               (None, None)),
        ("19th century",     (None, None)),   # no extractable year tokens
        ("1899",             (1899, 1899)),
    ],
)
def test_parse_year_range(raw, expected):
    assert _parse_year_range(raw) == expected


def test_clean_text_strips_pseudo_html_and_truncates():
    raw = "<em>Italics</em>   with   extra  spaces  and a really long tail " * 5
    out = _clean_text(raw, max_len=80)
    assert out is not None
    assert "<em>" not in out and "</em>" not in out
    assert "  " not in out
    assert len(out) <= 80
    assert out.endswith("…")


def test_clean_text_none_passes():
    assert _clean_text(None) is None
    assert _clean_text("") is None
    assert _clean_text("   ") is None


def test_artist_bio_synthesises_year_range():
    agent = {"birth": "1606-07-15", "death": "1669-10-08", "names": {"en": "Rembrandt"}}
    assert _artist_bio_from_agent(agent) == "1606 – 1669"


def test_artist_bio_handles_circa_prefix():
    agent = {"birth": "ca. 1592 - ca. 1593", "death": "1655-08-16 - 1655-08-26"}
    # Birth: "ca. 1592 - ca. 1593" → birth-circa True, first year 1592
    # Death: "1655-08-16 - 1655-08-26" → death-circa False, last year 1655
    bio = _artist_bio_from_agent(agent)
    assert bio is not None
    assert "1592" in bio and "1655" in bio
    assert bio.startswith("ca. ")  # circa prefix preserved


def test_artist_bio_returns_none_when_no_dates():
    assert _artist_bio_from_agent({"birth": None, "death": None}) is None
    assert _artist_bio_from_agent({"birth": "", "death": ""}) is None


# --------------------------------------------------------- XML envelope helpers

def test_iter_records_skips_deleted_markers():
    xml = _envelope(
        '<record><header status="deleted"><identifier>x</identifier></header></record>'
        + _NIGHT_WATCH_RECORD
    )
    records = _iter_records_from_xml(xml)
    assert len(records) == 1
    assert records[0]["numeric_id"] == "200107928"


def test_resumption_token_extracted():
    xml = _envelope(_NIGHT_WATCH_RECORD,
                    resumption="bWV0YWRhdGE=", complete_size=4916)
    token, size = _resumption_token(xml)
    assert token == "bWV0YWRhdGE="
    assert size == 4916


def test_resumption_token_absent_when_no_more_pages():
    xml = _envelope(_NIGHT_WATCH_RECORD)
    token, size = _resumption_token(xml)
    assert token is None
    assert size is None


# --------------------------------------------------------- map_rijks_record

def _parse_one(record_xml: str) -> dict:
    return _iter_records_from_xml(_envelope(record_xml))[0]


def test_map_rijks_record_happy_path_night_watch():
    out = map_rijks_record(_parse_one(_NIGHT_WATCH_RECORD))

    assert out is not None
    # Canonical id + source columns (idempotency key).
    assert out["id"] == "rijks:200107928"
    assert out["source"] == RIJKS_SOURCE_SLUG
    assert out["source_id"] == "200107928"
    assert out["museum"] == RIJKS_MUSEUM_NAME

    # Title prefers English where available.
    assert out["title"] == "The Night Watch"

    # Artist resolved from inline edm:Agent.
    assert out["artist"] == "Rembrandt van Rijn"

    # Bio synthesised from dateOfBirth / dateOfDeath.
    assert out["artist_bio"] == "1606 – 1669"

    # Date.
    assert out["date"] == "1642"
    assert out["date_begin"] == 1642
    assert out["date_end"] == 1642

    # Medium resolves both concepts and joins with comma.
    assert "canvas" in out["medium"]
    assert "oil paint (paint)" in out["medium"]

    # Tags: dedup union of type + subject labels.
    assert "painting" in out["tags"]
    assert "weapons" in out["tags"]

    # source_url synthesised from museum_id.
    assert out["source_url"] == "https://www.rijksmuseum.nl/en/collection/SK-C-5"

    # Image url straight from edm:isShownBy WebResource.
    assert out["image_url"] == "https://iiif.micr.io/PJEZO/full/max/0/default.jpg"

    # Public domain.
    assert out["is_public_domain"] is True

    # Dimensions from dcterms:extent (English variant).
    assert "379.5 cm" in out["dimensions"]

    # credit_line from dcterms:provenance.
    assert out["credit_line"] is not None
    assert "Kloveniersdoelen" in out["credit_line"]

    # raw_metadata safety valve.
    assert out["raw_metadata"]["license"] == RIJKS_LICENSE
    assert out["raw_metadata"]["license_url"] == RIJKS_LICENSE_URL
    rijks_meta = out["raw_metadata"]["rijks"]
    assert rijks_meta["museum_id"] == "SK-C-5"
    assert "Arquebusiers" in rijks_meta["description_en"]
    assert rijks_meta["description_nl"].startswith("Rembrandts")
    assert "http://www.wikidata.org/entity/Q5598" in rijks_meta["agent_wikidata_urls"]

    # culture / period / dynasty / object_wikidata_url unmapped for Rijks → null.
    assert out["culture"] is None
    assert out["period"] is None
    assert out["dynasty"] is None
    assert out["object_wikidata_url"] is None


def test_map_rijks_record_no_image_returns_none():
    """Records without <edm:isShownBy> are filtered (counted as skipped_filter)."""
    assert map_rijks_record(_parse_one(_NO_IMAGE_RECORD)) is None


def test_map_rijks_record_restricted_rights_returns_none():
    """Non-public-domain records are filtered."""
    assert map_rijks_record(_parse_one(_RESTRICTED_RECORD)) is None


def test_map_rijks_record_anonymous_graceful_nulls():
    """A sparse record (no creator, no description, no provenance) ingests with nulls."""
    out = map_rijks_record(_parse_one(_ANONYMOUS_RECORD))

    assert out is not None
    assert out["id"] == "rijks:200107774"
    assert out["title"] == "The Tree of Jesse"

    # No creators → null artist / null artist_bio.
    assert out["artist"] is None
    assert out["artist_bio"] is None

    # Date parsing handles "c. 1500".
    assert out["date"] == "c. 1500"
    assert out["date_begin"] == 1500
    assert out["date_end"] == 1500

    # Medium still resolves single concept.
    assert out["medium"] == "oil paint (paint)"

    # Tags include resolved type.
    assert "painting" in out["tags"]

    # source_url synthesised from museum_id even without provenance.
    assert out["source_url"] == "https://www.rijksmuseum.nl/en/collection/SK-A-3901"

    # No provenance → null credit_line. No extent → null dimensions.
    assert out["credit_line"] is None
    assert out["dimensions"] is None

    # raw_metadata still emits the safety blob (empty description fields are None).
    assert out["raw_metadata"]["rijks"]["description_en"] is None
    assert out["raw_metadata"]["rijks"]["description_nl"] is None


def test_map_rijks_record_id_is_source_prefixed():
    """AGENTS.md hard rule: cross-source ids must be source-prefixed."""
    out = map_rijks_record(_parse_one(_NIGHT_WATCH_RECORD))
    assert out["id"].startswith("rijks:")
    assert ":" in out["id"]


def test_map_rijks_record_tags_are_deduped():
    """If the same label appears as both type and subject, it should appear once."""
    # Synthesise a record where dc:type and dc:subject both resolve to "painting".
    custom = _NIGHT_WATCH_RECORD.replace(
        '<dc:subject rdf:resource="https://id.rijksmuseum.nl/221306"/>',
        '<dc:subject rdf:resource="https://id.rijksmuseum.nl/2208"/>',  # same as dc:type
    )
    out = map_rijks_record(_parse_one(custom))
    assert out["tags"].count("painting") == 1


# ----------------------------------------- resume-skip-existing for Rijks
#
# Same pattern as test_aic_db: monkeypatch the OAI page fetcher + image
# download so the test stays in-process.

class _FakeRijksEmbedder:
    def __init__(self):
        self.calls: list[bytes] = []

    def embed_bytes(self, img_bytes: bytes):
        import numpy as np
        self.calls.append(img_bytes)
        return np.ones(768, dtype="float32")


def test_resume_skip_existing_skips_known_rijks_ids(monkeypatch):
    """Records whose source_id is in the skip set never reach embed/image download."""
    import asyncio
    from ml.ingest import rijks_db

    # Three records in a single OAI page; we'll skip the first two.
    night_watch = _NIGHT_WATCH_RECORD  # source_id = "200107928"
    anonymous = _ANONYMOUS_RECORD       # source_id = "200107774"

    # Build a synthetic third record by relabeling Night Watch's numeric id.
    third = _NIGHT_WATCH_RECORD.replace("200107928", "999999999").replace(
        "SK-C-5", "SK-C-99",
    )
    xml_bytes = _envelope(night_watch + anonymous + third)

    async def fake_list_records_page(client, base_url, *, set_spec,
                                     resumption_token, stats):
        return xml_bytes

    image_calls: list[str] = []

    async def fake_download_image(client, url):
        image_calls.append(url)
        return b"\x00"

    async def fake_load_existing(pool, source):
        assert source == rijks_db.RIJKS_SOURCE_SLUG
        return {"200107928", "200107774"}  # skip first two, keep third

    monkeypatch.setattr(rijks_db, "_list_records_page", fake_list_records_page)
    monkeypatch.setattr(rijks_db, "_download_image", fake_download_image)
    monkeypatch.setattr(rijks_db, "_load_existing_source_ids", fake_load_existing)

    embedder = _FakeRijksEmbedder()

    stats = asyncio.run(rijks_db.ingest_rijks_to_db(
        pool=object(),  # type: ignore[arg-type]
        limit=0,
        set_specs=("test-set",),
        request_delay=0.0,
        embedder=embedder,
        dry_run=True,
        resume_skip_existing=True,
    ))

    assert stats.skipped_existing == 2
    assert len(embedder.calls) == 1  # only the third record
    assert len(image_calls) == 1     # only the third record's image fetched
    assert stats.as_dict()["skipped_existing"] == 2


def test_resume_skip_existing_off_by_default_for_rijks(monkeypatch):
    """Flag defaults False — every record embeds, loader is never invoked."""
    import asyncio
    from ml.ingest import rijks_db

    xml_bytes = _envelope(_NIGHT_WATCH_RECORD)

    async def fake_list_records_page(client, base_url, *, set_spec,
                                     resumption_token, stats):
        return xml_bytes

    async def fake_download_image(client, url):
        return b"\x00"

    def boom_loader(*a, **kw):
        raise AssertionError("_load_existing_source_ids called with flag off")

    monkeypatch.setattr(rijks_db, "_list_records_page", fake_list_records_page)
    monkeypatch.setattr(rijks_db, "_download_image", fake_download_image)
    monkeypatch.setattr(rijks_db, "_load_existing_source_ids", boom_loader)

    embedder = _FakeRijksEmbedder()

    stats = asyncio.run(rijks_db.ingest_rijks_to_db(
        pool=None,
        limit=0,
        set_specs=("test-set",),
        request_delay=0.0,
        embedder=embedder,
        dry_run=True,
        # resume_skip_existing omitted → defaults to False
    ))

    assert stats.skipped_existing == 0
    assert len(embedder.calls) == 1
