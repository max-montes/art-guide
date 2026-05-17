# Skill: Azure Bicep Prod Provisioning (Phased, Cost-Gated)

**Confidence:** High (burned twice — D-038 codified the hard lessons)  
**Captured:** 2026-05-10 | **Last updated:** 2026-05-17  
**Agent:** backend-engineer

---

## Pattern

Provision a multi-resource Azure stack for a solo/portfolio project using Bicep
at resource group scope, with a thin `deploy.sh` wrapper that:
1. Verifies az context before spending money
2. Handles secrets in KV without ever writing them to disk
3. Gates access-dependent resources (Azure OpenAI) as a separate step that
   fails gracefully without aborting the full deployment
4. Is idempotent: safe to re-run at any state of partial completion

---

## Key decisions and why

### Bicep at resource group scope (not subscription scope)
- `az deployment group create` is simpler and faster than `az deployment sub create`
- The spec called for `az deployment group create` explicitly
- Budget (`Microsoft.Consumption/budgets`) can be deployed at RG scope with
  an explicit `filter.dimensions.ResourceGroupName` filter
- Trade-off: to create the RG itself, `deploy.sh` runs `az group create` before
  the Bicep invocation (one idempotent CLI call)

### Postgres password never touches disk
- `deploy.sh` generates password with `tr -dc ... </dev/urandom | head -c 40`
- Passed as `--parameters pgAdminPassword=<value>` — Azure masks `@secure()` params
  in deployment history automatically
- On re-runs, `deploy.sh` reads the existing password from KV before invoking
  Bicep, ensuring idempotency even if Postgres already has the password set

### Azure OpenAI access gate
- The AOAI quota/access check CANNOT be done as a dry run — you must attempt
  the `az cognitiveservices account create` call and inspect the exit code + stderr
- Key signal strings for 403: `403 | Forbidden | access | NotAllowed |
  PrincipalNotAuthorized | SubscriptionNotRegistered | AuthorizationFailed`
- Pattern: `set +e` around the create call, capture output, `set -e`, classify

### Role assignments are idempotent via deterministic GUID
- `guid(scope, principalId, roleDefinitionId)` produces the same assignment name
  every time → `az deployment group create` with Incremental mode skips existing

### Container App secrets vs KV references
- Full KV-reference pattern (`keyVaultUrl` in ACA secrets) has a circular
  dependency: the system-assigned identity is created *with* the Container App,
  but KV RBAC must be set *before* ACA validates the KV reference at deploy time
- Solution: store secret *values* in ACA's encrypted secret store at deploy time
  (Bicep `secrets[].value`). KV still holds the master copies for rotation.
  The KV Secrets User RBAC is set up for future use (e.g. migrate to KV refs
  with a user-assigned identity in a later iteration)

---

## Naming constraints cheat sheet

| Resource type | Constraints | Example |
|---|---|---|
| ACR | No hyphens, globally unique, 5–50 alphanumeric | `artguideprodcr` |
| Storage | No hyphens, globally unique, 3–24 lowercase alphanumeric | `artguideprodst` |
| Key Vault | Max 24 chars, alphanumeric+hyphens, no leading/trailing hyphen | `art-guide-prod-kv` |
| AOAI account | Custom domain becomes `{name}.openai.azure.com` — globally unique | `art-guide-prod-aoai` |

---

## Cost guardrails for personal subscription

- `minReplicas: 0` on Container Apps = $0 compute at idle
- Postgres B1ms is the cheapest Flexible Server SKU (~$15/mo)
- Budget alert (`Microsoft.Consumption/budgets`) at subscription or RG scope
- AOAI: start at 10 K TPM capacity; bump `--sku-capacity` only if eval proves need

---

## Gotchas learned

1. **Budget at RG scope:** `Microsoft.Consumption/budgets` supports RG scope in
   the ARM API. Add explicit `filter.and[].dimensions.ResourceGroupName` filter.
2. **`pgvector` allow-list:** Must set `azure.extensions = vector` via
   `Microsoft.DBforPostgreSQL/flexibleServers/configurations` BEFORE running
   `CREATE EXTENSION IF NOT EXISTS vector` in SQL. Without this, the extension
   create will fail with a permissions error.
3. **AOAI `--custom-domain`:** Required for Azure OpenAI accounts. The domain
   becomes the subdomain of the endpoint URL. Must be globally unique.
4. **KV `enableRbacAuthorization: true`:** Switches from legacy access policies
   to RBAC. Role assignments on the KV then control who can read secrets.
   Do NOT mix both models on the same KV.
5. **Container App FQDN:** Available as
   `containerApp.properties.configuration.ingress.fqdn` in Bicep outputs.
   Format: `{appname}.{envhash}.{region}.azurecontainerapps.io`

---

## ⚠️ Critical lessons from D-038 prod outage (2026-05-17)

**Confidence:** High (burned by this twice in one week)

### NEVER hardcode a placeholder image as the API container default

The initial "Phase 3 (placeholder image)" pattern left
`mcr.microsoft.com/azuredocs/containerapps-helloworld:latest` as the image.
When another agent re-deployed the Bicep to add a new resource, the default won —
resetting the live API to hello-world. Brady's iOS app broke immediately.

**Rule:** Always parameterize `apiImage` (and `apiCpu`, `apiMemory`) with defaults
that match the *current live prod image*, never a sample image. Also pin them
explicitly in `parameters.prod.json`.

### Always run `what-if` before `az deployment group create`

`az deployment group what-if` catches unintended regressions before they hit prod.
Add a guard in `deploy.sh` that fails if the API container image would change to
something unexpected. Expression-drift (principalId as reference expression vs
hardcoded value) is safe to ignore — it does not cause actual resource changes.

### The registry binding can be wiped by a Bicep redeploy

If `properties.configuration.registries` is absent from the Bicep template at
deploy time, ACA removes the existing ACR binding. The MI remains but image pull
fails with UNAUTHORIZED. Keep `registries: [{server: '...', identity: 'system'}]`
in the template at all times.

### `az acr build` requires the repo root as context (not `services/api/`)

The Dockerfile COPYs from both `services/api/` and `services/ml/`. If you pass
`services/api` as the build context, the build fails with
`"COPY failed: services/ml: file not found"`.

Correct invocation:
```bash
az acr build \
  --registry artguideprodcr \
  --image art-guide-api:v2 \
  --file services/api/Dockerfile \
  .    # ← repo root, not services/api/
```

### Gotcha quick-reference table

| Symptom | Root cause | Fix |
|---|---|---|
| Container App shows hello-world after Bicep deploy | `apiImage` not parameterized or `parameters.prod.json` had wrong value | Add `param apiImage` with real default; pin in params; run what-if |
| `/healthz` returns HTML | `targetPort` set to 80 (hello-world) not 8000 (uvicorn) | `az containerapp ingress update --target-port 8000`; fix params |
| Image pull UNAUTHORIZED from ACR | `registries` config wiped by Bicep redeploy | `az containerapp registry set --server <acr>.azurecr.io --identity system` |
| `az acr build` "services/ml: file not found" | Build context is `services/api/` not repo root | Use `.` as build context with `--file services/api/Dockerfile` |
