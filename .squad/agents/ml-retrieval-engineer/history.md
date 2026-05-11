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

### 2026-05-10 — Met → embed → DB ingest landed

**Shipped:** `services/ml/ml/ingest/met_db.py` (sibling to the existing fetch-only `met.py`) with the end-to-end loop: discover via `/objects` → fetch `/objects/{id}` → filter (public-domain + has-image + paintings/sculpture classification) → in-memory image download → `embedder.embed_bytes` → batched `INSERT … ON CONFLICT (source, source_id) DO UPDATE`. Image bytes never touch disk; `img_bytes` is dropped immediately after `embed_bytes` returns (D-012 / hard rule #3 honored). The orchestrator routes preprocessing through `prepare_for_embedding` indirectly via `get_embedder()` so the one-pipeline rule still holds.

**CLI:** wired into `art-guide-ml ingest met`. Default behavior is now DB ingest (limit 100 unless overridden). Flags:
- `--limit N` (default 100 in DB mode; unlimited for `--jsonl-out`)
- `--dry-run` (fetch + download + embed without DB writes — used when no Postgres is available)
- `--database-url DSN` (overrides `$DATABASE_URL` and the local docker-compose default)
- `--batch-commit-size N` (default 50)
- `--department-ids 11,21` (Met department pre-filter — see below)
- `--jsonl-out PATH` (legacy normalize-only; preserved for offline data exploration)

**Smoke test result (no Docker available in this env):** ran `art-guide-ml ingest met --limit 1 --dry-run` against the live Met API. The /objects feed returned **501,514** candidate IDs; the script fetched 521 records before one passed the painting/sculpture classifier — `met:466` "Plaque Portrait of Benjamin Franklin" — embedded with `vec_norm = 1.0000` (L2-normalized, as the embedder contract guarantees). One transient 403 in the middle was retried successfully on the first backoff.

**Schema fit:** the artworks columns matched everything we needed except `thumbnail_url`, `license`, and `license_url`. Per the rule "no schema changes without a 0002 migration", those three (plus the full raw Met JSON) live inside `raw_metadata` (jsonb) under keys `thumbnail_url`, `license`, `license_url`, `met`. If Phase 4 adds another source that also wants these as first-class columns, that's a 0002 migration request — flagged in the inbox decision.

**Idempotency:** ON CONFLICT target is `(source, source_id)`. Re-running the script just refreshes the embedding (and any metadata changes from the museum side) for existing rows. Verified by reading the upsert RETURNING `(xmax = 0)` to count inserts vs. updates separately. `id` is rewritten by the conflict clause to keep the namespaced key consistent if anyone ever changes the id format.

**Tests:** 16 new unit tests cover the pure mapper (`map_met_record`) — happy path, every filter rejection branch, optional-field nulling, and `format_pgvector`. Live Met API and live Postgres tests are intentionally omitted (network/DB are operator-driven smoke tests). Total ML suite: 31 green.

## Learnings

### Met API quirks worth knowing

- `GET /objects?isPublicDomain=true&hasImages=true` returns **the entire ID list at once** (501K+ ids). There is no pagination cursor. Without a `departmentIds` filter, the `classification` field is the only way to narrow to paintings/sculpture, and it has to happen client-side per object — that's a ~95% reject rate before you even get to the embedder. **Always pass `--department-ids` for non-trivial runs.** Useful starting set: `11` (European Paintings), `21` (Modern and Contemporary Art), `9` (Drawings & Prints — skip), `4` (American Decorative Arts), `13` (Greek and Roman Art — sculpture-heavy). Confirm exact ids per Met's `/departments` endpoint.
- `classification` is inconsistent: many sculptures are typed as `"Sculpture"`, but plenty come through as `"Statuettes"`, `"Reliefs"`, `"Bust"`, even `"Carvings-Architectural"`. The substring match in `_classification_matches` is intentionally permissive; tightening it loses real records.
- Per-object 403s appear sporadically (saw one at id 454 during smoke test). Backoff + retry handled it; the second attempt was a clean 200. Don't treat them as deny — treat as transient.
- Met has no published rate limit, but at ~3.5 req/s sustained the API stayed healthy for the duration. Keep `request_delay = 0.15s` (≈6 req/s ceiling); revisit if 429s start appearing at scale.
- `objectURL` is reliably present, but I added a fallback that constructs `https://www.metmuseum.org/art/collection/search/{id}` just in case — saw one record in the wild with empty `objectURL`.

### Performance notes

- Batch commit size 50 / txn is plenty: the bottleneck is single-record HTTP + embed (~250–400 ms each at 6 req/s with SigLIP CPU embed taking ~120 ms). DB write is microseconds by comparison. Don't bother increasing past 50; you'll just hold rows in memory longer.
- `asyncio.to_thread(embedder.embed_bytes, ...)` keeps the event loop responsive while PyTorch chews on the image. Without it the Met HTTP fetches stall behind the embed.
- Embedder is cached per-process via `get_embedder()`; never reload weights inside the loop.

### CLI invocation cheat-sheet

```
# Smoke test, no DB needed:
art-guide-ml ingest met --limit 1 --dry-run

# Real local ingest (requires `cd infra && docker compose up -d` first):
AUTO_MIGRATE=true art-guide-ml ingest met --limit 100

# Scale run, paintings + sculpture departments only:
art-guide-ml ingest met --limit 5000 --department-ids 11,21

# Legacy normalize-only (no embeds, no DB):
art-guide-ml ingest met --limit 200 --jsonl-out data/met/normalized.jsonl
```

### What this unblocks

- backend-engineer can now wire `/v1/identify`: `embedder.embed_bytes(uploaded_jpeg)` → SQL `ORDER BY embedding <=> $1 LIMIT k` against the populated `artworks` table → confidence math from D-005.
- Real retrieval-v1 evaluation set can be bootstrapped against live DB content (next on my list).
- The fetch→preprocess→embed→upsert loop is generic enough to template for the Phase 4 sources (Rijksmuseum, Harvard, Smithsonian, AIC, Cleveland) — captured as the `museum-ingest-loop` skill.

---

### 2026-05-11 — Backend `/v1/identify` wired end-to-end

/v1/identify is now live and queries against the catalog via nearest_neighbors (ORDER BY embedding <=> $1 LIMIT k against HNSW index). Confidence + grounding complete. Ingest next.


