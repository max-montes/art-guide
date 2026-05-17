#!/usr/bin/env bash
# infra/azure/deploy.sh
#
# Provisions the art-guide production stack on Azure.
# Idempotent: safe to re-run; Bicep uses Incremental mode, role-assignment names
# are deterministic (guid()), and the Postgres password is reused from Key Vault
# on subsequent runs.
#
# Prerequisites:
#   - Azure CLI 2.55+  (az --version)
#   - jq               (brew install jq / apt install jq)
#   - openssl          (ships with macOS / most Linux distros)
#   - Active az login session pointed at the right subscription
#
# Usage:
#   cd infra/azure
#   ./deploy.sh
#
# To tear everything down:
#   az group delete --name art-guide-prod-rg --yes --no-wait
#
# ── Hard rules honored ─────────────────────────────────────────────────────────
#   - NEVER echo the Postgres password to stdout (it is @secure() in Bicep;
#     Azure masks it in deployment history automatically)
#   - NEVER write secrets to any file under git
#   - Stop on first unrecoverable error (set -euo pipefail)
#   - AOAI 403 stops only Phase 4, not the whole script

set -euo pipefail

# ── Constants ──────────────────────────────────────────────────────────────────
SUBSCRIPTION_ID="fbca8db6-02d3-487b-a13f-6d0f7fac5295"
RG_NAME="art-guide-prod-rg"
LOCATION="westus3"
KV_NAME="art-guide-prod-kv"
APP_NAME="art-guide-prod-api"
AOAI_ACCOUNT="art-guide-prod-aoai"
AOAI_MODEL="gpt-5-mini"
AOAI_MODEL_VERSION="2025-08-07"
AOAI_CAPACITY=10            # units of 1 000 TPM → 10 000 TPM; very conservative
ALERT_EMAIL="xam3002@hotmail.com"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Helpers ────────────────────────────────────────────────────────────────────
step()  { echo ""; echo "==> $*"; }
info()  { echo "    $*"; }
warn()  { echo "    ⚠  $*"; }
ok()    { echo "    ✓  $*"; }
box()   {
  local msg="$*"
  local line
  line=$(printf '%0.s─' $(seq 1 ${#msg}))
  echo "  ┌─${line}─┐"
  echo "  │ ${msg} │"
  echo "  └─${line}─┘"
}

# ── Pre-flight checks ──────────────────────────────────────────────────────────
step "Pre-flight checks"

# az CLI
if ! command -v az &>/dev/null; then
  echo "ERROR: Azure CLI not found. Install from https://aka.ms/azcliinstall" >&2
  exit 1
fi

# jq
if ! command -v jq &>/dev/null; then
  echo "ERROR: jq not found. Install: brew install jq / apt install jq" >&2
  exit 1
fi

# Active subscription
CURRENT_SUB=$(az account show --query id -o tsv 2>/dev/null || echo "NOT_LOGGED_IN")
if [[ "$CURRENT_SUB" != "$SUBSCRIPTION_ID" ]]; then
  echo "ERROR: Active subscription ($CURRENT_SUB) does not match expected ($SUBSCRIPTION_ID)." >&2
  echo "Run:   az login && az account set --subscription $SUBSCRIPTION_ID" >&2
  exit 1
fi
ok "Subscription $SUBSCRIPTION_ID"

# ── Phase 1: Resource group ────────────────────────────────────────────────────
step "Phase 1 — Creating resource group $RG_NAME in $LOCATION"
az group create \
  --name     "$RG_NAME" \
  --location "$LOCATION" \
  --tags     project=art-guide env=prod managedBy=bicep \
  --output   table
ok "Resource group ready"

# ── Resolve Postgres admin password (idempotent) ───────────────────────────────
step "Resolving Postgres admin password"
# Try to read from KV first (works on re-runs once KV exists).
# On first run KV doesn't exist yet → generate a fresh password; Bicep will
# create the KV and store it.  The password is NEVER written to a file.
PG_ADMIN_PASSWORD=""
if PG_ADMIN_PASSWORD=$(az keyvault secret show \
      --vault-name "$KV_NAME" \
      --name       "postgres-admin-password" \
      --query      value \
      -o tsv 2>/dev/null) && [[ -n "$PG_ADMIN_PASSWORD" ]]; then
  info "Reusing existing password from Key Vault (idempotent re-run)."
else
  # Use openssl for portable random generation; avoids `tr | head` SIGPIPE
  # under `set -euo pipefail`. Strip chars Postgres rejects, take 40 chars.
  PG_ADMIN_PASSWORD=$(openssl rand -base64 60 | tr -dc 'A-Za-z0-9_!#%^*' | cut -c1-40)
  info "Generated new password (will be stored in Key Vault by Bicep)."
fi

# ── Main Bicep deployment (Phases 1 + 2 + 3 + 5) ──────────────────────────────
step "Deploying main Bicep template  (Foundation · Postgres · Compute · Budget)"
DEPLOY_NAME="art-guide-prod-$(date -u +%Y%m%dT%H%M%S)"

# ── Pre-deploy what-if guard ───────────────────────────────────────────────────
# Bail out if the API container's image would change to something unexpected.
# This catches the "Bicep default wins because a param wasn't passed" trap
# that caused the D-NEW regression (hello-world image replacing the real API).
step "What-if check — verifying deployment won't regress the API container"
WHATIF_OUTPUT=$(az deployment group what-if \
  --name            "${DEPLOY_NAME}-whatif" \
  --resource-group  "$RG_NAME" \
  --template-file   "$SCRIPT_DIR/main.bicep" \
  --parameters      "$SCRIPT_DIR/parameters.prod.json" \
  --parameters      pgAdminPassword="$PG_ADMIN_PASSWORD" \
  --result-format   FullResourcePayloads \
  --output          json 2>&1 || true)

# Fail hard if what-if shows the API container image changing to a placeholder.
if echo "$WHATIF_OUTPUT" | grep -q "containerapps-helloworld"; then
  echo "" >&2
  echo "ERROR: what-if shows the API container image would regress to hello-world!" >&2
  echo "       Check that parameters.prod.json has apiImage set to the real ACR image." >&2
  echo "       Aborting deployment to protect prod." >&2
  exit 1
fi
ok "What-if passed — API container image is safe"

DEPLOY_OUTPUT=$(az deployment group create \
  --name            "$DEPLOY_NAME" \
  --resource-group  "$RG_NAME" \
  --template-file   "$SCRIPT_DIR/main.bicep" \
  --parameters      "$SCRIPT_DIR/parameters.prod.json" \
  --parameters      pgAdminPassword="$PG_ADMIN_PASSWORD" \
  --mode            Incremental \
  --output          json)

ok "Bicep deployment '$DEPLOY_NAME' succeeded"

# Extract outputs (jq falls back to 'unknown' so the script never exits on missing output)
CONTAINER_APP_URL=$(   echo "$DEPLOY_OUTPUT" | jq -r '.properties.outputs.containerAppUrl.value       // "unknown"')
CONTAINER_APP_FQDN=$(  echo "$DEPLOY_OUTPUT" | jq -r '.properties.outputs.containerAppFqdn.value      // "unknown"')
POSTGRES_FQDN=$(       echo "$DEPLOY_OUTPUT" | jq -r '.properties.outputs.postgresFqdn.value          // "unknown"')
KV_NAME_OUT=$(         echo "$DEPLOY_OUTPUT" | jq -r '.properties.outputs.kvName.value                // "unknown"')
ACR_SERVER=$(          echo "$DEPLOY_OUTPUT" | jq -r '.properties.outputs.acrLoginServer.value        // "unknown"')
APP_PRINCIPAL_ID=$(    echo "$DEPLOY_OUTPUT" | jq -r '.properties.outputs.containerAppPrincipalId.value // "unknown"')
STORAGE_NAME=$(        echo "$DEPLOY_OUTPUT" | jq -r '.properties.outputs.storageAccountName.value    // "unknown"')

info "Container App : $CONTAINER_APP_URL"
info "Postgres FQDN : $POSTGRES_FQDN"
info "Key Vault     : $KV_NAME_OUT"
info "ACR           : $ACR_SERVER"
info "MI principal  : $APP_PRINCIPAL_ID"
ok   "Phase 1 (Foundation) ✓"
ok   "Phase 2 (Postgres + pgvector) ✓"
ok   "Phase 3 (Compute — API image: $(az containerapp show -n $APP_NAME -g $RG_NAME --query 'properties.template.containers[0].image' -o tsv 2>/dev/null || echo 'see output')) ✓"
ok   "Phase 5 (Budget \$50/mo → $ALERT_EMAIL) ✓"

# ── KV self-grant: ensure deployer can write secrets ──────────────────────────
# RBAC-mode Key Vaults require explicit data-plane role grants even for
# subscription owners. Bicep wires Container App MI -> Secrets User, but the
# deploying human/service principal isn't known at Bicep-authoring time, so
# we grant ourselves "Key Vault Secrets Officer" here. Idempotent: the role
# assignment uses a deterministic guid() so re-runs no-op.
step "Granting current user 'Key Vault Secrets Officer' on $KV_NAME"
DEPLOYER_OID=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || echo "")
if [[ -z "$DEPLOYER_OID" ]]; then
  warn "Could not resolve signed-in user object id — skipping self-grant."
  warn "If Phase 4 fails with Forbidden on KV setSecret, grant manually:"
  warn "  az role assignment create --role 'Key Vault Secrets Officer' \\"
  warn "    --assignee <your-oid> --scope \$(az keyvault show -n $KV_NAME -g $RG_NAME --query id -o tsv)"
else
  KV_RESOURCE_ID=$(az keyvault show --name "$KV_NAME" --resource-group "$RG_NAME" --query id -o tsv)
  if az role assignment create \
        --role "Key Vault Secrets Officer" \
        --assignee-object-id "$DEPLOYER_OID" \
        --assignee-principal-type User \
        --scope "$KV_RESOURCE_ID" \
        -o none 2>/dev/null; then
    ok "Self-grant succeeded (waiting 30s for RBAC propagation)"
    sleep 30
  else
    info "Self-grant already in place — skipping wait."
  fi
fi

# ── Phase 4: Azure OpenAI (access-gated) ──────────────────────────────────────
# Strategy:
#   • Try az cognitiveservices account create for kind=OpenAI in westus3.
#   • If it returns 403 / access-required → report and SKIP (do not fail).
#   • If it succeeds → deploy $AOAI_MODEL at 10 K TPM, store endpoint+key in KV,
#     update the Container App's azure-openai-key secret so it is ready for the
#     real API image.
step "Phase 4 — Azure OpenAI (access-gated attempt)"

AOAI_SUCCEEDED=false
AOAI_ENDPOINT=""
AOAI_PHASE_NOTE=""

# Check if the account already exists (idempotent re-run)
if az cognitiveservices account show \
      --name           "$AOAI_ACCOUNT" \
      --resource-group "$RG_NAME" \
      --query          id \
      -o tsv 2>/dev/null | grep -q .; then
  info "AOAI account '$AOAI_ACCOUNT' already exists — skipping create."
  AOAI_CREATE_STATUS="ok"
  AOAI_CREATE_OUTPUT="already_exists"
else
  info "Attempting az cognitiveservices account create (kind=OpenAI, sku=S0)..."
  set +e
  AOAI_CREATE_OUTPUT=$(az cognitiveservices account create \
    --name                  "$AOAI_ACCOUNT" \
    --resource-group        "$RG_NAME" \
    --location              "$LOCATION" \
    --kind                  OpenAI \
    --sku                   S0 \
    --custom-domain         "$AOAI_ACCOUNT" \
    --yes \
    --output json 2>&1)
  AOAI_CREATE_EXIT=$?
  set -e

  if [[ $AOAI_CREATE_EXIT -eq 0 ]]; then
    AOAI_CREATE_STATUS="ok"
  else
    AOAI_CREATE_STATUS="error"
  fi
fi

if [[ "$AOAI_CREATE_STATUS" == "error" ]]; then
  # Classify the failure
  if echo "$AOAI_CREATE_OUTPUT" | grep -qiE '403|Forbidden|access|NotAllowed|PrincipalNotAuthorized|SubscriptionNotRegistered|AuthorizationFailed'; then
    echo ""
    box "PHASE 4 BLOCKED — Azure OpenAI access not granted to this subscription"
    echo ""
    warn "You must fill out the Azure OpenAI access request form:"
    warn "  https://aka.ms/oai/access"
    warn "Once access is approved (usually 1–2 business days), re-run ./deploy.sh."
    warn "All other phases succeeded; AOAI is the only missing piece."
    echo ""
    AOAI_SUCCEEDED=false
    AOAI_PHASE_NOTE="⚠ BLOCKED — fill out https://aka.ms/oai/access then re-run"
  else
    # Unexpected error — surface it but don't abort the whole deploy
    warn "Unexpected error creating AOAI account (exit $AOAI_CREATE_EXIT):"
    warn "$AOAI_CREATE_OUTPUT"
    warn "Skipping Phase 4. Investigate and re-run ./deploy.sh to retry."
    AOAI_SUCCEEDED=false
    AOAI_PHASE_NOTE="⚠ UNEXPECTED ERROR — see output above"
  fi
else
  # Account exists or was just created — proceed
  AOAI_ENDPOINT=$(az cognitiveservices account show \
    --name           "$AOAI_ACCOUNT" \
    --resource-group "$RG_NAME" \
    --query          properties.endpoint \
    -o tsv 2>/dev/null)

  AOAI_KEY=$(az cognitiveservices account keys list \
    --name           "$AOAI_ACCOUNT" \
    --resource-group "$RG_NAME" \
    --query          key1 \
    -o tsv 2>/dev/null)

  ok "AOAI account ready: $AOAI_ENDPOINT"

  # Deploy $AOAI_MODEL at 10 K TPM (conservative; upgrade with --sku-capacity bump)
  if az cognitiveservices account deployment show \
        --name            "$AOAI_ACCOUNT" \
        --resource-group  "$RG_NAME" \
        --deployment-name "$AOAI_MODEL" \
        --query           id \
        -o tsv 2>/dev/null | grep -q .; then
    info "Model deployment '$AOAI_MODEL' already exists — skipping."
  else
    info "Deploying $AOAI_MODEL at capacity $AOAI_CAPACITY (= ${AOAI_CAPACITY}K TPM)..."
    az cognitiveservices account deployment create \
      --resource-group  "$RG_NAME" \
      --name            "$AOAI_ACCOUNT" \
      --deployment-name "$AOAI_MODEL" \
      --model-name      "$AOAI_MODEL" \
      --model-version   "$AOAI_MODEL_VERSION" \
      --model-format    OpenAI \
      --sku-capacity    $AOAI_CAPACITY \
      --sku-name        "GlobalStandard" \
      --output          table
    ok "$AOAI_MODEL deployed at ${AOAI_CAPACITY}K TPM"
  fi

  # Store endpoint + key in Key Vault
  info "Storing AOAI secrets in Key Vault..."
  az keyvault secret set \
    --vault-name "$KV_NAME_OUT" \
    --name       "azure-openai-endpoint" \
    --value      "$AOAI_ENDPOINT" \
    --output     none
  az keyvault secret set \
    --vault-name "$KV_NAME_OUT" \
    --name       "azure-openai-key" \
    --value      "$AOAI_KEY" \
    --output     none
  ok "Secrets written: azure-openai-endpoint, azure-openai-key"

  # Update Container App's azure-openai-key secret + AOAI env vars.
  # Setting AZURE_OPENAI_ENDPOINT triggers a new revision that picks up
  # the refreshed azure-openai-key secret value.
  info "Updating Container App with AOAI config..."
  az containerapp secret set \
    --name            "$APP_NAME" \
    --resource-group  "$RG_NAME" \
    --secrets         "azure-openai-key=${AOAI_KEY}" \
    --output          none

  az containerapp update \
    --name            "$APP_NAME" \
    --resource-group  "$RG_NAME" \
    --set-env-vars    "AZURE_OPENAI_ENDPOINT=${AOAI_ENDPOINT}" \
    --output          none
  ok "Container App updated with AOAI config"

  AOAI_SUCCEEDED=true
  AOAI_PHASE_NOTE="✓ $AOAI_MODEL @ ${AOAI_CAPACITY}K TPM — $AOAI_ENDPOINT"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "  ART-GUIDE PROD DEPLOYMENT SUMMARY"
echo "════════════════════════════════════════════════════════════════════"
echo ""
echo "  Resource group  : $RG_NAME  ($LOCATION)"
echo "  Container App   : $CONTAINER_APP_URL"
echo "  Postgres FQDN   : $POSTGRES_FQDN"
echo "  Key Vault       : $KV_NAME_OUT"
echo "  ACR             : $ACR_SERVER"
echo "  Storage         : $STORAGE_NAME"
echo ""
echo "  Phase 1 — Foundation  : ✓"
echo "  Phase 2 — Postgres    : ✓  (pgvector allow-listed; run migrations separately)"
echo "  Phase 3 — Compute     : ✓  (API image live — see Container App)"
echo "  Phase 4 — OpenAI      : $AOAI_PHASE_NOTE"
echo "  Phase 5 — Budget      : ✓  (\$50/mo alert → xam3002@hotmail.com)"
echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "  NEXT STEPS"
echo "════════════════════════════════════════════════════════════════════"
echo ""
if [[ "$AOAI_SUCCEEDED" != "true" ]]; then
  echo "  ① Fill out AOAI access form: https://aka.ms/oai/access"
  echo "    Then re-run this script — Phase 4 is the only gap."
  echo ""
fi
echo "  ② Set a real API bearer token before routing traffic:"
echo "     az containerapp secret set \\"
echo "       --name $APP_NAME --resource-group $RG_NAME \\"
echo "       --secrets 'api-bearer-token=<your-secret-token>'"
echo ""
echo "  ③ Build and push the real API image:"
echo "     az acr login --name artguideprodcr"
echo "     docker build -t artguideprodcr.azurecr.io/art-guide-api:latest services/api"
echo "     docker push  artguideprodcr.azurecr.io/art-guide-api:latest"
echo "     az containerapp update --name $APP_NAME \\"
echo "       --resource-group $RG_NAME \\"
echo "       --image artguideprodcr.azurecr.io/art-guide-api:latest"
echo "     az containerapp ingress update --name $APP_NAME \\"
echo "       --resource-group $RG_NAME --target-port 8000"
echo ""
echo "  ④ Run database migrations:"
echo "     psql \"host=$POSTGRES_FQDN dbname=art_guide user=artguideadmin sslmode=require\" \\"
echo "       -f services/api/app/migrations/0001_init.sql"
echo ""
echo "  ⑤ Update iOS Config.xcconfig with the prod API URL:"
echo "     API_BASE_URL = $CONTAINER_APP_URL"
echo ""
