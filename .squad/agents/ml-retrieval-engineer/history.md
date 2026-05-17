# ML/Retrieval Engineer History (Condensed — 2026-05-17)

> **Full Phase 0–early Phase 1 work archived to `history-archive.md`. Current file: latest completed work + active next steps.**

## 2026-05-17 — Full Catalog Ingest Job + Parallel Sharding + Eval Baseline (D-050, D-051, D-052)

**Tasks completed:**
1. **Full Met Catalog Job (D-051):** Added `Microsoft.App/jobs` to Bicep (`art-guide-prod-ingest`), manual trigger, replicaTimeout 7200s, command: `art-guide-ml ingest met --limit 0 --batch-commit-size 64`. Execution `art-guide-prod-ingest-brsioxp` started 2026-05-17T00:51:42Z (projected 23 hr).
2. **Eval Baseline (D-050):** Ran harness against 100-record prod catalog. Result: recall@1=0.000, status_accuracy=0.079 — all failures are expected catalog-coverage artifacts (dataset uses `met:435xxx`, catalog is `met:436xxx`, zero overlap). 3 false-exact cases (over-confidence in small homogeneous catalog), 3/3 out-of-catalog over-confident. Re-run after full 500K ingest. Gate: recall@1 ≥ 0.80.
3. **Parallel Ingest Infrastructure (D-052):** Parallelism design with Container Apps Jobs self-sharding via `--shard-index auto` (parsed from `CONTAINER_APP_REPLICA_NAME`). Three test executions revealed **Met API per-IP throttle is the binding constraint:** soft-throttle flips all responses to 403 (no 429, no Retry-After) after crossing ~27–160 req/s threshold; penalty lasts ≥ 40 min. Backed off to parallelism=4, request_delay=0.15s → realistic ETA 5–6 hr at ~27 req/s aggregate. **Code shipped (image v4):** CLI flags `--shard-index`, `--shard-count`, `--request-delay`; bugfix `limit<=0` now means unbounded (was silent ValueError); `_get_with_retry()` no longer retries permanent 4xx (saves 80% throughput loss at 30–50% 403 rates).

**Key learnings:**
- **Container Apps Jobs Bicep gotcha:** `parallelism`/`replicaCompletionCount` live under `manualTriggerConfig`, not top of `configuration` (BCP037 warning).
- **Don't retry permanent 4xx** in parallel scrapers — exponential backoff per-record (31 s × 30–50% rate) is catastrophic at scale.
- **Met API rate-limit reality:** No intelligent backoff possible; must back off blind. Penalty ≥ 40 min.
- **Secrets reset on incremental deploy:** `api-bearer-token` resets to PLACEHOLDER on each Bicep incremental deploy (recurrence of D-046 incident). Manual KV restore + revision restart needed. Long-term fix: pull from KV via secretRef.

**DB row count:** unchanged at 100 (from D-029); parallel runs added zero rows due to throttle.

---

## 2026-05-16 — Phase 1 Prod Catalog Seed + End-to-End Verification

✅ **PROD CATALOG LIVE:** 100 Met European Paintings ingested to Azure Postgres. All Wave 1 enrichment (7 columns) populated. End-to-end `/v1/identify` verified (Van Gogh Sunflowers → exact, score=1.0). Warm latency ~2.9s (retrieval 2.0s + LLM 0.08s). HNSW index healthy (m=16, ef_construction=64, cosine). SigLIP-base-patch16-224 (D-015) confirmed working in prod.

**Cross-agent incidents:** backend-engineer's Bicep deploy (D-029 ingest job add) caused API revision to revert to hello-world container. Root cause: `containerPort: 80` in parameters.prod.json, no `apiImage` param in Bicep, registry binding wiped. Fixed in ~10 min via `az containerapp registry set` + manual image update; added params to Bicep + what-if guard in deploy.sh. (D-038)

**iOS deployment target raised to iOS 17.0** (SwiftData required for D-034 history feature). No impact on backend/ML pipelines.

**Decisions closed:** D-015 (SigLIP confirmed prod-ready), D-016 (database-url flag), D-027 (Wave 1 enrichment pattern), D-029 (prod seeding decision).

---

See `history-archive.md` for Phase 0 foundation, embedding selection, test discipline, eval bootstrap.
