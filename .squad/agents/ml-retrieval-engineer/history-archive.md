# ML/Retrieval Engineer History Archive

> Older work and learnings archived to keep current history concise.

## Phase 0 — ML Package Foundation (2026-05 early)

### Done
- Established the ML package skeleton at `services/ml/` with `pyproject.toml`, `.env.example`, `README.md`.
- Defined the canonical `NormalizedArtwork` Pydantic model in `services/ml/ml/schema.py`.
- Created the pluggable `IngestionAdapter` base in `services/ml/ml/ingest/base.py` and the concrete Met adapter in `services/ml/ml/ingest/met.py` (filters to public-domain paintings/sculptures with images; backoff/retry logic; JSONL output).
- Added the CLI at `services/ml/ml/cli.py` with `ingest` and `augment` subcommands.
- Built the eval scaffold at `services/ml/evals/`:
  - `evals/datasets/retrieval-v1.jsonl` and `evals/datasets/explanations-v1.jsonl` (placeholders + one real example each).
  - `evals/evaluators/retrieval_metrics.py` (top-K accuracy, MRR with self-tests).
  - `evals/evaluators/field_citation.py` (every named field must exist in the retrieved record).
  - `evals/evaluators/confidence_honesty.py` (hedging required for `likely`/`style_only`; no claims for `no_match`; "resembles"-style required when naming an artist in `style_only`).
- Added the augmentation utility at `services/ml/ml/eval_data/augment.py` (random crop, rotation, brightness/contrast jitter, perspective warp, simulated glare overlay) with manifest output.
- Implemented the centralized image pipeline at `services/ml/ml/imageops.py` (`prepare_for_embedding`, `prepare_from_path`, `prepare_from_url`) per `docs/image-pipeline.md`. Pytest covers shape, channel, EXIF orientation, padding, and normalization. HEIC supported via `pillow-heif`.

### 2026-05-10 — Phase 1 embedding benchmark + pick

**What ran (all on Apple Silicon, PyTorch 2.11, CPU-only):**
- `google/siglip-base-patch16-224` — 768-d, R@1=1.000, R@5=1.000, top1–top2 gap 0.0975, p50=89 ms, p95=120 ms.
- `laion/CLIP-ViT-B-32` — 512-d, R@1=0.983, R@5=1.000, gap 0.1415, p50=51 ms, p95=83 ms.
- `facebook/dinov2-base` — 768-d, R@1=1.000, R@5=1.000, gap 0.1839, p50=89 ms, p95=118 ms.

**Eval:** 15 Met references × 4 augmented queries each = 60 queries. No extrapolation; all checkpoints ran end-to-end.

**Pick:** `siglip-base-224`, D=768. Tiebreaker against DINOv2 is vision-language alignment. SigLIP's tighter gap is a known risk; D-005 thresholds tuned once a larger eval set exists.

**Key files:**
- `services/ml/ml/embeddings.py` — `EmbedderSpec`, `_HFEmbedder`, `get_embedder()`, `DEFAULT_EMBEDDER_NAME`.
- `services/ml/evals/embed_benchmark.py` — reproducible harness + markdown reports.
- `services/ml/tests/test_embeddings.py` — smoke tests.
- Decision written: inbox/ml-retrieval-engineer-embedding-pick.md (merged as D-015).

### 2026-05-10 — Met → embed → DB ingest landed

**Shipped:** `services/ml/ml/ingest/met_db.py` end-to-end loop: discover → fetch → filter (public-domain + has-image + paintings/sculpture) → download in-memory → embed → batched upsert.

**CLI:** wired into `art-guide-ml ingest met`. Flags:
- `--limit N` (default 100 in DB mode)
- `--dry-run` (fetch + download + embed, no DB writes)
- `--database-url DSN`
- `--batch-commit-size N` (default 50)
- `--department-ids 11,21` (Met department pre-filter)
- `--jsonl-out PATH` (legacy normalize-only)

**Smoke test:** `art-guide-ml ingest met --limit 1 --dry-run` against live Met API. 501K+ candidates, one (met:466) passed filter, embedded with `vec_norm=1.0000`.

**Schema fit:** `thumbnail_url`, `license`, `license_url` live in `raw_metadata` (jsonb).

**Idempotency:** ON CONFLICT `(source, source_id)` — re-runs refresh embeddings.

**Tests:** 16 new unit tests on `map_met_record`; total 31 tests pass.

### Learnings — Met API quirks

- `GET /objects?isPublicDomain=true&hasImages=true` returns entire ID list at once (501K+). No pagination. Must use `--department-ids` for practical runs. Useful ids: 11 (European Paintings), 21 (Modern & Contemporary), 4 (American Decorative Arts), 13 (Greek & Roman).
- `classification` is permissive (Sculpture, Statuettes, Reliefs, Busts, etc.); substring match intentionally broad to avoid dropping real records.
- Per-object 403s appear sporadically; backoff + retry handles them — don't treat as deny.
- No published rate limit; kept at ~3.5 req/s sustained (0.15s per request).
- `objectURL` reliable; added fallback to constructed URL just in case.
- Batch commit 50 / txn plenty; bottleneck is HTTP + embed (~250–400 ms). DB write is microseconds.
- Embedder cached per-process; never reload inside loop.
