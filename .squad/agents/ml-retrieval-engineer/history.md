# ML/Retrieval Engineer History (Condensed — 2026-05-16)

> **Full Phase 0–early Phase 1 work archived to `history-archive.md`. Current file: latest completed work + active next steps.**

## Phase 1 End-to-End Status — 2026-05-16

✅ **PROD CATALOG SEEDED & LIVE**

- **100 Met European Paintings** ingested into prod Postgres (Azure Flexible Server)
- **All 7 Wave 1 enrichment columns** populated (artist_bio 100/100)
- **End-to-end `/v1/identify` verified:** Van Gogh Sunflowers → exact match, score=1.0
- **Warm latency:** ~2.9s (retrieval 2.0s + LLM explanation 0.08s)
- **HNSW index healthy:** m=16, ef_construction=64, vector_cosine_ops
- **Embedding model:** SigLIP-base-patch16-224 (768-dim, L2-normalized, D-015)

### Key Decisions Closed

- **D-015:** SigLIP-base-patch16-224 confirmed working end-to-end in prod
- **D-016:** `--database-url` flag reused (DSN from Key Vault secret `database-url`)
- **D-027:** Wave 1 enrichment (7 columns) complete — backfill pattern proven
- **D-029:** Prod seeding decision — 100 records as Phase 1 baseline before scaling

### What's Next

1. **Full Met catalog (~492K records)** — scale beyond 100 baseline
2. **Real eval set bootstrap** — against live prod DB (currently placeholder in D-025)
3. **Phase 4 expansion** — Rijksmuseum, Harvard, Smithsonian, AIC, Cleveland (reuse `museum-ingest-loop` skill)

### Working Rules (Locked)

- `docs/data-model.md` — NormalizedArtwork shape + confidence model (exact | likely | style_only | no_match)
- `docs/image-pipeline.md` — Single `prepare_for_embedding` function (no pipeline drift)
- **Hard Rule #3:** No raw images stored (anywhere)
- **Hard Rule #1:** LLM explains only retrieved fields (no world knowledge injection)
- Don't fine-tune in v1 (D-013)

## Latest Learning — 2026-05-16: Prod Ingest

**Key Takeaway:** `--database-url` flag (Option B, D-016) eliminates env var gymnastics. DSN from Key Vault secret flows cleanly; no shell pollution.

**Cold vs. Warm Latency:**
- Warm (observed): ~2,900 ms (image xfer + retrieval JIT)
- Cold (expected): 10–30 s (model load from scale-to-zero)
- **Bottleneck:** retrieval_ms dominates warm (2,016 ms = image preprocessing on SigLIP)
- Expectation: 200–400 KB iOS uploads will improve transfer overhead vs. 6 MB test image

**Enrichment backfill validated:** All 7 Wave 1 columns populated on first ingest pass (no re-embed step needed). Dynasty 0% populated for European Paintings (expected — only future Asian/Egyptian records will have it).

**No prod-specific surprises:**
- Azure Postgres Flexible Server `sslmode=require` already baked into Key Vault secret
- `asyncpg` pool connects cleanly; no firewall issues
- All health endpoints 200 OK

## Cross-Agent Status

- **backend-engineer:** Prod image deployed (D-028); all endpoints healthy; cold-start gotcha D-028 logged
- **ios-engineer:** Prod now live. Test against real catalog. Sunflowers = known-good smoke test.
  - **Update (2026-05-16 23:35):** iOS app build is now green (13 tests passing, zero warnings). Brady fixed xcodegen regen + async lock issues and is testing on simulator against live prod. Watch for any retrieval surface issues he surfaces (e.g., confidence thresholds, ambiguous matches, score distribution).

---

See `history-archive.md` for earlier learning (Phase 0 foundation, embedding selection, 5x transformers bug, test discipline fixes, eval bootstrap, iOS Codable shape mismatch, Met enrichment tier (a) implementation).
