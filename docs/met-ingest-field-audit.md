# Met Open Access API — Field Audit & Ingest Enrichment Proposal

**Author:** ml-retrieval-engineer  
**Date:** 2026-05-10  
**Status:** Implemented — D-024 tier (a) shipped 2026-05-10  
**Anchor record:** `met:436105` — Jacques-Louis David, *The Death of Socrates*, 1787

---

## 1. Full Field Inventory

> Every field returned by `GET /collection/v1/objects/{objectID}`. Verified against 9 diverse records: 436105 (David), 435905 (Lorrain), 436535 (Van Gogh, Wheat Field), 436528 (Van Gogh, Irises), 436944 (Manet), 39799 (Hokusai), 544946 (Egyptian relief), 207778 (Bovy medal), and 436121 (Degas).

| Field | Type | Example (436105) | Present in anchor | Reliability across 9 records | Current status |
|---|---|---|---|---|---|
| `objectID` | integer | `436105` | ✅ | **Always** | Already ingested (→ `source_id`) |
| `isPublicDomain` | boolean | `true` | ✅ | **Always** | Already ingested (→ filter + `is_public_domain`) |
| `primaryImage` | string (URL) | `"https://images.metmuseum.org/…"` | ✅ | When image exists | Already ingested (→ `image_url`) |
| `primaryImageSmall` | string (URL) | `"https://images.metmuseum.org/…/web-large/…"` | ✅ | When image exists | In `raw_metadata` (thumbnail) |
| `title` | string | `"The Death of Socrates"` | ✅ | **Almost always** | Already ingested |
| `artistDisplayName` | string | `"Jacques Louis David"` | ✅ | When artist identified | Already ingested (→ `artist`) |
| `objectDate` | string | `"1787"` | ✅ | **Almost always** | Already ingested (→ `date`) |
| `medium` | string | `"Oil on canvas"` | ✅ | **Almost always** | Already ingested |
| `culture` | string | `""` (empty) | ❌ | Sparse — populated for Asian/ancient; empty for most European paintings | Already ingested (null when empty) |
| `period` | string | `""` (empty) | ❌ | Sparse — populated for Asian/ancient (e.g., "Edo period (1615–1868)") | Already ingested (null when empty) |
| `museum` | — | — | — | — | Already ingested (hardcoded constant) |
| `objectURL` | string | `"https://www.metmuseum.org/art/collection/search/436105"` | ✅ | **Always** | Already ingested (→ `source_url`) |
| `tags[].term` | string[] | `["Socrates","Men","Lyres"]` | ✅ (3 items) | Well populated for paintings/sculpture; empty for minor objects | Already ingested (merged into `tags` array) |
| `department` | string | `"European Paintings"` | ✅ | **Almost always** | Already ingested (merged into `tags`) |
| `classification` | string | `"Paintings"` | ✅ | Often (blank for some ancient objects) | Already ingested (merged into `tags`) |
| `objectName` | string | `"Painting"` | ✅ | **Almost always** | Already ingested (merged into `tags`) |
| `repository` | string | `"Metropolitan Museum of Art, New York, NY"` | ✅ | **Always** | Skip — redundant with `museum` constant |
| `metadataDate` | string (ISO) | `"2026-02-28T04:58:01.027Z"` | ✅ | **Always** | Skip — internal timestamp |
| **`artistDisplayBio`** | string | `"French, Paris 1748–1825 Brussels"` | ✅ | **When artist identified** (~80% of paintings/sculptures) | **⬛ Dropped — high-signal candidate** |
| **`artistNationality`** | string | `"French"` | ✅ | When artist identified | Subset of `artistDisplayBio`; skip separately |
| **`creditLine`** | string | `"Catharine Lorillard Wolfe Collection, Wolfe Fund, 1931"` | ✅ | **Almost always** | **⬛ Dropped — high-signal candidate** |
| **`dimensions`** | string | `"51 x 77 1/4 in. (129.5 x 196.2 cm)"` | ✅ | **Almost always** | **⬛ Dropped — high-signal candidate** |
| **`dynasty`** | string | `""` (empty for David; `"Dynasty 18"` for Egyptian) | ❌ for this work | Sparse — essential for Egyptian/ancient Asian | **⬛ Dropped — high-signal for ancient art** |
| **`objectWikidata_URL`** | string | `"https://www.wikidata.org/wiki/Q1752990"` | ✅ | Fairly well populated for notable works (~60-70%) | **⬛ Dropped — retrieval anchor for tier (b)** |
| **`objectBeginDate`** | integer | `1787` | ✅ | **Almost always** | **⬛ Dropped — filtering/retrieval use** |
| **`objectEndDate`** | integer | `1787` | ✅ | **Almost always** | **⬛ Dropped — filtering/retrieval use** |
| **`accessionYear`** | string | `"1931"` | ✅ | **Almost always** | **⬛ Dropped — low storytelling value** |
| **`isHighlight`** | boolean | `true` | ✅ | Sparse — highlights only (~top 3K works) | Skip for schema; useful as tag/filter |
| `additionalImages` | string[] | 3 alternate view URLs | Sparse | Sparse (0–4 items) | Skip — CDN URLs, not storytelling value |
| `constituents` | array[object] | `[{id, role, name, ULAN_URL, Wikidata_URL, gender}]` | ✅ | When artist identified | Partially covered; `artistWikidata_URL` is the most useful sub-field |
| **`artistWikidata_URL`** | string | `"https://www.wikidata.org/wiki/Q83155"` | ✅ | When artist identified | **⬛ Dropped — tier (b) enrichment anchor** |
| `artistULAN_URL` | string | `"http://vocab.getty.edu/page/ulan/500115221"` | ✅ | When artist identified | Skip — Getty internal, not user-facing |
| `artistAlphaSort` | string | `"David, Jacques Louis"` | ✅ | When artist identified | Skip — sort key only |
| `artistBeginDate` | string | `"1748"` | ✅ | When artist identified | Subset of `artistDisplayBio`; skip separately |
| `artistEndDate` | string | `"1825"` | ✅ | When artist identified | Subset of `artistDisplayBio`; skip separately |
| `artistGender` | string | `""` | ❌ | **Rarely populated** (~5%) | Skip — low signal, potential sensitivity |
| `artistRole` | string | `"Artist"` | ✅ | When artist identified | Skip — nearly always "Artist" |
| `artistPrefix` / `artistSuffix` | string | `""` | ❌ | Very rare | Skip |
| `measurements` | array[object] | `[{elementName, elementMeasurements: {Height, Width}}]` | ✅ | Often | Skip — machine-readable form of `dimensions` |
| `accessionNumber` | string | `"31.45"` | ✅ | Almost always | Skip — internal museum number |
| `reign` | string | `""` | ❌ | **Very rare** | Skip |
| `portfolio` | string | `""` | ❌ | Very rare | Skip |
| `geographyType` | string | `""` | ❌ | Sparse — partial phrase ("Probably originally from") | Skip — unreliable as standalone text |
| `city` | string | `""` | ❌ | Sparse | Skip for now |
| `country` | string | `""` | ❌ | Sparse | Skip for now |
| `region` | string | `""` | ❌ | Sparse | Skip for now |
| `subregion` | string | `""` | ❌ for this work; `"Amarna (Akhetaten)"` for Egyptian | Sparse | Skip for now |
| `locale` / `locus` / `excavation` / `river` | string | all `""` | ❌ | **Almost never** | Skip |
| `state` / `county` | string | all `""` | ❌ | **Almost never** | Skip |
| `rightsAndReproduction` | string | `""` | ❌ | Almost never set (cleared for PD works) | Skip |
| `linkResource` | string | `""` | ❌ | Very rare | Skip |
| `GalleryNumber` | string | `"634"` | ✅ | When on display (~50%) | Skip — useful for navigation, not storytelling |
| `isTimelineWork` | boolean | `true` | Sparse | Sparse — notable works only | Skip |
| `tags[].AAT_URL` / `tags[].Wikidata_URL` | string | `"https://www.wikidata.org/wiki/Q913"` | ✅ | When tags present | In `raw_metadata`; Wikidata URLs are tier (b) anchor |

### ⚠️ Critical negative finding

**The Met API has no free-text description field.** There is no `objectDescription`, `description`, `label`, `curatorText`, or any field containing interpretive prose. The API returns structured metadata only. The `tags[].term` values ("Socrates", "Men", "Lyres") are iconographic subject headings — they name what is depicted, not what it means.

---

## 2. Recommended Schema Additions

### 2a. High signal — for storytelling (grounded fields)

Four new columns are proposed for `NormalizedArtwork` and the `artworks` table:

| Proposed field | Source Met field | DB column type | Nullable | Population rate | Why it matters |
|---|---|---|---|---|---|
| `artist_bio` | `artistDisplayBio` | `text` | yes | ~80% of paintings/sculptures | "French, Paris 1748–1825 Brussels" — gives LLM artist's nationality, birthplace, and dates to weave into narration |
| `credit_line` | `creditLine` | `text` | yes | ~95% of all records | "Catharine Lorillard Wolfe Collection, Wolfe Fund, 1931" — provenance and acquisition; signature museum-guide detail |
| `dimensions` | `dimensions` | `text` | yes | ~90% of all records | "51 x 77 1/4 in. (129.5 x 196.2 cm)" — scale is meaningful for a museum visitor standing in front of the painting |
| `dynasty` | `dynasty` | `text` | yes | Sparse (~15%), but essential when present | "Dynasty 18" — for Egyptian/ancient Asian art, dynasty replaces `period` as the primary chronological anchor |

All four are `null`-by-default (Met data is messy; most fields are empty for many records). No NOT NULL constraint is appropriate.

These four would be added to `grounded_fields` allow-list in `docs/api.md` and in the LLM prompt builder.

### 2b. Retrieval / filtering only (not grounded)

| Proposed field | Source Met field | DB column type | Nullable | Purpose |
|---|---|---|---|---|
| `object_wikidata_url` | `objectWikidata_URL` | `text` | yes | Tier (b) enrichment anchor — link to Wikidata/Wikipedia; **not** exposed to LLM as grounded text |
| `date_begin` | `objectBeginDate` | `integer` | yes | Date-range filtering queries ("find paintings from 1780–1800"); not user-facing |
| `date_end` | `objectEndDate` | `integer` | yes | Same |

`object_wikidata_url` and `date_begin`/`date_end` are **not** added to `grounded_fields`. They are retrieval infrastructure.

### 2c. Migration sketch

A new `0002_enrich_artworks.sql` would `ALTER TABLE artworks ADD COLUMN` for each of the seven fields listed above. All nullable text or integer columns — no index needed for the text fields at this scale; a btree on `(date_begin, date_end)` is worth adding for range queries. No `embedding` column changes, no HNSW rebuild.

The `NormalizedArtwork` Pydantic model gains seven new optional fields with `= None` defaults. `model_config = ConfigDict(extra="forbid")` means they must be listed explicitly — no implicit passthrough.

The `_INSERT_SQL` in `met_db.py` grows by seven `$N` bindings. The `map_met_record` function gains seven new extraction lines. Existing records can be backfilled via a standard `ingest met --limit 100` re-run (idempotent upsert handles it).

---

## 3. Before / After: *The Death of Socrates*

### Raw Met response (relevant fields)

```
title:           "The Death of Socrates"
artistDisplayName: "Jacques Louis David"
artistDisplayBio:  "French, Paris 1748–1825 Brussels"
objectDate:        "1787"
medium:            "Oil on canvas"
dimensions:        "51 x 77 1/4 in. (129.5 x 196.2 cm)"
creditLine:        "Catharine Lorillard Wolfe Collection, Wolfe Fund, 1931"
culture:           ""  (empty — typical for European paintings)
period:            ""  (empty — typical for European paintings)
department:        "European Paintings"
classification:    "Paintings"
tags:              ["Socrates", "Men", "Lyres"]
objectWikidata_URL: "https://www.wikidata.org/wiki/Q1752990"
```

**There is no description, label, or interpretive text anywhere in this response.**

---

### BEFORE (current schema — 5 grounded fields)

Fields available to LLM: `title`, `artist`, `date`, `medium`, `museum`  
Tags available (in `tags` array, not formally grounded): "European Paintings", "Paintings", "Painting", "Socrates", "Men", "Lyres"

> *Example LLM output:*  
> "The Death of Socrates is an oil-on-canvas painting by Jacques-Louis David, completed in 1787, and held in the collection of The Metropolitan Museum of Art."

Short. Accurate. Zero context beyond the placard.

---

### AFTER (proposed enriched schema — 9 grounded fields)

Fields available to LLM: `title`, `artist`, `artist_bio`, `date`, `medium`, `dimensions`, `credit_line`, `museum`, plus subject tags `["Socrates","Men","Lyres"]`

> *Example LLM output:*  
> "The Death of Socrates is a monumental oil on canvas—measuring 51 by 77 inches—painted in 1787 by Jacques-Louis David, a French painter born in Paris in 1748 who later settled in Brussels. It entered the Metropolitan Museum of Art in 1931 through the Catharine Lorillard Wolfe Collection. The canvas depicts Socrates in his final moments, with figures of men and a lyre visible in the composition."

Better. The artist's biography grounds the Neoclassical attribution implicitly (a French painter working in 1787). The scale communicates that this is not a cabinet picture. The acquisition note gives museum-guide texture.

---

### Honest ceiling assessment: will the LLM answer "what does this symbolize?"

**No — not under tier (a).** The enriched schema produces a richer museum placard, but still cannot truthfully answer an iconographic question.

What the LLM **can** truthfully say under tier (a):
- Who painted it and when (David, 1787)
- The medium and scale (oil on canvas, large-format)
- Who is depicted (Socrates, men, a lyre — from subject tags)
- Where it is and how it was acquired (Met, Wolfe Fund 1931)

What the LLM **cannot** truthfully say under tier (a) — because no Met field contains it:
- That Socrates is reaching for the hemlock cup
- That the lyre in the foreground alludes to Plato, who was too overcome with grief to play
- That David painted this on the eve of the French Revolution as an allegory of civic virtue and sacrifice
- That the stoic restraint of Socrates contrasts with the emotional collapse of his disciples
- That the painting's composition owes a debt to Poussin and was received as a proto-Revolutionary manifesto

All of that is interpretive art history, and none of it lives in the Met catalog record. For the painting `met:436105`, the `objectWikidata_URL` is `Q1752990`, which bridges to a Wikipedia article with rich prose covering exactly this interpretive content.

---

## 4. Symbolism Tier Decision

Three options for max-montes to choose:

### Tier (a) — Met-only enrichment

**What:** Add `artist_bio`, `credit_line`, `dimensions`, `dynasty` as schema columns; add `object_wikidata_url`, `date_begin`, `date_end` as retrieval infrastructure.  
**Work:** One migration, ~40 lines of ingest code change, re-ingest 100 records (idempotent), update LLM prompt builder to include new fields, update `docs/api.md` `grounded_fields` allow-list, update ingest tests.  
**Ceiling:** Museum-placard quality. Richer than today. Still no symbolism.  
**Good for:** Shipping quickly; demonstrably better explanations for scale, provenance, artist biography.

### Tier (b) — Wikipedia / Wikidata adapter

**What:** After retrieval, look up `object_wikidata_url` against the Wikidata API and/or Wikipedia summary API. Extract the English Wikipedia introductory paragraphs or Wikidata statements for the specific painting. Feed as a `description` field into the LLM prompt.  
**Work:** New ingest adapter or on-the-fly enrichment at query time. The `objectWikidata_URL` is already returned by the Met for ~60-70% of notable works. Wikipedia API is free and has no authentication. Wikidata SPARQL is free.  
**Ceiling:** Genuine iconographic content. For *Death of Socrates*, Wikipedia's article covers the composition, Neoclassical program, political context, and symbolic reading in detail.  
**Cost in complexity:** ~2–3 days for a working prototype; adds a network dependency at query time (or additional ingest column `description text` for offline storage). Licensing: Wikipedia text is CC BY-SA 4.0, compatible with the project's public-domain posture. Wikidata is CC0.  
**Risk:** Not all works have Wikipedia articles. Quality varies. LLM must be instructed not to extend beyond the retrieved Wikipedia text (Hard Rule #1 still applies).

### Tier (c) — Defer to Phase 2

**What:** Keep the current minimal schema, revisit after eval set is in place and user feedback shows whether "tell me about the symbolism" is actually a frequent high-value query.  
**Work:** None.  
**Good for:** Avoiding over-building before knowing what users actually ask.

---

## 5. Cost / Risk Pass

| Area | Tier (a) impact | Tier (b) additional impact |
|---|---|---|
| **DB migration** | One `0002_enrich_artworks.sql`; 7 nullable columns; no HNSW rebuild | One more `description text` column if storing Wikipedia text offline |
| **Re-ingest** | Yes — 100 existing records need backfill; idempotent upsert handles it; one CLI re-run | Same + one Wikidata/Wikipedia fetch per record |
| **Ingest tests** | `test_map_met_record` must assert all 7 new field extractions; add null-case tests for empty strings | Additional tests for Wikipedia fetcher |
| **LLM prompt** | `build_prompt()` grows by 4 new grounded fields; more raw text to weave; low hallucination risk (all factual) | Adds up to ~500 tokens of Wikipedia prose; LLM has more to work with but must stay grounded; test with `field_citation` evaluator |
| **API contract** | `grounded_fields` allow-list grows by 4 values; additive, non-breaking per D-007 versioning policy | Same |
| **Privacy** | `creditLine` sometimes names individual donors ("Gift of Mr. and Mrs. Jonathan P. Rosen, 1991") — these are public Met catalog records, no PII concern | Wikipedia prose is public; no PII |
| **Phase 4 adapters** | Rijks/Harvard/AIC etc. gain the same new columns at their adapter layer; the `museum-ingest-loop` skill just maps nullable fields | Each Phase 4 adapter needs its own Wikipedia linkage (or leaves `description` null) |

---

## 6. Open Question for max-montes

**Pick the symbolism scope:**

- **(a)** Enrich with Met-only fields and accept the "descriptive but not interpretive" ceiling. Ships fast. The LLM gives a richer museum placard — scale, artist biography, provenance. Will not answer "what does the lyre symbolize."
- **(b)** Add a Wikipedia/Wikidata enrichment adapter using the `objectWikidata_URL` bridge already returned by the Met. Ships real iconographic content for ~60–70% of notable works. Adds ~2–3 days of work and a new ingest layer.
- **(c)** Defer entirely and revisit in Phase 2 after you have an eval set showing what users actually ask.

**Recommendation:** Implement tier (a) now (it's a pure win with low cost and no correctness risk), and time-box tier (b) as a Phase 2 spike once the eval set is bootstrapped. The `object_wikidata_url` column proposed in tier (a) is the forward-compatible anchor for tier (b) — it costs nothing extra and keeps the path open.
