---
updated_at: 2026-05-17T07:17:00-07:00
focus_area: Retrieval index at 21,194 records; v1 API paths exhausted
active_issues: []
---

# Current focus
Last updated: 2026-05-17 by Brady

## Where we are
Retrieval index at 21,194 artworks (aic=14,504, rijks=6,590, met=100). Both AIC v1 and Rijks v1 ingests have hit their natural PD catalog ceilings via the public APIs. Overnight unlock was the `--resume-skip-existing` flag landed in commit a13db42 (see D-060 & D-061 in decisions.md).

## What We're Focused On

## Status

**🟢 v1 API ingest complete. 🟢 Resume-skip pattern locked in.**

AIC v1 + Rijks v1 ran to natural completion overnight with `--resume-skip-existing` flag (D-060). Added 10,099 AIC + 1,662 Rijks records (commit a13db42, local-only). Natural PD ceilings: AIC ~14,504 (70% filtered by `is_public_domain=true` + image checks), Rijks ~6,590 (OAI-PMH set 261208).

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
