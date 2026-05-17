---
updated_at: 2026-05-17T00:20:00-07:00
focus_area: Full Met catalog ingest running; eval baseline pinned against 100-record prod catalog
active_issues: []
---

# What We're Focused On

## Status

**🟢 Full-catalog ingest running. 🟢 Eval baseline pinned.**

Full Met catalog ingest (`art-guide-prod-ingest-brsioxp`, D-046) started 2026-05-17T00:51:42Z. 501,696 public-domain Met records. Estimated ~23 hr for completion. Monitor via Azure Portal → Container Apps Jobs → `art-guide-prod-ingest`.

Eval baseline pinned (D-047): `baseline-2026-05-17T00-54-11Z.json`. recall@1=0.000, status_acc=0.079, latency p99=3130ms. All failures are catalog-coverage artifacts (eval artworks not in 100-record prod catalog). Baseline is the regression anchor for post-full-ingest eval re-run.

iOS history feature (D-034, D-036, D-037) is complete: SwiftData persistence, Camera|History TabView, 52/52 tests pass. Backend API regression (D-038) fixed; prod stack healthy.

**Phase 1 milestones:**
- API contract v0 ✓
- Data + confidence model ✓
- Image pipeline (single `prepare_for_embedding` function) ✓
- Embedding (SigLIP-base-patch16-224, 768-dim, bundled in image) ✓
- Deployment (Azure prod stack + local Docker Compose) ✓
- Privacy + observability ✓
- Production image built & deployed ✓ (D-028)
- Museum-plaque LLM prompt design locked ✓ (D-026)
- Enrichment tier (a) backfill complete (D-027)
- **Prod catalog seeded (100 Met records, full enrichment) ✓ (D-029)**

**Infrastructure now live:**
- Prod API: `https://art-guide-prod-api.kindglacier-84ffc0b4.westus3.azurecontainerapps.io` (static bearer auth)
- Postgres Flexible Server + pgvector (migrations applied)
- Azure Key Vault (bearer token + future secrets)
- ACR image registry + Container Apps orchestration
- iOS Config.local.xcconfig wired to prod

**Blockers lifted:**
- Backend-engineer ready for iOS integration
- ml-retrieval-engineer can ingest to prod Postgres
- Evaluation bootstrap unblocked (AOAI gpt-5-mini deployed)

## Next concrete work

In priority order:

1. **Monitor full-catalog ingest** (ml-retrieval-engineer): `art-guide-prod-ingest-brsioxp` running. Check Azure Portal or Log Analytics after ~12 hr. Expected completion ~2026-05-18T00:00Z.
2. **Re-run eval post-ingest** (ml-retrieval-engineer): Once 500K records indexed, re-run harness. Gate: recall@1 ≥ 0.80. Also fix 7 Wikimedia dataset URL 400/404 failures (cases 38, 39, 41–44, 45) before re-run.
3. **Monitor cold-start + prod health** (backend-engineer + ios-engineer): Watch p99 latency after larger ANN index; should stay under 5000 ms.

## Out of scope Phase 1

- Wave 2 plaque LLM refinement (unblocked for Phase 2; foundation in place)
- Catalog expansion beyond Met (Phase 4; adapters ready per D-004)
- Fine-tuning, Core ML on-device, multi-region (D-013 defer list)

## Coordinator notes

- Azure prod stack is operator-ready. Team can now work against live infrastructure.
- Cold-start latency is an operational note, not a blocker for v1. Revisit if eval P99 regresses.
- Catalog ingest and iOS testing can happen in parallel.
- Historical archives: updated backend-engineer and ios-engineer `history-archive.md` (files >15KB summarized per scribe workflow).
