---
name: "dump-based-ingest"
description: "Pattern for adding a v2 dump-based ingest adapter alongside an existing API adapter, when the API rate limit is the wall-clock bottleneck."
domain: "ml-retrieval"
confidence: "high"
source: "earned (art-guide v2 Met CSV + AIC api-data adapters, 2026-05-16, D-059). Validated against two independent sources sharing a common shape."
---

## Context

Once a museum's API adapter exists and is rate-limit-bound (Met's 80
req/s aggregate, AIC's 60 req/min — both wall-clock-dominant for
multi-hundred-thousand-record catalog rebuilds), the next throughput
gain is usually NOT in the adapter code. It's in switching to a static
**dump** of the same data (CSV, Git repo of JSONs, S3 tarball) that the
museum publishes for exactly this use case.

This skill captures how to add the dump path **alongside** the API path
without breaking idempotency or schema invariants.

## Patterns

- **Two adapters, one mapper.** The API adapter (`*_db.py`) and the
  dump adapter (`*_csv.py`, `*_dump.py`) both produce rows via the
  SAME pure `map_*_record(raw_dict) -> dict | None` function. The
  mapper lives in the API adapter (which shipped first). The dump
  adapter imports it. Output rows are byte-for-byte identical. This
  guarantees the catalog has no v1-vs-v2 schema drift.

- **Cache dumps on local disk with a TTL.** Default
  `~/.cache/art-guide/<source>-dump-name/`. Refresh policy: re-download
  if older than 7 days. Override path via `ART_GUIDE_CACHE_DIR`. The
  cache is the unit of resumability across runs (the catalog is the
  unit of resumability across processes — see "Idempotency" below).

- **Atomic cache writes.** Stream the download to `<path>.partial`,
  then `shutil.move()` to the real path. Ctrl-C mid-download must not
  leave a truncated cache that the next run parses as if complete.

- **Pre-filter at the dump layer, BEFORE any network call.** The dump
  carries enough fields to apply the project's PD / classification /
  denylist filters without hitting the API. This is where the
  throughput win comes from — typically ~50% of records get rejected
  upfront, saving half the API calls (or, in AIC's case, ALL the API
  calls).

- **Be explicit when the dump can't replace the API.** Sometimes the
  dump is missing a field (Met CSV doesn't carry `primaryImage`).
  Document this in the module docstring and design the adapter to fall
  back to a single per-record API call for that field only. The
  caching + filtering wins still hold.

- **Idempotency is the schema, not the adapter.** With `ON CONFLICT
  (source, source_id) DO UPDATE` already in the table, re-running the
  dump adapter is safe. No `--resume-from-id` flag needed; just kill
  and restart. Already-embedded rows refresh their embedding (slight
  extra cost) but the catalog never duplicates.

- **Don't touch the API adapter.** The v1 path is the incremental
  catch-up path. Refactoring it to "share more code" with the dump
  path tends to obscure the live-sync behavior that real operators
  depend on. Better to have two well-named adapters with explicit
  field-mapping reuse via a single imported function.

- **CLI subcommand names mirror the adapter name.** `met` -> `met-dump`,
  `aic` -> `aic-dump`. Predictable, no aliases, and grep-able when an
  operator pastes a command into the team channel.

- **Stats objects diverge slightly.** Each adapter has its own
  `*IngestStats` dataclass with source-specific counters (Met CSV adds
  `csv_rows_total` / `csv_rows_skipped_filter`; AIC dump adds
  `json_files_total` / `json_files_skipped_parse_error`). Don't try
  to force a common base class — the counters reflect the actual
  per-source filter stages and merging them loses information.

- **Honor the shared embedder seam.** Both dump adapters call
  `get_embedder().embed_batch(images)` rather than instantiating a
  model directly. This way the MPS-vs-CPU decision (and any future
  embedder swap) happens in ONE place (`ml.embeddings`), and the
  dump path inherits warmup, batching, and device routing automatically.

- **Batch the embed, async the I/O.** The dump path's CPU-bound work
  is the SigLIP forward pass; the I/O-bound work is image download.
  Collect `embed_batch_size` images in memory, run one MPS forward
  pass against them via `asyncio.to_thread(embed_batch, images)`, then
  while that runs the next batch of HTTP downloads drains
  concurrently on the asyncio loop. No need for an explicit
  producer/consumer queue — the natural ordering of the outer `for`
  loop does it.

- **Drop image bytes immediately after batch flush.** The pending list
  holds `(mapped_row, image_bytes)` tuples. After embed, clear the
  list. Honor D-012 / hard rule #3 — image bytes never live longer
  than one batch's worth of work.

## When NOT to use this

- The source's API already returns multiple records per request with
  inlined entities (e.g. Rijks OAI-PMH with EDM). Adding a dump path
  is pure code without throughput benefit.
- The total dataset is <10K records. The dump download cost (download
  + parse) dominates the API-call cost you'd save.
- The source doesn't publish a dump. Adding a "dump path" that scrapes
  the API and writes it to disk is just caching with extra steps —
  use a Postgres-backed cache table instead, or fix the rate limit.
- Field coverage in the dump is materially worse than the API
  response. If you'd have to make >=1 API call per record anyway to
  fill in critical fields, the API path is fine.

## Operational notes

- **First-run cost is dominated by the dump download.** Met CSV is
  ~300 MB; AIC tar.bz2 is ~115 MB compressed / ~2.5 GB extracted.
  Subsequent runs (within the TTL) skip the download.
- **The TTL is generous on purpose.** Museum catalogs change slowly
  (monthly cadence is typical). 7 days is a good default. The
  incremental catch-up between dump refreshes can run via the v1 API
  path on its normal cron.
- **MPS speedup is dominated by sequential throughput, not batching.**
  For SigLIP-base, `embed_batch=8` is only ~1.2-1.5x faster than 8
  sequential `embed_bytes` calls because per-image preprocessing is
  ~half the per-call time. The big lever is MPS-vs-CPU (~2.4x). Don't
  over-promise batch speedups in roadmap docs.

## Related

- `museum-ingest-loop` SKILL — shared outer-loop pattern (applies to
  both v1 and v2 paths).
- `embedder-factory` SKILL — the single `get_embedder()` seam.
- `exercise-the-real-path` SKILL — why we have model-loading tests
  that aren't skip-if-uncached.
- D-059 — the architecture decision that defined this pattern in the
  art-guide repo.
