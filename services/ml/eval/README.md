# services/ml/eval — Retrieval Eval Harness

Automated recall@k + latency tracking for the art-guide retrieval pipeline.
Turns "I tested it once with curl" into "I'd notice if a model swap or schema
change broke quality."

---

## Quick start

```bash
# From repo root, with the local stack running:
docker compose -f infra/docker-compose.yml up -d   # Postgres
cd services/api && uvicorn app.main:app --reload &  # API

cd services/ml
art-guide-ml eval
```

The run writes a timestamped JSON report + markdown summary to
`services/ml/eval/baseline-TIMESTAMP.{json,md}` and prints the markdown
to stdout.

**Default rate limit caveat:** The server allows 10 req/5 min by default.
For 45 test cases you either:
- Set `RATE_LIMIT_REQUESTS=100` in `services/api/.env` before starting the server, **or**
- Pass `--request-delay-s 32` (adds ~25 min to the run), **or**
- Use `--in-catalog-only` (35 cases — still hits the limit but passes ~3 windows).

For CI, set `RATE_LIMIT_REQUESTS=200 RATE_LIMIT_WINDOW_SECONDS=60` in the
server environment before running.

---

## CLI flags

```
art-guide-ml eval [OPTIONS]

  --api-url URL         Base URL of the API (default: $ART_GUIDE_API_URL or
                        http://localhost:8000)
  --api-key TOKEN       Bearer token (default: $ART_GUIDE_API_KEY or
                        $API_BEARER_TOKEN; omit for local dev with auth disabled)
  --dataset PATH        Path to dataset.jsonl (default: this directory)
  --thresholds PATH     Path to thresholds.yaml (default: this directory)
  --output-dir DIR      Where to write reports (default: this directory)
  --in-catalog-only     Skip out_of_catalog cases (faster, no Wikimedia downloads)
  --request-delay-s N   Sleep N seconds between requests (default: 0)
  --dry-run             Print cases without making HTTP requests
```

---

## Dataset format (`dataset.jsonl`)

One JSON object per line. Schema:

| Field | Type | Description |
|---|---|---|
| `image_url` | string | URL to download the test image. |
| `expected_artwork_id` | string \| null | The artwork ID we expect in the top-1 candidate. Null for out-of-catalog cases. |
| `expected_status` | string | Expected `match.status`: `exact`, `likely`, `style_only`, or `no_match`. |
| `category` | string | `exact` — exact source image from catalog; `perturbed` — same image with transformation applied; `out_of_catalog` — not in the index. |
| `perturbation` | string \| null | Transformation applied at eval time: `rotate_5`, `crop_10pct`, `brightness_120`, `contrast_80`, `color_90`. |
| `notes` | string | Human-readable description of the case. |

### Dataset composition (v1 bootstrap — 45 cases)

| Category | Count | Description |
|---|---|---|
| `exact` | 30 | Met source images for 30 artworks in the 100-record catalog. These use the exact `image_url` stored in the DB; should score cosine ≈ 1.0 and return `exact`. |
| `perturbed` | 5 | Same 5 catalog images with PIL transformations (rotate 5°, crop 10%, brightness ×1.2, contrast ×0.8, saturation ×0.9). Designed to stay `exact` — if they drop to `likely`, the embedder is sensitive to common photography artefacts. |
| `out_of_catalog` | 10 | 5 famous paintings not in the catalog (Starry Night, Impression Sunrise, Girl with Pearl Earring, Night Watch, Creation of Adam) + 5 non-art photos. Should return `style_only` or `no_match`; returning `exact` with a wrong ID is a hard failure of the confidence model. |

### Adding cases

1. Add a JSON line to `dataset.jsonl`.
2. If the case is in-catalog, set `expected_artwork_id` to the `met:XXXXXX` ID from the DB.
3. Verify the URL is reachable: `curl -sS -o /dev/null -w "%{http_code}" "<URL>"`.
4. Re-run `art-guide-ml eval` to update the baseline.
5. Commit both `dataset.jsonl` and the new `baseline-*.json`.

---

## Metrics

| Metric | Definition | Scope |
|---|---|---|
| **recall@1** | `top-1 candidate id == expected_artwork_id` | `exact` + `perturbed` cases only |
| **recall@3** | `expected_artwork_id in top-3 candidate ids` | `exact` + `perturbed` cases only |
| **status_accuracy** | `returned match.status == expected_status` | All non-skipped cases |
| **latency_p50/p95/p99** | Wall-clock ms per request (image download + API call) | All non-skipped cases |

---

## Threshold rationale (`thresholds.yaml`)

Starting bars are deliberately permissive for a 100-record catalog:

| Threshold | Value | Rationale |
|---|---|---|
| `recall_at_1` | 0.80 | Exact source images should almost always match. Allows a small buffer for CDN-recompressed images. |
| `recall_at_3` | 0.90 | Top-3 should be near-perfect when the exact image is indexed. |
| `status_accuracy` | 0.65 | Out-of-catalog expected values are best-guess; allow misclassification. Tighten as dataset grows. |
| `latency_p99_ms` | 5000 | Generous for local dev; includes image download. Tighten to 2000 ms once Azure latency is baselined. |

**When to tighten:**
- After catalog grows to 10K+ records, raise `recall_at_1` to 0.90.
- After Azure baseline is captured, lower `latency_p99_ms` to 2000.
- After out-of-catalog labels are verified by hand, raise `status_accuracy` to 0.75.

---

## Interpreting results

**Three test classes (per docs/data-model.md):**

1. **In-catalog exact**: Should hit `exact` with cosine ≈ 1.0. If recall@1 < 0.95 here, the embedding pipeline or DB is broken — not a model quality issue.

2. **Perturbed in-catalog**: Should stay `exact`. If they drop to `likely`, the embedder is losing precision on common gallery-photo artefacts. Investigate the `perturbation` field in the JSON report to find which transform is causing degradation.

3. **Out-of-catalog**: Should NOT return `exact` with a wrong artwork ID. If they do, the confidence thresholds in `docs/data-model.md` are too loose. The `style_only` / `no_match` distinction is a best-guess in the dataset; `no_match` when `style_only` is expected is acceptable.

---

## CI integration

See [`docs/eval-ci.md`](../../docs/eval-ci.md) for the GitHub Actions sketch.

---

## Layout

```
eval/
├── README.md              this file
├── __init__.py
├── dataset.jsonl          45 test cases (30 exact + 5 perturbed + 10 out-of-catalog)
├── run_eval.py            the harness
├── thresholds.yaml        gating thresholds
└── baseline-*.json        baseline run snapshots (gitignored except the initial one)
```
