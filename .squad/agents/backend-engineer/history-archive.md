# Backend Engineer History (Current)

> **Full history archived to `history-archive.md` (15,680 bytes).**

## Current Status

**Phase 1 implementation complete:** API contract locked, local infra (Docker Compose + Postgres+pgvector + asyncpg) wired, env-driven settings (Pydantic), schema migrations (plain SQL + runner), `/readyz` DB-aware, tests passing.

### What's been built (summary)

1. **API contract** (`docs/api.md`): `/v1/identify`, `/v1/artworks/{id}`, `/healthz`, `/readyz`, `/version`, bearer auth, rate limit 10 req / 5 min.
2. **Local infra** (`infra/docker-compose.yml`): Postgres 16 + pgvector on named volume, pgready healthcheck.
3. **Settings** (`app/config.py`): Pydantic Settings reading `DATABASE_URL`, `API_BEARER_TOKEN`, `AZURE_OPENAI_*`, `PROMPT_LOG_SAMPLE_RATE`, `AUTO_MIGRATE`.
4. **Async DB pool** (`app/db.py`): `asyncpg` only (no SQLAlchemy); `ping()` for health; `ArtworkRepository` skeleton.
5. **Schema migrations** (`app/migrations/*.sql` + runner): `0001_init.sql` creates `artworks` table with `vector(768)`, HNSW index, audit columns, unique `(source, source_id)`.
6. **Tests:** 21 passing (config, ops, migrations); integration tests skipped by default; async pytest wired.

### What's next

- Hook up the LLM (Azure OpenAI client wiring).
- Implement `/v1/identify` to call the retrieval + RAG pipeline (awaiting ML embedder integration).
- Add per-request structured logging (`docs/privacy-observability.md`).

### Working rules

- Conform to `docs/api.md`, `docs/deployment.md`, `docs/privacy-observability.md` for changes.
- Same container image for `local` and `prod`; no hardcoded URLs/keys.
- Coordinate schema changes with iOS and ML.

## Learnings

### 2026-05-10 — iOS app project generation is now deterministic

**From ios-engineer:** The iOS app `.xcodeproj` is no longer hand-crafted in Xcode UI. It's generated from `apps/ios/project.yml` (XcodeGen spec) by running `apps/ios/setup.sh`. The `.xcodeproj` is a build artifact and is gitignored. This means:
- If you ever need to suggest iOS-side env var changes, suggest them to ios-engineer as edits to `project.yml` (in the scheme or target settings); the regeneration is one command.
- No more "move the .xcodeproj around and fix the settings" onboarding friction.
- Decision D-017 has the full rationale; key pain point solved: `GENERATE_INFOPLIST_FILE` now forced OFF with `INFOPLIST_FILE` pinned to the on-disk plist, so camera/photo permissions and API config keys ship in the built app.

---

### 2026-05-10 — Runtime topology diagrams added to docs/architecture.md

**What happened**

- Added "## Runtime topology" section to `docs/architecture.md` (after product summary, before any other content).
- Included TWO side-by-side views (Mermaid + ASCII fallback for plain-text compatibility):
  1. **Local development**: iOS Simulator on Mac → uvicorn on host (services/api + services/ml in-process) → Postgres in Docker, Azure OpenAI external.
  2. **Production**: iPhone (TestFlight) → Azure Container Apps → Azure Postgres Flexible Server B1ms + pgvector, Azure OpenAI, Key Vault, Managed Identity, Azure Monitor.
- Each section includes a "What runs where" callout explicitly stating:
  - Backend API and ML pipeline live in the same Python process (D-003).
  - iOS app is a binary that runs on iOS—never in Docker, never on the server.
  - Postgres is the only thing in docker-compose (local).
  - Development typically uses `uvicorn` on the host for hot-reload speed.

**Diagram style chosen: Mermaid + ASCII fallback.**
- Rationale: Mermaid renders nicely on GitHub/web viewers; ASCII fallback ensures plain-text viewers and grep users see clear structure.
- Both diagrams show runtime relationships precisely (HTTP, asyncpg, HTTPS, Managed Identity, telemetry flows).

**Impact:** New developers land knowing exactly what runs where and why. Reduces onboarding confusion (the "wait, is the iOS app in Docker?" question that motivated this task).

---

### 2026-05-10 — Local infra + env-driven settings

**What landed**

- `infra/docker-compose.yml` (Postgres 16 + pgvector via `pgvector/pgvector:pg16`, named volume `art_guide_pgdata`, `pg_isready` healthcheck, port `5432`, defaults `art_guide / art_guide / art_guide`) and `infra/README.md` mapping local↔prod per `docs/deployment.md`.
- `services/api/app/config.py` — Pydantic Settings (`pydantic-settings`) exposing `ENV`, `DATABASE_URL`, `DB_POOL_*`, `API_BEARER_TOKEN` (with `API_KEY` legacy alias via `AliasChoices`), `RATE_LIMIT_REQUESTS`, `RATE_LIMIT_WINDOW_SECONDS`, `PROMPT_LOG_SAMPLE_RATE`, `AZURE_OPENAI_*`. `model_validator` makes `ENV=prod` reject placeholder/missing secrets; `local` ships sensible defaults that target the docker-compose stack. Cached via `@lru_cache` `get_settings()`.
- `services/api/app/db.py` — thin `asyncpg` pool: `init_pool`, `close_pool`, `get_pool`, `ping`. Migrations TODO documents the future `artworks` table including `vector(D)` and HNSW cosine index, gated on the embedding model pick (D-NNN, Track B).
- `services/api/app/main.py` — async `lifespan` opens/closes the pool. Pool open errors are logged but don't kill startup so `/healthz` stays answerable and `/readyz` reports the truth.
- `services/api/app/routes/ops.py` — `/healthz` is liveness only; `/readyz` runs `SELECT 1` and returns 200 when OK, 503 (`{status:"not_ready", db:"unavailable"}`) otherwise. Wire shape is additive — new `db` field, no removals.
- Auth + rate-limit middleware now read from `Settings` instead of `os.environ`/hardcoded constants.
- `services/api/.env.example`, `services/api/README.md`, and `docs/api.md` updated to use `API_BEARER_TOKEN` (legacy `API_KEY` still accepted to avoid silently breaking dev `.env`s).
- `services/api/tests/{test_config.py,test_ops.py,conftest.py}` — 13 tests, all green via `pytest -q`. Covers defaults, env overrides, scheme normalization, legacy alias precedence, prod strictness, sample-rate bounds, settings caching, `/healthz` 200 even with broken DB, `/readyz` 200 vs 503, `/version`. `pytest-asyncio` in `dev`; `asyncio_mode = "auto"` in `pyproject.toml`.

**Driver choice: thin `asyncpg` pool, not SQLAlchemy 2.x async.** Rationale:

- pgvector queries are cleanest as raw SQL with the `vector` operator — no ORM mapping pulling its weight in v1.
- One fewer dependency tree.
- Lower per-query overhead, useful for the retrieval+LLM latency budget.
- Alembic for migrations doesn't require SQLAlchemy ORM models; the future migration story is unaffected.
- If we ever need a query builder or relationship loading, swapping in SQLAlchemy async on top of asyncpg is a one-module change because everything funnels through `app.db`.

**Gating note: embedding dimension D.** The `artworks.embedding` column type is `vector(D)`. `D` is set when the embedding model is picked (Track B, `docs/embeddings.md`). The migration that creates the `vector` extension, the `artworks` table, and the HNSW index is intentionally deferred until that decision lands as a new `D-NNN` entry. Placeholder + full SQL sketch live in the docstring of `services/api/app/db.py`.

**Constraint conformance**

- D-007 honored: `/v1/identify` and `/v1/artworks/{id}` shapes untouched. `/readyz` body change is additive (new `db` field) on an unversioned ops endpoint — explicitly permitted by the versioning policy.
- D-011 honored: `Settings.ENV` is `Literal["local", "prod"]` only.
- D-012 honored: no logging changes here; settings expose `PROMPT_LOG_SAMPLE_RATE` for the future RAG/LLM wiring.
- `packages/shared/api/*.json` not touched. `services/ml/` not touched.

**Files created/modified (paths):**

- created: `infra/docker-compose.yml`, `infra/README.md`
- created: `services/api/app/config.py`, `services/api/app/db.py`
- created: `services/api/tests/__init__.py`, `services/api/tests/conftest.py`, `services/api/tests/test_config.py`, `services/api/tests/test_ops.py`
- created: `.squad/decisions/inbox/backend-engineer-local-infra.md`
- created: `.squad/skills/fastapi-pydantic-settings/SKILL.md`
- modified: `services/api/app/main.py` (lifespan + asyncpg wiring)
- modified: `services/api/app/routes/ops.py` (DB-aware `/readyz`)
- modified: `services/api/app/middleware/auth.py` (reads from Settings)
- modified: `services/api/app/middleware/rate_limit.py` (reads from Settings)
- modified: `services/api/pyproject.toml` (`pydantic-settings`, `asyncpg`, `pytest-asyncio`)
- modified: `services/api/.env.example` (full env surface)
- modified: `services/api/README.md` (config table + tests section)
- modified: `docs/api.md` (`API_BEARER_TOKEN` env var name + new `/readyz` body)


### 2026-05-10 — Postgres schema + plain-SQL migration runner

**What landed**

- `services/api/app/migrations/0001_init.sql` — `CREATE EXTENSION vector`, `artworks` table mirroring `NormalizedArtwork`, `embedding vector(768)`, HNSW (`vector_cosine_ops`, `m=16`, `ef_construction=64`), btree on `source`, unique on `(source, source_id)`, `updated_at` trigger.
- `services/api/app/migrations/__init__.py` + `__main__.py` — runner: `discover_migrations()`, `apply_migrations(pool)`, `python -m app.migrations` CLI, per-file transaction wrapping the DDL plus the `INSERT INTO schema_migrations` row.
- `services/api/app/config.py` — added `AUTO_MIGRATE: bool = False`.
- `services/api/app/main.py` — lifespan opt-in: when `AUTO_MIGRATE` is true and the pool opened, call `apply_migrations(get_pool())`. Errors logged, never raise.
- `services/api/app/db.py` — replaced the Alembic TODO with a pointer to `0001_init.sql` and added a minimal `ArtworkRepository` (`get_by_id`, `insert`, `nearest_neighbors`) — all `NotImplementedError` until ml-retrieval-engineer wires the embedder into ingest/query.
- `services/api/.env.example` + `services/api/README.md` — document `AUTO_MIGRATE` and the migration story.
- `services/api/tests/test_migrations.py` — 8 new tests covering discovery (sort, ignore, missing dir), DDL contract on 0001_init.sql (vector, 768, HNSW + cosine_ops, m=16, ef_construction=64, unique source+source_id, source btree, all NormalizedArtwork columns present), runner happy-path, idempotency on re-run, skip-already-applied, no-files-still-creates-history-table.
- `services/api/tests/test_migrations_integration.py` — opt-in real-Postgres E2E (`pytest -m integration`) verifying the migration applies, `vector(768)` column type is exact, HNSW index exists with `vector_cosine_ops`, unique constraint exists, re-run is idempotent. Skipped by default via `addopts = "-m 'not integration'"` in `pyproject.toml`.
- Final test count: **21 passed, 2 deselected** (the 2 deselected are the integration tests).

**Migration tool: plain SQL files, not Alembic.**

- One initial migration; Alembic's autogen / branching / downgrade machinery is overhead with no current value.
- Stack is asyncpg-only — Alembic still pulls SQLAlchemy in, even when driving raw SQL.
- ~100-line runner is fully transparent and audit-friendly.
- Door to swap to Alembic later is open: the `schema_migrations(version, applied_at)` table renames to `alembic_version` in one migration of its own.

**Schema column list (NormalizedArtwork-aligned, NOT the prompt's aliased names):**

Used the canonical names from `services/ml/ml/schema.py` because data-model.md and that schema agree; the prompt's `artist_name`/`date_text`/`current_location`/`thumbnail_url`/etc. don't exist in the canonical shape and `services/ml/` is read-only here.

- Identity: `id text PK`, `source text NOT NULL`, `source_id text NOT NULL`, UNIQUE `(source, source_id)`.
- Display: `title`, `artist`, `date` (free text — D-005 forbids numeric normalization), `medium`, `culture`, `period`.
- Provenance: `museum NOT NULL`, `source_url NOT NULL`, `image_url` (URL only — D-012 forbids bytes).
- Extras: `tags text[] DEFAULT '{}'`, `is_public_domain boolean NOT NULL`, `raw_metadata jsonb DEFAULT '{}'::jsonb` (forward-compat safety valve so adapters can park source-specific payloads without schema churn).
- Vector: `embedding vector(768) NOT NULL` — locked to SigLIP per the embedding-pick decision.
- Audit: `created_at`, `updated_at` timestamptz with `now()` defaults; `updated_at` maintained by `art_guide_set_updated_at` trigger.

If/when the embedder swaps, write a NEW migration that ALTERs the column dimension; never edit `0001_init.sql`.

**AUTO_MIGRATE pattern.**

`Settings.AUTO_MIGRATE` defaults to `False` everywhere. The lifespan checks it AFTER pool open succeeds; on True, calls `apply_migrations(get_pool())` and logs the result. Failures don't kill startup (so `/readyz` can still report the truth). In `prod` we never set it; migrations are a deliberate `python -m app.migrations` step in the deploy pipeline.

**Repository skeleton location.**

`app/db.py::ArtworkRepository` — three methods stubbed (`get_by_id`, `insert`, `nearest_neighbors(vec, k, *, source=None)`). Real bodies land when ml-retrieval-engineer wires the embedder into ingest/query.

**Constraint conformance**

- D-003: single FastAPI service. The runner is an in-process module + CLI on the same image; no separate migration service.
- D-005: cosine; the top-K candidate rule lives in the future query path, not in DDL.
- D-007: `/v1/identify` shape untouched.
- D-012: only `image_url` stored; never bytes.

**Boot story (NOT verified live — Docker not available in this CLI environment).**

Documented in README. The unit suite + import-time CLI smoke test confirm wiring is correct; the integration test is the live verification path once Docker is up.

**Files created/modified**

- created: `services/api/app/migrations/0001_init.sql`
- created: `services/api/app/migrations/__init__.py`
- created: `services/api/app/migrations/__main__.py`
- created: `services/api/tests/test_migrations.py`
- created: `services/api/tests/test_migrations_integration.py`
- created: `.squad/decisions/inbox/backend-engineer-postgres-schema.md`
- created: `.squad/skills/sql-migration-runner/SKILL.md`
- modified: `services/api/app/config.py` (`AUTO_MIGRATE` field)
- modified: `services/api/app/main.py` (lifespan auto-migrate hook)
- modified: `services/api/app/db.py` (TODO → schema-file pointer; `ArtworkRepository` skeleton)
- modified: `services/api/.env.example` (AUTO_MIGRATE)
- modified: `services/api/README.md` (migrations section + file map)
- modified: `services/api/tests/conftest.py` (strip `AUTO_MIGRATE` from env)
- modified: `services/api/tests/test_config.py` (assert AUTO_MIGRATE default + override)
- modified: `services/api/pyproject.toml` (`integration` marker, `addopts -m 'not integration'`)
# Backend Engineer History (Current)

> **Learnings from prior iterations archived to `history-archive.md`. Current file focuses on the latest completed work.**

## Current Status

### 2026-05-10 — Tier (a) museum-plaque enrichment: API response shape (D-026)

**Task:** Extend the `POST /v1/identify` response to surface D-024 tier (a) enrichment fields per the museum-plaque directive.

**Changes:**
- `app/schemas.py`: `Candidate` gains 7 new nullable fields (`artist_bio`, `credit_line`, `dimensions`, `dynasty`, `object_wikidata_url`, `date_begin`, `date_end`). `GroundedField` Literal extended with 4 LLM-groundable values.
- `app/db.py`: `ArtworkRepository.nearest_neighbors` SQL SELECT now includes all 7 new columns (both source and WHERE-filtered paths). Columns are consumed opportunistically — if the migration hasn't run yet, asyncpg will raise a column-not-found error at runtime; ml-retrieval-engineer owns the migration via AUTO_MIGRATE.
- `app/llm.py`: `_GROUNDABLE_FIELDS` extended with `artist_bio`, `credit_line`, `dimensions`, `dynasty`. `object_wikidata_url`, `date_begin`, `date_end` are NOT in groundable fields (retrieval/forward-compat only per D-024).
- `app/routes/identify.py`: `_row_to_candidate` maps all 7 new fields.
- `packages/shared/api/identify-response.schema.json`: `Candidate` def + `grounded_fields` enum extended.
- `docs/api.md`: Candidate field table and `grounded_fields` description updated.

**Tests (73 passing, +3):**
- `test_candidate_includes_enrichment_fields_when_populated` — fields appear when row contains them.
- `test_candidate_enrichment_fields_null_when_absent` — no error when row omits them.
- `test_grounded_fields_includes_enrichment_fields_when_populated` — four prose-groundable fields appear in `grounded_fields`; forward-compat trio does not.

**Coordination:**
- Field names match D-024 exactly; ml-retrieval-engineer adds the matching columns via schema migration.
- ios-engineer can now read `artist_bio`, `credit_line`, `dimensions`, `dynasty` from `match.candidates[*]`.

---

### 2026-05-10 — `_validation_handler` JSON serialization fix

**Bug:** `POST /v1/identify` with a string where `UploadFile` was expected caused `RequestValidationError`. The handler passed `exc.errors()` directly to `JSONResponse`. Pydantic v2 error dicts include a `ctx.error` field containing the raw `ValueError` object, which `json.dumps` cannot serialize → 500 `TypeError`.

**Fix (`app/main.py`):**
- Added `_sanitize_validation_errors()` — walks Pydantic error dicts, replaces any `Exception` in `ctx` with `str(exc)` (not `repr`, so class name is not exposed).
- Added `_validation_summary()` — builds a human-readable `"field: message"` line from the first error's `loc` + `msg`, stripping Pydantic's `"Value error, "` prefix.
- `_validation_handler` now sanitizes before calling `error_response`.
- Audited `_http_handler` and `_unhandled_handler` — both safe (only pass literal strings).

**Regression tests (`tests/test_validation_handler.py`, 4 tests):**
- string-where-file-expected → clean 400 JSON conforming to API error contract
- missing required field → clean 400 JSON
- `ctx` values are JSON primitives (not Exception objects)
- error message doesn't expose exception class names

**Decision log:** `.squad/decisions/inbox/backend-engineer-validation-handler-fix.md`

All 70 tests pass. Live server verified: `curl -F "image=string" http://localhost:8000/v1/identify` returns clean 400 JSON.

---



**Phase 1 core path operational:** `/v1/identify` end-to-end pipeline wired (embed → retrieve → LLM → format). Postgres+pgvector schema in place, migrations runnable, embedder cache pattern baked in, LLM guardrails guard-railed. 65 tests passing. Met ingest pipeline (ml-retrieval-engineer) now wired — artworks table ready to populate (D-018).

### What's next

- Run the full Met ingest to populate the `artworks` table.
- Wiring `/v1/artworks/{id}` detail endpoint + frontend integration tests.
- Per-request structured logging (D-012).
- LLM Azure OpenAI error handling + cost monitoring.

**⚠️ Data pipeline ready:** ML has wired `art-guide-ml ingest met [--department-ids ID,ID,…]` → `services/ml/ml/ingest/met_db.py`. CLI produces records conforming to canonical `NormalizedArtwork` schema; `artworks` table is ready to be populated. Decision: D-018.

### 2026-05-10 — `/v1/identify` exception handling split (ml-retrieval-engineer fix)

**Context:** ml-retrieval-engineer fixed a transformers 5.x incompatibility in the embedding pipeline. As part of the fix, split the broad `except Exception` in `/v1/identify` route into two distinct catches:

- PIL decode failure → 400 `bad_request` "Image could not be decoded"
- Model inference failure → 500 `internal_error` "Embedding failed."

**Impact:** Embedding failures are now properly surfaced (previously hidden as 400s), aiding debugging and observability.

**Ready for iOS:** The `/v1/identify` endpoint is now end-to-end live and returning real Met candidates with confidence-aware status. iOS team can proceed with the app flip.

### Working rules

- Conform to `docs/api.md`, `docs/deployment.md`, `docs/privacy-observability.md` for changes.
- Same container image for `local` and `prod`; no hardcoded URLs/keys.
- Coordinate schema changes with iOS and ML.

## Latest Learning

### 2026-05-10 — `/v1/identify` end-to-end pipeline landed

**What landed**

- `app/identify_pipeline.py` — pure logic (`map_status_and_confidence`, `select_candidates`, `distances_to_similarities`, `Thresholds`). No DB/network touched. `DEFAULT_THRESHOLDS` pins D-005 v1 numbers.
- `app/embedding.py` — process-local embedder cache. `warm_embedder()` lazy-imports `ml.embeddings.get_embedder()` and caches the SigLIP wrapper. Failures are sticky-cached so we don't re-attempt the heavy import per request. Tests monkeypatch `get_cached_embedder()`.
- `app/llm.py` — prompt builder + `explain()` orchestrator. The single prompt template (`_PROMPT_TEMPLATE`) lives here. `no_match` short-circuits to canned re-shoot text and does NOT call the LLM. LLM failure → stub explanation, response still returned. Accepts an injectable `client_factory` for tests.
- `app/db.py` — `ArtworkRepository.nearest_neighbors` implemented against pgvector's `<=>` operator with `$1::vector` literal cast (no asyncpg codec required). Helper `_vector_literal` encodes numpy/list inputs as the pgvector text format.
- `app/main.py` lifespan — calls `warm_embedder()` after pool open; failures are logged, never raised. `/v1/identify` returns 503 `service_unavailable` if the embedder did not load.
- `app/config.py` — added `CONFIDENCE_EXACT_THRESHOLD`, `CONFIDENCE_LIKELY_THRESHOLD`, `CONFIDENCE_STYLE_ONLY_THRESHOLD`, `CONFIDENCE_LIKELY_AMBIGUOUS_GAP`. Defaults match D-005; tunable via env.
- `app/routes/identify.py` — full pipeline. Validates upload (415/413/400), reads cached embedder, decodes via `ml.imageops.prepare_for_embedding` and feeds the prepared CHW directly to the embedder's forward pass (avoids double-preprocess, single image pipeline honored), retrieves top-5, maps to status, trims candidates per D-005, calls `explain()`, builds `IdentifyResponse`. Ends with a structured log line — never image bytes.

**Tests:** 65 passing, 2 integration-deselected. Three new test files:

- `tests/test_identify_pipeline.py` — confidence boundaries, candidate trim rule, distance→similarity clamp, threshold defaults pinned.
- `tests/test_llm_prompt.py` — guardrail tests: prompt only contains populated record fields, no world-knowledge preamble, status-specific guardrails present, `style_only` requires "resembles"/"in the manner of" framing, `no_match` raises in the prompt builder, stub `no_match` text never names artist/artwork, `explain()` LLM-failure-non-fatal contract.
- `tests/test_identify_route.py` — TestClient integration with mocked embedder + mocked `nearest_neighbors` + no Azure creds. Covers exact/likely(top-3 ambiguous)/style_only/no_match/empty-DB/415/413/400/401/503-no-embedder, plus JSON-schema conformance against `packages/shared/api/identify-response.schema.json`.

--- Archived from 2026-05-16 main history ---



**Phase 1 core path operational:** `/v1/identify` end-to-end pipeline wired (embed → retrieve → LLM → format). Postgres+pgvector schema in place, migrations runnable, embedder cache pattern baked in, LLM guardrails guard-railed. 65 tests passing. Met ingest pipeline (ml-retrieval-engineer) now wired — artworks table ready to populate (D-018).

### What's next

- Run the full Met ingest to populate the `artworks` table.
- Wiring `/v1/artworks/{id}` detail endpoint + frontend integration tests.
- Per-request structured logging (D-012).
- LLM Azure OpenAI error handling + cost monitoring.

**⚠️ Data pipeline ready:** ML has wired `art-guide-ml ingest met [--department-ids ID,ID,…]` → `services/ml/ml/ingest/met_db.py`. CLI produces records conforming to canonical `NormalizedArtwork` schema; `artworks` table is ready to be populated. Decision: D-018.

### 2026-05-10 — `/v1/identify` exception handling split (ml-retrieval-engineer fix)

**Context:** ml-retrieval-engineer fixed a transformers 5.x incompatibility in the embedding pipeline. As part of the fix, split the broad `except Exception` in `/v1/identify` route into two distinct catches:

- PIL decode failure → 400 `bad_request` "Image could not be decoded"
- Model inference failure → 500 `internal_error` "Embedding failed."

**Impact:** Embedding failures are now properly surfaced (previously hidden as 400s), aiding debugging and observability.

**Ready for iOS:** The `/v1/identify` endpoint is now end-to-end live and returning real Met candidates with confidence-aware status. iOS team can proceed with the app flip.

### Working rules

- Conform to `docs/api.md`, `docs/deployment.md`, `docs/privacy-observability.md` for changes.
- Same container image for `local` and `prod`; no hardcoded URLs/keys.
- Coordinate schema changes with iOS and ML.

## Latest Learning

### 2026-05-10 — `/v1/identify` end-to-end pipeline landed

**What landed**

- `app/identify_pipeline.py` — pure logic (`map_status_and_confidence`, `select_candidates`, `distances_to_similarities`, `Thresholds`). No DB/network touched. `DEFAULT_THRESHOLDS` pins D-005 v1 numbers.
- `app/embedding.py` — process-local embedder cache. `warm_embedder()` lazy-imports `ml.embeddings.get_embedder()` and caches the SigLIP wrapper. Failures are sticky-cached so we don't re-attempt the heavy import per request. Tests monkeypatch `get_cached_embedder()`.
- `app/llm.py` — prompt builder + `explain()` orchestrator. The single prompt template (`_PROMPT_TEMPLATE`) lives here. `no_match` short-circuits to canned re-shoot text and does NOT call the LLM. LLM failure → stub explanation, response still returned. Accepts an injectable `client_factory` for tests.
- `app/db.py` — `ArtworkRepository.nearest_neighbors` implemented against pgvector's `<=>` operator with `$1::vector` literal cast (no asyncpg codec required). Helper `_vector_literal` encodes numpy/list inputs as the pgvector text format.
- `app/main.py` lifespan — calls `warm_embedder()` after pool open; failures are logged, never raised. `/v1/identify` returns 503 `service_unavailable` if the embedder did not load.
- `app/config.py` — added `CONFIDENCE_EXACT_THRESHOLD`, `CONFIDENCE_LIKELY_THRESHOLD`, `CONFIDENCE_STYLE_ONLY_THRESHOLD`, `CONFIDENCE_LIKELY_AMBIGUOUS_GAP`. Defaults match D-005; tunable via env.
- `app/routes/identify.py` — full pipeline. Validates upload (415/413/400), reads cached embedder, decodes via `ml.imageops.prepare_for_embedding` and feeds the prepared CHW directly to the embedder's forward pass (avoids double-preprocess, single image pipeline honored), retrieves top-5, maps to status, trims candidates per D-005, calls `explain()`, builds `IdentifyResponse`. Ends with a structured log line — never image bytes.

**Tests:** 65 passing, 2 integration-deselected. Three new test files:

- `tests/test_identify_pipeline.py` — confidence boundaries, candidate trim rule, distance→similarity clamp, threshold defaults pinned.
- `tests/test_llm_prompt.py` — guardrail tests: prompt only contains populated record fields, no world-knowledge preamble, status-specific guardrails present, `style_only` requires "resembles"/"in the manner of" framing, `no_match` raises in the prompt builder, stub `no_match` text never names artist/artwork, `explain()` LLM-failure-non-fatal contract.
- `tests/test_identify_route.py` — TestClient integration with mocked embedder + mocked `nearest_neighbors` + no Azure creds. Covers exact/likely(top-3 ambiguous)/style_only/no_match/empty-DB/415/413/400/401/503-no-embedder, plus JSON-schema conformance against `packages/shared/api/identify-response.schema.json`.

**Key patterns captured:**

- **Embedder warm-up:** Module-global `_EMBEDDER` in `app/embedding.py`, guarded by `threading.Lock`. FastAPI `lifespan` calls `warm_embedder()` once at startup; route handlers call `get_cached_embedder()` which short-circuits to the cached object. Sticky failure flag (`_LOAD_FAILED`) avoids re-attempting torch/transformers import on every request when the model isn't installed. Tests reset via `reset_cache()` and inject a fake.
- **Prompt template:** Single template in `app/llm.py::_PROMPT_TEMPLATE`. No "you are an art expert" preamble — test `test_build_prompt_does_not_contain_world_knowledge_preamble` enforces it. Status-specific guardrails via `_STATUS_GUARDRAILS` dict.
- **Confidence mapping:** `app/identify_pipeline.py::map_status_and_confidence`. Pure function, reads `Thresholds` (defaults from D-005). Tuning is a config change, not code.
- **LLM failure non-fatal:** `app/llm.py::explain()` wraps chat completion in try/except; on error, returns stub grounded in the same record fields, response still returned.
- **Rate-limit data structure:** `SlidingWindowRateLimiter` keyed on `key:<bearer_token>` with per-key deque of request timestamps. `check()` evicts stale timestamps, returns `(allowed, remaining, reset_at)`. **Test gotcha:** limiter is app singleton; integration tests must clear `limiter._hits` between requests.

**Files created/modified**

- created: `services/api/app/identify_pipeline.py`, `app/embedding.py`, `app/llm.py`
- created: `services/api/tests/test_identify_pipeline.py`, `test_llm_prompt.py`, `test_identify_route.py`
- created: `.squad/skills/grounded-llm-prompt/SKILL.md`
- modified: `services/api/app/db.py` (`nearest_neighbors` body + `_vector_literal`), `app/config.py` (4 thresholds), `app/main.py` (warm_embedder), `app/routes/identify.py` (full pipeline)
- modified: `services/api/pyproject.toml`, `tests/conftest.py`, `.env.example`, `README.md`

## Learnings

### 2026-05-10 — venv-first install pattern + the SigLIP/torch footgun

**The footgun.** `pip install -e ../ml` from the API directory pulls SigLIP's runtime: `torch>=2.2`, `transformers`, `sentencepiece`, `pillow-heif`. If a contributor runs that without a venv active, pip cheerfully upgrades torch *globally*. Anything else on their machine pinned to an older torch silently breaks. The canonical example is `pyannote-audio`, which pins `torch==2.1.2`/`torchaudio==2.1.2`/`torchvision==0.16.2`. max-montes hit exactly this.

**Recovery for an already-trashed pyannote-audio env** (run *outside* the art-guide venv): `pip install 'torch==2.1.2' 'torchaudio==2.1.2' 'torchvision==0.16.2'`.

**Prevention.** `services/api/.venv-bootstrap.sh` is the single supported install path now. It:

- bails if Python < 3.11 (matches `services/api/pyproject.toml::requires-python`),
- creates `services/api/.venv` if missing (else reuses it — idempotent),
- activates it, sanity-checks `command -v python` resolves into the venv before doing anything else,
- fast-skips the pip installs if `import app.main, ml.imageops` already succeeds,
- installs `-e ../ml` *first* (the heavy one, so torch resolves once), then `-e .[dev]`,
- prints the next-step `source .venv/bin/activate && AUTO_MIGRATE=true uvicorn app.main:app --reload`.

`set -euo pipefail` and `cd "$(dirname "$0")"` so it's safe to run from anywhere.

**README is now venv-first.** Quick Start step 2 is `./.venv-bootstrap.sh`. There's a "Why a venv?" callout right under it explaining the torch upgrade gotcha so future contributors don't have to rediscover it. The Configuration section's local-dev block also points at the bootstrap, and the Tests section assumes the bootstrap-created venv (no more `pip install -e ".[dev]"` outside a venv).

**Why a script and not poetry/uv/hatch.** Project style is "keep it boring": stdlib `venv` + `pip`. Adding a packaging tool just to fix an ergonomics hole would be a bigger commitment (lockfiles, CI changes, contributor onboarding) than the problem warrants. A 60-line bash script is the smallest reversible change that makes the right thing the easy thing. If we ever need cross-package version pinning (lockfile-style), that's the moment to revisit `uv` — not this.

**`.gitignore` was already good.** Root `.gitignore` has `**/.venv/`, so `services/api/.venv/` was already covered. No change needed.

**Files added/modified**

- created: `services/api/.venv-bootstrap.sh` (chmod +x)
- created: `.squad/skills/python-venv-isolation/SKILL.md`
- modified: `services/api/README.md` (Quick start, Why a venv?, Configuration block, Tests block)

## Cross-Agent Note

**2026-05-11 — ml-retrieval-engineer fixed SigLIP embedder + Met HTTP 406 bugs.** The cached embedder in FastAPI lifespan should now warm cleanly without crashing on first request. Met ingest pipeline is now fully executable.

**2026-05-11 — iOS live-mode wiring reconciled the JSON contract with Swift Codable models.** ios-engineer-2 discovered that the nested `match` envelope in the server's `/v1/identify` response (`{ request_id, match: { status, candidates }, explanation }`) was misaligned with the iOS Swift model's flat shape expectation. Additionally, per-candidate similarity is named `score` in `docs/api.md` but `confidence` in the Swift CodingKey. Fixes: reshaped `IdentifyResponse` with `MatchEnvelope` struct (mirrors server shape, computed properties preserve call sites), added `case confidence = "score"` CodingKey, added `NSAllowsLocalNetworking = true` to Info.plist (unblock HTTP localhost). Debug diagnostics also added: launch-time URL print, full DecodingError logging. This confirms the response shape `{ request_id, match: { status, confidence, candidates }, explanation, diagnostics }` is now canonical and exercised end-to-end by real client. Decision D-022.

## 2026-05-11 — Eval bootstrap shipped; becomes gate for all API changes

**ml-retrieval-engineer-5 closed Phase 1** by shipping an eval harness (45 test cases, 31 unit tests, thresholds, baseline). Result: **eval is now the gate** — any future backend change (embedder swap, confidence threshold tweak, schema migration, Azure deploy, catalog expansion) must satisfy baseline thresholds before landing.

**Implication for you:** Before committing any API-side change in Phase 2+, run `services/ml/eval/run_eval.py` locally (or in CI) to verify the change doesn't regress recall@1, recall@3, status_accuracy, or latency_p99. Baseline snapshot: `services/ml/eval/baseline-2026-05-11T06-30-17Z.json` (recall@1=1.0, recall@3=1.0, status_accuracy=1.0, p99=1385ms).

**Baseline thresholds (permissive for v1):**
- recall@1 ≥ 0.80
- recall@3 ≥ 0.90
- status_accuracy ≥ 0.65
- latency_p99_ms ≤ 5000

**Tightening roadmap:**
- After catalog grows to 10K+: raise `status_accuracy` to 0.80.
- After Azure West US 3 deploy: tighten `latency_p99_ms` to 2000.
- On embedder swap: re-run baseline first, then set thresholds at `baseline − 5%`.

**Files to know:**
- `services/ml/eval/run_eval.py` — harness entry point
- `services/ml/eval/thresholds.yaml` — gating thresholds
- `services/ml/eval/dataset.jsonl` — 45 test cases
- `docs/eval-ci.md` — CI sketch (Foundry integration Phase 2)
- D-025 in decisions.md — design rationale

**Rate limit note:** Local rate limit is 10 req/5 min. Use `--request-delay-s 35` when running eval locally. In CI, set `RATE_LIMIT_REQUESTS=200 RATE_LIMIT_WINDOW_SECONDS=60`.

## 2026-05-11 — Wave 1 Museum-Plaque Enrichment (Tier a) Complete

**Status:** All tier (a) enrichment shipped. API response shape finalized (D-026 API). 73 tests passing (+3 new). ml-retrieval-engineer backfilled 100 records; iOS rendering complete; metadata layer ready for Wave 2 LLM prompt.

**What shipped:**
- `Candidate` model gains 7 new nullable fields; 4 in `_GROUNDABLE_FIELDS` (artist_bio, credit_line, dimensions, dynasty).
- `ArtworkRepository.nearest_neighbors` SQL SELECT includes all 7 columns; forward-compat trio (object_wikidata_url, date_begin, date_end) never appears in grounded_fields.
- JSON schema + API docs updated.

**Next:** Wave 2 (Docent/plaque LLM prompt). Depends on Azure OpenAI Phase 1 deployment. Will finalize `build_prompt()` template for plaque prose (all confidence bands); iOS will render single explanation paragraph.

### 2026-05-16 — Dockerfile v1 shipped; prod Container App on art-guide-api:v1

**Task:** Build the first real prod image for `art-guide-prod-api` (placeholder
was MCR hello-world on port 80). Decision: `.squad/decisions/inbox/backend-engineer-dockerfile-v1.md`.

**Live state:**
- Image: `artguideprodcr.azurecr.io/art-guide-api:v1` (and `:latest`)
- Revision: `art-guide-prod-api--0000003`, `healthState=Healthy`, `provisioningState=Provisioned`
- Ingress target port: 8000 (was 80)
- ACR pull via system managed identity
- All probes pass: `/healthz` 200, `/version` 200, `/readyz` 200 (DB ok), `/docs` 200 with bearer

**Files:**
- created: `services/api/Dockerfile`, `services/api/.dockerignore`, `/.dockerignore`
- created: `.squad/skills/acr-remote-build/SKILL.md`
- decision: `.squad/decisions/inbox/backend-engineer-dockerfile-v1.md`

## Learnings

### 2026-05-16 — Dockerfile pattern for ML-fat API images

**Multi-stage with bundled HF weights is the right default.** Builder stage
(`python:3.11-slim-bookworm` + gcc/g++ + libpq-dev + libheif-dev) installs
into `/opt/venv`, then `huggingface_hub.snapshot_download` pulls the SigLIP
weights into `${HF_HOME}=/opt/hf-cache`. Runtime stage (same base + libpq5,
libheif1, curl) COPYs only `/opt/venv` and `${HF_HOME}` — drops ~200 MiB
toolchain. Set `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` at runtime so
boot never calls huggingface.co. Image is ~1 GiB; cold-start is predictable
because the slow part (weight load) happens in lifespan once, not on every
request.

**Stage-order matters for the torch footgun.** Install `services/ml` FIRST
inside the venv, then `services/api` on top. This matches `.venv-bootstrap.sh`
and ensures torch is resolved once by the heavy package before the lighter
package layers on top. If you reverse the order, pip can churn the resolve.

**Nested ARG before FROM breaks ACR's dependency scanner.** I initially wrote
`ARG BASE_IMAGE=python:${PYTHON_VERSION}-slim-bookworm` then `FROM ${BASE_IMAGE}`.
ACR Tasks' pre-build dependency scanner does not expand the nested ARG and
fails with `Failed to parse image reference … invalid reference format`.
Fix: just inline `FROM python:3.11-slim-bookworm AS builder` directly. The
local Docker CLI tolerates the nested form, but ACR doesn't.

**`.dockerignore` cannot exclude packages listed in pyproject.toml.** Stripped
`services/ml/eval/` and `services/ml/evals/` from build context to keep the
upload tiny — pip's `egg_info` step then aborted with
`package directory 'evals' does not exist` because `services/ml/pyproject.toml`
declares them as packages. Either keep them in context (they're ~100 KB)
or switch to `find_packages`. Kept them in context.

### 2026-05-16 — ACR remote build is the right call for fat ML images

**`az acr build` vs `docker build && docker push`:** the ACR Tasks build
shipped 760 KiB of context (after .dockerignore) instead of pushing ~1 GiB
of image layers from my Mac. Build ran on an Azure agent in 8m23s end-to-end
(including pulling base, installing all of torch+transformers+sentencepiece,
downloading SigLIP weights, building runtime layer, and pushing two tags
back to ACR). Bonus: no Docker Desktop dependency, layer cache is shared in
the registry, and the image is born intra-region for fast pull to ACA.

Captured in `.squad/skills/acr-remote-build/SKILL.md`.

### 2026-05-16 — uvicorn 1-worker is correct for a 1.5 vCPU / 3 GiB ACA container

The SigLIP-base embedder holds ~1.5 GiB resident in each worker process. Two
uvicorn workers would OOM the 3 GiB Container App. SigLIP forward passes
are already torch-multithreaded inside one process, so extra workers don't
buy throughput either — they just produce CPU contention. The right axis
to scale is *replicas* (Container Apps does this for us 0 → maxReplicas),
not workers per container. **Rule of thumb:** for any image bundling a
500MB+ ML model, default to 1 worker per container and let the orchestrator
scale horizontally.

### 2026-05-16 — `warm_embedder()` in lifespan blocks the first request on cold revisions

When Container Apps activates a new revision from `ScaledToZero`, the very
first inbound request blocks for the duration of:
1. image pull (~10–30 s for our ~1 GiB image, intra-region)
2. lifespan startup, including synchronous `warm_embedder()` (~3–10 s on
   the Container Apps Consumption vCPU)

That exceeded my `curl --max-time 30` and returned `Connection reset by
peer` after ~1162 s when I forgot to bump the timeout. Subsequent requests
took 136 ms. For v1 with no real users this is fine, but iOS clients will
need either:
- `minReplicas=1` to keep one warm replica (cost: ~$5–10/mo Consumption), or
- Move `warm_embedder()` to a background task and let `/v1/identify` 503
  until ready (already the contract, per D-019).

Documented in the decision file as an operational note; will revisit if the
D-025 eval P99 baseline regresses on real iOS traffic.

### 2026-05-16 — Verified SigLIP model id (NOT siglip-so400m)

The task spec referenced `google/siglip-so400m-patch14-384` and said
"verify the exact id in services/ml/". Verified: the actual default is
`google/siglip-base-patch16-224` (768-dim) — set in
`services/ml/ml/embeddings.py::DEFAULT_EMBEDDER_NAME` and
`services/api/app/embedding.py::EMBEDDING_MODEL_VERSION`, locked by D-015.
The Dockerfile bundles the base variant (~400 MB weights, ~1 GiB final
image). If/when we swap to so400m, bump `EMBEDDING_MODEL_VERSION`,
`DEFAULT_EMBEDDER_NAME`, the Dockerfile `ARG SIGLIP_MODEL`, and the
schema migration that bumps the vector column dimension (768 → 1152) in
lockstep, then rebuild v2 and tighten the D-025 baseline.
