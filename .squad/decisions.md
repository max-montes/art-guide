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

## D-016 — Postgres schema + migration runner
**Decision:** The art-guide Postgres schema is established by **plain SQL files** under `services/api/app/migrations/`, applied by an asyncpg-backed runner in `services/api/app/migrations/__init__.py`. The initial migration `0001_init.sql` creates the `pgvector` extension and the canonical `artworks` table with `vector(768)` for SigLIP embeddings, HNSW cosine index, and idempotent `UNIQUE(source, source_id)` conflict target for upserts.

### Migration tool: plain SQL, not Alembic (for now)
1. Single initial migration; Alembic overhead without current value.
2. API service uses asyncpg only — no SQLAlchemy ORM. No need to drag SQLAlchemy in as a dependency.
3. Runner is ~100 lines, transparent: developers can read it once and know exactly what runs at startup.
4. Exit door: when migrations multiply, swap this module for Alembic in a single follow-up migration.

### Vector dimension locked to `embedding vector(768)` (SigLIP)
Per D-015, embedder is `google/siglip-base-patch16-224`. If the default embedder swaps, write a **new** migration (`0002_*.sql`) that ALTERs the embedding column at the new dimension and rebuilds the HNSW index; do **not** edit `0001_init.sql`.

### Index strategy
- HNSW over `vector_cosine_ops` per D-003 / D-005 (cosine similarity, vectors L2-normalized at embedder boundary).
- Build params `m = 16, ef_construction = 64` — Phase-1 defaults for ~50K Met catalog.
- Btree on `source` for source-scoped filtering.

### Idempotency contract
`artworks` carries `id text PRIMARY KEY` (namespaced e.g. `met:436532`), `source text NOT NULL`, `source_id text NOT NULL`, with `UNIQUE(source, source_id)`. Ingestion adapters UPSERT against `(source, source_id)` to avoid duplicates on re-ingest; both keys point at the same row by construction.

### Schema columns
Aligned with `services/ml/ml/schema.py::NormalizedArtwork` and `docs/data-model.md`: `id`, `source`, `source_id`, `title`, `artist`, `date` (text, free-form), `medium`, `culture`, `period`, `museum`, `source_url`, `image_url` (museum-hosted URL only, never bytes per D-012), `tags` (text[]), `is_public_domain`, `raw_metadata` (jsonb safety valve), `embedding` (vector(768)), `created_at`, `updated_at` (maintained by trigger `art_guide_set_updated_at`). Fields from the original task brief not in `NormalizedArtwork` (e.g., `artist_name`, `date_start`, `dimensions`, `current_location`) were intentionally omitted; source-specific extras live in `raw_metadata` without schema churn.

### `AUTO_MIGRATE` behavior
New setting: `Settings.AUTO_MIGRATE: bool = False`. The FastAPI lifespan opens the asyncpg pool, then if `AUTO_MIGRATE=true` and the pool came up, calls `apply_migrations(get_pool())`. Errors are logged, never raised. Default is **False everywhere** (including `local`); local devs opt in via `AUTO_MIGRATE=true` in `.env`. Prod never enables it; migrations are a deliberate `python -m app.migrations` step in the deploy pipeline.

### Operational interface
- `python -m app.migrations` — apply pending migrations; prints `applied N migration(s): ...` or `no pending migrations`.
- `await app.migrations.apply_migrations(pool)` — programmatic API.
- `app.migrations.discover_migrations(directory=None)` — returns apply-ordered list.
- `schema_migrations(version text PK, applied_at timestamptz DEFAULT now())` records every successful apply.

### What this unblocks
- ml-retrieval-engineer can wire ingest to UPSERT into `artworks` with 768-dim embeddings.
- Query path can issue `ORDER BY embedding <=> $1 LIMIT k` against HNSW.
- `app.db.ArtworkRepository` skeleton gives callers a stable surface; bodies land when ingest/retrieval lands.

### Constraint conformance
D-003 (single FastAPI service), D-005 (cosine distance, top-K rule at query time), D-007 (API shape), D-012 (no raw image bytes).

**References:** D-003, D-005, D-007, D-011, D-012, D-015.
**Owner:** backend-engineer. **Status:** Active.

## D-017 — XcodeGen is the v1 iOS project-generation tool
**Decision:** Use [XcodeGen](https://github.com/yonaskolb/XcodeGen) to generate `apps/ios/ArtGuide.xcodeproj` from a checked-in `apps/ios/project.yml` spec. The spec is the source of truth; the generated `.xcodeproj` is a build artifact and is gitignored.

Setup is one command after Homebrew install:
```bash
brew install xcodegen
cd apps/ios && ./setup.sh && open ArtGuide.xcodeproj
```

The spec encodes the painful settings explicitly:
- `GENERATE_INFOPLIST_FILE = NO` and `INFOPLIST_FILE = ArtGuide/Info.plist` so the camera/photo permission strings and `API_BASE_URL` / `API_KEY` keys actually ship in the built app.
- `configFiles:` wires `ArtGuide/Config/Config.xcconfig` for both Debug and Release.
- Scheme env var `ART_GUIDE_MOCK_SCENARIO` (default `cycle`) is pre-declared so testers can pin a specific match status via Edit Scheme → Run → Environment Variables.
- Bundle id `com.maxmontes.artguide`, iOS 16 deployment, Swift 5.9, automatic signing with empty `DEVELOPMENT_TEAM` (Xcode auto-fills the developer's personal team on first open).

**Rationale:** Anyone with the repo can regenerate the project deterministically; no Xcode-UI clicking required. Build-setting changes happen in `project.yml` (one PR, reviewable diff) rather than inside the binary `.xcodeproj`. Adding a Swift file under `apps/ios/ArtGuide/` requires no project edits—re-running `./setup.sh` picks it up.

**Consequences:** Adds a one-time tooling dependency: contributors need `xcodegen` installed (`brew install xcodegen`). The `.xcodeproj` is no longer in git, so anyone who opens `apps/ios/` in Xcode without first running `setup.sh` will see "no project"—the README's Quick start covers this.

**Scope:** v1, iOS app only. Backend / ML / infra are unaffected.

**Owner:** ios-engineer. **Status:** Active.

## D-018 — Met Open Access ingest pipeline (fetch → embed → upsert)
**Decision:** The Met ingest job lives in `services/ml/ml/ingest/met_db.py` with CLI `art-guide-ml ingest met [--limit N] [--dry-run] [--database-url DSN] [--batch-commit-size N] [--department-ids ID,ID,…] [--jsonl-out PATH]`. Filters: `isPublicDomain=true`, `hasImages=true`, optional `departmentIds`, classification substring match (painting/sculpture vocabulary), and non-empty `primaryImage` URL. Field mapping: `id=met:{objectID}`, `source="met"`, `source_id=objectID`, `title`, `artist`, `date` (free text), `medium`, `culture`, `period`, `museum="The Metropolitan Museum of Art"`, `source_url`, `image_url` (public URL only, no bytes stored), `tags` (order-preserving union of department+classification+objectName+culture+period+tags[].term), `is_public_domain=true`, `raw_metadata` (jsonb: thumbnail_url, license, license_url, met). Embedding: SigLIP base 224, D=768, L2-normalized. Idempotency: `INSERT … ON CONFLICT (source, source_id) DO UPDATE SET …`; upsert is repeatable (safe to re-run on new embedding model rollout). Image handling: bytes flow museum URL → httpx → embed → cleared (never logged, written, or returned; hard rule #3). Preprocessing: no image decode in orchestrator; `embedder.embed_bytes` calls `prepare_for_embedding` (hard rule #4). Performance: 50 rows/txn, 0.15s request delay (≈6 req/s), exponential backoff, L2 norm logged. License: CC0 (Met Open Access); user-agent identifies project; museum-hosted URLs only. Captures `museum-ingest-loop` skill for Rijks/Harvard/Smithsonian/AIC/Cleveland Phase 4 adapters. Risk: classification vocabulary tuned to current Met edge cases; new cases surface as recall gaps in eval (expand vocabulary, not schema). Constraint conformance: D-002, D-004, D-005, D-006, D-009, D-012, D-015, D-016. **Owner:** ml-retrieval-engineer. **Status:** Active.

## D-019 — Backend `/v1/identify` end-to-end pipeline
**Decision:** Wire `POST /v1/identify` end-to-end as the canonical retrieval+RAG pipeline, owned by the FastAPI service. The route is the single seam through which a user image becomes a grounded explanation. Stub from `_hardcoded_irises_response` is removed.

### End-to-end request flow
1. **Validation** — multipart `image` (jpeg/png/heic, ≤10 MiB), optional `client_request_id` (≤64 chars). Bearer auth + 10 req/5 min sliding window enforced by middleware.
2. **Embedder** — process-local cache warmed once in FastAPI lifespan via `warm_embedder()`, never per-request. Route reads `get_cached_embedder()`. Failure → `503 service_unavailable`.
3. **Decode + preprocess** — single `ml.imageops.prepare_for_embedding` function (hard rule #4). Bytes explicitly cleared after preprocessing; never written to disk.
4. **Embed** — CHW → pixel values → forward pass → L2-normalize → 768-dim `np.float32` per D-015.
5. **Retrieve** — `ArtworkRepository.nearest_neighbors(vec, k=5)` using pgvector `embedding <=> $1::vector` over HNSW cosine index. Up to 5 rows, ordered by distance ascending.
6. **Confidence map** — `similarities = 1 - distance` (clamped [0,1]). `top_score = sims[0]`, `gap = sims[0] - sims[1]`. Empty rows → `top_score=0.0, gap=0.0` → `no_match` path.
7. **Status** — `map_status_and_confidence(top_score, gap, thresholds)` per D-005: `exact` (≥0.85 + gap≥0.05), `likely` (≥0.70), `style_only` (≥0.55), else `no_match`.
8. **Candidates** — top 1 unless `likely` + gap<0.05, then top 3. `no_match` → `[]`.
9. **Explanation** — `no_match` → canned re-shoot text (no LLM). Else: `build_prompt(record, status)` with status-specific guardrails (exact: "plainly", likely: "hedge", style_only: "resembles only") → Azure OpenAI. LLM failure is non-fatal: caught, logged, replaced with deterministic stub built from record fields; match still returned.
10. **Response** — `IdentifyResponse` validates against JSON schema. Tone `museum_guide`, length `short` server-locked (D-007). `diagnostics` report timings. `model_versions.embedding="siglip-base-224"`, `model_versions.llm="azure-openai:..."` or `"not-configured"`.
11. **Logging** — one structured log per request (request_id, status, confidence, scores, timings, model versions, llm_called, llm_error). **Never logs image bytes** (D-012). Sampled second line reports which fields were used.

### Threshold values (exposed via Settings, defaults match D-005)
- `CONFIDENCE_EXACT_THRESHOLD` = 0.85
- `CONFIDENCE_LIKELY_THRESHOLD` = 0.70
- `CONFIDENCE_STYLE_ONLY_THRESHOLD` = 0.55
- `CONFIDENCE_LIKELY_AMBIGUOUS_GAP` = 0.05

### LLM guardrail contract
- Prompt never contains world-knowledge preamble (enforced by test).
- Metadata block lists ONLY populated record fields (enforced by test).
- Status-specific guardrails (exact/likely/style_only) baked into template.
- `no_match` never calls LLM; raises ValueError if attempted.
- Grounding: every named title/artist in explanation comes from record by construction, composing with planned `field_citation` + `confidence_honesty` evaluators.

### Embedder cache pattern
Module-global cache in `app/embedding.py` (survives test-client lifespan boundaries). Loaded lazily (import torch/transformers only inside function; API and default tests don't require them). Sticky failure flag avoids re-attempting heavy import per request when model is missing.

### Operational
- Local dev: `pip install -e .[dev] && pip install -e ../ml`. Without `ml`, /v1/identify returns 503.
- Empty Postgres: route returns `no_match` cleanly (verified by unit test).
- Tests: 65 passing, 2 integration-deselected. Default suite needs no torch, DB, or Azure creds.

### Constraint conformance
D-002 (retrieval-first), D-003 (single service), D-005 (confidence), D-007 (API), D-008 (rate limit), D-012 (privacy), D-015 (SigLIP-768), D-016 (schema).

**Owner:** backend-engineer. **Status:** Active. **Date:** 2026-05-10.

## Governance

- All meaningful changes require explicit decisions here.
- Document architectural decisions here. Operational notes go in agent `history.md`.
- Supersede with a new D-NNN entry rather than rewriting an old one.
## D-020 — Transformers 5.x compatibility in embedding pipeline
**Decision:** Updated `_HFEmbedder._forward` in `services/ml/ml/embeddings.py` to handle both transformers ≤4.46.x (returns plain Tensor from `get_image_features`) and ≥5.x (returns BaseModelOutputWithPooling with `.pooler_output`). The fix checks if output has `.pooler_output` attribute; if so, uses it; otherwise treats return value as the Tensor directly. Also split broad exception handling in `/v1/identify` route into PIL decode failures (400 bad_request) vs. embedding inference failures (500 internal_error), exposing previously hidden model errors.
**Rationale:** Hard Rule #4 mandates one image pipeline (`prepare_for_embedding → _to_pixel_values → _forward`). When transformers 5.8.0 was installed, the method signature changed but tests and the API query path didn't exercise it end-to-end, masking the incompatibility. Narrow exception boundaries expose bugs earlier.
**Test coverage:** Added `test_identify_query_code_path_returns_finite_vector` in ML; added `test_identify_real_forward_path_returns_200` in API. Both exercise the real forward path. All 66 API tests pass.
**Verification:** curl against `/v1/identify` with real JPEG returns 200 with status="likely" and Met candidates.
**Owner:** ml-retrieval-engineer. **Status:** Resolved. **Date:** 2026-05-10.

## D-021 — All error handlers must sanitize before JSONResponse
**Decision:** All custom exception handlers must sanitize non-JSON-serializable values before calling `error_response()` / `JSONResponse`.

Concretely: any handler that passes structured data (not just a plain string message) into the response body must ensure every value in that structure is a JSON primitive (`str`, `int`, `float`, `bool`, `None`, or a container of those). Exception objects, dataclasses, Pydantic models, and other non-JSON types must be converted to strings (using `str()`, not `repr()`, to avoid leaking class names).

**Applied fix in `services/api/app/main.py`:**
1. `_sanitize_validation_errors(raw_errors)` — walks Pydantic error dicts and replaces any `Exception` in `ctx` with `str(exc)`.
2. `_validation_summary(errors)` — builds a human-readable single-line message from the first error's `loc` + `msg` without leaking the Python exception class.
3. `_validation_handler` calls both before `error_response()`.

Other handlers (`_http_handler`, `_unhandled_handler`) were audited and are safe.

**Propagation rule:** When adding any future exception handler that passes `details=` or other structured data to `error_response()`, run the structured data through `_sanitize_validation_errors` or write an equivalent sanitiser. Never pass a raw `exc.errors()`, `exc.__dict__`, or any object that may contain Python exception instances. Add a test that exercises the real handler path and asserts the response is valid JSON.

**Regression test:** `services/api/tests/test_validation_handler.py` — four tests covering string-where-file-expected, missing required field, `ctx` JSON primitives, and no exception class name exposure. 70/70 tests pass; live server now returns clean 400 bad_request envelope on malformed input instead of 500.

**Owner:** backend-engineer. **Status:** Active. **Date:** 2026-05-10.
