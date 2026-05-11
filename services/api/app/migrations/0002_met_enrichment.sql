-- 0002_met_enrichment.sql
--
-- Adds seven enrichment columns sourced from the Met Open Access API.
-- Approved under D-024 tier (a): museum-placard quality fields.
--
-- All columns are NULL by default — Met data is sparse and many fields
-- are empty strings that the ingest layer normalizes to NULL.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS is safe on repeated apply.
-- No HNSW rebuild: vector(768) column is unchanged.
--
-- Grounded fields (fed to LLM via grounded_fields allow-list):
--   artist_bio      "French, Paris 1748–1825 Brussels"  ~80 % of paintings
--   credit_line     "Catharine Lorillard Wolfe Collection, Wolfe Fund, 1931"
--   dimensions      "51 x 77 1/4 in. (129.5 x 196.2 cm)"
--   dynasty         "Dynasty 18"  — sparse, essential for Egyptian/ancient
--
-- Retrieval / filtering only (not grounded):
--   object_wikidata_url  bridge for tier (b) Wikipedia enrichment
--   date_begin / date_end  integer year range for temporal filtering

ALTER TABLE artworks ADD COLUMN IF NOT EXISTS artist_bio           TEXT NULL;
ALTER TABLE artworks ADD COLUMN IF NOT EXISTS credit_line          TEXT NULL;
ALTER TABLE artworks ADD COLUMN IF NOT EXISTS dimensions           TEXT NULL;
ALTER TABLE artworks ADD COLUMN IF NOT EXISTS dynasty              TEXT NULL;
ALTER TABLE artworks ADD COLUMN IF NOT EXISTS object_wikidata_url  TEXT NULL;
ALTER TABLE artworks ADD COLUMN IF NOT EXISTS date_begin           INTEGER NULL;
ALTER TABLE artworks ADD COLUMN IF NOT EXISTS date_end             INTEGER NULL;

-- Optional: btree index on the integer date range for temporal queries.
-- Low overhead at current scale; skip if Postgres objects to duplicate index.
CREATE INDEX IF NOT EXISTS artworks_date_begin_end_idx
    ON artworks (date_begin, date_end)
    WHERE date_begin IS NOT NULL;
