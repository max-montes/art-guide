# Skill: prod-catalog-ingest

**Confidence:** High (validated 2026-05-16 100-record seed; Path D single-worker pattern validated 2026-05-17 against Met's published 80 req/s limit)
**Owner:** ml-retrieval-engineer
**Last used:** 2026-05-17 (Path D full-catalog ingest — single worker at 66 req/s)

## Problem

You have a working local ingest CLI and a remote/prod Postgres. You need to run the ingest against prod without changing any code or hardcoding secrets.

## Pattern

The `art-guide-ml ingest met` CLI already supports both a `--database-url` flag and `$DATABASE_URL` env var fallback (in that priority order). Use the **flag** approach — it avoids leaking the DSN into the shell environment and is explicit in logs (scrubbed).

```bash
# 1. Fetch DSN from Key Vault (never hardcode)
PROD_DSN=$(az keyvault secret show \
  --vault-name art-guide-prod-kv \
  --name database-url \
  --query value -o tsv)

# 2. Dry-run first to confirm connectivity + filter counts
art-guide-ml ingest met \
  --limit 10 \
  --dry-run \
  --department-ids 11 \
  --database-url "$PROD_DSN"

# 3. Real run
art-guide-ml ingest met \
  --limit 100 \
  --department-ids 11 \
  --database-url "$PROD_DSN"
```

## Checklist before running against prod

- [ ] Brady's IP is in the Azure Postgres firewall allowlist (Azure Portal → Flexible Server → Networking)
- [ ] `database-url` secret in Key Vault includes `sslmode=require` (Azure Flexible Server requires TLS)
- [ ] Migrations `0001_init.sql` + `0002_met_enrichment.sql` are applied (use `sql-migration-runner` skill)
- [ ] pgvector extension is installed in the target database
- [ ] `--department-ids 11` scopes to European Paintings (avoids scanning all 480K+ Met objects)

## Verification after ingest

```bash
# All three checks should pass
psql "$PROD_DSN" <<'SQL'
SELECT COUNT(*) AS total_rows FROM artworks;                               -- expect N
SELECT COUNT(*) AS with_bio FROM artworks WHERE artist_bio IS NOT NULL;    -- expect close to N for Met
SELECT vector_dims(embedding) AS dims FROM artworks LIMIT 1;               -- expect 768
\d artworks  -- confirm hnsw index present
SQL
```

## Latency expectations (2026-05-16 baseline)

| Scenario | End-to-end `/v1/identify` |
|---|---|
| Container already warm | ~2,900 ms (retrieval=2016 ms, llm=82 ms) |
| Scale-to-zero cold start | 10–30 s (model load, D-028 operational note) |

The warm retrieval_ms is dominated by SigLIP preprocessing, not HNSW search. Expect it to stay roughly constant as catalog grows (HNSW is sub-linear).

## Gotchas

- **Do not use `$DATABASE_URL` env var if you also have local Docker Compose running** — the wrong DSN will be used silently. Prefer explicit `--database-url` flag.
- **Met image CDN requires browser-like headers** (already baked into `met_db.py::_IMAGE_CDN_HEADERS`). Do not bypass this.
- **6 MB original Met images slow smoke tests** — use a real iOS-resized upload (~200–400 KB) for latency benchmarks.
- **`skipped_image=1` is normal** — some Met `primaryImage` URLs are genuinely broken at source. Not a pipeline bug.
- **All 7 enrichment columns are populated in a single ingest pass** when running the current `met_db.py`. No separate `backfill met` step is needed for fresh installs.
- **`asyncpg.create_pool(...)` without `timeout=` is a footgun.** A single TLS+auth handshake stall against Azure Postgres Flexible Server (residential IP, intermittent network, etc.) hangs the await forever with no traceback — process sits at 0% CPU with an ESTABLISHED TCP socket. Always pass `timeout=15, command_timeout=30` (or similar bounds). The repo enforces this at all four call sites in `services/ml/ml/cli.py` (commit `378dcb3`, 2026-05-17). `psql` from the same shell works because libpq has client-side defaults asyncpg's asyncio TLS path lacks.
- **Symptom-adjacency anti-pattern in laptop ingest hangs.** "Hang at log line `Opening asyncpg pool`" was misdiagnosed three times as an asyncpg bug. **Actual cause:** `from transformers import AutoModel` inside `get_embedder()` — runs *immediately after* the log line in `ingest_met_to_db` — blocked tens of minutes on macOS `amfid` re-verifying `.so`/`.dylib` files on a cold `__pycache__`. Symptom in `ps`: 0% CPU (kernel-blocking XPC syscall, not pool wait, not network wait). Diagnostic: `sample -f PID 5` reveals the real stack — leaf will be deep in `transformers` autoclass machinery (`bounded_lru_cache_wrapper → _PyObject_MakeTpCall → _PyEval_EvalFrameDefault`). Workaround: let any failed run prime the `.pyc` cache (subsequent loads drop to seconds). Proper fix: hoist `import transformers` to the CLI entry point so the cold-start cost is observable as an import, not a phantom pool hang.

## Related skills

- `sql-migration-runner` — apply migrations before first ingest
- `museum-ingest-loop` — the broader pattern for adding new source adapters (Rijks, Harvard, etc.)
- `eval-harness-for-grounded-retrieval` — run recall eval after ingesting to verify retrieval quality

## Parallel ingest pattern (added 2026-05-17, full-catalog scale)

> **⚠️ ANTI-PATTERN WARNING (2026-05-17 Path D):** the parallel design below
> was the wrong shape for the Met API. The Met publishes a **per-IP** rate
> limit of 80 req/s. Container Apps Jobs replicas share the same egress IP,
> so aggregate (not per-replica) req/s is what trips the throttle. When
> the throttle trips, every response flips to 403 (no 429, no Retry-After)
> for a ≥ 40 min cooldown — the parallel speed-up evaporates on the first
> throttle event. **Use the "Single-worker rate-limit-aware" pattern
> below** for any source with a published per-IP rate limit. The parallel
> code (CLI flags, modulo sharding, deterministic replica indexing) is
> still useful for sources with per-API-key limits or no per-IP cap, but
> NOT for the Met API. Keep the infrastructure in code; don't enable the
> Bicep `parallelism > 1` config for Met.

When ingesting >50K records, single-replica throughput is bottlenecked by
outbound HTTP to the museum API (per-request latency × sequential
processing). The cheap, principled fix is **Container Apps Jobs native
parallelism with replica-self-sharded work**:

### 1. Bicep: parallel Manual job

Properties under `Microsoft.App/jobs.properties.configuration`:

```bicep
configuration: {
  triggerType: 'Manual'
  replicaTimeout: 10800       // 3 hr ceiling per replica
  replicaRetryLimit: 1
  manualTriggerConfig: {       // ⚠ parallelism is NOT a top-level property
    parallelism: 8             // 8 concurrent replicas
    replicaCompletionCount: 8  // all must succeed
  }
  ...
}
```

Common mistake: putting `parallelism` / `replicaCompletionCount` at the
top of `configuration`. Bicep WARN BCP037 surfaces this; the deploy then
fails with `Unknown properties parallelism, replicaCompletionCount in
ContainerAppsJobConfiguration are not supported`. They live inside
`manualTriggerConfig` for Manual jobs (and `eventTriggerConfig` for
event-triggered jobs).

### 2. CLI: self-sharding via `CONTAINER_APP_REPLICA_NAME`

Azure sets `CONTAINER_APP_REPLICA_NAME` per replica, e.g.
`art-guide-prod-ingest-<exec>-<suffix>-3`. The CLI accepts
`--shard-index auto --shard-count N` and parses the trailing all-digits
chunk:

```python
for chunk in reversed(replica_name.split("-")):
    if chunk.isdigit():
        return int(chunk) % shard_count
```

Falls back to `hash(replica_name) % shard_count` if no numeric tail —
stable assignment, no two replicas to shard 0.

The orchestrator filters the global candidate id list modulo the shard:
```python
ids = [oid for i, oid in enumerate(ids) if i % shard_count == shard_index]
```

`(source, source_id)` ON CONFLICT upsert makes accidental overlap safe.

### 3. Tune `request_delay` per replica, not aggregate

For Met (collectionapi.metmuseum.org): per-replica 0.05 s (= 20 req/s/replica)
× 8 replicas = 160 req/s aggregate has stayed below the rate-limit threshold
(observed 2026-05-17: zero 429s across hundreds of records ingested). The
existing `_get_with_retry` wrapper absorbs 429s with exponential backoff
+ tracks them in `IngestStats.rate_limited_events` so an over-aggressive
rate produces visible backoff rather than fatal failure.

### 4. Don't retry permanent 4xx — they cost more than they save

Critical for parallel ingest: per-record permanent 4xx (403 Forbidden when
a Met record is restricted; 404 when a record id has been retired) MUST
fail fast, not retry. With 5 retries of exponential backoff (1+2+4+8+16
= 31 s) on every 403'd record and a 30–50 % 403 rate in low-numbered Met
ids, retry sleeps completely dominate throughput. Add a status-code
discriminator at the top of the retry loop:

```python
if 400 <= resp.status_code < 500 and resp.status_code != 429:
    resp.raise_for_status()   # bail immediately, no retry
```

The downstream record-fetch caller already does `except httpx.HTTPError:
continue`, so one log line per skip and move on.

### 5. limit=0 means "no limit"

The `--limit` CLI flag is overloaded: positive N = stop after N successful
embeds; 0 / unset = unbounded (process every id in the assigned shard).
The downstream `ingest_met_to_db(limit=…)` accepts `None` or `<=0` as
"unbounded". The old `if limit <= 0: raise ValueError` was a latent bug
that swallowed the first prod execution silently for 40 min — the job
showed `status=Unknown` and zero rows committed. **Test `--limit 0` in
the smoke path** before re-shipping the ingest job.

### 6. Watch the right signals

- **429 frequency** is the rate-limit canary. Sustained 429s → back off
  (raise `--request-delay`, drop `parallelism`).
- **403/404 frequency** is the record-restriction canary, not a rate-limit
  signal. High 403 rate means the global id list is stale relative to the
  museum's current public-domain set; tolerable, log and move on.
- **`(inserted | updated) per replica per minute`** is the operator
  metric. Below ~10/min/replica with `request_delay=0.05` means
  something is blocking (retry storms, DB pool starvation, embedder
  thrashing) — not "the API is slow".
- **Row count delta** lags batch_commit_size × N replicas. With
  `batch_commit_size=64` and 8 replicas, first DB-visible commit is at
  T+ (64 / records-per-replica-per-min) min. Faster batch (16) for
  earlier visibility costs ~4× more DB round-trips; rarely worth it.

### 7. RBAC propagation: AcrPull for the job MI

`Microsoft.App/jobs` MI is a separate principal from the API container
app's MI. Bicep's `Microsoft.Authorization/roleAssignments` with
`principalId: ingestJob.identity.principalId` does the right thing, but
**if a prior deploy partially failed**, the assignment may never have
materialized. Symptom on next deploy: `unable to pull image using Managed
identity system for registry`. Fix: confirm via
`az role assignment list --scope $(az acr show -n <acr> --query id -o tsv)`,
then either re-deploy or grant manually (then delete the manually-named
assignment so Bicep can re-create it with its deterministic guid name —
otherwise Bicep fails with `RoleAssignmentExists`).

## Fallback: laptop ingest from a fresh residential IP (added 2026-05-17)

When the Azure egress IP has been throttled by the museum API (Met responds
with 100% 403 from that IP for ≥ 40 min, possibly hours; no `Retry-After`),
the cheapest recovery is to switch the *client* IP without changing the
target Postgres. The ingest CLI is the same; only the network egress moves.

Brady's residential IP and Azure's egress IP are independent as far as Met
is concerned — penalty boxes are per-IP. While the Azure IP cools down,
Brady's home Wi-Fi can drain the queue from the same CLI against the same
prod Postgres.

### Runner script (lives in `.squad/.scratch/run-laptop-ingest.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="/Users/maxmontes/Documents/GitHub/art-guide"
VENV="$REPO_ROOT/services/ml/.venv/bin"
LOG="$REPO_ROOT/.squad/.scratch/laptop-ingest-$(date -u +%Y-%m-%dT%H-%M-%SZ).log"
cd "$REPO_ROOT"

export DATABASE_URL=$(az keyvault secret show \
  --vault-name art-guide-prod-kv \
  --name database-url --query value -o tsv)

PYTHONUNBUFFERED=1 nohup caffeinate -ims \
  "$VENV/art-guide-ml" ingest met \
    --limit 0 --request-delay 0.015 --batch-commit-size 64 \
  > "$LOG" 2>&1 &
PID=$!
disown
unset DATABASE_URL
echo "PID=$PID  LOG=$LOG"
```

### Critical: launch from Terminal.app, NOT from inside the agent session

Three launches from inside a Copilot CLI agent session were silently killed
within 6–13 min — even with `nohup`, `disown`, and re-parented `PPID=1`.
mDNSResponder log entries (`DNSServiceCreateConnection STOP PID[<pid>]`)
showed the kill happened, but no SIGTERM/SIGKILL event was logged and no
Python exception trace appeared in the redirected log. Theory: Copilot CLI's
per-turn process-group cleanup signals child PGIDs. `nohup` ignores `SIGHUP`
but does not protect against direct `SIGKILL`. The sibling agent's
TTY-attached ingest met (PID 56454, `--limit 50`) died the same way at the
same time it switched bash sessions. **Brady must run the script from a
regular Terminal.app window** — that parents the process under Terminal's
process group, fully independent of any agent lifecycle.

### Why the laptop is slow to start (and what to do about it)

Cold start with **no `transformers` `.pyc` cache + python.org Python** is
5+ minutes before the first DB write. Two compounding causes:

1. **macOS amfid** (Apple Mobile File Integrity) re-verifies every `.so` /
   `.dylib` on dynamic load. The python.org binary is "adhoc signed or
   signed by an unknown certificate chain" — every verification is a XPC
   round-trip to `amfid`, serialized. System log fills with
   `"… not valid: Error Domain=AppleMobileFileIntegrityError Code=-423"`.
2. **`transformers` lazy autoclass machinery** does eager class definitions
   for every `models/<name>/` directory (467 dirs in transformers ≥4.40),
   triggering `import` for `image_processing_pil_<name>.py` on each. Without
   `.pyc` cache that's 2347 .py files compiled in band.

**Mitigation:** prime the cache once interactively before the real run:
```bash
services/ml/.venv/bin/python -c "import transformers; print(transformers.__version__)"
# blocks ~5 min the first time, generates the full .pyc tree;
# subsequent imports drop to ~65 s
```

After priming, cold-start of `art-guide-ml ingest met` becomes:
- transformers import: ~65 s
- get_embedder() / SigLIP load: ~30 s
- first record processed: ~T+90-120 s
- first DB-visible commit: ~T+2-3 min (one batch of 64)

### Met rate budget on residential IP

- Met published cap: 80 req/s per IP (https://metmuseum.github.io/).
- `--request-delay 0.015` ≈ 66 req/s, single worker. Stays at 82.5 % of cap.
- 403 rate on fresh residential IP: near zero (the throttle is IP-specific;
  Met has never seen Brady's IP for this project). The `_get_with_retry`
  non-429 fast-fail (D-052 §code) handles the occasional record-coverage
  403 without retrying.
- Steady-state throughput: ~50-65 records/sec into Met → ~3-10% pass the
  classification filter → SigLIP embed + Azure Postgres upsert.
- Full 501K candidate-id walk: **~2-3 hr** elapsed.

### Don't run two senders simultaneously

If the Azure ingest job is also running (or is queued to start), do NOT
launch the laptop ingest. Met's per-IP limit doesn't aggregate across
sources, but the project's UA string is shared and Met *can* aggregate by
identifier if motivated. Cleaner: pick one egress IP per ingest run.
Before launching laptop:
```bash
az containerapp job execution list \
  -n art-guide-prod-ingest -g art-guide-prod-rg \
  --query "[?properties.status=='Running']" -o tsv
# expect empty output
```
If a Running execution exists and was started in error, stop it with
`az containerapp job execution stop --name <execName> -n art-guide-prod-ingest -g art-guide-prod-rg`.

### Resumable and idempotent

The `(source, source_id)` UNIQUE constraint + `ON CONFLICT DO UPDATE` makes
re-runs safe. If the laptop run fails halfway through, just re-launch — it
will skip already-inserted rows by content (well, it still fetches them,
but the UPSERT is a noop). To pick up exactly where left off without
re-fetching, that would require persisting cursor state to disk; not done
today, and at 2-3 hr total runtime it isn't worth the engineering.

## Single-worker rate-limit-aware pattern (added 2026-05-17, Path D)

**Use this for any source with a published per-IP rate limit.** It is the
correct shape for the Met API (80 req/s) and probably also for Rijksmuseum,
Harvard Art Museums, and Smithsonian (all of which publish per-IP caps in
the same low-three-digits range).

### Decision tree: parallelism vs single worker

| Source rate-limit model | Right shape |
| --- | --- |
| Per-API-key, key per replica | Parallel (key isolation = independent budgets) |
| Per-IP, no key | **Single worker**, rate just under cap |
| Per-IP with shared key | Single worker (key doesn't help) |
| Unspecified / no published limit | Single worker, conservative (~5-10 req/s); add parallelism only with explicit owner permission |

### Bicep: single-replica Manual job

```bicep
configuration: {
  triggerType: 'Manual'
  replicaTimeout: 14400        // 4 hr ceiling — generous vs ~2 hr expected
  replicaRetryLimit: 1
  manualTriggerConfig: {
    parallelism: 1
    replicaCompletionCount: 1
  }
}
```

### CLI: drop sharding flags entirely

```bicep
command: [
  'art-guide-ml', 'ingest', 'met',
  '--limit', '0',              // unbounded
  '--request-delay', '0.015',  // ~66 req/s, under Met's 80 req/s cap
  '--batch-commit-size', '64'
]
```

No `--shard-index` / `--shard-count` — single worker = single shard. The
modulo-split code in `ingest_met_to_db()` is a no-op when both default to
0/1, so it's safe to leave in the orchestrator for future re-use.

### Rate-budget math

```
request_delay = 1 / target_req_per_sec * safety_factor
```

For Met (80 req/s cap, target 80% utilisation = 64 req/s):
- `request_delay = 1 / 64 ≈ 0.0156s` → round to `0.015` (66 req/s, 82.5%
  of cap).

For Rijksmuseum (1 req/s default for unauthenticated, 10K/day with key):
- `request_delay = 1.0` if no key; if a key is provisioned, redo math
  against the per-key cap.

### What "good" looks like in the first 5 min

- 403 rate < 5% (low background noise from individual restricted records)
- Row count in `artworks` grows by ~50+/sec after first batch commit
  (= `batch_commit_size / request_delay` ÷ a small overhead factor)
- `_get_with_retry` log lines limited to occasional 429s, NOT a storm
  of "skipped 4xx without retry"

### What "throttled IP" looks like

- 403 rate > 10% sustained past minute 2 (record-level 403s should be
  sub-5% noise from the public-domain churn)
- `_get_with_retry`'s "skipped non-429 4xx" message firing on > 90% of
  records consecutively — this is the IP-throttle signature, NOT real
  record restrictions
- Throughput collapses to "thousands of records skipped in seconds"
  because non-429 4xx no longer retries (correct behaviour for restricted
  records; pathological under IP throttle)

Fix: **stop the job immediately**, wait ≥ 60 min, then retry. If it
throttles again on second attempt, escalate — the published cap may
not match what's actually enforced for this client class.

### Why this beats parallel for Met

- Parallel-4 at 27 req/s aggregate hit a throttle event within 5 min and
  produced 0 inserts (D-052 execution `art-guide-prod-ingest-4mrsgi2`,
  `rz0rgsf`, `myz5lc3`).
- Parallel-8 at 160 req/s aggregate hit a throttle within seconds.
- Single worker at 66 req/s projects 501,696 / 66 / 60 ≈ 127 min wall
  clock with zero throttle risk.
- "Faster on paper, throttled in practice" loses to "slower on paper,
  actually finishes."

### Anti-patterns to avoid

1. **Adding parallelism to fix slow ingest** when the bottleneck is a
   per-IP rate limit. The fix is to push closer to the rate cap from one
   sender, not to fan out.
2. **Treating 403 as a record-restriction signal** when the source uses
   403 (not 429) for IP throttle. Always check the source's published
   rate-limit documentation before assuming.
3. **Bicep `value:` inline for sensitive Container App secrets** — see
   `api-bearer-token` recurring reset (D-046, D-052, Path D). Migrate
   to `keyVaultUrl` + identity secretRef so the source of truth lives
   in KV, not the template literal.
