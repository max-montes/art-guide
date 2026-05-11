# Backend Engineer History (Current)

> **Learnings from prior iterations archived to `history-archive.md`. Current file focuses on the latest completed work.**

## Current Status

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
