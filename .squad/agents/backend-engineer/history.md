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

## Learnings

- **IP-ban vs. rate-limit diagnosis:** Flatline of 403s with zero 200s in monitoring = IP-ban (not rate-limit). 429s with intermittent 200s = rate-limit, recoverable with exponential backoff. Checking log data alone is not sufficient — verify ground truth by querying the DB (`SELECT source, COUNT(*) FROM artworks GROUP BY source`).

- **Symptom adjacency lies in async codebases.** When an async handler hangs, the "hang happened at log line X" usually means the hang happened in a call *following* X, possibly many awaits deep. Three prior agents blamed asyncpg because `create_pool` is lexically next; only when ml-retrieval-engineer-3 ran a stack sample did the true culprit emerge: `from transformers import AutoModel` stalling on macOS amfid file verification.

- **Cache priming as accidental fix is invisible.** Three prior failed runs created `.pyc` files that unblocked the fourth. The "fix" is not in any diff. Document state-as-fix explicitly so future incarnations don't assume their code change worked.

