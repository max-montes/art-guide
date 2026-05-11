---
name: "embedder-factory"
description: "One-seam embedder wrapper so swapping embedding models is a one-character change"
domain: "ml-retrieval"
confidence: "high"
source: "earned (art-guide Phase 1 embedding benchmark — D-009)"
---

## Context

When a system depends on a pretrained embedding model (image, text, multimodal), the choice of model is uncertain at design time and likely to change as new models ship. Letting model-specific calls (`AutoProcessor`, `CLIPModel.get_image_features`, `open_clip.create_model_and_transforms`, etc.) leak into ingestion / query code makes every swap a multi-file diff and risks preprocessing drift between paths. This skill is the pattern that prevented that for `art-guide`.

## Patterns

- **One file, one factory.** Expose exactly one entry point — `get_embedder(model_name=None)` — that the rest of the codebase calls. Default to a module-level `DEFAULT_EMBEDDER_NAME` constant so swapping the production model is a one-character change. Other candidates stay registered (and benchmarkable) but not default.

- **Spec / runtime separation.** Keep an immutable `EmbedderSpec` dataclass per model (`name`, `hf_repo`, `family`, `input_size`, `dim`, `mean`, `std`, `pad_to_square`). Schema migrations and benchmarks read `spec.dim` without instantiating the model — critical for unblocking parallel work like sizing a `vector(D)` Postgres column.

- **Route every preprocessing call through the project's single image pipeline.** Never call the third-party processor (`AutoProcessor`, `CLIPProcessor`, `open_clip.preprocess`). Instead, pass the model's published `mean`/`std`/`size` into the project's `prepare_for_embedding` (or equivalent). This is the only way to guarantee ingest-time and query-time preprocessing don't drift.

- **L2-normalize at the embedder boundary.** Cosine similarity becomes a dot product, downstream confidence math can assume normalized vectors, and tests can assert `‖v‖ ≈ 1` as a contract.

- **Process-local cache keyed on `(name, device)`.** Repeated `get_embedder()` calls in one process don't reload weights. Expose a `reset_cache()` helper so tests can swap models cleanly.

- **Family-uniform forward.** Pick the smallest set of family branches inside `_forward(pixel_values)` (e.g. for HF `transformers >= 5`, all of SigLIP / CLIP / DINOv2 expose `outputs.pooler_output` as the projected image embedding). Push family-specific glue into the spec, not the runtime.

- **Make the wrapper benchmarkable.** Co-locate a small benchmark harness that scores **all** registered specs (Recall@K, mean top1–top2 gap for confidence calibration, p50/p95 single-embed latency). Re-running the benchmark must be one CLI command and must write a dated markdown report to a results dir.

- **Surface the wrapper end-to-end via the CLI** (e.g. `<tool> embed <path>`) that prints model name, dim, L2 norm, and the head of the vector. Cheap proof that the swap point really works.

## Examples

✓ **Correct:**

```python
# Single seam — only place model-specific code lives.
from project.embeddings import get_embedder, get_spec

# Backend can size the vector column without loading weights.
D = get_spec("siglip-base-224").dim   # -> 768

# Ingestion + query both go through the same factory.
vec = get_embedder().embed_bytes(image_bytes)   # (768,), float32, L2=1.0
```

```python
# Swap is a one-character change in the embeddings module.
DEFAULT_EMBEDDER_NAME = "dinov2-base"   # was "siglip-base-224"
```

✗ **Incorrect:**

```python
# Two parallel preprocessors → silent drift between ingest and query.
from transformers import CLIPProcessor, CLIPModel
processor = CLIPProcessor.from_pretrained(repo)   # NOT going through prepare_for_embedding
inputs = processor(images=img, return_tensors="pt")

# Model name hardcoded in the ingestion job → swap is multi-file.
model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
```

## Anti-Patterns

- **Bypassing the project's image pipeline.** Using each model's bundled processor splits preprocessing in two and drift will silently degrade retrieval (it won't fail loudly).
- **Returning un-normalized vectors.** Forces every caller to normalize, makes `ref_matrix @ q_vec` no longer cosine, breaks confidence math.
- **Skipping the spec object.** Without an `EmbedderSpec`, downstream code has to instantiate the model just to read `dim` — and Postgres schema work blocks until model weights download.
- **Hard-coding the chosen model name across the codebase.** The point of this pattern is one place to change. Reference `DEFAULT_EMBEDDER_NAME` everywhere else.
- **Picking the model on accuracy alone when accuracy is saturated.** When R@1 = 1.0 for two candidates on a small benchmark, fall back to secondary criteria (top1–top2 gap for confidence calibration, vision-language alignment for future product directions, license, model size). Document the tiebreaker.
