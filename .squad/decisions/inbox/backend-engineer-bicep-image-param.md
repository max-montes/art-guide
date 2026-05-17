# D-NEW — Bicep template: parameterized API image with non-placeholder defaults

**Date:** 2026-05-17 | **Owner:** backend-engineer | **Status:** Active

## Decision

`infra/azure/main.bicep` must expose the API container image, CPU, memory, and
target port as Bicep **parameters** with sensible, non-placeholder defaults that
match the current live production image.

Hardcoding a sample/hello-world image as a "bootstrap" value — or leaving
`containerPort: 80` in `parameters.prod.json` — is prohibited. Any future
re-deploy of the Bicep MUST NOT change the API container's image unless the
operator explicitly overrides `apiImage` in the parameters file.

## Why

On 2026-05-17, ml-retrieval-engineer deployed `main.bicep` to add the Met
ingest job (`art-guide-prod-ingest`). Because:
1. `parameters.prod.json` had `"containerPort": 80` (inherited from an old
   "placeholder" first-deploy phase, never updated to 8000).
2. The API container's image was not parameterized — any re-deploy could reset
   it if the Bicep default changed.
3. The registry binding (`registries: identity: system`) was wiped.

...the API container was reset to `mcr.microsoft.com/azuredocs/containerapps-helloworld:latest`
with port 80 ingress. The iOS app received HTML instead of JSON → decode error.
Brady's app was broken until remediation completed (~10 min downtime).

## Changes made

**`infra/azure/main.bicep`:**
- Added `param apiImage string = 'artguideprodcr.azurecr.io/art-guide-api:v2'`
- Added `param apiCpu string = '1.0'`
- Added `param apiMemory string = '2Gi'`
- Container resource now uses `image: apiImage`, `cpu: json(apiCpu)`, `memory: apiMemory`
- Added warning comment block at file top forbidding placeholder image defaults
- Removed stale "placeholder image" language from Phase 3 comment

**`infra/azure/parameters.prod.json`:**
- `"containerPort"` changed from `80` → `8000`
- Added `"apiImage"`, `"apiCpu"`, `"apiMemory"` as explicit overrides

**`infra/azure/deploy.sh`:**
- Added `what-if` pre-deploy guard that hard-fails if `containerapps-helloworld`
  would be deployed to the API container
- Updated "Phase 3" summary line to show actual image name, not "placeholder image"

## Rule going forward

When adding a new resource to `main.bicep`:
1. Run `az deployment group what-if` first.
2. Confirm the API container shows "No change" for image / CPU / memory / targetPort.
3. If it shows a change you didn't intend → fix `parameters.prod.json` or the Bicep param, don't proceed.

The `deploy.sh` what-if guard enforces this automatically for the hello-world regression case.
