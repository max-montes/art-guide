# Backend Engineer History (Current)

> **Learnings from prior iterations archived to `history-archive.md`. Current file focuses on the latest completed work.**

## Current Status

**Phase 1 core path operational:** `/v1/identify` end-to-end pipeline wired (embed → retrieve → LLM → format). Postgres+pgvector schema in place, migrations runnable, embedder cache pattern baked in, LLM guardrails guard-railed. 65 tests passing. Met ingest pipeline (ml-retrieval-engineer) now wired — artworks table ready to populate (D-018).

### What's next

- Run the full Met ingest to populate the `artworks` table.
- Wiring `/v1/artworks/{id}` detail endpoint + frontend integration tests.
- Per-request structured logging (D-012).
- LLM Azure OpenAI error handling + cost monitoring.

**⚠️ Data pipeline ready:** ML has wired `art-guide-ml ingest met [--department-ids ID,ID,…]` → `services/ml/ml/ingest/met_db.py`. CLI produces records conforming to canonical `NormalizedArtwork` schema; `artworks` table is ready to be populated. Decision: D-018.

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
