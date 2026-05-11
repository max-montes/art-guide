---
updated_at: 2026-05-11T02:19:46Z
focus_area: Phase 1 critical path closed — populate catalog, flip iOS live, bootstrap eval
active_issues: []
---

# What We're Focused On

## Status

**Design AND retrieval critical path are both complete.**

All cross-cutting decisions locked in `docs/` and `.squad/decisions.md` (D-001 through D-019):
- API contract v0 ✓
- Data + confidence model ✓
- Image pipeline ✓
- Embedding selection (SigLIP-base-224, 768-dim) ✓
- Deployment + environments ✓
- Privacy + observability ✓

**Phase 1 retrieval pipeline wired end-to-end** (from backend-engineer-3 session):
- Docker Compose: Postgres + pgvector ✓
- Env-driven settings + async asyncpg ✓
- Schema migrations + HNSW index ✓
- SigLIP embedder warmup in FastAPI lifespan ✓
- `/v1/identify` query path: embed → nearest_neighbors → confidence → grounded LLM ✓
- 65 unit tests passing; smoke tests confirm `/healthz`, `/version`, `503` on no-embedder ✓

Deliverables on disk:
- Backend: FastAPI service with full retrieval+RAG pipeline, non-fatal LLM failure handling
- ML: `services/ml/ml/ingest/met_db.py` ingest CLI (dry-run ready)
- iOS: SwiftUI app scaffolding with mocked status views + XcodeGen project spec (D-017)
- Infra: docker-compose.yml with Postgres+pgvector, local dev / prod env split

## Next concrete work

In priority order:

1. **Populate catalog**: `art-guide-ml ingest met --limit 100 --department-ids painting,sculpture` to populate Postgres with initial Met records. Verify /v1/identify retrieval works end-to-end (confidence scores, candidates flow).
2. **iOS flip to live mode**: One-line change in `ArtGuideApp.swift` (APIClient) + `Config.xcconfig` point at `http://localhost:8000`. Test on simulator with local backend running.
3. **Bootstrap evaluation**: Load retrieval-v1 dataset (Met reference images + augmentations + Wikimedia) into local eval suite. Run confidence + grounding evaluators. Pin baseline metrics.
4. **Field-name reconciliation**: Audit `artist` vs `artist_name`, `date` vs `date_start`, etc. across source adapters → canonical `NormalizedArtwork`. Codify in D-020 if schema changes needed.

## Out of scope right now

Anything in D-013. Don't speculate.

## Coordinator notes

- Project owner is iterating on planning + design with a parallel Copilot CLI session. Treat decisions in `.squad/decisions.md` as authoritative; defer ambiguous design questions back to them.
- Xcode is being installed; don't block on iOS build verification.
