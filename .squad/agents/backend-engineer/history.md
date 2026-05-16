# Backend Engineer History (Current)

## Current Status — 2026-05-16

**Phase 1 on track.** Core `/v1/identify` pipeline live and returning real Met candidates with confidence-aware status. Prod image v1 shipped and healthy on Azure Container Apps (revision art-guide-prod-api--0000003). 

**API endpoints live:**
- `POST /v1/identify` — multipart image upload, returns `IdentifyResponse` with status, confidence, grounded explanation.
- `GET /v1/artworks/{id}` — detail endpoint, wired to pgvector retrieve.
- `GET /healthz`, `/readyz`, `/version`, `/docs` — all 200.

**Infrastructure:**
- Azure Container App `art-guide-prod-api` at `https://art-guide-prod-api.kindglacier-84ffc0b4.westus3.azurecontainerapps.io`
- PostgreSQL+pgvector live (migrations applied, artworks table ready)
- Azure Key Vault (bearer token stored)
- Static bearer auth required

**Image details (D-028):**
- Multi-stage Dockerfile (python:3.11-slim-bookworm)
- SigLIP-base-patch16-224 bundled (~1.5 GiB process footprint, offline mode)
- uvicorn 1 worker (no gunicorn; 2 workers would OOM)
- Non-root app user (uid 1000)
- Built via ACR remote build (760 KiB context, 8m23s)

**What's working:**
- Embedder cache pattern in lifespan (warm_embedder → SigLIP load once)
- Confidence-aware pipeline (exact | likely | style_only | no_match per D-005)
- LLM grounding guardrails (no world knowledge injection; only fields in retrieved record)
- Museum-plaque prose explanations (D-026)
- Enrichment tier (a) backfill complete (7 columns, 100 Met records, D-027)
- Exception handling split (PIL 400, inference 500)
- Validation error sanitization (no Exception objects leaking to JSON)

**Next:**
- iOS integration testing against prod
- Full Met catalog ingest to prod (ml-retrieval-engineer scope)
- Cold-start latency eval vs. D-025 baseline; Phase 2 decision on minReplicas=1 or background task

## Cross-Agent Note — 2026-05-16 (ml-retrieval-engineer)

Prod catalog now seeded (100 European Paintings, D-029). `/identify` verified end-to-end with Sunflowers → score=1.0. Warm latency ~2.9s (retrieval-bound, not API-bound). Cold-start gotcha from D-028 still applies; monitor in iOS testing.

**Known operational issue:**
- First request after scale-to-zero blocks ~10–30s (image pull + model load). Revisit if eval P99 regresses.

## Learnings

**Dockerfile for ML-fat images:**
- Multi-stage build with bundled HF weights is standard. Builder pulls weights to `/opt/hf-cache`, runtime COPYs + runs offline.
- Install services/ml before services/api in venv (torch resolved once, then lighter layer on top).
- Preserve directories listed in pyproject.toml even if small (pip egg_info aborts if missing).

**ACR remote build:**
- `az acr build` uploads only tar context (~760 KiB), builds in-region, stores layer cache in ACR.
- Beats local docker push for fat images; no Docker Desktop dependency.
- Pattern captured in `.squad/skills/acr-remote-build/SKILL.md`.

**uvicorn for ML workloads:**
- 1 worker per container if model >500MB. Torch is already multithreaded inside the process. Horizontal scaling (replicas) is the right axis, not vertical workers.

**Lifespan blocking:**
- Synchronous `warm_embedder()` blocks first request on cold revisions. Document the trade-off in deployment docs; revisit in Phase 2.

See `history-archive.md` for Phase 0 foundation work (endpoint scaffolding, error handling, pipeline wiring, etc.).

## Cross-Agent Note — 2026-05-16 (ios-engineer outcome)

**iOS app build is now green (13 tests passing, zero warnings).** Brady fixed two blockers: (1) stale `.xcodeproj` regenerated via xcodegen, (2) MockAPIClient NSLock → OSAllocatedUnfairLock for async safety. Brady is now testing on simulator against live prod. Watch for any API contract issues he surfaces (e.g., multipart handling, response shape, error codes).
