---
name: "exercise-the-real-path"
description: "Real-code-path tests catch what import / mock / skip-if-uncached tests miss"
domain: "testing"
confidence: "high"
source: "earned (art-guide ML — SigLIP `pooler_output` regression shipped under green CI, 2026-05-10)"
---

## Context

Tests that *look* like they cover a feature but actually short-circuit (import only, mock the seam, or skip when external resources are missing) generate false confidence. The bug they would have caught ships, and post-mortems reach the wrong conclusion ("we have tests for that"). This skill is the discipline that prevents the next instance.

The pattern that bit `art-guide`: a SigLIP embedder unit test was guarded by `@pytest.mark.skipif(not _hf_cache_has(repo))`. In any environment without warmed weights, the test *silently skipped* and the line `outputs.pooler_output` (which is broken — `get_image_features` returns a `Tensor`, not an output object) was never executed. The first time the code ran for real was during a 100-record production ingest, and every embed crashed.

## Patterns

- **For each non-trivial seam, at least one test must execute the real call.** "Real" means: real model loaded (or a tiny in-process surrogate that hits the same code), real HTTP request (or a recorded fixture replayed through the actual client), real DB INSERT (or a transactional rollback against a real engine). Import smoke tests, type checks, and processor-mocked tests are *additions*, not replacements.

- **Default to running the test, not skipping it.** A skip condition should require an explicit opt-out (e.g. `ART_GUIDE_SKIP_MODEL_TESTS=1`), not the absence of an opt-in (e.g. `if cache_warm`). Reverse the polarity: in a fresh dev env or fresh CI runner, the test should *run* — and download / install / build whatever it needs — rather than vanish from the report.

- **Skip messages are second-class output.** `pytest -v` shows them once; `pytest --tb=short` and most CI summaries show them as green. Treat any `SKIPPED` as a yellow flag worth checking. If you can't run a test in CI for legitimate reasons (cost, secrets, hardware), put it behind a *separate* nightly job and surface its result, don't bury it in the per-PR run.

- **Assert the actual contract, not just "didn't crash".** For an embedder: shape, dtype, L2 norm ≈ 1.0. For an HTTP client: status code, headers, parsed body shape. For a DB writer: row count, returned id, idempotency on second call. "No exception" is too weak — `_forward` could have returned `None` and the original test would still have passed.

- **When fixing a production bug, write the test that would have failed first.** Then the diff has both the regression test and the fix; the test serves as the regression marker forever after. If you skip this step, you have proven nothing about the next time the same shape of bug shows up.

- **Be suspicious of tests that pass too fast.** A 0.01 s "embedder test" almost certainly isn't loading a real embedder. A 0.001 s "DB integration test" almost certainly isn't talking to a DB. If the test runtime doesn't reflect the work the production code does, the test isn't doing the work either.

- **Mocks belong at the *outer* boundary of the system under test, not at the inner one.** Mock the museum API (outer); don't mock your own embedder seam (inner) when the seam is what you're testing. Mocking the inner seam is how `_forward` got "tested" without ever being called.

## Smell list (drop test or upgrade it)

- `@pytest.mark.skipif(not <local resource present>)` on a unit test — invert to `skipif(<env opt-out flag>)`.
- A test whose body imports the module under test and asserts nothing else.
- A test that mocks the function it is named for (`test_foo` that replaces `foo` with a `MagicMock`).
- A test that asserts `result is not None` and stops there.
- A test that runs in <10 ms when the real code path involves network, disk, or a model load.

## Recipe

1. Identify the line that broke in production.
2. Trace upward to the nearest public API or CLI entry point.
3. Find or write a test that calls that entry point with realistic inputs (real bytes, real tensors, real fixtures).
4. Assert at least one property that depends on the broken line being correct (shape, norm, status, row count, etc.).
5. Confirm the test fails *before* your fix and passes *after*.
6. Make sure the test runs in the default `pytest` invocation — no opt-in env var, no cache warmup precondition.

## Cost / when to relax

- Hardware-bound tests (GPU-only, > 30 s on CPU) genuinely belong in a nightly lane. The relaxation is moving them, not skipping them silently.
- Network-bound tests that require paid APIs or rate-limited endpoints belong behind a recorded-fixture layer (e.g. `vcrpy`), not a `skipif`.
- The skill is "execute the path", not "execute it on every developer's laptop". The goal is that *some* automated run, on *every* PR, exercises the real code — not that every contributor pays the load cost locally.

## Confirmations

Three production bugs caught by this skill in a single day (2026-05-10):

1. **SigLIP ingest `pooler_output` crash** — Unit test skipped silently; `.pooler_output` was never called, so the wrong assumption (`get_image_features` returns an output object) shipped under green CI. Real ingest crashed on first batch.

2. **SigLIP query `BaseModelOutputWithPooling` crash** — Route test used `_FakeEmbedder` mock instead of calling `_forward` with the real model. On `transformers==5.8.0`, `get_image_features` return type changed to `BaseModelOutputWithPooling`, which doesn't have `.detach()`. Real query crashed with 400 (mis-reported as PIL decode).

3. **Validation handler `ValueError` JSON crash** — `POST /v1/identify` with wrong multipart field type (string instead of UploadFile) triggered `ValueError` in Pydantic error ctx. The custom `_validation_handler` passed the error dict directly to `JSONResponse` without sanitizing the Exception object, causing `TypeError` (not JSON-serializable), which returned 500 instead of 400.

All three fixed by writing a test that exercises the real code path with realistic inputs and asserts a property of the result (vector shape/norm, HTTP status, JSON envelope).

**Skill status:** High confidence. Proven pattern across three independent domains (ML ingest, ML query, API error handling). Recommend as mandatory practice for all future seam tests.

