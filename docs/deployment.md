# Deployment Shape

Locked Azure stack for v1. Designed for the project owner's existing $150/mo Azure credits.

## Stack

| Concern | Choice |
| --- | --- |
| Compute | **Azure Container Apps** (autoscale, scales to zero) |
| Container registry | **Azure Container Registry (ACR), Basic tier** |
| Metadata + vector store | **Azure Database for PostgreSQL Flexible Server, Burstable B1ms, with `pgvector` extension** |
| Object storage (debug only) | **Azure Blob Storage** (off by default) |
| LLM | **Azure OpenAI Service** (GPT-4.1 / GPT-5-class deployment) |
| Evaluation | **Azure AI Foundry** evaluators (Phase 5+) |
| Secrets | **Azure Key Vault** |
| Identity | **Managed Identity** for the Container App, used to read secrets, pull from ACR, and access Blob/Postgres |
| Observability | **Azure Monitor + Log Analytics** |
| TLS / domain | Container Apps managed certificate |
| Region | **West US 3** |

## Vector store

- Postgres + pgvector hosts both the normalized artwork records and the embeddings.
- Single store. No separate FAISS file in production.
- For local dev, run Postgres + pgvector via Docker Compose.
- ANN index: HNSW on the embedding column with cosine distance (`vector_cosine_ops`).

## Cost guardrails

| Item | Estimated monthly |
| --- | --- |
| Container Apps (low traffic, scales to zero) | $0–10 |
| Postgres Flexible Server B1ms | ~$15 |
| ACR Basic | ~$5 |
| Blob Storage | ~$1 |
| Key Vault | ~$1 |
| Log Analytics | ~$2 |
| Azure OpenAI usage | variable; biggest risk |

Concrete safeguards:

- **Subscription budget alert at $50/mo** with email notification.
- Per-day cost cap on Azure OpenAI deployment.
- Container Apps min replicas = 0.
- Blob storage thumbnail mode is **off by default** (debug-only env flag).

## Environments

Two environments only:

| Environment | Where it runs | Used for |
| --- | --- | --- |
| `local` | Developer laptop via Docker Compose (Postgres + pgvector) | Day-to-day development, eval runs, integration tests. May call a low-cost Azure OpenAI deployment or be stubbed out entirely. |
| `prod` | Azure (West US 3) — Container Apps + Postgres Flexible Server + Azure OpenAI + Blob + Key Vault | The real iOS app, TestFlight, and demo target. |

Rules:

- Config is fully env-driven. No hardcoded URLs, keys, or model names.
- Same container image runs locally and in prod; only env vars differ.
- A `preview` environment may be added later if TestFlight beta testers need isolation from dev.

## Local dev

```text
docker compose up postgres
# brings up Postgres with pgvector preinstalled, on localhost:5432
```

The same `services/api` container image runs locally (`uvicorn`) or in Container Apps. The image must:

- Be reproducible (pinned base image, locked deps).
- Read all configuration from env vars.
- Pull secrets from Azure Key Vault when running with a Managed Identity, fall back to env/`.env` for local.

## Non-goals for v1

- No multi-region.
- No App Service.
- No Kusto / ADX.
- No Cosmos DB.
- No dedicated vector DB (Pinecone, Qdrant, Milvus).
- No GPU.

## Open items deferred to Phase 6 (deploy)

- Exact Azure OpenAI deployment name + version (pick at deploy time).
- ACR build pipeline (GitHub Actions vs. local push).
- Custom domain + DNS provider (only if we want a branded URL).
- TestFlight signing identity and bundle ID.

## Cost monitoring

**Portal — Cost Analysis blade** (bookmark this):

```
https://portal.azure.com/#@3626e07d-bb5c-4615-a0ed-abe35aaa2502/resource/subscriptions/fbca8db6-02d3-487b-a13f-6d0f7fac5295/resourceGroups/art-guide-prod-rg/costAnalysis
```

Replace the tenant ID and subscription ID tokens above if the subscription changes.

**Budget alert:** `art-guide-prod-monthly` — $100/mo limit, alerts at 50 / 80 / 100 % sent to the account owner email. Created 2026-05-16 (C.1). A legacy `art-guide-prod-budget` at $50 also exists.

**CLI cost queries (run from any shell with `az login`):**

```bash
SUB_ID=$(az account show --query id -o tsv)
RG=art-guide-prod-rg

# Month-to-date spend by service
az rest --method POST \
  --url "https://management.azure.com/subscriptions/${SUB_ID}/resourceGroups/${RG}/providers/Microsoft.CostManagement/query?api-version=2023-11-01" \
  --body '{
    "type": "ActualCost",
    "timeframe": "MonthToDate",
    "dataset": {
      "granularity": "None",
      "aggregation": { "totalCost": { "name": "Cost", "function": "Sum" } },
      "grouping": [{ "type": "Dimension", "name": "ServiceName" }]
    }
  }' \
  --query "properties.rows" -o json

# Budget-to-date (verify alerts are wired)
az consumption budget list --resource-group ${RG} -o table

# List all budgets with thresholds (raw)
az rest --method GET \
  --url "https://management.azure.com/subscriptions/${SUB_ID}/resourceGroups/${RG}/providers/Microsoft.Consumption/budgets/art-guide-prod-monthly?api-version=2023-11-01" \
  --query "properties.notifications"
```
