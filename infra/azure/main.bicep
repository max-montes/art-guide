// infra/azure/main.bicep
//
// Art-guide production stack — Azure resource group scope.
// Deploy via: infra/azure/deploy.sh
//
// Per docs/deployment.md: West US 3, D-003 (Postgres+pgvector+AzureOpenAI),
// D-011 (local + prod only), D-013 (v1 constraints).
//
// Tags every resource: project=art-guide  env=prod  managedBy=bicep
//
// Phases executed in this template:
//   1 — Foundation  : Log Analytics, Container Apps Env, ACR, Key Vault, Storage
//   2 — Stateful    : Postgres Flex Server + pgvector allow-list + art_guide database
//   3 — Compute     : Container App (real API image from ACR) + system-assigned MI
//        RBAC       : KV Secrets User · AcrPull · Storage Blob Data Contributor
//   5 — Guardrails  : Budget alert $50/mo at 80% threshold
//
// Phase 4 (Azure OpenAI) is handled in deploy.sh: it is access-gated and
// a 403 must stop only the AOAI step, not the whole deployment.
//
// ⚠️  NEVER hardcode a placeholder image (e.g. mcr.microsoft.com/azuredocs/
//     containerapps-helloworld:latest) for the API container. Use apiImage param.
//     NEVER set apiImage default to a public sample/demo image.
//     ALWAYS run `az deployment group what-if` before applying changes to prod.
//     If you add a new resource, run what-if first and confirm the API container
//     image / CPU / memory / targetPort show "no change" before applying.

// ── Parameters ────────────────────────────────────────────────────────────────

param location string = 'westus3'

// Naming prefix — drives most resource names
param prefix string = 'art-guide-prod'

// Globally unique names (ACR / Storage must contain no hyphens)
param acrName string     = 'artguideprodcr'   // 14 chars; ACR max 50, no hyphens
param storageName string = 'artguideprodst'   // 14 chars; Storage max 24, no hyphens
param kvName string      = 'art-guide-prod-kv' // 17 chars; KV max 24

// Postgres
param pgName string      = 'art-guide-prod-pg'
param pgAdminUser string = 'artguideadmin'

@secure()
param pgAdminPassword string

// Budget / alert thresholds (D-011)
param budgetAlertEmail string = 'xam3002@hotmail.com'
param budgetAmountUsd   int   = 50
param budgetStartDate   string = '2026-05-01'   // YYYY-MM-01; updated each calendar year

// ── API Container App image + sizing ──────────────────────────────────────────
//
// ⚠️  apiImage MUST point to a real ACR image (never a placeholder).
//     Override via parameters.prod.json or --parameters on the CLI.
//     Default is the current prod image — bump to a new tag when promoting.
//     D-NEW: these params were introduced to prevent Bicep re-deploys from
//     accidentally resetting the container to a sample/hello-world image.
//
param apiImage     string = 'artguideprodcr.azurecr.io/art-guide-api:v2'
param apiCpu       string = '1.0'
param apiMemory    string = '2Gi'
param containerPort int   = 8000

// ── Variables ─────────────────────────────────────────────────────────────────

var tags = {
  project: 'art-guide'
  env: 'prod'
  managedBy: 'bicep'
}

// asyncpg connection string stored as a KV secret and injected via secretRef
var dbUrl = 'postgresql://${pgAdminUser}:${pgAdminPassword}@${postgres.properties.fullyQualifiedDomainName}:5432/art_guide?sslmode=require'

// Well-known Azure built-in role definition IDs (same across all subscriptions)
var roleKvSecretsUser          = '4633458b-17de-408a-b874-0445c86b69e6'
var roleAcrPull                = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var roleStorageBlobDataContrib = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

// ── Phase 1: Foundation ───────────────────────────────────────────────────────

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${prefix}-logs'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource containerAppsEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: '${prefix}-cae'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false       // pull via managed identity; no admin creds
    publicNetworkAccess: 'Enabled'
  }
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true   // RBAC model, not legacy access policies
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Enabled'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false    // debug blob off by default (D-012)
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    accessTier: 'Hot'
  }
}

// ── Phase 2: Stateful — Postgres Flexible Server ─────────────────────────────

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2022-12-01' = {
  name: pgName
  location: location
  tags: tags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    administratorLogin: pgAdminUser
    administratorLoginPassword: pgAdminPassword
    version: '16'
    storage: { storageSizeGB: 32 }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
    authConfig: {
      passwordAuth: 'Enabled'
      activeDirectoryAuth: 'Disabled'
    }
  }
}

// Allow inbound from Azure services (required for Container Apps → Postgres)
resource postgresFirewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2022-12-01' = {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// Allow-list the pgvector extension so CREATE EXTENSION IF NOT EXISTS vector works
resource postgresExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2022-12-01' = {
  parent: postgres
  name: 'azure.extensions'
  properties: {
    value: 'vector'
    source: 'user-override'
  }
}

// Create the art_guide database (migrations run separately via AUTO_MIGRATE or psql)
resource postgresDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2022-12-01' = {
  parent: postgres
  name: 'art_guide'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// Admin password — single source of truth for rotation
resource kvSecretPgPassword 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'postgres-admin-password'
  properties: { value: pgAdminPassword }
}

// Full asyncpg connection string — rotated by updating this secret + redeploying
resource kvSecretDatabaseUrl 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'database-url'
  properties: { value: dbUrl }
}

// ── Phase 3: Compute — Container App with real API image ──────────────────────
//
// Image param: apiImage (default artguideprodcr.azurecr.io/art-guide-api:v1).
// Sizing param: apiCpu / apiMemory. Pull via system-assigned managed identity.
// Built in D-028. AcrPull RBAC wired below.

resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: '${prefix}-api'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: containerAppsEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: containerPort
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: '${acrName}.azurecr.io'
          identity: 'system'
        }
      ]
      // Secrets are ACA-encrypted at rest.
      // azure-openai-key and api-bearer-token must be set before routing real traffic:
      //   az containerapp secret set --name art-guide-prod-api \
      //     --resource-group art-guide-prod-rg \
      //     --secrets "azure-openai-key=<real-key>" "api-bearer-token=<real-token>"
      secrets: [
        { name: 'database-url',      value: dbUrl }
        { name: 'api-bearer-token',  value: 'PLACEHOLDER-must-be-set-before-live-traffic' }
        { name: 'azure-openai-key',  value: 'PLACEHOLDER-set-after-aoai-provisioning' }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: { cpu: json(apiCpu), memory: apiMemory }
          env: [
            { name: 'ENV',                        value: 'prod' }
            { name: 'DATABASE_URL',               secretRef: 'database-url' }
            { name: 'API_BEARER_TOKEN',            secretRef: 'api-bearer-token' }
            { name: 'AZURE_OPENAI_API_KEY',        secretRef: 'azure-openai-key' }
            { name: 'AZURE_KEYVAULT_NAME',         value: kvName }
            { name: 'AZURE_OPENAI_DEPLOYMENT',     value: 'gpt-5-mini' }
            { name: 'AZURE_OPENAI_API_VERSION',    value: '2024-02-01' }
            { name: 'DB_POOL_MIN_SIZE',            value: '1' }
            { name: 'DB_POOL_MAX_SIZE',            value: '5' }
            { name: 'PROMPT_LOG_SAMPLE_RATE',      value: '1.0' }
            { name: 'RATE_LIMIT_REQUESTS',         value: '100' }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 2 }
    }
  }
}

// ── RBAC Wiring ───────────────────────────────────────────────────────────────
// guid() is deterministic → same inputs = same assignment name = idempotent.

// Container App MI → Key Vault Secrets User (reads secrets at runtime)
resource raKvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, containerApp.id, roleKvSecretsUser)
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKvSecretsUser)
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Container App MI → AcrPull (pull the real API image once it's pushed)
resource raAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, containerApp.id, roleAcrPull)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAcrPull)
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Container App MI → Storage Blob Data Contributor (debug blob, off by default)
resource raStorageBlobContrib 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, containerApp.id, roleStorageBlobDataContrib)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageBlobDataContrib)
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── Phase 3b: Container Apps Job — Met full-catalog ingest ───────────────────
//
// Manual-trigger job that runs `art-guide-ml ingest met` (limit=0 → unlimited)
// to ingest the full Met Open Access catalog (~500K records) into prod
// Postgres+pgvector.
//
// Path D — "respect the published rate limit from one sender" (D-053):
// The Met's official rate limit (https://metmuseum.github.io/) is **80 requests
// per second per IP**. Previous parallel attempts (4-8 replicas × per-replica
// delays) summed *aggregate* req/s above 80 and got the Azure egress IP
// silently 403-throttled (Met returns 403 — not 429 — when over the per-IP
// cap, with a multi-hour cooldown). The fix is the OPPOSITE of parallelism:
// drop to a single worker pushing ~66 req/s (request_delay=0.015s), well
// under the 80 req/s ceiling. Single worker on full 501,696-record catalog
// projects ~2 hr at this rate (501k / 66 / 60 ≈ 127 min). No sharding needed.
//
// Image: art-guide-api:v4 — already contains the limit=0=unlimited bugfix
// AND the "don't-retry-4xx-non-429" fix that prevents per-record permanent
// 403/404s from eating 30s of exponential backoff per record.
//
// Same ACR image as the API repo. Overrides CMD to run the ML CLI instead
// of uvicorn. 2 vCPU / 4 Gi per replica — headroom for SigLIP model load.
// replicaTimeout 14400 s (4 hr) — generous ceiling for the ~2 hr expected run.
//
// Deploy the job:
//   az deployment group create -g art-guide-prod-rg -f infra/azure/main.bicep \
//     --parameters @infra/azure/parameters.prod.json
//
// Kick off a run:
//   az containerapp job start -n art-guide-prod-ingest -g art-guide-prod-rg
//
// Stream logs:
//   az containerapp job execution list -n art-guide-prod-ingest -g art-guide-prod-rg -o table
//   az containerapp job logs show -n art-guide-prod-ingest -g art-guide-prod-rg \
//     --execution <exec-name> --container ingest --follow

resource ingestJob 'Microsoft.App/jobs@2023-05-01' = {
  name: '${prefix}-ingest'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    environmentId: containerAppsEnv.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 14400
      replicaRetryLimit: 1
      // Path D (D-053): single worker. Parallelism was the wrong fix — adding
      // replicas without respecting the per-IP rate cap (80 req/s, official)
      // triggered Met's IP-throttle and silent 403-storms with multi-hour
      // cooldowns. One sender at 66 req/s (< 80) covers the full 501K
      // catalog in ~2 hr fetch-bound.
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: '${acrName}.azurecr.io'
          identity: 'system'
        }
      ]
      secrets: [
        { name: 'database-url', value: dbUrl }
      ]
    }
    template: {
      containers: [
        {
          name: 'ingest'
          image: '${acrName}.azurecr.io/art-guide-api:latest'
          resources: {
            cpu: json('2')
            memory: '4Gi'
          }
          // Override CMD: run the ML CLI instead of uvicorn.
          //   met-dump               → v2 CSV dump path (pre-filters ~50% of rows before API calls)
          //   --limit 0              → unlimited (process every id)
          //   --request-delay 0.015  → ~66 req/s, under Met's 80 req/s cap
          //   --batch-commit-size 64 → commits per txn (unchanged from D-046)
          //   --batch-size 4         → CPU embed sweet spot (no MPS on ACA)
          //   --resume-skip-existing → skip already-embedded rows on restart
          // No sharding flags — single worker means single shard (D-053).
          command: [
            'art-guide-ml'
            'ingest'
            'met-dump'
            '--limit'
            '0'
            '--request-delay'
            '0.015'
            '--batch-commit-size'
            '64'
            '--batch-size'
            '4'
            '--resume-skip-existing'
          ]
          env: [
            { name: 'DATABASE_URL',          secretRef: 'database-url' }
            { name: 'ENV',                   value: 'prod' }
            { name: 'HF_HUB_OFFLINE',        value: '1' }
            { name: 'TRANSFORMERS_OFFLINE',  value: '1' }
            { name: 'HF_HOME',               value: '/opt/hf-cache' }
          ]
        }
      ]
    }
  }
}

// Ingest job MI → Key Vault Secrets User
resource raKvSecretsUserJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, ingestJob.id, roleKvSecretsUser)
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKvSecretsUser)
    principalId: ingestJob.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Ingest job MI → AcrPull
resource raAcrPullJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, ingestJob.id, roleAcrPull)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAcrPull)
    principalId: ingestJob.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── Phase 5: Budget Alert ($50/mo @ 80%) ─────────────────────────────────────

resource budget 'Microsoft.Consumption/budgets@2021-10-01' = {
  name: '${prefix}-budget'
  properties: {
    category: 'Cost'
    amount: budgetAmountUsd
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: '${budgetStartDate}T00:00:00Z'
    }
    filter: {
      dimensions: {
        name: 'ResourceGroupName'
        operator: 'In'
        values: [resourceGroup().name]
      }
    }
    notifications: {
      CostAlert80: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [budgetAlertEmail]
      }
    }
  }
}

// ── Outputs ───────────────────────────────────────────────────────────────────

output containerAppUrl       string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output containerAppFqdn      string = containerApp.properties.configuration.ingress.fqdn
output postgresFqdn          string = postgres.properties.fullyQualifiedDomainName
output kvName                string = kv.name
output acrLoginServer        string = acr.properties.loginServer
output containerAppPrincipalId string = containerApp.identity.principalId
output resourceGroupName     string = resourceGroup().name
output storageAccountName    string = storage.name
output ingestJobName         string = ingestJob.name
