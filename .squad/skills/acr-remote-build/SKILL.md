# Skill: ACR remote build for fat ML images

**Captured:** 2026-05-16 by backend-engineer
**Use when:** You need to build a Docker image with bundled ML model weights
(typically 0.5–5 GiB final size) and ship it to Azure Container Registry,
from a developer machine on a residential connection, possibly without
Docker Desktop available.

## The trick

`az acr build` ships only the build context (tarball) to ACR, then ACR's
hosted build agent runs `docker build` and pushes the final image — all
inside Azure. The local machine never sees the multi-GB layers.

```bash
az acr build \
  --registry artguideprodcr \
  --image art-guide-api:v1 --image art-guide-api:latest \
  --file services/api/Dockerfile .          # context = repo root
```

You can pass multiple `--image` flags to tag the same build with multiple
names (e.g., a moving `latest` plus a pinned `vN`).

## Why this beats `docker build && docker push`

| Concern                      | Local build + push                      | `az acr build`                       |
| ---------------------------- | --------------------------------------- | ------------------------------------ |
| Upload size                  | ~1 GiB image layers per push            | ~few hundred KiB of build context    |
| Docker Desktop required      | Yes                                     | No                                   |
| Build CPU / RAM              | Your laptop                             | Azure agent (faster, free for first 6000 min/mo on Basic SKU) |
| Layer cache                  | Local Docker layer cache                | Centralized cache in ACR Tasks       |
| Intra-region pull to ACA     | After upload                            | Already in-region                    |
| Cross-platform (M1/M2 Macs)  | Need `--platform linux/amd64`           | Always builds for linux/amd64        |

## Tighten the build context with .dockerignore

`az acr build` honors `.dockerignore` (and prints what it excludes in the
upload step). For monorepos where the build context is the repo root,
aggressive exclusions are critical — exclude `**/.venv/`, `apps/`, `docs/`,
`infra/`, `.git/`, `.squad/`, `**/tests/`, `.env*` (keep `.env.example`),
`.secrets-local/`, etc.

**Gotcha:** if your `pyproject.toml` lists a package directory
(e.g. `packages = ["evals"]`), you cannot exclude that directory from the
build context — pip's `egg_info` step aborts with
`package directory 'evals' does not exist`. Either keep it in context, or
rewrite the package list to be auto-discovered (`find_packages`).

## Wire ACR to the Container App once

Use a system-assigned managed identity so the Container App pulls from ACR
without a stored password:

```bash
az containerapp registry set \
  --name <app> --resource-group <rg> \
  --server <acr>.azurecr.io --identity system
```

The Bicep template in `infra/azure/main.bicep` already grants `AcrPull`
to the system MI. After the first `registry set`, every subsequent
`az containerapp update --image …` works with no extra auth.

## Cold-start gotcha when switching images

When an inactive (`ScaledToZero`) revision is the active revision and
ingress hits it, the request blocks while the container pulls the image
AND runs lifespan startup (e.g., `warm_embedder()`). On a fat ML image,
that can exceed the default 240 s ingress timeout — the first request
returns a connection reset. Subsequent requests work fine because the
container is already warm.

Mitigations:
- Set `minReplicas=1` on the Container App (~$5–10/mo on Consumption).
- Move heavy warm-up to a background task; route returns 503 until ready.
- Prime the revision after deploy with a throwaway curl before traffic.

## See also

- `infra/azure/deploy.sh` — the Bicep deploy that provisioned ACR + ACA.
- `.squad/decisions/inbox/backend-engineer-dockerfile-v1.md` — the v1 image.
- `.squad/skills/azure-bicep-prod-provisioning/SKILL.md` — broader prod stack.
