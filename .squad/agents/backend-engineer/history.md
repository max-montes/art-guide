# Backend Engineer History (Condensed — 2026-05-17)

> **All Phase 0–early Phase 1 detailed work and 2026-05-16 entries archived to `history-archive.md` on 2026-05-17. Current file: latest operational findings + cross-agent coordination.**

## Cross-Agent Coordination — 2026-05-17 (ml-retrieval-engineer v2 dump-based ingest available)

**New capability shipped (D-059):**

ml-retrieval-engineer completed a v2 dump-based ingest path to bypass rate-limit issues and enable fast catalog rebuild on Brady's laptop:

- **Met dump adapter** (`services/ml/ml/ingest/met_csv.py`): reads `metmuseum/openaccess` Git LFS CSV dump (300 MB, ~500K records), caches locally with 7-day TTL, pre-filters PD + denylist, still fetches `/objects/{id}` for `primaryImage`, embeds with MPS (laptop) or CPU (prod), upserts to `artworks` table. **Zero backend API changes required** — rows are byte-for-byte identical to v1 API-sourced rows.

- **AIC dump adapter** (`services/ml/ml/ingest/aic_dump.py`): reads `art-institute-of-chicago/api-data` Git repo or S3 tar.bz2 (~120K records), same pattern, same idempotent upsert via `(source, source_id)` conflict target.

- **Shared embedder upgrades**: MPS auto-detect, `embed_batch()` for 1.16x speedup on Apple Silicon, warmup to amortize kernel-JIT, MPS↔CPU agreement test (cosine > 0.999).

**Status:** 186 tests passing, 0 regressions. Ready for Brady's test run tomorrow morning. Projection: ~200K combined records in <2 hours on laptop (image download is bottleneck at ~110 min). **No schema changes; `/v1/identify` endpoint unchanged.**

## Key Operational Findings — 2026-05-17

### Bicep Container App Secret Reset Bug (Recurring)

**Problem:** `api-bearer-token` Container App secret resets to `PLACEHOLDER-must-be-set-before-live-traffic` on every Bicep incremental redeploy of `Microsoft.App/containerApps/art-guide-prod-api`.

**Root cause:** `infra/azure/main.bicep` hard-codes placeholder values in the `secrets:` array; on each redeploy, Bicep is the source of truth and overwrites the `az containerapp secret set` mutations. KV is unaffected (KV is its own resource).

**Workaround (current):**
```bash
KV_VAL=$(az keyvault secret show --vault-name art-guide-prod-kv --name api-bearer-token --query value -o tsv)
az containerapp secret set --name art-guide-prod-api -g art-guide-prod-rg --secrets "api-bearer-token=$KV_VAL"
az containerapp revision restart --name art-guide-prod-api -g art-guide-prod-rg --revision <latest>
```

**Proper fix (prioritise in next PR):** Migrate `api-bearer-token` (and `azure-openai-key`) to KV-backed `secretRef` so KV is canonical and eliminates the reset bug.

### Met API IP-Throttle Diagnosis (2026-05-17T03:33Z)

**Diagnosis:** Container Apps Job `art-guide-prod-ingest` with 4-replica parallelism hit Met's per-IP aggregate rate limit (~80 req/s published, but parallelism summed to >100 req/s), triggering IP-level soft-throttle: every response flips to 403 Forbidden (no 429 Retry-After). Cooldown measured in **hours**, not minutes. 

**Evidence:** All three test executions (`rz0rgsf`, `4mrsgi2`, `myz5lc3`) returned >88% 403s within ~30s, then stayed 403 for ≥1 hour despite job kills and restarts (the IP ban persists across job restarts).

**Lessons for future ingest strategies:**
1. **Fresh residential IP is not a guaranteed escape hatch.** Even Brady's home Wi-Fi IP (never used by project before) was still throttled after async Container Apps IP had inflamed Met.
2. **Two simultaneous senders from different IPs = compounded risk.** Each IP individually respects Met's 80 req/s published limit, but simultaneous traffic from two IPs to the same organization may trip per-org aggregate limits.
3. **Single-egress discipline is non-negotiable for rate-limited sources.** All requests through one IP at a time until full ingest completes. Parallelism is for compute-bound or multi-source workloads.
4. **Cooldown verification protocol:** Before re-running Met ingest, verify recovery with `curl https://collectionapi.metmuseum.org/public/collection/v1/objects/1 -i` from the intended egress IP. Expect HTTP 200; any 403 = IP still in penalty box.

**Current status (end of session):** Brady's residential IP in ≥40 min cooldown (last burst 2026-05-17T04:34Z). Azure Container Apps egress IP in ≥60 min cooldown (last burst 2026-05-17T03:20Z). Both expected to clear by ~06:30Z. Rijks laptop ingest (different host: `data.rijksmuseum.nl`) is live and healthy.

## Met ACA Job Deployment — 2026-05-17T12:44Z (session)

**Gate results:**

| Gate | Result | Notes |
|------|--------|-------|
| T-1 DSN | ✅ | Secret exists as `database-url` (not `db-url` as runbook says — runbook has wrong name). Host = `art-guide-prod-pg.postgres.database.azure.com`. Well-formed `postgresql://` scheme. |
| T-2 Met API key | ✅ | Auto-pass — no key required. |
| T-3 ACR image | ✅ | `latest` tag present, pushed 2026-05-17T19:04:06Z (HEAD 8544dc7 contains `--resume-skip-existing`). No new build needed. |
| T-4 ACA env | ✅ | `art-guide-prod-cae` confirmed in West US 3. |
| T-5 IP cooldown | ✅ | HTTP 200 from Met API — Azure egress IP clear of penalty box. |

**Actions taken:**
- Updated job definition via `az containerapp job update --yaml` (not Bicep, to avoid secret-reset bug): command changed from `met` → `met-dump --limit 0 --request-delay 0.015 --batch-commit-size 64 --batch-size 4 --resume-skip-existing`; image changed from `v4` → `latest`.
- Started job: execution `art-guide-prod-ingest-eins3l3`, status Running, start time 2026-05-17T19:47:44+00:00.
- Log Analytics unavailable immediately after start (expected — logs take 2-3 min to propagate).
- Expected completion: ~23:00–23:47Z (3–4 hr wall time).
- Runbook bug noted: T-1 gate references secret name `db-url` but actual name in KV is `database-url`. Fixed in this entry; runbook should be updated.

## Learnings

- **IP-ban vs. rate-limit diagnosis:** Flatline of 403s with zero 200s in monitoring = IP-ban (not rate-limit). 429s with intermittent 200s = rate-limit, recoverable with exponential backoff. Checking log data alone is not sufficient — verify ground truth by querying the DB (`SELECT source, COUNT(*) FROM artworks GROUP BY source`).

- **Symptom adjacency lies in async codebases.** When an async handler hangs, the "hang happened at log line X" usually means the hang happened in a call *following* X, possibly many awaits deep. Three prior agents blamed asyncpg because `create_pool` is lexically next; only when ml-retrieval-engineer-3 ran a stack sample did the true culprit emerge: `from transformers import AutoModel` stalling on macOS amfid file verification.

- **Cache priming as accidental fix is invisible.** Three prior failed runs created `.pyc` files that unblocked the fourth. The "fix" is not in any diff. Document state-as-fix explicitly so future incarnations don't assume their code change worked.

- **ACA Met ingest plan (2026-05-17):** Brady approved planning the off-laptop Met ingest path. Key findings:
  - `art-guide-prod-ingest` ACA Job already exists in Bicep (`services/ml/ml/ingest/met_csv.py` + `met_db.py` are the two paths). The Bicep job currently runs the v1 `met` API path; plan is to switch to `met-dump` v2 CSV path.
  - v2 CSV dump pre-filters ~50% of 501K rows before any `/objects/{id}` call — reduces throttle exposure and wall time.
  - `--resume-skip-existing` ported to `met_csv.py` (`ingest_met_csv_to_db()`) in this session. Pattern mirrors D-060 (aic_db / rijks_db). Skip set loaded at startup; all IDs matched against `source='met'` rows already in DB.
  - No new `Dockerfile.ingest` needed — API image already contains `art-guide-ml` CLI. Job overrides CMD.
  - Resource sizing: 2 vCPU / 4 GiB (prior OOM at 2 GiB; SigLIP holds ~1.5 GiB on CPU). Single worker, `request_delay=0.015 s`, `--batch-size 4` (CPU-optimal vs. MPS batch=8).
  - Expected ~$1.10–1.60 per full run, ~3–4 hr wall time.
  - Brady reviews `docs/met-aca-job.md` before any `az` command runs.
  - Key files: `docs/met-aca-job.md` (runbook), `services/ml/ml/ingest/met_csv.py` (resume-skip port), `services/ml/ml/cli.py` (flag + summary), `.squad/decisions/inbox/backend-engineer-met-aca-job.md` (D-NNN candidate).

- **Met IP-ban circuit breaker incident (2026-05-17 / eins3l3):** Job ran for ~3 hours writing 0 rows because the Azure Container Apps egress IP was banned. Root cause: `--request-delay 0.015` (66 req/s) is ~11× the safe `met_db` default and likely caused a re-ban within minutes of starting, even though the T-5 gate check showed HTTP 200. The code silently counted each record as `skipped_api_error` and continued — no hard failure, no log-analytics data (known workspace lag), 0 rows written.
  - **Fix shipped (commit 90bd6a0):** `MetAPIBannedError` + `_probe_met_api()` (upfront 403 probe) + `DEFAULT_CONSECUTIVE_403_LIMIT=50` in-loop circuit breaker. Validated on execution `fw140av`: failed in ~50s with clear error instead of hours.
  - **Request delay corrected:** `--request-delay` updated from `0.015` → `0.1` (10 req/s) in job definition. This halves throughput but keeps within a safe margin.
  - **IP state at 23:10Z:** Still banned. Do not restart until 01:00Z at earliest. Start the job — if the IP is clear, it will run; if not, the circuit breaker will fail in <60s.
  - **Probe limitation noted:** The upfront probe checks object ID 1. If that specific object returns 404 (not PD or non-existent), the probe won't catch a full IP ban; the circuit breaker at 50 consecutive 403s will still catch it. Consider using a known PD object (e.g., 436523) as the probe target in a future improvement.


## Met ingest circuit breaker + rate fix — 2026-05-17T23:09:26Z

**Session:** Scribe processed backend-engineer's ingest diagnosis.

**Status update:**
- D-065 committed to decisions log: circuit breaker deployed, request delay corrected from 66 req/s → 10 req/s.
- Met API IP still banned; recovery expected ~01:00Z UTC (Monday).
- Circuit breaker (`MetAPIBannedError`) validated: execution `fw140av` failed cleanly in 50s vs. prior 3h silent drain.
- **Safe rate confirmed:** 10 req/s (0.1s delay) per Met API 80 req/s documentation; 0.1s margin applied.
- DB state stable: 21,194 rows (100 met, 14,504 aic, 6,590 rijks). Resumption will skip existing via `--resume-skip-existing` flag.
- Heartbeat v2 (15m interval) monitors for circuit-breaker patterns; escalates if 4+ trips within 1h.

**Lessons consolidated:**
1. Met API bans operate at IP level, not request level. 403 flatline = IP ban; 429 + intermittent 200s = rate limit.
2. Pre-flight gate (T-5 HTTP 200 from Met) can pass even if IP about to be banned; post-gate burst can trigger ban within seconds if rate is unsafe.
3. Circuit breaker cost-benefit: detects ban state in <60s, preventing hours of wasted compute. Probe logic (object ID 1) works but can be sharpened with known-PD object in follow-up.
4. **GitHub-hosted runner fallback (2026-05-19):** GitHub Actions is now the preferred Met bulk-ingest execution path because it avoids Azure egress IP bans while keeping the same `art-guide-ml ingest met-dump` CLI, prod Postgres target, and idempotent `--resume-skip-existing` semantics. Exposing the circuit-breaker threshold as a CLI flag lets operators tune fail-fast behavior from the workflow boundary without patching Python code.

## Region swap for Met ACA ingest — 2026-05-20T01:33:51Z

- Read the current West US 3 job config from `art-guide-prod-ingest`: image `artguideprodcr.azurecr.io/art-guide-api:latest`, command `art-guide-ml ingest met-dump --limit 0 --request-delay 0.05 --batch-commit-size 64 --batch-size 4 --resume-skip-existing`, envs `DATABASE_URL` / `ENV=prod` / offline HF cache flags, resources 2 vCPU / 4 GiB, manual trigger, parallelism 1.
- Deployed a fresh East US ACA job: resource group `art-guide-ingest-eastus-rg`, environment `art-guide-ingest-eastus-env`, job `art-guide-ingest-eastus`.
- Azure CLI gotcha: on `azure-cli 2.83.0` + `containerapp 1.3.0b4`, `az containerapp job create --args ... --request-delay ...` and full `--command ... --request-delay ...` both rejected flag-like tokens as top-level CLI args. Reliable workaround: create the job via `az containerapp job create --yaml`.
- Infra gotcha: the new job required a system-assigned identity plus explicit `AcrPull` on `artguideprodcr` before start.
- Started execution `art-guide-ingest-eastus-xl7vstp` at `2026-05-20T01:26:39Z` with `--request-delay 0.0133` (~75 req/s).
- Outcome: **failed within ~3 minutes**. Logs flatlined to `403 Forbidden` and ended with `ml.ingest.met_csv.MetAPIBannedError: Circuit breaker tripped: 50 consecutive 403s ... Azure egress IP is likely IP-banned.` Met row count stayed at `100`.
- Conclusion: East US ACA is also unusable for Met ingest right now; next retry should move to another distant region (for example `northeurope`, `westeurope`, or `eastasia`).

## Met HF adapter (v3 API-bypass) — 2026-05-19T19:20:44Z

**Shipped:** `services/ml/ml/ingest/met_hf.py` — a new ingest adapter that streams
the `metmuseum/openaccess` Hugging Face dataset, bypassing the Met Collection API
entirely. Registered as `ingest met-hf` in `cli.py`. All 239 tests pass (40 new
`test_met_hf` tests + 0 regressions).

**Key design decisions:**
- Uses `datasets>=2.14` with `streaming=True` — no full ~300 MB download before
  iteration. Library handles parquet/CSV format detection automatically.
- `convert_hf_row()` is the sole coercion function: `objectID` str→int,
  `isPublicDomain` str→bool, `objectBeginDate`/`objectEndDate` str→int,
  `tags` JSON string→list. All other field names already match the Met API exactly.
- `map_met_record()` is called unchanged — output rows are identical to v1/v2.
- Circuit breaker tracks consecutive CDN image failures (`MetCDNCircuitBreakerError`),
  not API 403s (there are no API calls in this path).
- Per-image `embed_bytes()` rather than `embed_batch()` — CDN download is the
  bottleneck; batch overhead not worth the complexity here.
- `--image-delay` / `--request-delay` (alias) for CDN politeness; default 0.1s.

**Lessons:**
- HF dataset field names match the Met API JSON response almost perfectly — the
  only real divergence is `tags` as a JSON string. All other coercions
  (`objectID`, `isPublicDomain`, date fields) are just CSV-vs-JSON format
  differences, not schema differences.
- Tests written ahead of implementation are a gift: `test_met_hf.py` fully
  specified the public interface (`convert_hf_row`, `_row_passes_hf_filter`,
  `HFIngestStats`, `MetCDNCircuitBreakerError`, `DEFAULT_CONSECUTIVE_CDN_FAIL_LIMIT`,
  `ingest_met_hf_to_db`), including edge cases (tags=null, tags=malformed JSON,
  empty primaryImageSmall, circuit breaker reset-on-success).
- The `rows=` keyword argument on `ingest_met_hf_to_db` for test injection (vs.
  live HF streaming) is the right seam: it keeps the core logic unit-testable
  without any mocking of the `datasets` library itself.


## Met ingest circuit breaker + rate fix — 2026-05-17T23:09:26Z

**Session:** Scribe processed backend-engineer's ingest diagnosis.

**Status update:**
- D-065 committed to decisions log: circuit breaker deployed, request delay corrected from 66 req/s → 10 req/s.
- Met API IP still banned; recovery expected ~01:00Z UTC (Monday).
- Circuit breaker (`MetAPIBannedError`) validated: execution `fw140av` failed cleanly in 50s vs. prior 3h silent drain.
- **Safe rate confirmed:** 10 req/s (0.1s delay) per Met API 80 req/s documentation; 0.1s margin applied.
- DB state stable: 21,194 rows (100 met, 14,504 aic, 6,590 rijks). Resumption will skip existing via `--resume-skip-existing` flag.
- Heartbeat v2 (15m interval) monitors for circuit-breaker patterns; escalates if 4+ trips within 1h.

**Lessons consolidated:**
1. Met API bans operate at IP level, not request level. 403 flatline = IP ban; 429 + intermittent 200s = rate limit.
2. Pre-flight gate (T-5 HTTP 200 from Met) can pass even if IP about to be banned; post-gate burst can trigger ban within seconds if rate is unsafe.
3. Circuit breaker cost-benefit: detects ban state in <60s, preventing hours of wasted compute. Probe logic (object ID 1) works but can be sharpened with known-PD object in follow-up.
4. **GitHub-hosted runner fallback (2026-05-19):** GitHub Actions is now the preferred Met bulk-ingest execution path because it avoids Azure egress IP bans while keeping the same `art-guide-ml ingest met-dump` CLI, prod Postgres target, and idempotent `--resume-skip-existing` semantics. Exposing the circuit-breaker threshold as a CLI flag lets operators tune fail-fast behavior from the workflow boundary without patching Python code.

## Region swap for Met ACA ingest — 2026-05-20T01:33:51Z

- Read the current West US 3 job config from `art-guide-prod-ingest`: image `artguideprodcr.azurecr.io/art-guide-api:latest`, command `art-guide-ml ingest met-dump --limit 0 --request-delay 0.05 --batch-commit-size 64 --batch-size 4 --resume-skip-existing`, envs `DATABASE_URL` / `ENV=prod` / offline HF cache flags, resources 2 vCPU / 4 GiB, manual trigger, parallelism 1.
- Deployed a fresh East US ACA job: resource group `art-guide-ingest-eastus-rg`, environment `art-guide-ingest-eastus-env`, job `art-guide-ingest-eastus`.
- Azure CLI gotcha: on `azure-cli 2.83.0` + `containerapp 1.3.0b4`, `az containerapp job create --args ... --request-delay ...` and full `--command ... --request-delay ...` both rejected flag-like tokens as top-level CLI args. Reliable workaround: create the job via `az containerapp job create --yaml`.
- Infra gotcha: the new job required a system-assigned identity plus explicit `AcrPull` on `artguideprodcr` before start.
- Started execution `art-guide-ingest-eastus-xl7vstp` at `2026-05-20T01:26:39Z` with `--request-delay 0.0133` (~75 req/s).
- Outcome: **failed within ~3 minutes**. Logs flatlined to `403 Forbidden` and ended with `ml.ingest.met_csv.MetAPIBannedError: Circuit breaker tripped: 50 consecutive 403s ... Azure egress IP is likely IP-banned.` Met row count stayed at `100`.
- Conclusion: East US ACA is also unusable for Met ingest right now; next retry should move to another distant region (for example `northeurope`, `westeurope`, or `eastasia`).

