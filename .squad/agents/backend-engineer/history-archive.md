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
