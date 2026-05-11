# Squad Decisions

> Decision log. Append-only; supersede with a new entry rather than rewriting history.

## D-001 — Project shape
**Decision:** End-to-end iPhone art recognition app with iOS + Python backend + ML + Azure cloud, organized as a monorepo at the repo root.
**Rationale:** Maximizes resume signal across mobile, ML, and cloud while keeping cross-cutting changes manageable.
**Owner:** Coordinator. **Status:** Active.

## D-002 — Architecture is retrieval-first
**Decision:** Recognition is a retrieval problem (image embedding + ANN search over indexed museum metadata), not a giant trained classifier. The LLM is the storyteller; the retrieval system owns the facts.
**Rationale:** The label space (every artwork ever made) is enormous and constantly changing. Retrieval is updateable without retraining and is grounded by design.
**Owner:** ml-retrieval-engineer. **Status:** Active.

## D-003 — Stack
**Decision:**
- Backend: Python + FastAPI; ML and API live in the same service in v1.
- Vector + metadata store: Postgres + `pgvector` (Azure Postgres Flexible Server B1ms in prod; Docker Compose locally). HNSW index, cosine distance.
- LLM: Azure OpenAI Service (GPT-4.1- or GPT-5-class deployment).
- Cloud: Azure (West US 3) with Container Apps + ACR Basic + Key Vault + Managed Identity + Azure Monitor.
- Evaluation: Azure AI Foundry (built-in + custom evaluators).
- iOS: Swift + SwiftUI, no on-device ML in v1.
**Rationale:** Idiomatic Azure web-app stack; Postgres+pgvector replaces the earlier FAISS-only plan to avoid a v2 migration; Azure Foundry is a strong, defensible RAG evaluation story.
**Owner:** Coordinator. **Status:** Active. **Supersedes:** any earlier mention of FAISS-only.

## D-004 — Datasets
**Decision:** Phase 1 ingests **The Met Open Access** only (~50K paintings/sculpture records with public-domain images). Phase 4 expands to a multi-source catalog: Rijksmuseum, Harvard Art Museums, Smithsonian Open Access, Art Institute of Chicago, Cleveland Museum of Art (~200K records combined). WikiArt only after eval is in place. Each source is a pluggable ingestion adapter that emits the canonical `NormalizedArtwork` shape. **Store metadata + embeddings + image URLs; never store original images.**
**Rationale:** Versatility comes from the catalog, not the model. Eliminates a giant image archive problem.
**Owner:** ml-retrieval-engineer. **Status:** Active.

## D-005 — Data + confidence model
**Decision:** Locked in `docs/data-model.md`. Status enum `exact | likely | style_only | no_match` driven by `top_score` and `gap` (top - second). Starting bars 0.85 / 0.70 / 0.55. Top 1 candidate only unless `likely` with `gap < 0.05` (then top 3). `style_only` may name an artist with "resembles"-style framing only; never claim the artwork is by them.
**Rationale:** Confidence-aware UX is non-negotiable; thresholds are tunable from the eval set.
**Owner:** ml-retrieval-engineer + backend-engineer. **Status:** Active.

## D-006 — Image pipeline
**Decision:** Locked in `docs/image-pipeline.md`. Single `services/ml/ml/imageops.py::prepare_for_embedding` function used by **both** ingest and query paths. iOS pre-uploads at long-edge ≤ 1600 px, JPEG q=0.85, EXIF (incl. GPS) stripped. Server always re-decodes.
**Rationale:** Drift between ingest-time and query-time pre-processing silently degrades retrieval. One function = no drift.
**Owner:** ml-retrieval-engineer. **Status:** Active.

## D-007 — API contract v0
**Decision:** Locked in `docs/api.md` and `packages/shared/api/*.json`. Single core endpoint `POST /v1/identify` (multipart, image + optional `client_request_id`); `GET /v1/artworks/{id}`; `/healthz`, `/readyz`, `/version` ops endpoints. Static bearer-token auth. Path-based versioning, additive only. Tone is server-locked to `museum_guide`, length to `short` for v1 — not in the request shape.
**Rationale:** Locks the iOS↔backend boundary so all three workstreams move in parallel.
**Owner:** backend-engineer. **Status:** Active.

## D-008 — Rate limiting
**Decision:** 10 requests per 5 minutes per API key, sliding window. 429 with `Retry-After`; surface `X-RateLimit-*` headers.
**Rationale:** Reasonable demo-app safeguard; cheap to enforce in v1.
**Owner:** backend-engineer. **Status:** Active.

## D-009 — Embedding model
**Decision:** Selection criteria locked in `docs/embeddings.md`. The actual model pick is deferred to Phase 1 implementation; benchmark at least one SigLIP-class, one OpenCLIP-class, and one image-only baseline (e.g., DINOv2). CPU-only inference target to start; GPU only if benchmarks force it. Embedding API must be wrapped behind a single function so swapping models is a one-file change.
**Rationale:** State-of-the-art shifts; lock criteria, not a name.
**Owner:** ml-retrieval-engineer. **Status:** Active.

## D-010 — Resume narrative
**Decision:** Optimize for three signals:
1. Applied ML engineer who ships end-to-end systems.
2. Retrieval / RAG / evaluation depth via Azure AI Foundry.
3. Production-minded mobile + cloud delivery (TestFlight, Azure deploy, observability).
**Rationale:** Differentiates from "called an LLM API" portfolios; matches both general ML and Microsoft-flavored hiring.
**Owner:** Coordinator. **Status:** Active.

## D-011 — Deployment shape
**Decision:** Locked in `docs/deployment.md`. Two environments only: `local` (Docker Compose) and `prod` (Azure West US 3). Add `preview` only when TestFlight beta testers need isolation. `$50/mo` subscription budget alert with daily Azure OpenAI cost cap of $3/day in v1.
**Rationale:** Solo portfolio project; staging is overkill until traffic exists.
**Owner:** backend-engineer. **Status:** Active.

## D-012 — Privacy + observability
**Decision:** Locked in `docs/privacy-observability.md`. Raw images never stored or logged. PII scrubbed regardless of sample rate. `PROMPT_LOG_SAMPLE_RATE` v1 default = 1.0 in both local and prod (no real users yet). Lower in prod once usage justifies the cost.
**Rationale:** Maximum signal during early development; safe defaults baked in.
**Owner:** backend-engineer. **Status:** Active.

## D-013 — Out of scope for v1
**Decision:** Out of scope until proven necessary by metrics or product need:
- Fine-tuning the embedding model.
- Learned reranker.
- On-device Core ML inference.
- Custom-trained description LLM.
- User accounts / sign-in.
- Per-user history / preferences sync.
- Multi-region.
- Crash reporting SDKs (Apple's TestFlight crash logs suffice).
**Rationale:** Resume-strong signal already comes from retrieval + RAG eval + end-to-end shipping. Don't over-build.
**Owner:** Coordinator. **Status:** Active.

## D-014 — Backend: local infra + env-driven settings
**Decision:** Adopt the following for Phase 1 backend work:
1. **Local Postgres image:** `pgvector/pgvector:pg16` (preinstalled `vector` extension). Single container in `infra/docker-compose.yml`, named volume `art_guide_pgdata`, `pg_isready` healthcheck, port 5432, defaults `art_guide / art_guide / art_guide`.
2. **Settings module:** Pydantic Settings at `services/api/app/config.py` with `get_settings()` cache. Fields: `ENV` (Literal["local", "prod"]), `DATABASE_URL` (asyncpg DSN), `DB_POOL_{MIN,MAX}_SIZE`, `API_BEARER_TOKEN` (with legacy `API_KEY` alias), `RATE_LIMIT_{REQUESTS,WINDOW_SECONDS}`, `PROMPT_LOG_SAMPLE_RATE`, Azure OpenAI fields. Prod mode requires non-empty tokens and explicit DATABASE_URL. Schema migrations deferred until embedding dimension `D` is locked.
3. **Async DB driver:** thin asyncpg pool (~0.29+), not SQLAlchemy. Rationale: pgvector SQL is cleaner, lower per-query overhead, one-module refactoring later if needed.
**Operational:** `/readyz` now pings Postgres (`SELECT 1`), returns 503 if down. `/healthz` stays liveness-only. Tests: 13 green.
**Constraints honored:** D-007, D-008, D-011, D-012, D-013.
**Owner:** backend-engineer. **Status:** Active.

## D-015 — Embedding model pick — Phase 1
**Decision:** Phase 1 default embedding model is **`google/siglip-base-patch16-224`** (available as `siglip-base-224` spec in `services/ml/ml/embeddings.py`, selected by `DEFAULT_EMBEDDER_NAME`).
- **Embedding dimension `D = 768`** — backend should size `vector(768)` column on Postgres `artworks` table. Unblocks schema migration.
- Vectors L2-normalized at embedder boundary → cosine similarity = dot product.
- Preprocessing routes through `ml.imageops.prepare_for_embedding` (one-pipeline rule honored).
**Benchmark (15 Met ref images, 4 augmentations per ref, CPU Apple Silicon):**
| Model | Family | D | Recall@1 | p95 ms | Notes |
|-------|--------|---:|-------:|------:|-------|
| siglip-base-224 | SigLIP | 768 | 1.000 | 120 | Vision-language capable; picked. |
| openclip-vitb32 | OpenCLIP | 512 | 0.983 | 83 | Slightly faster; weaker gap. |
| dinov2-base | DINOv2 | 768 | 1.000 | 118 | Image-only; wider confidence gap. |

**Rationale:** Vision-language alignment (enables future text queries, zero-shot tagging). SigLIP + DINOv2 tie on accuracy; SigLIP's sigmoid pretraining favors retrieval. Robust to augmentations (crop, jitter, glare, perspective). p95 120ms well within budget. Apache-2.0 license. Pluggable factory allows instant swap if tuning shows DINOv2 preferred.
**Caveats:** SigLIP gap (0.10) tighter than DINOv2 (0.18) — confidence thresholds in D-005 may need retune from real data. Small eval set (60 total queries). Model weights cache must be baked into Container Apps image at deploy time.
**Unblocks:** Backend can now finalize `artworks.embedding: vector(768)` schema. Backend calls `get_embedder().embed_bytes(image_bytes)` for /v1/identify. ML Engineer proceeds to Met-ingest + retrieval-v1 eval dataset bootstrap.
**Owner:** ml-retrieval-engineer. **Status:** Active.

## Governance

- All meaningful changes require explicit decisions here.
- Document architectural decisions here. Operational notes go in agent `history.md`.
- Supersede with a new D-NNN entry rather than rewriting an old one.
