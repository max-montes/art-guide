# Met Ingest — Azure Container Apps Job

**Status:** Plan approved by Brady (2026-05-17). Not yet deployed.  
**Author:** backend-engineer  
**Last updated:** 2026-05-17

---

## ⚠️ TODOs — confirm before first `az` run

| # | Unknown | Where to find it |
|---|---------|------------------|
| T-1 | **Database DSN** — the Postgres connection string used by the ingest job lives as a `database-url` secret in the Bicep definition (injected at deploy time from `parameters.prod.json` `dbUrl` param, which must be set by the operator). Verify it is the correct prod Flexible Server host + DB name + user + password. | `az keyvault secret show --vault-name art-guide-prod-kv --name db-url` or check `parameters.prod.json` |
| T-2 | **Met API key** — The Met Open Access API (`collectionapi.metmuseum.org`) does **not** require an API key as of 2026-05-17. No secret needed. If this changes, add a KV secret `met-api-key` and inject as `MET_API_KEY` env var. | [metmuseum.github.io](https://metmuseum.github.io/) |
| T-3 | **ACR image tag to use** — this runbook uses `art-guide-api:latest`. Confirm the latest tag in ACR contains the `--resume-skip-existing` flag (shipped in this session). If not, build a new image first (see step 2 below). | `az acr repository show-tags --name artguideprodcr --repository art-guide-api -o table` |
| T-4 | **ACA environment name** — Bicep sets it to `${prefix}-cae` = `art-guide-prod-cae`. Verify: `az containerapp env list -g art-guide-prod-rg -o table` |
| T-5 | **IP cooldown check** — Before starting the job, verify the Azure egress IP is not in Met's penalty box: run the curl probe below from the Azure environment or just start the job with `--max-records 5 --dry-run` to confirm 200 responses. |

---

## Background and rationale

The laptop cannot run Met ingest reliably:
- Met's `/objects/{id}` API enforces a per-IP soft-throttle at ~80 req/s. When exceeded it silently flips all responses to HTTP 403 (no `Retry-After`) with a multi-hour cooldown.
- Parallel Container Apps replicas (D-052) hit the cap too. Root cause: _aggregate_ req/s across replicas exceeded the per-IP ceiling.
- The v2 CSV dump path (`met-dump` CLI command) pre-filters ~50% of Met's 501K rows before making any API call (rejects non-PD + denylist), cutting `/objects/{id}` calls from ~501K to ~250K. This halves both wall time and throttle exposure vs. the v1 API path.
- A **single-worker** ACA Job with `request_delay=0.015s` (~66 req/s, under the 80 req/s cap) is the correct shape. No sharding, no parallelism.
- `--resume-skip-existing` (D-060, now ported to `met-dump` in this session) lets a restarted job skip records already embedded in a prior run — critical for multi-hour jobs that may need restarts.

Expected total wall time: ~3–4 hours (250K accepted rows × 0.015 s/API call = ~62 min fetch-bound; image download + CPU embed adds ~2× = ~2 hr; 4 hr ceiling is conservative).

---

## What already exists in prod (no action needed)

| Resource | Name | Notes |
|---|---|---|
| Resource group | `art-guide-prod-rg` | West US 3 |
| Container Apps environment | `art-guide-prod-cae` | Log Analytics wired |
| Container registry | `artguideprodcr` | Basic tier; Managed Identity pull |
| Postgres Flexible Server | `art-guide-prod-pg` | pgvector extension applied |
| Key Vault | `art-guide-prod-kv` | Holds bearer token + DB URL |
| Log Analytics workspace | `art-guide-prod-la` | 30-day retention |
| ACA Job definition | `art-guide-prod-ingest` | Defined in Bicep; currently wired to v1 `met` command |

The job definition already exists but is pointed at the v1 `met` API path. This runbook **updates** the job to use `met-dump` (v2 CSV path) with `--resume-skip-existing`.

---

## Prerequisites

```bash
# 1. Authenticate
az login
az account set --subscription fbca8db6-02d3-487b-a13f-6d0f7fac5295

# 2. Set shell vars used throughout this runbook
RG=art-guide-prod-rg
JOB=art-guide-prod-ingest
ACR=artguideprodcr
IMAGE=${ACR}.azurecr.io/art-guide-api:latest

# 3. IP cooldown check — confirm Met API is reachable from your current IP
#    (this doesn't test the Azure egress IP, but rules out global outage)
curl -si https://collectionapi.metmuseum.org/public/collection/v1/objects/1 | head -5
# Expect: HTTP/2 200
# If 403: Azure egress IP is still in penalty box. Wait ≥1 hr and re-check from the job.
```

---

## Step 1 — Build and push the ingest image

The ingest job reuses the **existing API image** (`art-guide-api`). No separate `Dockerfile.ingest` is needed: the API image already contains `art-guide-ml` (the ML CLI entrypoint) because `services/ml` is installed in both stages. The job overrides `CMD` at runtime.

```bash
# Build remotely on ACR (recommended — avoids uploading ~1 GiB build context)
az acr build \
  --registry ${ACR} \
  --image art-guide-api:latest \
  --image art-guide-api:$(git rev-parse --short HEAD) \
  --file services/api/Dockerfile \
  .

# Confirm the image is present
az acr repository show-tags --name ${ACR} --repository art-guide-api -o table
```

> **If the ACR build fails** due to a missing `dbUrl` parameter or Bicep secrets issue, see the "KV-backed secrets" note in `history.md` — you may need to re-set the `api-bearer-token` secret after the Bicep redeploy.

---

## Step 2 — Update the job definition (Bicep)

The Bicep job resource (`infra/azure/main.bicep`, `resource ingestJob`) currently runs:
```
art-guide-ml ingest met --limit 0 --request-delay 0.015 --batch-commit-size 64
```

Update it to:
```
art-guide-ml ingest met-dump --limit 0 --request-delay 0.015 --batch-commit-size 64 --batch-size 4 --resume-skip-existing
```

**Why `--batch-size 4`:** Azure Container Apps runs on CPU (no Apple Silicon MPS). The CPU embed loop is single-threaded; batch=4 is the sweet spot between forward-pass overhead and per-image latency on CPU. (On MPS, batch=8 gives ~4–5× speedup, but MPS is unavailable in Azure.)

**Why `--request-delay 0.015`:** ~66 req/s for the `/objects/{id}` calls on accepted rows, well under Met's 80 req/s per-IP cap.

**Why `--resume-skip-existing`:** If the job restarts (timeout, OOM, manual kill), it loads all existing `source_id` values for `source='met'` from the DB at startup and skips those IDs in the CSV walk — no re-fetch, no re-embed.

Edit `infra/azure/main.bicep` — change the `command` block inside `ingestJob`:

```diff
-          command: [
-            'art-guide-ml'
-            'ingest'
-            'met'
-            '--limit'
-            '0'
-            '--request-delay'
-            '0.015'
-            '--batch-commit-size'
-            '64'
-          ]
+          command: [
+            'art-guide-ml'
+            'ingest'
+            'met-dump'
+            '--limit'
+            '0'
+            '--request-delay'
+            '0.015'
+            '--batch-commit-size'
+            '64'
+            '--batch-size'
+            '4'
+            '--resume-skip-existing'
+          ]
```

Also update the image tag from `v4` to `latest` (or to the specific SHA tag from the ACR build):

```diff
-          image: '${acrName}.azurecr.io/art-guide-api:v4'
+          image: '${acrName}.azurecr.io/art-guide-api:latest'
```

Then redeploy Bicep:

```bash
az deployment group create \
  -g ${RG} \
  -f infra/azure/main.bicep \
  --parameters @infra/azure/parameters.prod.json \
  --mode Incremental
```

> ⚠️ **Secret-reset bug:** Bicep redeploy resets the `api-bearer-token` Container App secret to its placeholder value (see `history.md` — recurring issue). After the Bicep redeploy, re-apply the secret:
> ```bash
> KV_VAL=$(az keyvault secret show --vault-name art-guide-prod-kv --name api-bearer-token --query value -o tsv)
> az containerapp secret set --name art-guide-prod-api -g ${RG} --secrets "api-bearer-token=${KV_VAL}"
> LATEST_REV=$(az containerapp revision list --name art-guide-prod-api -g ${RG} --query '[0].name' -o tsv)
> az containerapp revision restart --name art-guide-prod-api -g ${RG} --revision ${LATEST_REV}
> ```

---

## Step 3 — Start the job

```bash
# Start a run
az containerapp job start -n ${JOB} -g ${RG}

# Confirm the execution was created and note the execution name
az containerapp job execution list -n ${JOB} -g ${RG} -o table
# Example output:
#   Name                              StartTime             Status
#   art-guide-prod-ingest-abc123xyz   2026-05-17T14:00:00Z  Running
```

---

## Step 4 — Monitor

### Live log stream (Azure CLI)

```bash
# Get the most recent execution name
EXEC=$(az containerapp job execution list -n ${JOB} -g ${RG} \
  --query '[0].name' -o tsv)

# Follow logs (press Ctrl-C to stop following; job keeps running)
az containerapp job logs show \
  -n ${JOB} -g ${RG} \
  --execution ${EXEC} \
  --container ingest \
  --follow
```

### Log Analytics query (Azure Portal)

Portal link:
```
https://portal.azure.com/#@3626e07d-bb5c-4615-a0ed-abe35aaa2502/resource/subscriptions/fbca8db6-02d3-487b-a13f-6d0f7fac5295/resourceGroups/art-guide-prod-rg/providers/Microsoft.OperationalInsights/workspaces/art-guide-prod-la/logs
```

KQL to watch progress:
```kql
ContainerAppConsoleLogs_CL
| where ContainerName_s == "ingest"
| where TimeGenerated > ago(1h)
| where Message has "Met-dump ingest" or Message has "inserted" or Message has "rate_limited"
| project TimeGenerated, Message
| order by TimeGenerated desc
```

### Verify row count in Postgres

After the job completes (or periodically mid-run), query the prod DB to confirm `met` rows are growing:

```bash
# Via the prod API health endpoint (indirect — counts not exposed):
curl -s https://art-guide-prod-api.kindglacier-84ffc0b4.westus3.azurecontainerapps.io/healthz

# Direct DB query (requires psql or pgAdmin with the prod DSN):
SELECT source, COUNT(*) AS n FROM artworks GROUP BY source ORDER BY n DESC;
# Expected after full run: met ~250,000, aic ~14,504, rijks ~6,590
```

---

## Kill switch — stop a running job

```bash
# List running executions
az containerapp job execution list -n ${JOB} -g ${RG} -o table

# Stop a specific execution
EXEC=<execution-name-from-above>
az containerapp job execution cancel \
  -n ${JOB} -g ${RG} \
  --job-execution-name ${EXEC}
```

The job can be safely killed at any time. All rows committed before the kill are preserved — the upsert is idempotent via `ON CONFLICT (source, source_id)`. Restart with `--resume-skip-existing` to pick up where you left off (see Step 3).

---

## Rollback

**There is no rollback for ingest.** Met rows are additive (`ON CONFLICT ... DO UPDATE`). If you need to undo:

```sql
-- Remove all Met rows from the prod DB (destructive — requires prod DB access)
DELETE FROM artworks WHERE source = 'met';
```

The job definition itself can be removed from Bicep without affecting the DB or the API:

```bash
# Delete just the job (leaves DB, API, all other resources untouched)
az containerapp job delete -n ${JOB} -g ${RG} --yes
```

---

## Resource sizing

| Resource | Value | Rationale |
|---|---|---|
| vCPU | 2.0 | SigLIP CPU inference is single-threaded; 2 vCPU gives headroom without wasting credits |
| Memory | 4 Gi | SigLIP model weights ~1.5 GiB + Python + asyncpg buffers + overhead. Prior OOM at 2 Gi (v1 path). 4 Gi provides safety margin. |
| Parallelism | 1 | Single-worker is the correct shape for Met API's per-IP rate limit (D-053). |
| replicaTimeout | 14400 s | 4 hr ceiling. Expected run: ~3–3.5 hr. |
| replicaRetryLimit | 1 | One automatic retry if the job exits non-zero. With `--resume-skip-existing`, a restart is cheap. |

---

## Cost estimate

| Component | Duration | Rate | Estimate |
|---|---|---|---|
| Container Apps Job, 2 vCPU / 4 Gi | ~3.5 hr | ~$0.00004 vCPU-s + ~$0.000004 GiB-s | ~$1.00–1.50 per run |
| Met API calls | ~250K | Free (no key) | $0 |
| Egress (CSV download ~300 MB + image bytes ~500 MB) | one-time | ~$0.09/GB outbound | ~$0.07 |
| **Total per full run** | | | **~$1.10–1.60** |

This is well within the $100/mo budget alert threshold.

---

## Assumptions and design choices

1. **Reuse the API image** (no `Dockerfile.ingest`). The `art-guide-api` image already has `services/ml` installed and `art-guide-ml` on PATH. A separate ingest image would halve the image size but add a second build pipeline with minimal real benefit — the image is not pulled on hot paths.

2. **CSV cache is ephemeral per run.** `~/.cache/art-guide/met-objects.csv` (300 MB) is downloaded at job startup every time — there is no persistent volume on ACA Jobs v1 (Azure Files mounts are available but add complexity). At ~2 min download time for a ~3–4 hr job, this is acceptable overhead. Set `--cache-max-age-days 0` to always re-download.

3. **`--resume-skip-existing` is always on in the ACA job command.** The first run has 0 existing Met rows so the skip set is empty and there is zero performance overhead. On restarts it provides the resume benefit.

4. **Request delay 0.015 s (~66 req/s) for single worker.** This is below Met's published 80 req/s cap. Do NOT raise above 0.012 s (83 req/s) without monitoring for 403 responses.

5. **No Met API key required.** The Met Open Access API is anonymous. If this changes in the future, add a KV secret `met-api-key` and inject via env var `MET_API_KEY`.

6. **The existing Bicep job resource is updated, not replaced.** `art-guide-prod-ingest` is already defined. Only the `command` array and `image` tag change.
