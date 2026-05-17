# Backend Engineer Orchestration — 2026-05-17T23:09:26Z

## Context

Met ingest job execution `eins3l3` ran for ~3 hours (19:47–22:40Z) writing 0 rows, consuming ~$0.64 in Azure compute, due to IP ban at 403-continuous rate. Secondary execution `fw140av` (23:05–23:10Z) used circuit breaker to fail fast (50s instead of 3h+).

## Diagnosis

1. **Root cause:** ACA egress IP banned by Met API (per incident from 2026-05-17T03:20Z). Rate 66 req/s exceeded safe threshold; ban lasted 3+ hours.
2. **Detection gap:** No hard failure, no alert. Code silently accumulated `skipped_api_error` counts → operator blind to 0-row outcome.
3. **Cost impact:** ~$0.80 wasted on idle container, plus reputational damage (Met may blacklist IP for 24–48h).

## Solution deployed

### 1. Circuit breaker (`MetAPIBannedError`)

File: `services/ml/ml/ingest/met_csv.py` (commit 90bd6a0)

- **Probe:** `_probe_met_api()` runs before CSV scan; single GET `/objects/1` returns 403 → raise immediately.
- **In-loop guard:** Counter resets on success; if 50 consecutive 403s, raise `MetAPIBannedError`.
- **Log output:** Both paths emit `curl` command to verify recovery: `curl -i https://collectionapi.metmuseum.org/public/collection/v1/objects/1`

**Outcome:** Execution `fw140av` failed in 50s instead of 3+ hours. Job logs clearly show "Circuit breaker tripped: 50 consecutive 403s (last object_id 232)".

### 2. Request rate correction

ACA job definition updated (via `az containerapp job update --yaml`):
- Old: `--request-delay 0.015` (66 req/s)
- New: `--request-delay 0.1` (10 req/s)

Stays safely within Met's 80 req/s per-IP limit. Wall-time impact: +25% (5–6h vs 3–4h).

## Recovery plan (2026-05-17T23:10Z onward)

1. **Wait for IP unban:** Expected ~01:00Z UTC (Monday). ACA IP banned since ~19:55Z (~3h15m); typical cooldown is 3h.
2. **Verify before restart:** Run manual curl from Container Apps environment:
   ```bash
   az containerapp job start -g art-guide-prod-rg -n art-guide-prod-ingest
   ```
   If circuit breaker fires within 60s → IP still banned → wait longer.
   If rows accumulate in DB → good; let run complete.
3. **DB state preserved:** 21,194 rows in `artworks` (100 met, 14,504 aic, 6,590 rijks). Flag `--resume-skip-existing` ensures no re-embedding on restart.

## Files written / deleted

- **Written:** D-065 merged into `.squad/decisions.md`
- **Deleted:** `.squad/decisions/inbox/backend-engineer-ingest-diagnosis.md`

## Next steps

- Heartbeat (v2, 15m interval) monitors for circuit-breaker log pattern → escalates if 4+ consecutive trips within 1h.
- Post-recovery: review probe logic (currently uses object ID 1; consider pinning to known-good PD record).
