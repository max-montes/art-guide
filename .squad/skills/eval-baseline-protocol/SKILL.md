---
name: "eval-baseline-protocol"
description: "Freeze timestamped baseline metrics for retrieval/confidence system; comparison anchor for catalog growth and model changes"
domain: "ml-retrieval"
confidence: "medium"
source: "earned (art-guide 100-record prod eval — 2026-05-16)"
---

## Context

A retrieval system's performance shifts as the catalog grows, thresholds change, or the embedder is swapped. Without a frozen baseline, you can't tell if a metric change is progress or regression. This skill captures the "freeze metrics + interpretation" workflow, including which surprises to look for when the catalog is a small sample of the eventual full catalog.

## Patterns

- **Timestamp everything.** Baseline files are named `baseline-{ISO8601Z}.json` and `baseline-{ISO8601Z}.md`. This makes chronological comparison trivial and avoids "which baseline is current?" questions. The timestamp comes from `datetime.now(timezone.utc)` inside the harness — never from the filename you give it.

- **Annotate catalog state in the file, not just the timestamp.** The JSON report should include the API URL, dataset path, and number of cases. A companion markdown file should note the catalog size at the time of the run (e.g., "100 records, prod" or "501K records, prod"). Metrics without catalog context are meaningless.

- **Three metric classes matter; capture all three:**
  1. **Retrieval accuracy** — `recall@1`, `recall@3` (in-catalog cases only). Expect near-1.0 when the exact artworks are in the catalog; expect lower when catalog is a sample.
  2. **Confidence calibration** — `status_accuracy` per tier. "exact" cases returning "likely" when the artwork isn't in the catalog is expected and acceptable; "exact" cases returning "exact" for the WRONG artwork is a miscalibration signal.
  3. **Latency** — `p50`, `p95`, `p99` wall-clock (download + API). Warm path only; cold-start is documented separately.

- **Run against prod, not local, for the canonical baseline.** Local Docker Compose has smaller I/O budgets and fewer replicas. The prod baseline is the one Brady (the user) will experience.

- **Rate limit awareness.** Prod APIs often enforce 10 req/5 min. At 45 cases with 32 s inter-request delay, one eval pass takes ~24 minutes. CI rate limits must be relaxed before automated runs. Do NOT run eval without `--request-delay-s 32` (or higher) against prod.

- **Errors must not be silently ignored.** Auth failures (401) appear as `error` fields in the case output. A baseline run where 3/45 cases errored on auth is NOT a 42-case baseline — it's a broken run. Check `n_skipped` in the metrics and re-run if necessary. Fix the auth before pinning.

- **The "exact status / wrong ID" failure mode is the key calibration signal.** If `status=exact` but `recall@1=False`, the system is reporting high confidence for the WRONG artwork. This is worse than `status=likely` with wrong ID, because the UI would show definitive attribution. Track this as `false_exact_count` in the interpretation markdown.

- **Interpretation markdown is required alongside the JSON.** The JSON has numbers; the markdown explains why. At minimum, answer:
  - What fraction of in-catalog cases were actually in the catalog (if catalog is a sample)?
  - Did any "exact" returns have wrong IDs? If so, what's the cosine score distribution?
  - What to watch as catalog grows (lower bound on recall improvement, risk of threshold drift)?

- **Pin to the canonical baselines path.** New files go in `services/ml/eval/baselines/`. Gitignore does NOT exclude this directory (the JSON + markdown are code, not data). The eval images are gitignored (`services/ml/eval/data/`).

## Running the harness

```bash
cd services/ml

# Full eval (45 cases, ~24 min with rate-limit delay)
python3 -c "
from pathlib import Path
from eval.run_eval import run_eval
exit_code, metrics, results = run_eval(
    api_url='https://art-guide-prod-api.kindglacier-84ffc0b4.westus3.azurecontainerapps.io',
    api_key='<token from KV>',
    output_dir=Path('eval/baselines'),
    request_delay_s=32,
    in_catalog_only=False,  # include out_of_catalog cases for negative-side calibration
)
print(f'recall@1={metrics.recall_at_1:.3f} recall@3={metrics.recall_at_3:.3f}')
print(f'status_accuracy={metrics.status_accuracy:.3f}')
print(f'p50={metrics.latency_p50_ms:.0f}ms p95={metrics.latency_p95_ms:.0f}ms')
"

# In-catalog-only (faster, ~15 min, no Wikimedia downloads)
# Art-guide-ml CLI shortcut:
art-guide-ml eval --api-url https://... --in-catalog-only --request-delay-s 32
```

## Key outputs

The harness writes two files per run:
- `baseline-{timestamp}.json` — machine-readable: all case results + aggregated metrics + thresholds
- `baseline-{timestamp}.md` — human-readable: pass/fail summary, per-category breakdown, threshold table

Also write a hand-authored interpretation at `baselines/{date}-{catalog-size}.md`:
```markdown
## Baseline: YYYY-MM-DD — {N} records

### Numbers
| Metric | Value |
|--------|-------|
| recall@1 | 0.xxx |
| recall@3 | 0.xxx |
| status_accuracy | 0.xxx |
| p50 | xxxms |
| p95 | xxxms |

### What's good
...

### What's surprising
...

### What to watch as catalog grows
...
```

## Metrics to track across catalog growth

| Metric | 100-record baseline | Expected at 500K |
|--------|---------------------|-----------------|
| recall@1 (in-catalog) | Low (most test artworks not in sample) | High (≥0.85 if eval artworks are in 500K) |
| recall@3 | Similar pattern to recall@1 | High |
| status_accuracy | Degraded (artworks not in catalog get "likely" not "exact") | Improved |
| false_exact_count | Watch closely — artworks in same artist/style may score above exact threshold | Should drop (correct artwork will be nearest) |
| p50 latency | ~2000ms warm | Stable (HNSW is sub-linear) |

## Calibration danger zones

- **false_exact**: `status=exact AND recall@1=False`. Count these. If count > 2% of in-catalog test cases, exact threshold is too low for current catalog density.
- **style_only for in-catalog**: `status=style_only AND expected_status=exact`. Means the embedder failed badly — investigate image preprocessing or model version.
- **out_of_catalog artworks getting "exact"**: `expected_status=no_match AND returned_status=exact`. Hard failure — the negative side is broken.
