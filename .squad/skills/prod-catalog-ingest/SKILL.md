# Skill: prod-catalog-ingest

**Confidence:** High (observed once, well-understood pattern)  
**Owner:** ml-retrieval-engineer  
**Last used:** 2026-05-16 (Met 100-record prod seed)

## Problem

You have a working local ingest CLI and a remote/prod Postgres. You need to run the ingest against prod without changing any code or hardcoding secrets.

## Pattern

The `art-guide-ml ingest met` CLI already supports both a `--database-url` flag and `$DATABASE_URL` env var fallback (in that priority order). Use the **flag** approach — it avoids leaking the DSN into the shell environment and is explicit in logs (scrubbed).

```bash
# 1. Fetch DSN from Key Vault (never hardcode)
PROD_DSN=$(az keyvault secret show \
  --vault-name art-guide-prod-kv \
  --name database-url \
  --query value -o tsv)

# 2. Dry-run first to confirm connectivity + filter counts
art-guide-ml ingest met \
  --limit 10 \
  --dry-run \
  --department-ids 11 \
  --database-url "$PROD_DSN"

# 3. Real run
art-guide-ml ingest met \
  --limit 100 \
  --department-ids 11 \
  --database-url "$PROD_DSN"
```

## Checklist before running against prod

- [ ] Brady's IP is in the Azure Postgres firewall allowlist (Azure Portal → Flexible Server → Networking)
- [ ] `database-url` secret in Key Vault includes `sslmode=require` (Azure Flexible Server requires TLS)
- [ ] Migrations `0001_init.sql` + `0002_met_enrichment.sql` are applied (use `sql-migration-runner` skill)
- [ ] pgvector extension is installed in the target database
- [ ] `--department-ids 11` scopes to European Paintings (avoids scanning all 480K+ Met objects)

## Verification after ingest

```bash
# All three checks should pass
psql "$PROD_DSN" <<'SQL'
SELECT COUNT(*) AS total_rows FROM artworks;                               -- expect N
SELECT COUNT(*) AS with_bio FROM artworks WHERE artist_bio IS NOT NULL;    -- expect close to N for Met
SELECT vector_dims(embedding) AS dims FROM artworks LIMIT 1;               -- expect 768
\d artworks  -- confirm hnsw index present
SQL
```

## Latency expectations (2026-05-16 baseline)

| Scenario | End-to-end `/v1/identify` |
|---|---|
| Container already warm | ~2,900 ms (retrieval=2016 ms, llm=82 ms) |
| Scale-to-zero cold start | 10–30 s (model load, D-028 operational note) |

The warm retrieval_ms is dominated by SigLIP preprocessing, not HNSW search. Expect it to stay roughly constant as catalog grows (HNSW is sub-linear).

## Gotchas

- **Do not use `$DATABASE_URL` env var if you also have local Docker Compose running** — the wrong DSN will be used silently. Prefer explicit `--database-url` flag.
- **Met image CDN requires browser-like headers** (already baked into `met_db.py::_IMAGE_CDN_HEADERS`). Do not bypass this.
- **6 MB original Met images slow smoke tests** — use a real iOS-resized upload (~200–400 KB) for latency benchmarks.
- **`skipped_image=1` is normal** — some Met `primaryImage` URLs are genuinely broken at source. Not a pipeline bug.
- **All 7 enrichment columns are populated in a single ingest pass** when running the current `met_db.py`. No separate `backfill met` step is needed for fresh installs.

## Related skills

- `sql-migration-runner` — apply migrations before first ingest
- `museum-ingest-loop` — the broader pattern for adding new source adapters (Rijks, Harvard, etc.)
- `eval-harness-for-grounded-retrieval` — run recall eval after ingesting to verify retrieval quality
