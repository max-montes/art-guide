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

## Cross-Agent Note — 2026-05-17 (backend-engineer — Bicep regression + fix)

**INCIDENT:** ml-retrieval-engineer's Bicep deploy (D-029 ingest job add) caused API revision --0000004 to revert to `mcr.microsoft.com/azuredocs/containerapps-helloworld:latest` with port 80 ingress. Root causes:

1. `parameters.prod.json` had `containerPort: 80` (hello-world default, never updated to 8000).
2. `main.bicep` had no `apiImage` parameter — image was hardcoded to the literal string (which was v1 in HEAD but may have been hello-world in the deployed version).
3. The registry binding (`registries: identity: system`) was wiped by the Bicep redeploy.

**Fix:** backend-engineer restored prod in ~10 min via `az containerapp registry set` + `az containerapp update --image v1`. Then added `apiImage`, `apiCpu`, `apiMemory` params to Bicep (D-038). Fixed `parameters.prod.json` containerPort to 8000. Added `what-if` guard in `deploy.sh` that hard-fails if hello-world would be deployed. **Future infra deploys are now protected.**

**v2 image shipped:** Wave 2 museum-plaque prompt (D-036) is now live as `art-guide-api:v2`, revision --0000006.

**For future infra work:** Always run `az deployment group what-if` before `az deployment group create`. If you see the API container changing in what-if, check `parameters.prod.json` first.

## Cross-Agent Coordination Note — 2026-05-17 (ios-engineer-3 completed)

iOS deployment target **raised to 17.0** (required for SwiftData local history feature, D-034). Affects any future iOS coordination work. No impact on dataset ingest or embedding pipeline; iOS handles persistence locally.

---

See `history-archive.md` for earlier learning (Phase 0 foundation, embedding selection, 5x transformers bug, test discipline fixes, eval bootstrap, iOS Codable shape mismatch, Met enrichment tier (a) implementation).

## Session 2026-05-17 — Full Catalog Ingest Job + Eval Baseline

**Tasks:** A.1 (Container Apps Job for full Met ingest) + A.2 (eval baseline against 100-record prod catalog)

### Met corpus count
Verified 2026-05-16: **501,696 public-domain objects** (up from 492K estimate — normal API drift).

### A.1 — Container Apps Job (D-046)
Added `Microsoft.App/jobs@2023-05-01` to `infra/azure/main.bicep`:
- `art-guide-prod-ingest`, `triggerType: Manual`, `replicaTimeout: 7200`
- Command: `art-guide-ml ingest met --limit 0 --batch-commit-size 64`
- `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` — required for baked-in SigLIP weights (D-028)
- ACR pull via system-assigned MI + RBAC `AcrPull` role assignment
- Bicep also corrected: containerApp image → `art-guide-api:v1`, `containerPort` → 8000

**Deployment:** `ingest-job-202605161727` (incremental). Bicep validation passed. ARM returned `provisioningState=Failed` for the job resource (transient "Operation expired" — known Azure CA Jobs behavior). Job is functional.

**Post-deploy incident:** Bicep incremental deploy reset `api-bearer-token` secret to PLACEHOLDER. Restored from KV + restarted revision. API confirmed healthy: `/healthz` → `{"status":"ok"}`.

**Execution:** `art-guide-prod-ingest-brsioxp` started 2026-05-17T00:51:42Z. Expected ~23 hr for 500K records at 6 req/s.

### A.2 — Eval baseline (D-047)
**Run:** `baseline-2026-05-17T00-54-11Z.json`, 38 evaluated / 7 skipped / 45 total.

| Metric | Value | Threshold | Pass? |
|--------|-------|-----------|-------|
| recall@1 | 0.000 | 0.80 | ❌ expected |
| recall@3 | 0.000 | 0.90 | ❌ expected |
| status_accuracy | 0.079 | 0.65 | ❌ expected |
| latency p50 | 1929 ms | — | healthy |
| latency p99 | 3130 ms | 5000 ms | ✅ |

**Root cause of all failures:** Catalog–dataset mismatch. Prod catalog = `met:436xxx` (100 Van Gogh–era EP). Eval dataset = `met:435xxx` (Bruegel, Cézanne, Caravaggio…). Zero overlap. Every recall miss is a coverage miss.

**Key signals:**
- 3 false-exact cases (returned `status=exact`, wrong ID, conf 0.851–0.926) — small/homogeneous catalog amplifies near-duplicate embeddings
- 3/3 out_of_catalog evaluated returned `likely` (expected `style_only`) — all-Van-Gogh catalog has no contrast for out-of-catalog artworks
- 7 OOC cases skipped — Wikimedia 400/404 errors (thumbnail size policy change); dataset URLs need update
- 3 cases: 401 auth errors during eval start (bearer token being restored)

**Next step:** Re-run after full 500K catalog loads. Gate: recall@1 ≥ 0.80.

### New skills written
- `.squad/skills/museum-ingest-loop/SKILL.md` — added "Container Apps Job Pattern" section
- `.squad/skills/eval-baseline-protocol/SKILL.md` — created; covers timestamped baseline protocol, metric classes, calibration danger zones
