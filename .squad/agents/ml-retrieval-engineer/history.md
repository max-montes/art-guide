# ML/Retrieval Engineer History (Condensed — 2026-05-17)

> **Full Phase 0–early Phase 1 work archived to `history-archive.md`. Current file: latest completed work + active next steps.**

> **Recent 2026-05-17 work (Path C/D scramble: asyncpg defensive bound, Rijks adapter, AIC adapter, single-worker Met at 80 req/s, full-catalog Azure job + parallel sharding, Met IP-throttle penalty box) summarized to `history-archive.md` on 2026-05-16. Current file: latest completed work only.**

## 2026-05-16 — v2 dump-based ingest path (Met CSV + AIC api-data) [ml-retrieval-engineer-d059]

**Context:** Brady asked for a path to ingest ~200K curated records on the
laptop in <2 hr. The v1 API adapters (`met_db`, `aic_db`) are rate-limit
bound (Met 80 req/s aggregate per-IP; AIC 60 req/min). Container Apps Jobs
are still in Met's penalty box (D-053).

**Tasks completed:**

1. **Embedder MPS + batching (services/ml/ml/embeddings.py).** Auto-detect
   `mps` on Apple Silicon via `torch.backends.mps.is_available()`, fall back
   to CPU; override via `ART_GUIDE_EMBED_DEVICE`. Added `embed_batch(images)
   -> list[np.ndarray]` (and kept `embed_batch_bytes` array-returning alias);
   `embed_bytes` now delegates to `embed_batch([img])[0]` so the
   single-image and batched code paths share identical numerics. Embedder
   warmup runs a dummy 224×224 forward pass at construction so the first
   real call doesn't pay MPS kernel-JIT cost. New regression test
   `test_mps_and_cpu_embeddings_agree` locks in cosine > 0.999 between
   MPS and CPU paths.

2. **Met CSV dump adapter (services/ml/ml/ingest/met_csv.py, ~580 LOC).**
   Streams `MetObjects.csv` from `media.githubusercontent.com/media/...`
   (Git LFS media proxy, no `git-lfs` install needed), caches at
   `~/.cache/art-guide/met-objects.csv` with 7-day TTL and atomic
   `.partial` writes. Pre-filters PD + non-empty `Link Resource` +
   denylist (substring: `ephemera`; exact: `coins`, `books`,
   `manuscripts`) BEFORE any API call. For each accepted row, still
   calls `/objects/{id}` to get `primaryImage` (the CSV doesn't carry
   it), then reuses `map_met_record` from `met_db.py` unchanged. Batches
   embed via `embed_batch`. Upserts via the same `_INSERT_SQL` constant
   as `met_db` — output rows are byte-for-byte identical.

3. **AIC dump adapter (services/ml/ml/ingest/aic_dump.py, ~530 LOC).**
   `git clone --depth=1` of `art-institute-of-chicago/api-data` to
   `~/.cache/art-guide/aic-data/`, `git pull --depth=1 --ff-only` on
   refresh. Detects when the cache dir contains the extracted tar.bz2
   full dataset instead of a Git repo and skips the clone. Walks
   `json/artworks/*.json` (one record per file, same shape as the API
   response), pre-filters PD + `image_id` + denylist, then reuses
   `map_aic_record` unchanged. **Zero AIC API calls** — only IIIF
   image downloads against Cloudflare-fronted CDN (not subject to the
   60 req/min cap). Same batched embed + upsert.

4. **CLI subcommands:** `art-guide-ml ingest met-dump` and `art-guide-ml
   ingest aic-dump`, both with `--max-records`, `--batch-size`, and
   `--force-refresh` flags. Same `asyncpg.create_pool(timeout=15,
   command_timeout=30)` defensive wrapping as D-058.

5. **Tests:** 35 cases in `test_met_csv.py` + 26 cases in `test_aic_dump.py`
   + 9 new cases in `test_embeddings.py` (device resolution, batch
   numerics, MPS↔CPU agreement, batch throughput). All 186 project tests
   pass (was 129 baseline). Two integration tests (`test_met_dump_
   integration_max_records_20`, `test_aic_dump_integration_max_records_10`)
   gated behind env vars because they need the cached dump + live network.

6. **Docs:** New `docs/ingest.md` (operator runbook covering both v1 and
   v2 paths, env vars, troubleshooting, full AIC tar.bz2 extraction).
   Updated CLI usage examples accordingly.

7. **Decision file:** `.squad/decisions/inbox/ml-retrieval-engineer-d059-
   dump-adapters.md`.

8. **Skill:** `.squad/skills/dump-based-ingest/SKILL.md` capturing the
   reusable pattern (two adapters / one mapper, cache TTL, atomic
   writes, pre-filter before network, when NOT to use a dump path,
   honest MPS speedup expectations).

**Hard rules honored:**
- met_db.py / aic_db.py / rijks_db.py NOT modified (live-sync path
  preserved).
- All preprocessing routes through `prepare_for_embedding` via the
  embedder seam (hard rule #4).
- Image bytes live in memory only and are dropped immediately after
  batch flush (D-012 / hard rule #3).
- Schema unchanged; `ON CONFLICT (source, source_id) DO UPDATE` makes
  re-runs idempotent.

**Benchmark (50-record synthetic, M-series MPS, SigLIP-base):**
```
 CPU: seq 3.41s (14.7 rec/s); batch=8 2.16s (23.1 rec/s); speedup=1.58x
 MPS: seq 1.43s (35.0 rec/s); batch=8 1.23s (40.6 rec/s); speedup=1.16x
```
Projection for 200K Met records: ~83 min embed (MPS batch=8), ~110 min
image download (dominant wall-clock, overlapped with embed), ~83 min
Met `/objects` calls (overlapped). Total <2 hr achievable.

**Not done (deferred):** No prod run. Brady will launch tomorrow after
review. AIC `description` field promotion to grounded column (D-057
follow-up) still open.

## Learnings

- **The "dump path" benefit is partly an illusion when the dump doesn't
  carry image URLs.** Met CSV gives us metadata but not `primaryImage`,
  so we STILL pay one `/objects/{id}` API call per accepted record. The
  real win is the pre-filter step: ~50% of candidate records are rejected
  (non-PD + denylist) before any API call. For AIC the dump is genuinely
  zero-API because every field including the IIIF image_id is in the
  JSON file. **Lesson:** always audit the dump's field coverage against
  the canonical mapping function BEFORE estimating the throughput win.

- **MPS batch speedup is overhyped for small models.** Initial target was
  ≥4x batch-vs-sequential on MPS. Empirical: 1.16x. The reason is
  per-image PIL preprocessing dominates the SigLIP-base forward pass at
  these dimensions — only the model forward is parallelised by batching.
  The actual MPS lever is **sequential throughput** (35 rec/s vs CPU's
  14.7 rec/s = 2.4x), not batching. Test threshold lowered from "≥4x on
  MPS" to "strictly faster than sequential with 5% noise floor". I
  documented the real numbers in `docs/ingest.md` and the decision file
  so future incarnations don't repeat the over-promise.

- **MPS-vs-CPU numerical drift is small but non-zero.** Cosine between
  the same image embedded on MPS and CPU is ~0.9994-0.9999 (within
  float32 noise). I added `test_mps_and_cpu_embeddings_agree` to gate
  this — a regression that pushed it below 0.999 would silently destroy
  retrieval recall because the catalog (ingested on laptop MPS) and the
  prod query path (CPU on Container Apps) would live in subtly
  different spaces.

- **Atomic cache writes matter for long-running downloads.** The Met
  CSV is ~300 MB — a Ctrl-C mid-download without the `.partial` +
  `shutil.move` pattern would leave a truncated file that the next run
  parses as if complete, silently dropping ~half the catalog. Cheap to
  implement, expensive to debug after the fact.

- **macOS amfid cold start is still the biggest "WTF" on a fresh
  laptop.** First `from transformers import AutoModel` after a reboot
  can take 5+ minutes (history note from ml-retrieval-engineer-3,
  D-058). The warmup pass in `_HFEmbedder.__init__` doesn't fix this —
  it happens AFTER the import. Operator-facing note added to
  `docs/ingest.md` troubleshooting.

- **Per-source `IngestStats` should NOT be merged into a base class.**
  Tempting to share, but the source-specific counters (Met CSV's
  `csv_rows_skipped_filter`, AIC dump's `json_files_skipped_parse_error`)
  reflect actual per-source pipeline stages. A common base would lose
  information about WHY records were rejected. Two stats classes, two
  `as_dict()` methods — the operator prints both and grep finds the
  right one.

- **`art-institute-of-chicago/api-data` repo carries 10 sample records
  per endpoint, not the full catalog.** The full dump is a 115 MB
  tar.bz2 at `artic-api-data.s3.amazonaws.com`. The adapter handles
  both: if the cache dir is empty it clones the repo (10 samples); if
  the operator manually extracted the tar.bz2 over the cache dir
  the adapter skips the clone and walks the resulting tree. Documented
  in `docs/ingest.md` "Full dataset for AIC" section.

## 2026-05-17 — `--resume-skip-existing` for v1 AIC + Rijks adapters

**Why:** Brady's overnight AIC run (v1 live-API path; the v2 dump only
refreshes weekly) was restarting from page 1 each invocation and producing
~63 UPDATEs per 64-row batch against the 4400 already-ingested rows. Net
new rows per re-run: near zero.

**Files changed:**
- `services/ml/ml/ingest/aic_db.py` — added `IngestStats.skipped_existing`,
  module-level `_load_existing_source_ids(pool, source) -> set[str]`,
  `resume_skip_existing: bool = False` parameter on `ingest_aic_to_db`,
  and a single-line guard `if mapped["source_id"] in skip_existing_ids:
  stats.skipped_existing += 1; continue` *before* image download.
- `services/ml/ml/ingest/rijks_db.py` — matched pair of changes.
- `services/ml/ml/cli.py` — `--resume-skip-existing` flag on both `ingest
  aic` and `ingest rijks`, passed through, and `skipped_existing=...`
  added to the printed summary line.
- `services/ml/tests/test_aic_db.py` — 3 new tests: skips known IDs
  (asserts embed_bytes not called + image download not called for skipped
  IDs), default-off (asserts loader is never invoked), and empty-set
  (asserts everything still embeds when skip set is empty).
- `services/ml/tests/test_rijks_db.py` — 2 new tests mirroring the
  AIC skip behaviour against the OAI page fetcher.

**Behavior on `resume_skip_existing=True`:**
1. Startup: `SELECT source_id FROM artworks WHERE source=$1` → `set[str]`.
   Logged: `resume-skip-existing: found N existing aic source_ids in DB;
   will skip those`.
2. Per-record loop: after the listing-page parse, before the per-record
   image GET, skip if `source_id` in set. No image fetch, no SigLIP
   forward pass, no insert/update.
3. Final summary line: includes `skipped_existing=N`.

**Tests:** 198 passing, 2 skipped (pre-existing model-loading skips). No
regressions in `test_aic_db.py`, `test_rijks_db.py`, `test_met_db_ingest`,
or the dump-adapter tests.

## Learnings

- **The skip-set guard belongs *after* the listing-page response, not
  before.** AIC's listing endpoint is what tells us which `source_id`
  values are even candidates; we can't pre-filter against the DB without
  walking the listing. The real cost savings are the per-record image
  GET (counts against the same 1 req/s budget as listing pages — so a
  record skipped pre-image saves ~1.05s) and the SigLIP forward pass
  (~30 ms on MPS). Across 4400 already-ingested rows, that's ~1.3 hr of
  wall-clock saved on a re-run.

- **`pool=None` + `dry_run=True` was the right test seam.** The function
  already tolerates a missing pool when `dry_run=True`, and the resume
  branch is gated on `pool is not None`. To exercise the skip path in
  tests without a real Postgres, I pass `pool=object()` (truthy
  sentinel) + `dry_run=True` and monkeypatch
  `_load_existing_source_ids` to return the desired set. The commit
  path is never touched, so the sentinel pool is never used. Clean
  contract: the loader is the only DB-touching code on the resume path,
  and it's the only thing tests need to mock for that path.

- **Listing-page cost is still paid for skipped records — by design.**
  AIC has no "get by id" cheap path and no incremental-since cursor on
  the public API. The flag's value is in the per-record image+embed
  saving, not in shaving listing pages. For ~14M total AIC records at
  100/page that's ~140K listing calls = ~41 hr at 1.05s/req, so a real
  full-catalog run is still days. The flag is for resuming mid-run, not
  for accelerating cold starts (use the v2 dump adapter for that —
  D-059).

- **No pytest-asyncio in `services/ml`.** I initially wrote the tests as
  `@pytest.mark.asyncio async def ...` and they collected fine because
  pytest-asyncio is installed in the user site-packages, but the
  project's declared dev deps don't include it. Rewrote the new tests
  as sync functions using `asyncio.run(...)` to keep them independent of
  any user-site plugins. Matches the discipline of the existing tests
  in this file (mapper-only, no async).

- **The task spec said "external_id" but the actual schema column is
  `source_id`.** Used the real column name everywhere in code, comments,
  and CLI `--help` text. Worth double-checking column names against the
  migration before naming a flag from a Slack message.
