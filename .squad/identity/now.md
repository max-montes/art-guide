---
updated_at: 2026-05-16T22:13:00Z
focus_area: Azure prod live; catalog next; iOS integration testing
active_issues: []
---

# What We're Focused On

## Status

**Azure prod stack is LIVE with real image.** Backend-engineer shipped v1 image (art-guide-api:v1) to Azure Container Apps revision art-guide-prod-api--0000003. All core endpoints returning 200. Postgres+pgvector + Key Vault + bearer auth fully operational.

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

1. **Ingest Met catalog to prod** (ml-retrieval-engineer): `art-guide-ml ingest met` → prod Postgres (100 initial, then full ~50K set). Verify `/v1/identify` retrieval works end-to-end with real data.
2. **iOS end-to-end test** (ios-engineer): Create `.xcodeproj`, build + run in Simulator against live prod. Test camera → upload → identify endpoint. Monitor cold-start latency (first request blocks ~10–30s).
3. **Bootstrap eval** (ml-retrieval-engineer): Load retrieval-v1 dataset, run confidence + grounding evaluators, pin D-025 baseline metrics.
4. **Monitor cold-start** (backend-engineer + ios-engineer): Gather real latency data; decision in Phase 2 on minReplicas=1 vs. background task warm-up (D-028 operational note).

## Out of scope Phase 1

- Wave 2 plaque LLM refinement (unblocked for Phase 2; foundation in place)
- Catalog expansion beyond Met (Phase 4; adapters ready per D-004)
- Fine-tuning, Core ML on-device, multi-region (D-013 defer list)

## Coordinator notes

- Azure prod stack is operator-ready. Team can now work against live infrastructure.
- Cold-start latency is an operational note, not a blocker for v1. Revisit if eval P99 regresses.
- Catalog ingest and iOS testing can happen in parallel.
- Historical archives: updated backend-engineer and ios-engineer `history-archive.md` (files >15KB summarized per scribe workflow).
