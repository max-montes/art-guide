# Backend Engineer History (Current)

## Cross-agent note from ml-retrieval-engineer — 2026-05-17 (Path D ingest deploy)

**Recurring bug:** `api-bearer-token` Container App secret resets to
`PLACEHOLDER-must-be-set-before-live-traffic` on **every** Bicep
incremental redeploy of `Microsoft.App/containerApps/art-guide-prod-api`.
This happened again on the 2026-05-17T03:54Z Path D deploy
(`art-guide-prod-20260517T035459`). Restored manually via:

```bash
KV_VAL=$(az keyvault secret show --vault-name art-guide-prod-kv --name api-bearer-token --query value -o tsv)
az containerapp secret set --name art-guide-prod-api -g art-guide-prod-rg --secrets "api-bearer-token=$KV_VAL"
az containerapp revision restart --name art-guide-prod-api -g art-guide-prod-rg --revision <latest>
```

**Root cause** (in `infra/azure/main.bicep` ~line 251):

```bicep
secrets: [
  { name: 'database-url',      value: dbUrl }
  { name: 'api-bearer-token',  value: 'PLACEHOLDER-must-be-set-before-live-traffic' }
  { name: 'azure-openai-key',  value: 'PLACEHOLDER-set-after-aoai-provisioning' }
]
```

Bicep `value:` is the source of truth for ACA on every redeploy — `az
containerapp secret set` mutations get overwritten. KV is unaffected
(KV is its own resource, Bicep doesn't touch the KV secret value).

**Proper fix (please prioritise this in a future PR):** migrate
`api-bearer-token` (and `azure-openai-key`) to KV-backed secretRef:

```bicep
secrets: [
  { name: 'database-url',      value: dbUrl }   // keep — generated in Bicep
  {
    name: 'api-bearer-token'
    keyVaultUrl: '${kv.properties.vaultUri}secrets/api-bearer-token'
    identity: 'system'
  }
  {
    name: 'azure-openai-key'
    keyVaultUrl: '${kv.properties.vaultUri}secrets/azure-openai-key'
    identity: 'system'
  }
]
```

This makes KV the canonical store and eliminates the reset bug
permanently. The Container App's system MI already has
`Key Vault Secrets User` (see `raKvSecretsUser` in same Bicep).

Recurrence count: 3+ (D-046, D-052, Path D 2026-05-17). Each recurrence
loses ~1-2 min of API downtime and requires a manual revision restart.
Worth fixing before the next deploy.

---

## Current Status — 2026-05-16

**Phase 1 on track.** Core `/v1/identify` pipeline live and returning real Met candidates with confidence-aware status. Prod image v1 shipped and healthy on Azure Container Apps (revision art-guide-prod-api--0000003). 

**API endpoints live:**
- `POST /v1/identify` — multipart image upload, returns `IdentifyResponse` with status, confidence, grounded explanation.
- `GET /v1/artworks/{id}` — detail endpoint, wired to pgvector retrieve.
- `GET /healthz`, `/readyz`, `/version`, `/docs` — all 200.

**Infrastructure:**
- Azure Container App `art-guide-prod-api` at `https://art-guide-prod-api.kindglacier-84ffc0b4.westus3.azurecontainerapps.io`
- PostgreSQL+pgvector live (migrations applied, artworks table ready)
- Azure Key Vault (bearer token stored)
- Static bearer auth required

**Image details (D-028):**
- Multi-stage Dockerfile (python:3.11-slim-bookworm)
- SigLIP-base-patch16-224 bundled (~1.5 GiB process footprint, offline mode)
- uvicorn 1 worker (no gunicorn; 2 workers would OOM)
- Non-root app user (uid 1000)
- Built via ACR remote build (760 KiB context, 8m23s)

**What's working:**
- Embedder cache pattern in lifespan (warm_embedder → SigLIP load once)
- Confidence-aware pipeline (exact | likely | style_only | no_match per D-005)
- LLM grounding guardrails (no world knowledge injection; only fields in retrieved record)
- Museum-plaque prose explanations (D-026)
- Enrichment tier (a) backfill complete (7 columns, 100 Met records, D-027)
- Exception handling split (PIL 400, inference 500)
- Validation error sanitization (no Exception objects leaking to JSON)

**Next:**
- iOS integration testing against prod
- Full Met catalog ingest to prod (ml-retrieval-engineer scope)
- Cold-start latency eval vs. D-025 baseline; Phase 2 decision on minReplicas=1 or background task

## Cross-Agent Note — 2026-05-16 (ml-retrieval-engineer)

Prod catalog now seeded (100 European Paintings, D-029). `/identify` verified end-to-end with Sunflowers → score=1.0. Warm latency ~2.9s (retrieval-bound, not API-bound). Cold-start gotcha from D-028 still applies; monitor in iOS testing.

**Known operational issue:**
- First request after scale-to-zero blocks ~10–30s (image pull + model load). Revisit if eval P99 regresses.

## Learnings

**Dockerfile for ML-fat images:**
- Multi-stage build with bundled HF weights is standard. Builder pulls weights to `/opt/hf-cache`, runtime COPYs + runs offline.
- Install services/ml before services/api in venv (torch resolved once, then lighter layer on top).
- Preserve directories listed in pyproject.toml even if small (pip egg_info aborts if missing).

**ACR remote build:**
- `az acr build` uploads only tar context (~760 KiB), builds in-region, stores layer cache in ACR.
- Beats local docker push for fat images; no Docker Desktop dependency.
- Pattern captured in `.squad/skills/acr-remote-build/SKILL.md`.

**uvicorn for ML workloads:**
- 1 worker per container if model >500MB. Torch is already multithreaded inside the process. Horizontal scaling (replicas) is the right axis, not vertical workers.

**Lifespan blocking:**
- Synchronous `warm_embedder()` blocks first request on cold revisions. Document the trade-off in deployment docs; revisit in Phase 2.

See `history-archive.md` for Phase 0 foundation work (endpoint scaffolding, error handling, pipeline wiring, etc.).

## Cross-Agent Note — 2026-05-16 (ios-engineer outcome)

**iOS app build is now green (13 tests passing, zero warnings).** Brady fixed two blockers: (1) stale `.xcodeproj` regenerated via xcodegen, (2) MockAPIClient NSLock → OSAllocatedUnfairLock for async safety. Brady is now testing on simulator against live prod. Watch for any API contract issues he surfaces (e.g., multipart handling, response shape, error codes).

## Cross-Agent Note — 2026-05-16 (ios-engineer — cold-start hardening)

**iOS now warms `/healthz` before `/identify` (D-033).** The camera view (RootView) fires a background `GET /healthz` on appear, so the container wakes + model loads while the user is looking at the UI. Combined with raised timeouts (60s per-segment, 90s total), this mitigates the cold-start latency observed in D-028. Expect occasional unprovoked `/healthz` traffic from the app — that's intentional.

## Prod Outage — Bicep Regression + Remediation — 2026-05-17

**Incident (from D-038):** ml-retrieval-engineer's ingest job Bicep deploy regressed the API container to `mcr.microsoft.com/azuredocs/containerapps-helloworld:latest` with port 80. Brady's iOS app received HTML instead of JSON → decode error.

**Root causes:**
1. `parameters.prod.json` had `containerPort: 80` (hello-world's port, never updated).
2. `main.bicep` lacked `apiImage` / `apiCpu` / `apiMemory` params — any Bicep redeploy could reset them.
3. Registry binding (`registries: identity: system`) was wiped by the Bicep redeploy, blocking image pull from ACR.

**Phase 1 remediation (prod restored in ~10 min):**
```bash
# 1. Fix ingress port
az containerapp ingress update -n art-guide-prod-api -g art-guide-prod-rg --target-port 8000

# 2. Restore ACR registry binding (wiped by bad deploy)
az containerapp registry set -n art-guide-prod-api -g art-guide-prod-rg \
  --server artguideprodcr.azurecr.io --identity system

# 3. Restore image + sizing + env vars (new revision --0000005)
az containerapp update -n art-guide-prod-api -g art-guide-prod-rg \
  --image artguideprodcr.azurecr.io/art-guide-api:v1 \
  --cpu 1.0 --memory 2.0Gi \
  --set-env-vars ENV=prod AZURE_KEYVAULT_NAME=art-guide-prod-kv \
    AZURE_OPENAI_DEPLOYMENT=gpt-5-mini AZURE_OPENAI_API_VERSION=2024-02-01 \
    DB_POOL_MIN_SIZE=1 DB_POOL_MAX_SIZE=5 PROMPT_LOG_SAMPLE_RATE=1.0 \
    AZURE_OPENAI_ENDPOINT=https://art-guide-prod-aoai.openai.azure.com/
```
Revision --0000005 healthy. `{"status":"ok"}` confirmed. Brady unblocked.

**Phase 2 — Bicep fix (D-038):**
- Added `param apiImage string`, `param apiCpu string`, `param apiMemory string` to `main.bicep`.
- Updated `parameters.prod.json`: `containerPort 80 → 8000`, added `apiImage`, `apiCpu`, `apiMemory`.
- Added `what-if` guard in `deploy.sh` that hard-fails if `containerapps-helloworld` would be deployed.
- Added warning comment block at top of `main.bicep`.
- `az deployment group what-if` confirmed 0 API-container changes after fix.

**Phase 3 — Wave 2 prompt shipped (D-036):**
```bash
az acr build --registry artguideprodcr --image art-guide-api:v2 \
  --file services/api/Dockerfile .   # repo root context required (includes services/ml/)
az containerapp update -n art-guide-prod-api -g art-guide-prod-rg \
  --image artguideprodcr.azurecr.io/art-guide-api:v2
```
Revision --0000006 (v2, museum-plaque curator voice) Healthy. `apiImage` default + `parameters.prod.json` updated to v2.

**Smoke test:** `GET /healthz` → `{"status":"ok"}` from uvicorn (content-type: application/json). 

**Cross-agent note:** ml-retrieval-engineer history updated with incident summary and pointer to D-038.



**Task A.3 — Wave 2 LLM prompt shipped.**

Rewrote `services/api/app/llm.py` prompt layer:

- `_BASE_RULES`: changed from generic "museum-guide-style explanation" to explicit **warm curator/docent voice** ("knowledgeable but warm, authoritative and unhurried, not marketing copy").
- `_STATUS_GUARDRAILS[exact]`: now instructs LLM to weave `artist_bio` and `credit_line` naturally into prose, skip dimensions unless notably large/small, lead with dynasty/period for non-Western works.
- `_STATUS_GUARDRAILS[likely]`: same enrichment weave but maintains hedging tone throughout; allows brief visual ambiguity note.
- `_STATUS_GUARDRAILS[style_only]`: reinforced — "resembles the work of X" / "in the manner of X" only; explicit "do NOT name or claim the specific artwork".
- `_NO_MATCH_TEXT`: updated to docent voice — "I can't place this one — could be a private work, a reproduction, or just outside what I know."
- **LLM params**: removed `temperature` (gpt-5-mini is a reasoning model, only supports default=1). Replaced `max_tokens=220` with `max_completion_tokens=1500` (reasoning models consume ~700 tokens internally; 250 was insufficient and returned empty content).

**Smoke test result (gpt-5-mini-2025-08-07, L'Arlésienne record):**

> "Vincent van Gogh (Dutch, Zundert 1853–1890 Auvers-sur-Oise) painted L'Arlésienne: Madame Joseph-Michel Ginoux (Marie Julien, 1848–1911) in 1888–89 in oil on canvas. The painting is in The Metropolitan Museum of Art and entered the collection as the bequest of Sam A. Lewisohn in 1951."

Old output (flat stub style):
> "L'Arlésienne: Madame Joseph-Michel Ginoux (Marie Julien, 1848–1911) is attributed to Vincent van Gogh, dated 1888–89. It is Oil on canvas. It is held in the collection of The Metropolitan Museum of Art."

New output uses `artist_bio` + `credit_line`; skips dimensions (36×29 in is average-sized — correct). 73 API tests pass.

**Key discovery:** `gpt-5-mini` = `gpt-5-mini-2025-08-07`, a reasoning model. Constraints:
- No `temperature` param (hard error from API)
- Uses `max_completion_tokens` not `max_tokens`
- ~700 reasoning tokens consumed before output; need ≥1500 total

**Task C.1 — Azure budget alert.**

Created `art-guide-prod-monthly` budget via `az rest PUT` against Microsoft.Consumption/budgets API (2023-11-01):
- Amount: $100/mo, Monthly grain, 2026-05-01 → 2027-05-01
- Notifications at 50%, 80%, 100% actual spend → owner email (xam3002@hotmail.com)
- Verified with `az consumption budget list --resource-group art-guide-prod-rg`
- Note: legacy `art-guide-prod-budget` at $50 also exists (not removed)

**Task C.2 — Cost dashboard docs.**

Added "Cost monitoring" section to `docs/deployment.md` with portal URL, CLI queries, budget notes.

## Cross-Agent Coordination Note — 2026-05-17 (ios-engineer-3 completed)

iOS deployment target **raised to 17.0** (required for SwiftData local history feature, D-034). Affects any future iOS coordinate work. No backend changes required; iOS handles persistence locally. Server continues to accept uploads from any compatible iOS version; feature is opt-in on device.

## Diagnosis — Met Ingest Parallel Failure — 2026-05-17T03:33Z

Read-only triage of `art-guide-prod-ingest` after ml-retrieval-engineer-1 ran 3 reconfigured executions with zero rows persisted. Full report: `.squad/decisions/inbox/backend-engineer-ingest-diagnosis.md`.

**TL;DR.** Bicep + CLI are correctly wired (parallelism=4, --shard-index auto, --shard-count 4, --request-delay 0.15, image v4). Each execution was killed by the agent ("Job suspended") within 7–34 min after Met IP-blacklisted the Container Apps egress IP and returned 403 to >88% of requests. The IP-ban persists across job restarts, so each retry hits the same wall. DB still at 100 rows (D-029 seed).

**Recommended:** wait ≥60 min cooldown, then revert to parallelism=1 with --request-delay 0.05 (8 h to full catalog) — the proven config.

## Learnings

**Container Apps Jobs parallelism gotchas:**

- **`Status: Stopped` is ambiguous.** It covers both "user-stopped" and "failed". Always inspect `ContainerAppSystemLogs_CL.Reason_s`. `Suspended` = user/API stop; `Failed` = replica retry-limit hit; otherwise look for `OOMKilled`, `Error`, `BackOff`.
- **Per-execution config is captured at start time.** `az containerapp job execution show --job-execution-name <name>` returns the template snapshot used for that execution, including command + image tag. This is gold for "what was actually deployed when this ran" — much better than guessing from current job spec.
- **`az containerapp job logs show` only works while pods are alive.** After ~10 min, ACA garbage-collects replicas and you get `ERROR: No replicas found for execution`. Always go to Log Analytics directly for forensics: `ContainerAppConsoleLogs_CL | where ContainerName_s == '<container>' and TimeGenerated between (...)`. Filter on `ContainerGroupName_s` (= replica pod name) to isolate per-replica streams.
- **`CONTAINER_APP_REPLICA_NAME` has NO trailing ordinal in manual-trigger jobs.** The format is `<job>-<execId>-<5char-random>` (e.g. `art-guide-prod-ingest-myz5lc3-n9gdj`). Any code that does `name.split('-')[-1]` and expects a digit is wrong. There is no first-class shard-index env var; hash-mod fallback collides ~32% on 4 replicas (birthday-style). Either pre-coordinate shards via a Postgres claim table, or accept the loss with parallelism=1.

**Ingest debugging pattern (replicable):**

1. `az containerapp job execution list -n <job> -g <rg> -o table` → enumerate executions + statuses.
2. `az containerapp job execution show ... --job-execution-name <name>` → snapshot of command/image/env per execution.
3. `ContainerAppSystemLogs_CL | where JobName_s == '<job>' | project TimeGenerated, ExecutionName_s, ReplicaName_s, Reason_s, Log_s` → high-level lifecycle events (image pull, start, stop, suspend, OOM).
4. `ContainerAppConsoleLogs_CL | where ContainerName_s == '<container>'` → application stdout/stderr.
5. **HTTP-status histogram per minute** is the highest-signal aggregation for any HTTP-bound workload:
   ```kusto
   ContainerAppConsoleLogs_CL
   | where TimeGenerated between (...) and ContainerName_s == 'ingest' and Log_s contains 'HTTP/1.1'
   | summarize ok=countif(Log_s contains '200 OK'),
               t429=countif(Log_s contains '429'),
               t403=countif(Log_s contains '403')
       by bin(TimeGenerated, 1m)
   ```
   Flatline of 403s with zero 200s = IP-ban (not rate-limit). 429s with intermittent 200s = rate-limit, recoverable.
6. **Verify ground truth in the DB** (`SELECT source, COUNT(*) FROM artworks GROUP BY source`) before believing any log narrative — logs can show successful fetches that never made it to persist due to an exception further down the pipeline.

**Met API specifics:**

- `collectionapi.metmuseum.org` rate-limit floor appears to be ~80 req/s sustained but the IP-blacklist threshold is much lower — likely <40 req/s sustained, with cooldown measured in **hours** not minutes.
- Once blacklisted, the response is 403 with no `Retry-After` header. Exponential backoff does nothing (the ban is sticky). The code's "don't-retry-4xx-non-429" fix is correct — retrying 403s would just refresh the ban timer.
- Restarting the job while still banned **prolongs the ban**. Cooldown the egress IP (no requests at all) for ≥60 min before retry; verify with a single curl from the same egress IP before kicking the job.

## Cross-Agent Note — 2026-05-17 (ml-retrieval-engineer ingest saga handoff)

**Residential IP throttle pattern (for future ingest strategies):**

During the multi-source ingest scramble (D-053 through D-058), the Met API IP-banned Brady's residential IP while he was running a single-worker laptop ingest from his home Wi-Fi. The ban persisted for ≥1 hour after the agent killed the process, preventing any retry without waiting for the cooldown to clear.

**Operational lessons for future scenarios:**
1. **Fresh residential IP is not a guaranteed escape hatch.** ISPs rotate IP ranges; Brady's home Wi-Fi IP happened to never been used by this project before, but was still throttled after the async Container Apps IP had triggered the ban (possibly because Met attributes requests to the organization/UA-string, not just the individual IP).
2. **Two simultaneous senders from different IPs = compounded risk.** Even though each IP individually respects Met's 80 req/s published limit, simultaneous traffic from two different IPs to the same project may trip per-organization aggregate limits.
3. **Single-egress discipline is non-negotiable for rate-limited sources.** All requests should flow through one IP at a time until the full catalog is ingested. Parallelism is for compute-bound or multi-source workloads, not per-IP-rate-limited sources.
4. **Cooldown verification protocol:** Before re-running any Met ingest (whether from Azure or a new residential IP), verify recovery with a single `curl https://collectionapi.metmuseum.org/public/collection/v1/objects/1 -i` from the intended egress IP. Expect HTTP 200 with reasonable latency; any 403 means the IP is still in the penalty box — wait longer before retry.

**For next session:** If Met ingest is needed and both Azure egress IP and Brady's residential IP are in cooldown, consider:
- **Azure Container Apps from a different region** (requires D-013 amendment to multi-region, out of scope v1)
- **Met's CSV bulk feed** (GitHub `metmuseum/openaccess`, no HTTP calls to `/objects/{id}`, skips IP-ban risk entirely — ml-retrieval-engineer to evaluate)
- **Delayed start from a clean VPN/proxy IP** (operational workaround, not recommended for production)

**Current status (end of session):** Brady's residential IP in ≥40 min cooldown (last burst 2026-05-17T04:34Z). Azure Container Apps egress IP in ≥60 min cooldown (last burst 2026-05-17T03:20Z). Both expected to clear by ~06:30Z. Rijks laptop ingest (different host: `data.rijksmuseum.nl`) is live and healthy.
