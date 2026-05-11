# ML/Retrieval Engineer History

## Done so far

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

## Notes on what exists vs. what's missing

- No embedding model integrated yet. Selection criteria are in `docs/embeddings.md`. Phase 1 work picks one.
- No vector store implementation yet. Postgres + `pgvector` is the chosen direction; HNSW + cosine.
- No real Met data yet — ingestion CLI works but hasn't been run beyond small samples.

## Next for me

1. ~~Add `services/ml/ml/embeddings/` with a `BaseEmbedder` interface and at least one concrete embedder (SigLIP-class) to start benchmarking.~~ **Done 2026-05-10.** See `services/ml/ml/embeddings.py` and Phase 1 benchmark below.
2. Wire `imageops.prepare_for_embedding` into the Met ingestion path for embedding generation.
3. Implement `services/ml/ml/vector_store.py` with a `pgvector`-backed `ArtworkVectorStore` (insert and ANN search). Awaiting Postgres schema from backend.
4. Bootstrap a real augmented eval set against Met reference images so future retunes are meaningful (current 15-ref sample is a smoke test).

## Working rules

- Conform to `docs/data-model.md` for `NormalizedArtwork` shape and confidence model.
- Conform to `docs/image-pipeline.md` for any image processing.
- Don't store original images. Metadata + URL + embedding only.
- Don't fine-tune in v1. Out of scope per D-013.
- Coordinate response shapes with the Backend Engineer before changing anything that touches the API.

## Learnings

### 2026-05-10 — Phase 1 embedding benchmark + pick

**What ran (all on this machine, CPU-only, Apple Silicon, PyTorch 2.11):**

- `google/siglip-base-patch16-224` — loaded, 768-d, R@1=1.000, R@5=1.000, top1–top2 gap 0.0975, p50=89 ms, p95=120 ms.
- `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` — loaded via HuggingFace `CLIPModel`, 512-d, R@1=0.983, R@5=1.000, gap 0.1415, p50=51 ms, p95=83 ms.
- `facebook/dinov2-base` — loaded, 768-d, R@1=1.000, R@5=1.000, gap 0.1839, p50=89 ms, p95=118 ms.

**Nothing was extrapolated.** All three checkpoints downloaded and ran end-to-end. Eval was 15 Met Open Access references (in-memory only, never persisted) × 4 augmented queries each = 60 queries total. Augmentation routed through the existing `ml.eval_data.augment` helpers; preprocessing routed through `ml.imageops.prepare_for_embedding` (one-pipeline rule honored, no parallel preprocessor).

**Pick:** `siglip-base-224`, locked as `DEFAULT_EMBEDDER_NAME` in `services/ml/ml/embeddings.py`. Tiebreaker against DINOv2 (which has a cleaner top1–top2 gap) is vision-language alignment — it keeps the door open for future text queries without a re-index. SigLIP's tighter confidence gap is a known risk; D-005 thresholds may need a retune once a larger eval set exists. Swapping to DINOv2 is a one-character change.

**Embedding dimension D = 768.** This unblocks the backend's `vector(D)` column on the Postgres `artworks` table.

**On-device/runtime notes:**

- HuggingFace `transformers >= 5` changed `get_image_features` for SigLIP/CLIP to return a `BaseModelOutputWithPooling`. `_HFEmbedder._forward` reads `pooler_output` uniformly across the three families.
- All three are loaded with `transformers.AutoModel` except OpenCLIP, which uses `CLIPModel` directly because the LAION repo is a pure CLIPModel checkpoint.
- Weight on disk (the `.safetensors` blob, not the cache directory total): SigLIP 812 MB, OpenCLIP 605 MB, DINOv2 346 MB. For SigLIP / OpenCLIP we only need the vision tower in production, so real footprint is materially smaller (~370 MB and ~150 MB respectively).

**Key file paths added/changed:**

- `services/ml/ml/embeddings.py` — new. `EmbedderSpec`, `_HFEmbedder`, `get_embedder()`, `list_specs()`, `DEFAULT_EMBEDDER_NAME`.
- `services/ml/evals/embed_benchmark.py` — new. Reproducible harness; writes a markdown report per run to `services/ml/evals/results/`.
- `services/ml/ml/eval_data/augment.py` — added `augment_pil(image, rng)` so the augmentation chain is reusable in-memory; `augment_image` now delegates to it.
- `services/ml/ml/cli.py` — new `embed` subcommand; `art-guide-ml embed <image>` prints model name, shape, L2 norm, head.
- `services/ml/tests/test_embeddings.py` — new smoke tests (skip-if-uncached for the model-loading paths).
- `services/ml/pyproject.toml` — added `torch`, `transformers`, `sentencepiece` to runtime deps.
- `services/ml/README.md` — documented the new CLI subcommand and benchmark.
- `docs/embeddings.md` — appended `## Benchmark — Phase 1 selection` section with table, rationale, and caveats.
- `.squad/decisions/inbox/ml-retrieval-engineer-embedding-pick.md` — decision write for the coordinator.

## 2026-05-11 — Backend: Postgres schema live (D-016)

**From backend-engineer:** Artworks table is now deployed:
- `vector(768)` column for SigLIP embeddings (D-015 locked).
- HNSW cosine index with `m=16, ef_construction=64`.
- Idempotent upsert via `UNIQUE(source, source_id)`.
- `app.db.ArtworkRepository` skeleton live with stub surface (bodies to land when ingest wires up).
- Migration runner ready: `python -m app.migrations` to apply pending; auto-migrate opt-in locally, never in prod.

**Your next:** Wire `imageops.prepare_for_embedding` into the Met ingest path to generate embeddings. Implement `services/ml/ml/vector_store.py` (`ArtworkVectorStore` with insert + ANN search). Bootstrap real augmented eval set from Met references. Then ingest → DB pipeline is complete.

