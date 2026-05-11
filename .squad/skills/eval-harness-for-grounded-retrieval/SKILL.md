# Skill: Eval Harness for Grounded Retrieval

**Confidence:** Low (single observation, 2026-05-10)  
**Observed in:** art-guide Phase 1 eval bootstrap  
**Author:** ml-retrieval-engineer  

---

## Core insight

When shipping a retrieval system, the test set must contain **three distinct case classes**. Without
all three, you cannot tell whether the confidence enum is calibrated:

**(a) In-catalog cases** — exact source images that should hit `exact` with cosine ≈ 1.0.  
These verify the happy path: ingest → embed → ANN search round-trips correctly. If these fail,
something broke in the pipeline (schema migration, embedder version bump, CDN URL change).

**(b) Perturbed in-catalog cases** — same source images with minor transforms (rotate ~5°, crop
~10%, brightness/contrast/saturation shifts ±20%). Should still return `exact` or `likely`.  
These verify that the embedder is not a hash function — that minor photographic variation (handheld
camera tilt, museum lighting) doesn't destroy recall. If perturbed cases start returning `style_only`
or `no_match`, the embedder has regressed in robustness.

**(c) Out-of-catalog cases** — images the catalog has never seen (famous works from other
museums, non-art photos). Should return `style_only` or `no_match`.  
These verify the negative side of the confidence model. Without them, you can't tell if a threshold
change accidentally makes the system over-confident (naming artworks for things it's never seen).

---

## Why this matters for grounded retrieval specifically

Hard rule #1 in art-guide: **the LLM does not own facts**. It may only explain fields present in
the retrieved record. If the confidence model is miscalibrated (e.g., `style_only` images get
promoted to `exact`), the LLM will produce grounded-but-wrong explanations — the worst failure mode,
because it looks correct.

The three-class test set makes miscalibration visible:
- Class (a) catches recall regression.
- Class (b) catches robustness regression.
- Class (c) catches false-positive / over-confidence regression.

---

## Practical lessons

- **Rate limits bite.** The local API enforces 10 req/5 min. At 45 cases that's ~26 min with a
  32 s inter-request delay. In CI, raise the server-side rate limit before running eval.
- **Met CDN requires browser-like headers.** The same `User-Agent: art-guide-ml/0.1` that works
  for the Collection API earns 406s on `images.metmuseum.org`. Use Safari UA + `Accept: image/...`
  for image downloads (same fix as ingest pipeline).
- **Start permissive on thresholds.** recall@1 ≥ 0.80, status_accuracy ≥ 0.65. The first baseline
  with a small catalog will likely be 1.000 everywhere; don't lock to that — future catalog growth
  changes the ANN neighborhood and may shift confidence scores slightly.
- **Exact source images score ≈ 1.0 but never exactly 1.0 after quantization.** Floating-point
  rounding in the pipeline means the round-trip cosine is 0.9999... not 1.0000. Don't assert exact
  equality; assert ≥ threshold.

---

## Template for future retrieval systems

```python
# dataset.jsonl structure
{
  "image_url": "...",
  "expected_artwork_id": "source:123",  # null for out-of-catalog
  "expected_status": "exact",           # exact | likely | style_only | no_match
  "category": "exact",                  # exact | perturbed | out_of_catalog
  "perturbation": null,                 # rotate_5 | crop_10pct | brightness_120 | ...
  "notes": "human-readable description"
}
```

Metric targets (bootstrap):
- `recall@1 ≥ 0.80`
- `recall@3 ≥ 0.90`
- `status_accuracy ≥ 0.65`
- `latency_p99_ms ≤ 5000`
