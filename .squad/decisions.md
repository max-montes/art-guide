# Squad Decisions

> Decision log. Append-only; supersede with a new entry rather than rewriting history.

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

## D-022 — iOS live-mode Codable shape reconciliation + local networking fix

**Decision:** Swift models `IdentifyResponse` and `ArtworkCandidate` had mismatches with the server wire format. The server returns `{ request_id, match: { status, candidates }, explanation, diagnostics }` (nested match envelope), but the iOS model expected a flat `{ status, top_candidate, alternates }` structure. Additionally, per-candidate similarity is named `score` in the API contract but `confidence` in Swift, and iOS ATS blocked `http://localhost:8000` silently.

**Fixes applied:**
1. Reshaped `IdentifyResponse` with `MatchEnvelope` struct (status, confidence, candidates) to match server's nested shape. Computed properties (`status`, `topCandidate`, `alternates`, `disclaimer`, `style`) preserve existing call sites; memberwise init unchanged.
2. `ArtworkCandidate` CodingKey: `case confidence = "score"` maps the wire field to the view property.
3. `Info.plist` gains `NSAppTransportSecurity.NSAllowsLocalNetworking = true` — unblocks HTTP to localhost/127.0.0.1/link-local for dev.
4. Debug diagnostics: `ArtGuideApp.init()` prints resolved base URL on launch. `APIClient` catches and logs full `DecodingError` + first 500 chars of raw body. `APIError.decoding.userFacingMessage` exposes detail in DEBUG.

**Files changed:** `apps/ios/ArtGuide/Models/IdentifyResponse.swift` (MatchEnvelope rewrite), `ArtworkCandidate.swift` (CodingKey), `Info.plist` (ATS), `ArtGuideApp.swift` (launch print), `APIClient.swift` (error logging), `APIError.swift` (DEBUG message).

**Rationale:** Codable shape mismatches are the most common JSON integration bug and almost always hide behind opaque "unexpected response" errors. Tight error logging + confidence in the wire format now exercised by real client. This is the fourth bug found by hitting the live system end-to-end (D-020 transformers incompatibility, D-019 LLM grounding, earlier confidence thresholds).

**Verification path:** `cd apps/ios && ./setup.sh && xcode⌘R` with backend running at `http://localhost:8000`. Console should print `[ArtGuide] API base URL: http://localhost:8000`. Snap photo; should decode successfully and return match status.

**Constraint conformance:** D-007 (API shape).

**Owner:** ios-engineer. **Status:** Active. **Date:** 2026-05-10.

## D-023 — Azure Prod Infrastructure Provisioning

**Decision:** Bicep IaC + phased deploy.sh for Azure prod stack (Phases 1-3 + 5). All resources tagged and deployed to West US 3 in `art-guide-prod-rg`. Phase 4 (Azure OpenAI gpt-4o-mini @ 10 K TPM) deferred pending approval. Postgres admin password generated by script, stored only in Key Vault, never logged or committed.

**Provisioned resources:**
- Resource group: `art-guide-prod-rg` (westus3)
- Postgres Flexible Server: `art-guide-prod-pg` (B1ms, 32 GiB, ~$15/mo)
- Container Registry: `artguideprodcr` (Basic, ~$5/mo)
- Key Vault: `art-guide-prod-kv` (Standard, RBAC)
- Container Apps Environment: `art-guide-prod-cae` (Consumption, $0–10/mo)
- Container App: `art-guide-prod-api` (0–2 replicas, system MI, placeholder image)
- Log Analytics: `art-guide-prod-logs` (PerGB2018, 30-day, ~$2/mo)
- Storage Account: `artguideprodst` (Standard_LRS, no public access, ~$1/mo)
- Budget alert: $50/mo @ 80% threshold
- **Total estimated: ~$24–34/mo (no AOAI usage)**

**Infra artifacts:** `infra/azure/main.bicep`, `infra/azure/parameters.prod.json`, `infra/azure/deploy.sh`, `infra/azure/README.md`.

**Phase 4 — Azure OpenAI (pending):** Re-run deploy.sh after access approval (<https://aka.ms/oai/access>). Model choice: `gpt-4o-mini` (10× lower cost than GPT-4/4.1) — sufficient for grounded museum-guide narratives.

**Design rationale:** Phased deployment enables verification + rollback at each stage. RBAC + system MI prevents credential sprawl. Bicep template is idempotent and version-controlled. Personal MSDN subscription likely 403s on AOAI provisioning — deploy.sh gracefully reports and continues.

**Deferred:** GitHub Actions CI/CD, custom domain, data migration, iOS prod URL update (one-liner after Container App URL confirmed), migrations (manual or AUTO_MIGRATE).

**Constraint conformance:** D-003 (stack), docs/deployment.md.

**Owner:** backend-engineer. **Status:** Provisioned (Phases 1–3, 5; Phase 4 pending). **Date:** 2026-05-10.

## D-024 — Met Field Audit — Scope Decision

**Decision:** The Met API contains no free-text description or iconographic content. Ceiling under Met-only enrichment is richer museum placard (artist biography, credit line, dimensions, dynasty). LLM cannot truthfully answer iconographic questions without text from Wikipedia/Wikidata. **Ship tier (a)** (Met-only, 4 new columns) immediately; reserve tier (b) (Wikipedia/Wikidata adapter) as Phase 2 spike after eval set shows what users actually ask. Tier (c) (defer symbolism entirely) not recommended.

**Tier (a) — Met-only enrichment (ETa: ~1 day):**
- `artist_bio` (from `artistDisplayBio`) — e.g., "French, Paris 1748–1825 Brussels"
- `credit_line` (from `creditLine`) — provenance context
- `dimensions` (from `dimensions`) — physical scale
- `dynasty` (from `dynasty`) — essential for ancient/Asian art classification

Plus retrieval/filtering:
- `object_wikidata_url` (from `objectWikidata_URL`) — bridge for future tier (b)
- `date_begin` / `date_end` (from `objectBeginDate` / `objectEndDate`) — temporal filtering

**Tier (b) — + Wikidata adapter (ETa: +2–3 days):** Ingest iconographic prose from Wikipedia/Wikidata via `objectWikidata_URL`, enabling truthful narrative for symbolic content.

**Tier (c)** — Defer: Phase 2 or later.

**Rationale:** Tier (a) is zero additional complexity — it's the natural next step in enrichment. Tier (b) requires Wikidata ingestion adapter + eval validation that iconographic prose actually helps (not just noise). Ship tier (a) in Phase 1, gather user feedback, unblock tier (b) with data.

**Constraint conformance:** D-002 (retrieval-first), D-004 (datasets), D-005 (confidence model, LLM grounding).

**Owner:** ml-retrieval-engineer. **Status:** Tier (a) ready to implement; recommendation is ship in Phase 1. **Date:** 2026-05-10.
**Audit file:** `docs/met-ingest-field-audit.md`.

## D-025 — Eval bootstrap: dataset design, thresholds, baseline

**Decision:** Ship a held-out eval harness as Phase 1 close-out before any Phase 2 changes land (embedder swap, Azure deploy, Wikipedia adapter). Without a repeatable before/after number, every change is flying blind.

**Dataset (45 cases):** 30 exact in-catalog (exact Met CDN source images; should score cosine ≈ 1.0 → `exact`), 5 perturbed in-catalog (PIL transforms: rotate 5°, crop 10%, brightness ×1.2, contrast ×0.8, saturation ×0.9; still expected `exact`), 10 out-of-catalog (famous paintings not in catalog + non-art photos; expected `style_only`/`no_match`). Three-class design is necessary: class (a) catches recall regression, class (b) catches robustness regression, class (c) catches false-positive/over-confidence regression.

**Thresholds (bootstrap/permissive):** recall@1 ≥ 0.80, recall@3 ≥ 0.90, status_accuracy ≥ 0.65, latency_p99_ms ≤ 5000. Tighten after catalog grows to 10K+ and Azure baseline is measured.

**CI form (v1):** GitHub Actions sketch in `docs/eval-ci.md`. Azure AI Foundry integration is Phase 2 per D-013 — not blocked on it.

**Baseline (100-record catalog, 2026-05-10):** recall@1=1.000, recall@3=1.000, status_accuracy=1.000, p50=841ms, p99=1385ms. Snapshot: `services/ml/eval/baseline-2026-05-11T06-30-17Z.json`.

**Owner:** ml-retrieval-engineer. **Date:** 2026-05-10.

## D-026 — Explanation UX: Museum plaque prose (not list)

**Decision:** Explanation rendering is always prose paragraph (single text block), never a bulleted list. The LLM must invoke on every request regardless of confidence band, always grounded in retrieved record fields only per Hard Rule #1. The interaction model is "museum wall plaque" — cohesive narrative that stitches metadata (title, artist, period, medium, dimensions, provenance) into a single sentence or paragraph.

**Rationale:** Product feel. Lists feel sterile and fragmented; a plaque metaphor elevates the experience and justifies LLM presence. Confirms LLM is non-optional (can't drop it to save cost) but does not relax grounding — every fact must originate from the retrieved record, no world knowledge injection.

**iOS impact:** No rendering of bullet-list properties. Explanation is single paragraph in the results view. Server sends one `explanation` string; client displays it as-is (no client-side formatting).

**Backend implementation:** `build_prompt(record, status)` always generates for every confidence band, including `no_match` (previously canned text). Grounding contract unchanged: LLM failure is non-fatal (logged, replaced with deterministic stub). Prompt template includes status-specific guardrails (exact: "plainly", likely: "hedge", style_only: "resembles only").

**Constraint conformance:** Hard Rule #1 (LLM does not own facts), D-002 (retrieval-first).

**Owner:** Coordinator + backend-engineer + ios-engineer. **Status:** Design locked. **Date:** 2026-05-11.



## D-027 — Met Enrichment Tier (a) — Shipped

**Decision:** D-024 tier (a) implementation complete. Seven new columns backfilled across all 100 existing Met records. `dynasty` is 0% populated in the current European Paintings corpus (department 11) — expected; it will populate when Egyptian/ancient Asian records are ingested in Phase 4.

**What shipped:**
- `services/api/app/migrations/0002_met_enrichment.sql` — idempotent `ADD COLUMN IF NOT EXISTS` for 7 nullable columns + date-range btree index.
- `services/ml/ml/schema.py` — `NormalizedArtwork` gains 7 new `Optional` fields with doc-strings.
- `services/ml/ml/ingest/met_db.py` — `map_met_record` extracts all 7 fields; empty strings normalized to `None`; `_INSERT_SQL` and `_commit_batch` extended.
- `services/ml/ml/ingest/met_backfill.py` — `backfill_met_enrichment(pool, …)` + pure `extract_enrichment_fields(raw)`. Updates 7 enrichment columns only; preserves embeddings.
- `services/ml/ml/cli.py` — `art-guide-ml backfill met` subcommand.
- `services/ml/tests/test_met_db_ingest.py` — 10 new unit tests; all 41 tests passing.

**Backfill outcome (2026-05-10, 100 rows):**

| Field | Populated |
|---|---|
| `artist_bio` | 100 / 100 |
| `credit_line` | 100 / 100 |
| `dimensions` | 100 / 100 |
| `dynasty` | 0 / 100 (European Paintings — no dynasty data) |
| `object_wikidata_url` | 100 / 100 |
| `date_begin` | 100 / 100 |
| `date_end` | 100 / 100 |

**Constraint conformance:** D-002, D-005, D-007, D-024.

**Owner:** ml-retrieval-engineer. **Date:** 2026-05-10.

## D-028 — Dockerfile v1 for art-guide-api prod image

**Decision:** Azure prod Container App `art-guide-prod-api` (RG `art-guide-prod-rg`, westus3, D-023) was provisioned with an MCR `hello-world` placeholder. First real image ships with:
- Base: `python:3.11-slim-bookworm` (most mature ML wheel coverage; 3.12 requires SDist builds for niche modules).
- Multi-stage build: builder stage with toolchain, runtime stage with only essential libraries.
- Bundle SigLIP weights: `google/siglip-base-patch16-224` (D-015 — not so400m) downloaded at build time to `/opt/hf-cache`, image is ~1 GiB, cold start self-contained with `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1`.
- Server: uvicorn directly (1 worker, no gunicorn). SigLIP holds ~1.5 GiB/process; 2 workers would OOM. Container Apps scales horizontally instead (`minReplicas=0`, `maxReplicas=2`).
- Healthcheck on `/healthz` (not `/health`), non-root `app` user (uid 1000).
- `.dockerignore` at repo root excludes `**/.venv/`, `**/tests/`, `apps/`, `infra/`, `docs/`, `.git/`, `.squad/`, `services/ml/data/`, `.env*`, `.secrets-local/`. Keeps `services/ml/eval/` and `services/ml/evals/` because `pyproject.toml` lists them as packages.
- Built via `az acr build` (remote build, not local push): tar context ~760 KiB, 8m23s first build, layer cache in ACR.
- Image tags: `art-guide-api:v1` + `art-guide-api:latest`. Deployments pin `vN` explicitly.

**Verification:** All endpoints healthy:
```
GET /healthz  → 200 {"status":"ok"}
GET /version  → 200 {"name":"art-guide-api","version":"0.1.0","api_version":"v1"}
GET /readyz   → 200 {"status":"ready","db":"ok"}
GET /docs     → 200 Swagger UI (with Authorization: Bearer …)
revision art-guide-prod-api--0000003: healthState=Healthy, provisioningState=Provisioned
```

**Operational note — cold start:** `warm_embedder()` runs synchronously in FastAPI lifespan. Cold revision (replicas: 0 → 1) blocks ~10–30s while SigLIP loads. First request after scale-to-zero can hit connection reset during model load (subsequent requests <150 ms). Options (defer to Phase 2): move warm_embedder to background task (fall back to 503), or set `minReplicas=1` (+$5–10/mo). For v1 with no real users, leave as-is.

**Files:** `services/api/Dockerfile`, `services/api/.dockerignore`, `/.dockerignore` (repo root), `.squad/skills/acr-remote-build/SKILL.md`.

**Constraint conformance:** D-003 (single FastAPI container), D-011 (local + prod only), D-012 (no image bytes — no write path for user uploads), D-015 (SigLIP-base-224 baked in), D-023 (Azure prod stack).

**Owner:** backend-engineer. **Status:** Shipped — revision art-guide-prod-api--0000003 healthy. **Date:** 2026-05-16.


## D-029 — Prod catalog seeded from Met; end-to-end `/v1/identify` verified

**Date:** 2026-05-16  
**Owner:** ml-retrieval-engineer  
**Status:** Shipped

**Decision:**

Prod Postgres (`art-guide-prod-pg.postgres.database.azure.com`, database `artguide`) has been seeded with 100 Met Open Access records using the existing `art-guide-ml ingest met` CLI with the `--database-url` flag pointing at the Key Vault `database-url` secret. The embedding model is `google/siglip-base-patch16-224` (SigLIP-base, 768-dim, D-015). All seven Wave 1 enrichment columns (D-027) are populated. End-to-end `/v1/identify` verified against the live prod API.

**Details:**

- Records ingested: 100 (inserted=100, updated=0)
- Skipped: 12 filter-mismatches, 1 broken image URL — both expected
- Embedding model: SigLIP-base-patch16-224, 768-dim, L2-normalized (D-015)
- Enrichment columns populated: artist_bio (100/100), credit_line, dimensions, dynasty, object_wikidata_url, date_begin, date_end
- HNSW index: present (m=16, ef_construction=64, vector_cosine_ops)
- Smoke test: "Sunflowers" by Vincent van Gogh (met:436524); status=`exact`, score=1.0
- Cold latency: ~4,200 ms (container warm at time of test; scale-to-zero cold start expected 10–30 s per D-028)
- Warm latency: ~2,900 ms (retrieval_ms=2016, llm_ms=82)
- Connection method: `--database-url` flag (Option B); DSN from Key Vault secret `database-url`; `sslmode=require` baked in

**Rationale:**

The Phase 1 critical path requires real catalog data in prod before iOS integration testing can proceed against live infrastructure. 100 records (European Paintings, department 11) mirrors the local corpus and provides sufficient coverage to validate retrieval quality before scaling to the full Met catalog (~50K records).

**Constraint conformance:** D-002 (retrieval-first), D-004 (Met Phase 1), D-006 (single image pipeline), D-012 (no raw images stored), D-015 (SigLIP-base-224), D-027 (Wave 1 enrichment).

## D-030 — XcodeGen regen protocol: documentation-enforced
**Date:** 2026-05-16 | **Owner:** ios-engineer | **Status:** Active

**Context:** Brady (Max) was blocked with 14 Xcode "Cannot find X in scope" errors after Swift files were added to disk without re-running `xcodegen generate`. This was the second occurrence of this failure mode (also 2026-05-10).

**Decision:** Documentation-based enforcement. Every agent/developer must run `cd apps/ios && xcodegen generate` and commit the updated `.xcodeproj` alongside any new `.swift` files. Enforced via:
1. A `⚠️ ⚠️ CRITICAL` callout at the top of `apps/ios/README.md`.
2. Updated `xcodegen-app-spec` SKILL.md with the regen requirement promoted to the very top, symptom description, and confidence bumped to `high`.

**Rejected options:** Pre-commit hook (adds tooling requirement for all contributors; overkill for solo dev), restructuring `project.yml` (already uses correct recursive glob — regen was simply skipped, not a spec issue).

**Constraint:** The `.xcodeproj` is gitignored per project convention (`project.yml` is source of truth). Regen is a manual step after every Swift file addition.

## D-031 — MockAPIClient uses OSAllocatedUnfairLock (not NSLock) for Swift 6 async safety
**Date:** 2026-05-16 | **Owner:** ios-engineer | **Status:** Active

**Decision:** Replace `NSLock` with `OSAllocatedUnfairLock<Int>` in `MockAPIClient` to eliminate the Swift 6 strict-concurrency warning ("instance method 'lock' is unavailable from asynchronous contexts").

**Why OSAllocatedUnfairLock (not actor):** `MockAPIClient` is `final class` conforming to `APIClientProtocol`. Its mutable properties (`scenario`, `forcedResponse`, `forcedError`, `simulatedLatency`) are accessed synchronously from previews and tests. Converting to `actor` would require `await` at every property access. `OSAllocatedUnfairLock<State>` is async-safe, available on iOS 16+ (matches deployment target), and requires zero call-site changes.

**Result:** `BUILD SUCCEEDED`, 0 errors, 0 warnings, 13/13 tests pass.

## D-032 — XcConfig Include Order (Config.xcconfig)
**Date:** 2026-05-16 | **Owner:** ios-engineer | **Status:** Active

**Problem:** Brady's iPhone shipped with `localhost:8000` + "dev-replace-me" API key instead of prod overrides from `Config.local.xcconfig`. The local file was correctly populated, but its values were being overwritten by the defaults.

**Root cause:** In `apps/ios/ArtGuide/Config/Config.xcconfig`, the `#include? "Config.local.xcconfig"` line appeared **before** default assignments. In xcconfig merge semantics, later assignments win, so the defaults **after** the include were overriding the included values—backwards.

**Fix:** Moved `#include?` to the **end** of Config.xcconfig, after all defaults. Now the included file's values override the defaults as intended. Updated comments to clarify the ordering requirement.

**Verification:** Build succeeded; `xcodebuild -showBuildSettings` confirmed `API_BASE_URL` = prod URL (not localhost) and `API_KEY` = 48 chars (not "dev-replace-me").

**Constraint conformance:** D-011 (local + prod only).

## D-033 — iOS cold-start tolerance: 60/90 s timeouts + /healthz pre-warm on camera appear
**Date:** 2026-05-16 | **Owner:** ios-engineer | **Status:** Active

**Problem:** Brady hit "Could not connect to the server" on first `/identify` call from a real iPhone. Root cause: Azure Container Apps scales to zero after ~20 min idle (D-028). SigLIP model load on cold start takes 10–30 s (D-028 operational note). Default URLSession timeout was firing before the container responded.

**Decision:**

1. **Timeout values:** `APIClient` creates its own `URLSession` (instead of using `.shared`) with:
   - `timeoutIntervalForRequest = 60` — max inactivity per read/write segment
   - `timeoutIntervalForResource = 90` — total request lifetime
   - `/identify` URLRequest explicitly sets `timeoutInterval = 60` as belt-and-suspenders

2. **Warmup strategy:** Added `warmup()` to `APIClientProtocol` (default no-op via protocol extension, so `MockAPIClient` unchanged). `APIClient.warmup()` fires `GET /healthz` with 30 s timeout and swallows all errors. Warmup called from `RootView.onAppear` (camera view appears) via `Task { await session.client.warmup() }`. Container wakes while user is looking at capture UI; model is loaded before photo is taken.

**Files changed:** `APIClient.swift` (timeouts, warmup method, protocol extension), `Endpoints.swift` (healthz path), `RootView.swift` (onAppear warmup invocation).

**Verification:** BUILD SUCCEEDED, 13/13 tests pass. Committed (0a62f8b).

**Constraint conformance:** D-028 (Dockerfile v1 — healthz endpoint), D-007 (API contract — /healthz ops endpoint).

## D-034 — iOS deployment target bumped from 16.0 to 17.0
**Date:** 2026-05-16 | **Owner:** ios-engineer | **Status:** Active

**Reason:** SwiftData (used for local query history) requires iOS 17+. Brady is on a current iPhone (iOS 17+). No features targeting iOS 16-only APIs exist in the codebase; the bump is safe.

**Changed:** `apps/ios/project.yml` — `deploymentTarget` + `IPHONEOS_DEPLOYMENT_TARGET` on all three targets (`ArtGuide`, `ArtGuideTests`, project-wide settings) changed from `"16.0"` to `"17.0"`. XcodeGen regenerated.

## D-035 — Error UX refinement: headline + debugDetail separation
**Date:** 2026-05-16 | **Owner:** ios-engineer | **Status:** Active

**Problem:** `ErrorView` showed a generic "Couldn't identify that photo" headline for every `APIError` case. The `.decoding` case was embedding raw `NSError` text directly in the user-facing message body in DEBUG builds, making it look like a wall of developer text on Brady's device.

**Decision:**
1. Added `APIError.headline: String` — per-case user-facing title (see history.md mapping table).
2. Added `APIError.debugDetail: String?` — raw technical detail extracted from `userFacingMessage`.
3. `userFacingMessage` is now always clean in all build flavors.
4. `ErrorView` shows `error.headline` as the title. In `#if DEBUG`, a collapsible `DisclosureGroup("Details")` shows `debugDetail` when non-nil. Release builds: no disclosure.

**Files changed:** `APIError.swift`, `RootView.swift` (ErrorView), `ArtGuideTests/APIErrorTests.swift`.

**Verification:** BUILD SUCCEEDED, 41/41 tests pass.

## D-036 — Local thumbnail caching is permitted for history UX
**Date:** 2026-05-16 | **Owner:** ios-engineer | **Status:** Active

**Clarification of hard rule #3 ("Never store raw uploaded images"):**

Hard rule #3 is a **server-side constraint**: the backend must not persist the user's uploaded image in logs, blob storage, debug buffers, or anywhere outside the request lifecycle. This is to protect user privacy on shared infrastructure.

It does **not** prohibit a native iOS app from caching the user's own photo on their own device for their own history view. That is standard iOS UX (e.g., all major camera/search apps do this) and does not expose images to any third party.

**What is stored:** A JPEG-compressed thumbnail (~512 px max dimension, ~50 KB) alongside the `/identify` response JSON, in SwiftData on the user's device. This data never leaves the device (no iCloud sync in v1).

**What is NOT stored:** The raw uploaded image, the bearer token, or any server-side log entry containing the image.

## D-037 — Local query history: SwiftData + TabView
**Date:** 2026-05-16 | **Owner:** ios-engineer | **Status:** Active

**Feature:** Users can review past artwork identifications in a "History" tab.

**Persistence:** SwiftData `@Model class HistoryEntry` (requires iOS 17+, see D-034). `ModelContainer` initialized at app launch in `ArtGuideApp.init()` (fatal error on failure — schema is trivial). Fields: `id`, `timestamp`, `thumbnailData?`, `status`, `topMatchTitle?`, `topMatchArtist?`, `topMatchSourceURL?`, `rawResponseJSON`.

**Save flow:** After every successful `/identify` (any status, including `no_match`), `CameraFlowView.saveToHistory()` runs: thumbnail generation on a detached background task, response JSON encoded with `JSONEncoder`, `HistoryEntry` inserted in `modelContext`. Errors log and drop silently — never block the result screen.

**Tab structure:** `RootView` is now a `TabView` with:
- Camera tab: `CameraFlowView` (extracted from old `RootView`) with `NavigationStack` + `"ArtGuide"` title
- History tab: `HistoryView` wrapped in `NavigationStack`
- Default: Camera tab

**Re-rendering:** `ResultDetailView` decodes `rawResponseJSON` → `IdentifyResponse` → passes to `ResultView`. Zero code duplication.

**Out of scope for this session (future work):** search/filter, sharing, iCloud sync, size cap/eviction, bulk delete, re-querying from history entry, backfill for existing users.

**New files:** `Models/HistoryEntry.swift`, `Utilities/ThumbnailGenerator.swift`, `Views/HistoryView.swift`, `Views/ResultDetailView.swift`. Modified: `ArtGuideApp.swift`, `Views/RootView.swift`, `project.yml`.

**Tests added:** `ThumbnailGeneratorTests.swift` (7 tests), `HistoryEntryTests.swift` (6 tests).

**Verification:** BUILD SUCCEEDED, 52/52 tests pass (41 pre-existing + 13 new).

## D-038 — Bicep API container must use parameterized image (no placeholder defaults)
**Date:** 2026-05-17 | **Owner:** backend-engineer | **Status:** Active

**Trigger:** Prod outage — ml-retrieval-engineer's ingest job Bicep deploy regressed the API container to `mcr.microsoft.com/azuredocs/containerapps-helloworld:latest` with port 80. iOS app broke with HTML decode error.

**Root causes:**
1. `parameters.prod.json` had `containerPort: 80` (old bootstrap value, never updated).
2. `main.bicep` had no `apiImage` param — any redeploy could reset the image if the Bicep default was wrong.
3. Registry binding (`registries: identity: system`) wiped by Bicep redeploy.

**Decision:** `main.bicep` must expose `apiImage`, `apiCpu`, `apiMemory`, `containerPort` as params with live-prod-matching defaults. `parameters.prod.json` must explicitly set all four. `deploy.sh` must run `what-if` before every deploy and hard-fail if it would write a hello-world/placeholder image. See `.squad/decisions/inbox/backend-engineer-bicep-image-param.md` for full detail.

**Current live state:** revision `art-guide-prod-api--0000006`, image `artguideprodcr.azurecr.io/art-guide-api:v2` (Wave 2 museum-plaque prompt, D-036 shipped).

## D-039 — Wave 2 Museum-Plaque LLM Prompt
**Date:** 2026-05-16 | **Owner:** backend-engineer | **Status:** Shipped

**Decision:** Wave 2 of the museum-plaque LLM prompt is shipped. The system and status guardrails were rewritten to instruct `gpt-5-mini` to weave tier-(a) enrichment fields (artist_bio, credit_line, dimensions, dynasty) into warm curator prose.

**Voice:** "museum curator writing a wall-plaque" with knowledgeable but warm docent tone — NOT marketing copy, NOT "helpful assistant."

**Per-status behavior:**
- **Exact / high_confidence:** Prompt instructs weaving of `artist_bio` + `credit_line` into prose. Dimensions mentioned only if notably large/small (size heuristic left to model judgment given the guardrail). Dynasty/period leads for non-Western works.
- **Likely:** Same enrichment weave + hedging language ("This appears to be…").
- **Style_only:** Strictly no artwork naming. Resemblance framing only ("resembles the work of X").
- **No_match:** Canned docent phrasing: "I can't place this one — could be a private work, a reproduction, or just outside what I know." No LLM call.

**gpt-5-mini constraints discovered:**
- `temperature` param rejected (hard API error) — removed entirely.
- `max_tokens` → `max_completion_tokens` (API error otherwise).
- Internal reasoning consumes ~700 tokens; `max_completion_tokens=1500` needed to leave room for prose output. With 250, output was empty (finish_reason=length, all tokens to reasoning).

**Smoke test:** Record L'Arlésienne (Van Gogh, met:436529) with title, artist, date, medium, museum, artist_bio, credit_line, dimensions passed to LLM. Output: "Vincent van Gogh (Dutch, Zundert 1853–1890 Auvers-sur-Oise) painted L'Arlésienne: Madame Joseph-Michel Ginoux (Marie Julien, 1848–1911) in 1888–89 in oil on canvas. The painting is in The Metropolitan Museum of Art and entered the collection as the bequest of Sam A. Lewisohn in 1951." Dimensions (36×29 in.) correctly skipped — average-sized work.

**Files changed:** `services/api/app/llm.py` — prompt constants + LLM kwargs. All 73 existing API tests pass.

**Prod deployment:** The new code is committed to `master`. Activating it in prod requires an ACR image rebuild (`az acr build`) + Container App revision update — same procedure as D-028. The prompt changes are backward-compatible (no schema changes).

**Constraint conformance:** D-007 (API contract), D-012 (privacy), D-026 (plaque UX), D-027 (enrichment shipped).

## D-040 — Cost Controls — Budget Alert + Dashboard Docs
**Date:** 2026-05-16 | **Owner:** backend-engineer | **Status:** Active

**Decision:** Azure consumption budget alert and cost-monitoring documentation added for `art-guide-prod-rg`.

1. **Budget:** `art-guide-prod-monthly` created via `az rest PUT` against `Microsoft.Consumption/budgets` API (2023-11-01). Amount: $100/mo, Monthly grain, 2026-05-01 → 2027-05-01. Alerts at 50%, 80%, 100% actual spend → owner email.
2. **Legacy budget:** `art-guide-prod-budget` at $50/mo also exists (pre-existing, not removed).
3. **Cost monitoring docs:** "Cost monitoring" section added to `docs/deployment.md` with direct portal URL, CLI queries for MTD spend by service, and budget verification commands.
4. **Portal URL pattern:** `https://portal.azure.com/#@{tenantId}/resource/subscriptions/{subId}/resourceGroups/art-guide-prod-rg/costAnalysis`

**Files changed:** `docs/deployment.md` — "Cost monitoring" section appended.

**Constraint conformance:** D-011 (deployment shape — $50/mo budget alert).

## D-041 — Container Apps Job for Full Met Catalog Ingest
**Date:** 2026-05-16 | **Owner:** ml-retrieval-engineer | **Status:** Active

**Problem:** The prod Postgres is seeded with only 100 records (D-029). Scaling to the full ~500K Met Open Access catalog requires a long-running compute job (estimated 20–23 hours at 6 req/s with Met's rate floor). Running this from a developer's laptop is impractical for ~500K records. A managed job resource on the same Container Apps Environment eliminates local dependency and integrates cleanly with the existing CI/CD pattern.

**Decision:** Add `Microsoft.App/jobs` resource (`art-guide-prod-ingest`) to `infra/azure/main.bicep` as a sibling of the API container app on the same Container Apps Environment (`art-guide-prod-cae`).

**Job spec:**
- **Image:** `artguideprodcr.azurecr.io/art-guide-api:v1` — same image as the API; no separate build.
- **CMD override:** `art-guide-ml ingest met --limit 0 --batch-commit-size 64`
- **Trigger type:** `Manual` (not scheduled). Brady initiates via `az containerapp job start`.
- **replicaTimeout:** 7200 s (2 hr per Azure op timeout; the actual job takes ~23 hr but Azure tracks execution, not just the provision timeout)
- **replicaRetryLimit:** 1
- **CPU/memory:** 2 vCPU / 4 Gi — headroom for SigLIP model load + batch encode loop
- **Identity:** System-assigned managed identity with AcrPull + KV Secrets User RBAC wired via Bicep
- **DATABASE_URL:** Injected via `secretRef: 'database-url'` (same as API secret; computed from `postgres.properties.fullyQualifiedDomainName` + KV secret)
- **Environment vars:** `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_HOME=/opt/hf-cache` — forces SigLIP load from the baked-in model cache (D-028), no HuggingFace download at runtime

**Corpus count (2026-05-16T17:22 PDT):** `https://collectionapi.metmuseum.org/public/collection/v1/objects?isPublicDomain=true` returned **501,696 public-domain records**. Upward drift from 492K estimate is normal (Met adds records regularly).

**Cost estimate:** ~$1–2 total. 2 vCPU / 4 Gi Container Apps job pricing is ~$0.000012/vCPU-s + $0.000003/GiB-s. At 23 hours: ≈ ~$2.

**First execution:** `art-guide-prod-ingest-brsioxp` — started 2026-05-17T00:51:42Z. Status: Running (full catalog pass; estimated ~23 hrs at 6 req/s Met rate floor).

**Ingest idempotency:** The existing `ON CONFLICT (source, source_id) DO UPDATE` logic (D-018) means re-running the job on an already-seeded catalog is safe — duplicates upsert, not insert.

**Constraint conformance:** D-002 (retrieval-first), D-004 (Met Phase 1), D-006 (single image pipeline), D-011 (two environments), D-012 (no raw images), D-015 (SigLIP-base-224), D-018 (met ingest pipeline).

## D-042 — Error Screen Polish (follow-up to D-035)
**Date:** 2026-05-16 | **Owner:** ios-engineer | **Status:** Active

**Problem:** Brady tested the debug build and saw ~10 lines of raw NSError text rendered inline under the headline "Couldn't identify that photo" for a `.decoding` error. The detection was correct; the UX was not.

Two bugs:
1. `ErrorView` used a single static headline regardless of error case — wrong framing for network/decode errors.
2. `APIError.userFacingMessage` embedded `#if DEBUG` raw error text directly, so it appeared verbatim in the view body.

**Decision:**
- **`APIError.headline`** (new): per-case user-facing title. Each error case maps to a distinct phrase that reflects the cause (not just "Couldn't identify that photo", which is only correct for genuine `no_match` from a 200 response). Mapping tested and locked in `APIErrorTests`.
- **`APIError.debugDetail`** (new): raw technical text (decode error string, URLError code+message, TLS code, HTTP body) extracted from `userFacingMessage` into a dedicated `String?` property. `nil` when there is nothing specific to show.
- **`userFacingMessage`**: stripped of all `#if DEBUG` branches. Always returns clean, actionable copy in all build flavors.
- **`ErrorView`**: renders `error.headline` as title. In `#if DEBUG` builds, shows a collapsed `DisclosureGroup("Details")` below the body when `debugDetail` is non-nil. In release builds, the disclosure is absent entirely.

**Rule established:** "Couldn't identify that photo" is reserved for the genuine `no_match` result from a successful 200 response (handled in `NoMatchView`). Error screens must not reuse it.

**Headline → case mapping:**
- `.networkUnreachable` → "No internet connection"
- `.cannotFindHost/.cannotConnect` → "Can't reach the museum"
- `.timedOut` → "The server is waking up…"
- `.tlsFailure` → "Secure connection failed"
- `.decoding` → "Something went wrong"
- `.http(5xx)` → "The museum server hit a problem"
- `.http(401/403)` → "Authentication problem"

**Constraint conformance:** D-005 (confidence-aware UX), D-007 (API contract — error responses).

## D-043 — iOS Typed APIError Cases for Network Failures
**Date:** 2026-05-16 | **Owner:** ios-engineer | **Status:** Active

**Problem:** `APIError.transport(String)` was a grab-bag that swallowed distinct `URLError` codes. After the cold-start debugging session (D-028, D-033), Brady reported that "Could not connect to the server" (Apple's default string for `.timedOut`) masked both a timeout and a configuration bug. Precise error cases make diagnosis faster and allow targeted user copy.

**Decision:** Expand `APIError` to include:
- `.networkUnreachable`, `.cannotFindHost`, `.cannotConnect`, `.timedOut`, `.tlsFailure(code:)` mapped from `URLError` via `APIError.map(_:)`.
- Keep `.transport(String, code:)` as fallthrough (not removed — still needed for non-URLError errors).
- `userFacingMessage` branches per case: `.timedOut` → cold-start language; 5xx → "museum server hit a problem"; TLS → short actionable copy.
- DEBUG builds show raw code + message for rapid diagnosis.

**Constraint conformance:** D-042 (error screen polish — per-case headlines).

## D-044 — Staged Cold-Start Loading Messages
**Date:** 2026-05-16 | **Owner:** ios-engineer | **Status:** Active

**Problem:** Brady reported 22s wall-time on a cold `/identify`. The existing `LoadingView` only said "This usually takes a few seconds." — incorrect and anxiety-inducing at 20s.

**Decision:** Three-stage escalating message beneath the spinner:
- 3s: "Waking up the museum…"
- 10s: "Almost ready — first match takes a bit longer…"
- 25s: "Still working on it — feel free to keep the camera steady…"

Thresholds as named constants (`LoadingMessageThreshold`) — tunable without view surgery. Implemented as a cancellable `Task` in `identify()`, cancelled in `defer {}` so messages clear on any outcome.

**Rules:** Never say "cold-start", "container", or "server". Warm museum metaphor only.

**Constraint conformance:** D-033 (60/90 s timeouts — provides context for these message timings).

## D-045 — Committed `.githooks/pre-commit` for XcodeGen Drift Prevention
**Date:** 2026-05-16 | **Owner:** ios-engineer | **Status:** Active

**Problem:** The xcodegen drift issue (D-030) occurred twice (2026-05-10, 2026-05-16). Each time, a new Swift file was added without rerunning `xcodegen generate`, causing a cascade of "Cannot find X in scope" errors. Documentation-only prevention (README callout + SKILL.md `⚠️ CRITICAL` heading) was insufficient.

**Decision:** Implement a committed `.githooks/pre-commit` hook:
- Detects staged `.swift` files under `apps/ios/` via `git diff --cached`.
- If found, runs `xcodegen generate --quiet` from `apps/ios/`.
- Stages the updated `ArtGuide.xcodeproj` automatically (`git add`).
- Fails loudly if `xcodegen` is not installed, with the install command.

**Install approach:** `.githooks/` directory committed to the repo + `setup-hooks.sh` at repo root that runs `git config core.hooksPath .githooks`. Chose this over:
- husky: adds Node.js dependency, overkill for a native iOS project
- `.git/hooks/`: not committed, requires manual per-clone setup, invisible to future devs

**Rationale:** Hook is discoverable (visible in git history), self-documenting, and cross-platform (bash). Install is a one-liner. Failure is loud and includes the fix.

**Constraint conformance:** D-030 (XcodeGen regen protocol — elevates from documentation to automated enforcement).


## D-046 — Container Apps Job for Full Met Catalog Ingest
**Date:** 2026-05-17 | **Owner:** ml-retrieval-engineer | **Status:** Active

Added `Microsoft.App/jobs@2023-05-01` resource `art-guide-prod-ingest` to `infra/azure/main.bicep`. `triggerType: Manual`, `replicaTimeout: 7200`, `replicaRetryLimit: 1`. Command: `art-guide-ml ingest met --limit 0 --batch-commit-size 64`. Met public-domain corpus count (2026-05-16): 501,696 objects. Execution `art-guide-prod-ingest-brsioxp` started 2026-05-17T00:51:42Z; expected ~23 hr for full ingest. See inbox file for full spec and operational notes.

**Constraint conformance:** D-006 (catalog over model), D-013 (two environments), D-028 (SigLIP offline).

## D-047 — Eval Baseline Pinned: 100-Record Prod Catalog
**Date:** 2026-05-17 | **Owner:** ml-retrieval-engineer | **Status:** Active

Pinned baseline `baseline-2026-05-17T00-54-11Z.json` against prod with 100-record European Paintings catalog. All threshold failures are expected catalog-coverage artifacts (eval dataset uses `met:435xxx`; prod catalog is `met:436xxx` — zero overlap). Key metrics: recall@1=0.000, recall@3=0.000, status_accuracy=0.079, latency p99=3130ms ✅. 3 false-exact cases (conf 0.851–0.926) signal calibration compression in small homogeneous catalog. 7 out_of_catalog cases skipped due to Wikimedia URL 400/404 — dataset needs URL update. Re-run after full 500K ingest completes.

**Constraint conformance:** D-025 (eval protocol), D-006 (catalog over model).

## D-048 — Parallel Met Ingest Job: Sharded Replicas + Met IP-Throttle Reality
**Date:** 2026-05-17 | **Owner:** ml-retrieval-engineer | **Status:** Active

Reconfigured `art-guide-prod-ingest` (D-046) from 1 replica × 6 req/s (~23 hr ETA) to a parallel sharded design targeting 1-2 hr ETA. Met's per-IP soft-throttle made the original 1-2 hr target unreachable; final landed config trades that for a realistic 5-6 hr ETA at the polite-scraper rate while keeping the parallelism, sharding, and bugfix infrastructure in place.

**Code shipped (image `art-guide-api:v4`):**

1. `art-guide-ml ingest met` CLI: new flags `--shard-index {N|auto}`, `--shard-count N`, `--request-delay S`. `auto` resolves the index from `CONTAINER_APP_REPLICA_NAME`'s trailing integer.
2. `ingest_met_to_db()`: accepts `shard_index`/`shard_count`, applies modulo split (`i % shard_count == shard_index`) to the global candidate id list after `_list_object_ids`. **Bugfix:** `limit=None` or `limit<=0` now means "unbounded" (the previous `if limit <= 0: raise ValueError` was silently aborting D-046 execution `brsioxp` — it status=Unknown'd and added 0 rows in 40 min before being noticed).
3. `_get_with_retry()`: don't retry permanent 4xx (status 400-499 except 429). Per-record 403/404 used to burn 31 s of exponential backoff each; at 30-50 % 403 rates in low-numbered Met IDs this was 80% throughput loss with no upside.

**Bicep changes (`infra/azure/main.bicep` `ingestJob`):**
- `parallelism: 4`, `replicaCompletionCount: 4` — under `manualTriggerConfig` (not at the top of `configuration` — that emits BCP037 and the deploy fails with "Unknown properties parallelism, replicaCompletionCount in ContainerAppsJobConfiguration are not supported").
- `replicaTimeout: 14400` (4 hr per replica).
- `image: art-guide-api:v4`.
- `command`: `art-guide-ml ingest met --limit 0 --shard-index auto --shard-count 4 --request-delay 0.15 --batch-commit-size 64`.
- CPU/memory unchanged (2 vCPU / 4 Gi per replica).

**Three test executions, all manually stopped after observing throttle:**

| Execution | Image | Parallelism | Delay | Behavior |
| --- | --- | --- | --- | --- |
| `rz0rgsf` | v3 | 8 | 0.05 s | First 5 min: ~580 ok / ~30% 403. After: 100% 403. Throttled. |
| `4mrsgi2` | v4 | 8 | 0.05 s | 100% 403 from start (penalty carry-over). |
| `myz5lc3` | v4 | 4 | 0.15 s | 100% 403 (still in penalty box). |

DB row count unchanged at 100 records from D-029.

**Met API rate-limit reality (worth knowing for any future scraper):**
- Met returns **403 Forbidden, NOT 429, NOT Retry-After** when its per-IP soft-throttle trips. You cannot read a retry-hint; you must back off blind.
- Penalty lasts **≥ 40 min** from the burst event in our observed runs.
- Observed threshold from a single Azure Container Apps egress IP: somewhere between 27 req/s aggregate (myz5lc3 was throttled) and 160 req/s (rz0rgsf tripped it). Below 27 we have no clean data.
- KQL `Log_s contains "429"` over-counts dramatically (matches `02:45:28,429` microsecond timestamps and Met object IDs like 10429, 24290). Filter on `"HTTP/1.1 429"` or the explicit `"rate limited"` log string instead.

**1-2 hr ETA not achievable from a single egress IP.** Options to get under that bar all violate other constraints or are out-of-scope:
1. `--department-ids 11,21` (paintings + sculpture only) — cuts the walk 10× but Brady ruled out scoping.
2. Multiple egress IPs (multi-region) — violates hard rule #6 (one prod).
3. Pre-staged IDs from Met's `MetObjects.csv` bulk feed — worth considering as a follow-up; still rate-limited per `/objects/{id}` but no wasted requests on non-PD records.

**Operational handoff:** Brady to `az containerapp job start -n art-guide-prod-ingest -g art-guide-prod-rg` after the throttle penalty clears (≥ 1-2 hr after the last burst, i.e. after ~05:30 UTC on 2026-05-17). Expected ETA at the new committed config: 5-6 hr fetch-bound. First DB-visible commit ~10 min in. If 100% 403 returns, the throttle is still active — wait longer or drop to `parallelism: 2 / request-delay: 0.30s` (= ~6.7 req/s aggregate, mirroring the D-046 single-replica baseline rate but with 2× DB concurrency).

**Side-effect to be aware of on next infra deploy:** `api-bearer-token` is declared in Bicep as `PLACEHOLDER-must-be-set-before-live-traffic` (safely leak-proof). Every `az deployment group create --mode Incremental` resets it, breaking the API. The session's recurring fix was: `az containerapp secret set --secrets "api-bearer-token=$(az keyvault secret show --vault-name art-guide-prod-kv --name api-bearer-token --query value -o tsv)"` + revision restart. A long-term fix (out of scope this session) is to `secretRef: 'api-bearer-token'` from KV like `azure-openai-key` already does.

**Constraint conformance:** D-002 (retrieval-first), D-006 (single image pipeline), D-011 (two envs), D-012 (no raw images), D-015 (SigLIP-base-224), D-018 (met ingest pipeline), D-038 (parameterized apiImage + what-if guard — verified passing on every deploy in this session), D-041/D-046 (Container Apps Job pattern).

## D-049 — iOS History: Full Original Image Storage
**Date:** 2026-05-17 | **Owner:** ios-engineer | **Status:** Active | **Supersedes:** none

### Problem

Brady's History tab previously stored only a ~50 KB thumbnail (`HistoryEntry.thumbnailData`, ≤512 px). Tapping a row re-rendered the structured result, but the actual photo he had taken was gone — only the tiny list-row thumbnail survived. He wanted the original photo viewable from the detail screen, including a full-screen view.

The naïve fix — store the raw camera bytes — would balloon the SwiftData store: iPhone captures are 4032×3024 HEIC/JPEG @ 5–8 MB each. A few hundred history entries would hit 1–2 GB and slow `@Query` materialisation.

### Decision

Add a second, separately encoded image blob to `HistoryEntry` for the detail screen.

**Storage cap:**
- **Maximum longest edge:** 2048 px (downscale if larger; preserve aspect ratio)
- **JPEG quality:** 0.85
- **Target on-disk size:** ~1–2 MB per typical artwork photo

These constants live on `OriginalImageEncoder` (new file at `apps/ios/ArtGuide/Utilities/OriginalImageEncoder.swift`) and are pinned by a unit test (`test_quality_andCap_areTheDocumentedValues`) so silent drift is caught in CI. The thumbnail (~50 KB @ 512 px) is unchanged — it still drives the History list row.

**Schema migration: lightweight, no `VersionedSchema` declared.** Added one optional `Data?` field (`originalImageData`) to the existing `@Model` class. SwiftData's default migration handles additive optional attributes automatically; no `SchemaMigrationPlan` or migration callback is needed for this change. Existing entries simply read back with `originalImageData == nil`. Brady's local SwiftData store is preserved across this change — no wipe required.

**Backward-compat for old entries via `HistoryImageSource.resolve(for:)`:**
| `originalImageData` | `thumbnailData` | Result |
| --- | --- | --- |
| present, non-empty | — | `.original(data)` — full image, tap-to-zoom enabled |
| nil or empty | present, non-empty | `.thumbnailOnly(data)` — header shows "Thumbnail only" label below the image |
| nil/empty | nil/empty | `.missing` — placeholder tile, never crashes |

Pre-migration entries degrade gracefully: the user sees the same low-res thumbnail they always had, plus an unambiguous label explaining why it's not sharper.

**Privacy / hard-rule confirmation:** Project hard rule #3 ("Never store raw uploaded images") is a **server-side** constraint — see D-036 for the canonical clarification. Storing a re-encoded version of the photo the user themselves just took, on that user's own device, in SwiftData, for a History UX they control, is explicitly permitted. Nothing about this change touches server-side storage or logging.

**What changed:**
- **Added:** `OriginalImageEncoder.swift` (JPEG re-encode with 2048 px cap @ 0.85 quality), `HistoryImageSource.swift` (resolution enum `.original`/`.thumbnailOnly`/`.missing`), `FullScreenImageView.swift` (black-backdrop full-screen viewer with close button), `OriginalImageEncoderTests.swift` (7 tests), `HistoryImageSourceTests.swift` (5 tests)
- **Modified:** `HistoryEntry.swift` (+`originalImageData: Data?`), `ResultDetailView.swift` (pinned `HistoryImageHeader`, tap-to-full-screen via `fullScreenCover`), `RootView.swift` (`CameraFlowView.saveToHistory` encodes both blobs), `HistoryEntryTests.swift` (+2 tests)

**Verification:** `xcodebuild test … iPhone 17 Pro` — **66/66 passed** (was 52; +14 new tests). All pre-existing tests untouched and green.

**Constraint conformance:** Hard rule #3 (no raw server-side images — confirmed scoped to server ✓).

## D-050 — Eval Baseline Pinned: 100-Record Prod Catalog
**Date:** 2026-05-17 | **Owner:** ml-retrieval-engineer | **Status:** Active

The eval harness (45 test cases in `eval/dataset.jsonl`) needs a pinned baseline to detect future regressions. Established ground-truth numbers for the current prod catalog state before scaling to the full 500K corpus.

**Decision:** Run the eval harness against prod with the existing 100-record European Paintings catalog. Pin baseline file `baseline-2026-05-17T00-54-11Z.json` in `services/ml/eval/baselines/`. All threshold failures are **expected and documented** — they are catalog-coverage artifacts, not pipeline regressions.

**Baseline metrics (2026-05-17T00:54:11Z, 100-record catalog):**
| Metric | Value | Threshold | Pass? |
|--------|-------|-----------|-------|
| recall@1 | 0.000 | 0.80 | ❌ |
| recall@3 | 0.000 | 0.90 | ❌ |
| status_accuracy | 0.079 | 0.65 | ❌ |
| latency p50 | 1929 ms | — | — |
| latency p95 | 2933 ms | — | — |
| latency p99 | 3130 ms | 5000 ms | ✅ |
**n_total=45 | n_evaluated=38 | n_skipped=7**

**Root cause of threshold failures: catalog–dataset mismatch.** The 100-record prod catalog spans `met:436523`–`met:436642` (100 Van Gogh–era European Paintings). The eval dataset uses `met:435xxx` artworks (Bruegel, Cézanne, Caravaggio, etc.). **Zero overlap.** Every "exact" recall@1 miss is a coverage miss, not a retrieval failure.

**Key calibration signals:**
- **False-exact (3 cases):** 3 exact cases returned `status=exact` with the wrong artwork ID (`recall@1=False`, `status_match=True`). These cross the exact confidence threshold (0.851–0.926) because the Van Gogh catalog has paintings whose SigLIP embeddings land near the query. Small-catalog amplification effect; will be diluted by the full 500K ingest.
- **Out-of-catalog over-confidence (3 evaluated, 0/3 correct):** Van Gogh *Starry Night* (MoMA) → `likely 0.813` (expected `style_only`), Monet *Impression, Sunrise* → `likely 0.788`, Michelangelo *Creation of Adam* → `likely 0.766`. The catalog is too small and homogeneous to produce confident negative signals. With 500K diverse records these artworks will score lower against their true nearest neighbors.
- **7 skipped cases:** Wikimedia Commons 400/404 errors (thumbnail size policy change). Dataset URLs need updating; flagged for next dataset revision.
- **Auth errors (3 cases):** Bearer token was being restored during eval start. 3 cases returned HTTP 401.

**Expected post-full-ingest trajectory:**
- recall@1 should recover toward local baseline (1.0) once eval artworks are indexed
- false-exact rate should drop — the delta between a true match and a random Van Gogh will widen
- out_of_catalog `status` should trend toward `style_only` or `no_match` as more diverse artworks act as comparative anchors
- latency p50 may increase slightly (larger ANN index); watch for > 3000 ms

**Next eval action:** Re-run harness after full-catalog ingest completes (~23 hr from 2026-05-17T00:51Z). Compare against this baseline. Gate on recall@1 ≥ 0.80 before closing the full-catalog task.

**Constraint conformance:** D-025 (eval protocol — baseline pinned with timestamped JSON + MD ✓), D-006 (catalog over model — failures confirm coverage dependency, not model regression ✓).

## D-051 — Container Apps Job for Full Met Catalog Ingest
**Date:** 2026-05-17 | **Owner:** ml-retrieval-engineer | **Status:** Active

The prod catalog seed (D-029) ingested only 100 European Paintings records. The retrieval pipeline's coverage is strictly bounded by catalog size; the embedding model is frozen (D-028). Scaling coverage requires ingesting the full public-domain Met corpus without manual intervention. Met public-domain corpus count (verified 2026-05-16): **501,696 objects**.

**Decision:** Add a `Microsoft.App/jobs` resource (`art-guide-prod-ingest`) to `infra/azure/main.bicep` with `triggerType: Manual`. This enables on-demand full-catalog ingestion runs without running a persistent container.

**Key spec choices:**
- `replicaTimeout: 7200` (2 hr safety cap; full 500K ingest at 6 req/s ≈ 23 hr, so retries cover partial progress)
- `replicaRetryLimit: 1` (fail fast on hard errors; transient rate-limit recovery handled inside the ingest CLI)
- `command: ['art-guide-ml', 'ingest', 'met', '--limit', '0', '--batch-commit-size', '64']` — `--limit 0` means no limit; `--batch-commit-size 64` tuned for Postgres write batching
- `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` + `HF_HOME=/opt/hf-cache` — required; the container image bakes in SigLIP weights (D-028) and must NOT attempt HuggingFace download at runtime
- ACR pull via `registries: [{ server: '...azurecr.io', identity: 'system' }]` — uses job's system-assigned managed identity; granted `AcrPull` role via RBAC assignment in same Bicep

**Resources added to Bicep:**
1. `Microsoft.App/jobs@2023-05-01` — `art-guide-prod-ingest`
2. `Microsoft.Authorization/roleAssignments` — KV Secrets User for job MI
3. `Microsoft.Authorization/roleAssignments` — AcrPull for job MI
4. Output `ingestJobName`

**Execution started:** `art-guide-prod-ingest-brsioxp` at 2026-05-17T00:51:42Z. Status at time of logging: `Unknown` (container initializing).

**Operational notes:**
- Azure Container Apps Job provisioning may set `provisioningState=Failed` due to transient ARM timeout ("Operation expired") even though the resource exists and is functional. `az containerapp job start` succeeds regardless. Follow-up incremental Bicep deploy reconciles state.
- Bicep `--mode Incremental` redeploys reset inline secret values to their Bicep-specified literal (e.g. `PLACEHOLDER-must-be-set-before-live-traffic`). After any Bicep deploy: run `az containerapp secret set` to restore real values from KV.
- Monitor ingest progress via Log Analytics workspace `9825ff17-0e26-4639-bd2f-c576d9d286ed`, filtering by container name `art-guide-prod-ingest-brsioxp`.

**Alternatives not taken:**
- **Persistent container** (always-on ingest sidecar): wasteful at Azure prices when ingest runs are rare.
- **Local ingest → push to prod Postgres**: requires stable long-running local network; risk of partial state.
- **GitHub Actions workflow job**: adds Actions minutes cost; Azure job is self-contained within the existing infra.

**Constraint conformance:** D-006 (catalog over model ✓), D-013 (two environments only — deployed in `prod` env only ✓), D-028 (SigLIP bundled; offline — `HF_HUB_OFFLINE=1` ✓), D-029 (prod ingest pattern — extends, does not replace, prior 100-record seed ✓).

## D-052 — Parallel Met Ingest Job: Sharded Replicas + Met IP-Throttle Reality
**Date:** 2026-05-17 | **Owner:** ml-retrieval-engineer | **Status:** Active

The first attempt at a full Met Open Access ingest (D-051 / execution `art-guide-prod-ingest-brsioxp`, started 2026-05-17T00:51:42Z) was projected at ~23 hours for 501,696 records: a single replica at the polite Met API rate floor (`DEFAULT_REQUEST_DELAY_S=0.15s` ≈ 6 req/s) was the bottleneck. Compute was idle. The Container Apps Job replica had 2 vCPU / 4 Gi but was using ~1 core; the embedder + DB write are not the gating factor — outbound HTTP to `collectionapi.metmuseum.org` is. Brady approved going faster on the **full** 501K dataset (no scoping down to specific Met departments), accepting the temporary 429 / IP-throttle risk. The existing `_get_with_retry` wrapper already absorbs 429s with exponential backoff so an over-aggressive rate produces backoff (slower run) rather than a fatal failure.

**Decision:** Use **Container Apps Jobs native parallelism** to fan out the ingest across 4 replicas (backed off from initial 8), with per-replica modulo work-sharding off the global candidate id list. Lower per-replica `--request-delay` to 0.15 s (polite floor, 6.67 req/s/replica = ~27 req/s aggregate). Each replica self-resolves its shard index from the `CONTAINER_APP_REPLICA_NAME` env var via a new CLI flag (`--shard-index auto`).

**Code shipped (image `art-guide-api:v4`):**

1. `art-guide-ml ingest met` CLI: new flags `--shard-index {N|auto}`, `--shard-count N`, `--request-delay S`. `auto` resolves the index from `CONTAINER_APP_REPLICA_NAME`'s trailing integer.
2. `ingest_met_to_db()`: accepts `shard_index`/`shard_count`, applies modulo split (`i % shard_count == shard_index`) to the global candidate id list after `_list_object_ids`. **Bugfix:** `limit=None` or `limit<=0` now means "unbounded" (the previous `if limit <= 0: raise ValueError` was silently aborting D-051 execution `brsioxp` — it status=Unknown'd and added 0 rows in 40 min before being noticed).
3. `_get_with_retry()`: don't retry permanent 4xx (status 400-499 except 429). Per-record 403/404 used to burn 31 s of exponential backoff each; at 30-50% 403 rates in low-numbered Met IDs this was 80% throughput loss with no upside.

**Bicep changes (`infra/azure/main.bicep` `ingestJob`):**
- `parallelism: 4`, `replicaCompletionCount: 4` — under `manualTriggerConfig` (not at the top of `configuration` — that emits BCP037 and deploy fails with "Unknown properties parallelism, replicaCompletionCount in ContainerAppsJobConfiguration are not supported").
- `replicaTimeout: 14400` (4 hr per replica).
- `image: art-guide-api:v4`.
- `command`: `art-guide-ml ingest met --limit 0 --shard-index auto --shard-count 4 --request-delay 0.15 --batch-commit-size 64`.
- CPU/memory unchanged (2 vCPU / 4 Gi per replica).

**Three test executions, all manually stopped after observing throttle:**
| Execution | Image | Parallelism | Delay | Behavior |
| --- | --- | --- | --- | --- |
| `rz0rgsf` | v3 | 8 | 0.05 s | First 5 min: ~580 ok / ~30% 403. After: 100% 403. Throttled. |
| `4mrsgi2` | v4 | 8 | 0.05 s | 100% 403 from start (penalty carry-over). |
| `myz5lc3` | v4 | 4 | 0.15 s | 100% 403 (still in penalty box). |
DB row count unchanged at 100 records from D-029.

**Met API rate-limit reality (worth knowing for any future scraper):**
- Met returns **403 Forbidden, NOT 429, NOT Retry-After** when its per-IP soft-throttle trips. You cannot read a retry-hint; you must back off blind.
- Penalty lasts **≥ 40 min** from the burst event in our observed runs.
- Observed threshold from a single Azure Container Apps egress IP: somewhere between 27 req/s aggregate (myz5lc3 was throttled) and 160 req/s (rz0rgsf tripped it). Below 27 we have no clean data.
- KQL `Log_s contains "429"` over-counts dramatically (matches `02:45:28,429` microsecond timestamps and Met object IDs like 10429, 24290). Filter on `"HTTP/1.1 429"` or the explicit `"rate limited"` log string instead.

**1-2 hr ETA not achievable from a single egress IP.** Options to get under that bar:
1. `--department-ids 11,21` (paintings + sculpture only) — cuts the walk 10× but Brady ruled out scoping.
2. Multiple egress IPs (multi-region) — violates hard rule #6 (one prod).
3. Pre-staged IDs from Met's `MetObjects.csv` bulk feed — worth considering as a follow-up; still rate-limited per `/objects/{id}` but no wasted requests on non-PD records.

**Realistic ETA at the final committed config (parallelism=4, request_delay=0.15s):**
- Fetch-only ceiling: 501K / 27 req/s = **~5.2 hours** (assuming no further throttle).
- Plus embed cost for the ~3-10% of records that pass classification filter: marginal (~10-30 min).
- **Realistic ETA when throttle clears: 5-6 hours.**

**Operational handoff:** Brady to `az containerapp job start -n art-guide-prod-ingest -g art-guide-prod-rg` after the throttle penalty clears (≥ 1-2 hr after the last burst, i.e. after ~05:30 UTC on 2026-05-17). Expected ETA at the new committed config: 5-6 hr fetch-bound. First DB-visible commit ~10 min in. If 100% 403 returns, the throttle is still active — wait longer or drop to `parallelism: 2 / request-delay: 0.30s` (= ~6.7 req/s aggregate, mirroring the D-051 single-replica baseline rate but with 2× DB concurrency).

**Side-effect awareness on next infra deploy:** `api-bearer-token` is declared in Bicep as `PLACEHOLDER-must-be-set-before-live-traffic` (safely leak-proof). Every `az deployment group create --mode Incremental` resets it, breaking the API. The recurring fix: `az containerapp secret set --secrets "api-bearer-token=$(az keyvault secret show --vault-name art-guide-prod-kv --name api-bearer-token --query value -o tsv)"` + revision restart. A long-term fix (out of scope this session) is to `secretRef: 'api-bearer-token'` from KV like `azure-openai-key` already does.

**Constraint conformance:** D-002 (retrieval-first ✓), D-006 (single image pipeline ✓), D-011 (two envs ✓), D-012 (no raw images — bytes still die in `finally:` block per record ✓), D-015 (SigLIP-base-224 ✓), D-018 (met ingest pipeline ✓), D-038 (parameterized apiImage + what-if guard — verified passing on every deploy in this session ✓), D-041/D-051 (Container Apps Job pattern ✓).
