---
updated_at: 2026-05-16T22:54:00Z
focus_area: Prod catalog live; iOS integration testing; eval bootstrap
active_issues: []
---

# What We're Focused On

## Status

**🟢 STACK IS FULLY END-TO-END LIVE WITH REAL CATALOG.**

Backend image deployed (D-028) + 100 Met European Paintings seeded in prod Postgres (D-029). End-to-end `/v1/identify` verified: Van Gogh Sunflowers → exact match, score=1.0, latency ~2.9s warm. All Phase 1 infrastructure operational. iOS team can test against live prod with real museum catalog.

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

1. **iOS end-to-end test** (ios-engineer): Create `.xcodeproj`, build + run in Simulator against live prod. Test camera → upload → identify endpoint with real museum catalog. Monitor cold-start latency (D-028: first request blocks ~10–30s from scale-to-zero).
2. **Bootstrap eval** (ml-retrieval-engineer): Load retrieval-v1 dataset, run confidence + grounding evaluators against prod catalog, pin D-025 baseline metrics. Verify recall@1/3, status_accuracy, latency p50/p95/p99.
3. **Scale catalog** (ml-retrieval-engineer): Full Met dataset (~492K) or confirm Phase 1 scope at 100 records. Phase 4 multi-source expansion (Rijksmuseum, etc.) queued for later.
4. **Monitor cold-start** (backend-engineer + ios-engineer): Gather real latency data from iOS tests; decision in Phase 2 on minReplicas=1 vs. background task warm-up (D-028 operational note).

## Out of scope Phase 1

- Wave 2 plaque LLM refinement (unblocked for Phase 2; foundation in place)
- Catalog expansion beyond Met (Phase 4; adapters ready per D-004)
- Fine-tuning, Core ML on-device, multi-region (D-013 defer list)

## Coordinator notes

- Azure prod stack is operator-ready. Team can now work against live infrastructure.
- Cold-start latency is an operational note, not a blocker for v1. Revisit if eval P99 regresses.
- Catalog ingest and iOS testing can happen in parallel.
- Historical archives: updated backend-engineer and ios-engineer `history-archive.md` (files >15KB summarized per scribe workflow).
