# ML/Retrieval Engineer History (Condensed — 2026-05-17)

> **Full Phase 0–early Phase 1 work archived to `history-archive.md`. Current file: latest completed work + active next steps.**

## 2026-05-17 — asyncpg pool defensive bound + Rijks live ingest active (ml-retrieval-engineer-6)

**Corrected root cause of the laptop hangs:** The kickoff prompt blamed `asyncpg.create_pool` default `min_size=10`. Both halves wrong — pool was already `min_size=1, max_size=4`, and asyncpg was not the actual hang. **ml-retrieval-engineer-3's `sample(1)` analysis was correct: the hang was `from transformers import AutoModel` inside `get_embedder()` blocked on macOS `amfid` re-verifying every `.so`/`.dylib` under `Python.framework` on a cold `.pyc` cache.** That import runs immediately after the "Opening asyncpg pool" log line in `ingest_met_to_db`, so the symptom looked exactly like a pool hang to `ps`.

**Why today's runs were unblocked:** The three failed agent attempts before me warmed the `.pyc` cache and amfid quarantine. My Met probe at 21:45:47 shows `create_pool` returning in 1.4s and `from transformers import AutoModel` returning in 3s — not because I fixed anything, but because the cache was hot. The "fix" was prior failed runs.

**Code fix I shipped anyway (commit `378dcb3`):** Added `timeout=15` (connect handshake bound) and `command_timeout=30` (per-query bound) to all four `asyncpg.create_pool()` call sites in `ml/cli.py`. ~24 net new lines. Reasoning: `asyncpg.create_pool` without `timeout=` is a real footgun for any *future* TLS stall (Container Apps egress, GitHub Actions, intermittent network); the fix converts a silent hang into a 15s fast-fail with a clear stderr message. Cheap, no operational downside. **All 129 tests still pass** (suite grew from the 94 noted earlier).

**Live state:**
- Met laptop run (PID 58189) launched cleanly, hit **91% 403 rate** — Met API per-IP throttle still penalising 71.212.143.26 from earlier D-052 burst. Killed to stop resetting the cooldown clock.
- **Rijks (PID 58536) running healthy: 64 rows committed, 106 embedded, ~0.7 rec/s, ETA ~2 hr for full ~5K.** Same Azure DSN, different ingest hosts (`data.rijksmuseum.nl`, `iiif.micr.io`). Log: `.squad/.scratch/ingest-rijks-fixed.log`.
- AIC zombie (PID 53812) killed. Recommended kickoff after Rijks ≥ 1K rows + CPU headroom.

**DB row counts:** met=100 (unchanged), rijks=64 (growing), aic=0.

**Recommended Met path:** Azure Container Apps Job (`az containerapp job start --name art-guide-prod-ingest --resource-group art-guide-prod`) — different egress IP, bypasses residential cooldown. This was the original Path D plan; laptop pivot was a workaround.

**Recommended SigLIP cold-start fix (separate, follow-up):** Either (a) hoist `import transformers` to the CLI entry point so the import cost happens before any asyncio call (same wall time but a debuggable location), or (b) move `get_embedder()` out of `ingest_met_to_db`'s async body into the sync CLI handler. Neither is a blocker today.

**Decision file:** `.squad/decisions/inbox/ml-retrieval-engineer-6-asyncpg-pool-fix.md`.

**Key learnings (for `wisdom.md`):**
- **Symptom adjacency lies.** "Hang at log line X" usually means "hang in the call after X" — but for an async stack, that next call can be many awaits deep. The lexical neighbour (`create_pool`) is almost never the right blame. `sample(1)` / `py-spy dump` is ground truth; `ps` + log inspection is not.
- **`asyncio` failure modes hide CPU work.** A synchronous `import` inside an async function shows as 0% CPU in `ps` when blocking on macOS XPC syscalls (amfid). It's neither pool wait nor network wait — it's the kernel.
- **Cache priming as accidental fix is invisible from the agent's perspective.** Three prior incarnations didn't commit code but their failed runs created `.pyc` files that unblocked the fourth attempt. The "fix" exists in no diff. Future incarnations should not assume their code change is what worked — verify by clean-slate repro when possible.
- **`asyncpg.create_pool(..., timeout=, command_timeout=)` is still a real defensive improvement** even if it wasn't today's bug. Any future TLS handshake stall against Azure PG Flex will now fast-fail in 15s instead of hanging silently.

---

## 2026-05-17 — Path D Laptop Pivot: Diagnosed Why Laptop Ingest Hangs (ml-retrieval-engineer-3)

**Context:** Azure egress IP for `art-guide-prod-ingest` still in Met 403 penalty box from earlier D-052 test bursts; Brady decided to pivot the full 501K ingest to his residential IP (fresh, never used). Sibling agent ml-retrieval-engineer-2 was wrapping the Bicep single-worker revert (D-053-inbox) — that work is preserved for future Azure runs but Azure job was NOT kicked off.

**Tasks attempted (three launches, all killed):**
1. **Pre-flight clean:** Confirmed `art-guide-ml` CLI at `services/ml/.venv/bin/`, KV `database-url` reachable, SigLIP weights cached at `~/.cache/huggingface/hub/models--google--siglip-base-patch16-224/`, no Azure job execution Running, laptop's public IP (71.212.143.26) is in the Postgres firewall allowlist.
2. **Launched ingest three times** with `nohup … &; disown` (incl. `PYTHONUNBUFFERED=1`); each died silently after 6–13 min, never advancing past the first `"Opening asyncpg pool"` log line. Same symptom ml-retrieval-engineer-4 documented earlier today.
3. **Root-cause diagnosis (new):** the hang is NOT asyncpg (`asyncpg.connect` smoke test from same shell completes in 0.7s; TCP to `:5432` is ESTABLISHED in the hung process). The hang is `embedder = embedder or get_embedder()` inside `ingest_met_to_db` — the synchronous SigLIP load runs inside the asyncio event loop and triggers `from transformers import AutoModel`. On Brady's laptop that import takes **5+ minutes** because:
   - **No `transformers` `.pyc` cache existed.** First run had to compile all 2347 `.py` files; subsequent runs are ~65 s.
   - **macOS amfid (Apple Mobile File Integrity) re-verifies every `.so`/`.dylib` on every load.** System log shows hundreds of lines like `"… not valid: The file is adhoc signed or signed by an unknown certificate chain"` for `/Library/Frameworks/Python.framework`. Each verify is serialized and slow.
   - Combined: 99.8% wall-time idle waiting on `amfid` XPC roundtrips, < 1 s of CPU in 10 min of elapsed time. `sample(1)` showed leaf in recursive `bounded_lru_cache_wrapper → _PyEval_EvalFrameDefault → _PyObject_MakeTpCall` — transformers' lazy autoclass machinery doing eager class definitions for every model dir.
4. **Why the detached processes died:** mDNSResponder log entry `"DNSServiceCreateConnection STOP PID[49204](Python)"` at 21:36:35 PDT, no SIGTERM/SIGKILL log record, no Python exception trace in the log file. Theory: Copilot CLI agent's per-turn process-group cleanup kills child PIDs even after `disown` + `PPID=1`. `nohup` ignores SIGHUP but not SIGKILL. The sibling agent's TTY-attached ingest met (PID 56454, --limit 50) died the same way. Confirms launches from inside an agent session are not survivable for multi-hour jobs.
5. **Handoff to Brady:** Wrote `.squad/.scratch/run-laptop-ingest.sh` (DSN from KV → env var, not argv; `caffeinate -ims` to block sleep; `nohup` + `disown`). Decision file `.squad/decisions/inbox/ml-retrieval-engineer-3-laptop-ingest.md` documents command, ETA (5-10 min cold start + 2-3 hr fetch-bound steady-state), monitoring, and **critical instruction: launch from Terminal.app, NOT from inside a Copilot CLI agent session**.

**DB row count:** still 100 (D-029 seed). No new rows committed in this session; deferred to Brady's manual launch.

**Met IP budget:** unchanged. The three failed launches never made a single Met API call (all died during transformers import). Laptop IP remains fresh for Brady's manual run.

---


## 2026-05-17 — Path C: AIC (Art Institute of Chicago) Adapter — Multi-Source Ingest #3 (ml-retrieval-engineer-5)

**Tasks completed (Phases 1 + 2):**
1. **AIC API field audit** (`docs/aic-ingest-field-audit.md`). 5 records audited across paintings, prints, antiquities. Verified: anonymous 60 req/min cap (= 1 req/s, hard published); **61,617 PD artworks**; IIIF URL pattern `https://www.artic.edu/iiif/2/{image_id}/full/843,/0/default.jpg` (HTTP 200 live, Cloudflare CDN); listing endpoint pagination unbounded (page 1000+ ok); search endpoint capped at 10K results. Major positive finding: AIC has a `description` field (CC-BY-4.0, NOT CC0) with curatorial prose for ~30% of records — exactly the interpretive gap Met's audit flagged in D-024. Captured to `raw_metadata.description_html` forward-compat but NOT promoted to a grounded column in this PR (deserves separate decision + backend coordination).
2. **AIC adapter** (`services/ml/ml/ingest/aic_db.py`, ~700 LOC) — mirrors `met_db.py` pattern with five AIC-specific deltas: (a) listing-only fetch (no per-id detail call — halves the request budget vs. Met); (b) constructed IIIF URLs (no special CDN headers needed); (c) wider classification filter (`_AIC_ACCEPTED_TYPES`: Painting/Sculpture/Print/Drawing/Photograph/Vessel/Textile/Coin/Manuscript/...); (d) `request_delay=1.05` default (95% of AIC's 1 req/s cap); (e) CC-BY-4.0 license tracked separately for `description`. Same `_INSERT_SQL` / `_commit_batch` / `_get_with_retry` / D-024 enrichment column mappings as Met (kept verbatim for matched-pair maintainability).
3. **Tests** (`services/ml/tests/test_aic_db.py`, 33 tests). Covers happy path, IIIF URL construction (pinned to v2 with regression-test for AIC v2→v3 migration), missing fields, BCE dates (negative integers), PD/image filters, classification filter (12 accepted types, 4 rejected, fallback when artwork_type_title is null), tags merge + dedup, raw_metadata licensing, date placeholder handling. **All 33 pass; combined with Met's 26 = 59 passed, no regressions.**
4. **CLI subcommand** `art-guide-ml ingest aic` with `--limit`, `--dry-run`, `--database-url`, `--batch-commit-size`, `--request-delay`, `--page-size`, `--start-page`. Warns if `--request-delay < 1.0` (would exceed AIC's published cap).
5. **Live listing-sample validation** (no embedder, 5 pages × 100 records): **62% acceptance rate** (310/500). Page 1: 92/100; page 200: 0/100 (cluster of non-PD); pages 500–1000: 76–100/100. Skip breakdown: 36% non-PD, 0.2% no-image, 1.4% wrong-type. Mapper, IIIF URLs, and filter all confirmed end-to-end against live API.
6. **`museum-ingest-loop` SKILL update** — appended an "AIC-specific deltas" section enumerating 5 axes where adapters diverge per-source (discovery shape / image URL pattern / rate-limit cap / classification filter width / field nullability) plus 7 things that stay identical across Met / AIC / Rijks. SKILL is now multi-source proven.
7. **Decision file** written to `.squad/decisions/inbox/ml-retrieval-engineer-5-aic-adapter.md`.

**Phase 3 (live ingest) — kicked off and died:**
- **CPU pre-check at 04:34Z:** Load avg 4.75 / 8.72 / 7.82, CPU 56% idle, 27% used. Looked fine for a 1-req/s third process. Went green.
- **AIC ingest launched detached:** PID 53812, log `.squad/.scratch/laptop-ingest-aic-2026-05-17T04-12Z.log`, cmd `python -u -m ml.cli ingest aic --database-url <kv> --limit 0 --request-delay 1.05 --batch-commit-size 64`. ETA projected 18.4 hr.
- **Process died silently at ~T+5 min** after asyncpg pool successfully connected to Postgres (TCP ESTABLISHED to `20.163.104.116:postgresql` confirmed via `lsof`) but before any further log output. No stack trace, no exit code captured (detached process). At time of death, system load avg had climbed to 22.62 (1-min), `PhysMem unused: 294M` — **machine was severely memory-pressured.** Met PID 49204 (the other concurrent ingest) also died around the same time. **Net AIC rows ingested: 0.**
- **Root cause analysis:** SigLIP-base model load peaks at ~3 GB resident. Two concurrent `art-guide-ml` processes both initialising the embedder at the same time on a 31 GB shared-memory Air, atop Brady's other apps, exceeded available physical memory. macOS likely OOM-killed both. The AIC adapter code itself is sound — same SigLIP path, same asyncpg path, same model that Met has been running fine in isolation.
- **Operational recommendation to Brady:** the laptop cannot comfortably run 3 SigLIP-loading processes concurrently. Three remediation options: (1) stagger kickoffs by ≥ 60 s and verify each is past SigLIP load before starting the next; (2) reduce SigLIP load cost by sharing weights across processes (would need code change to use the same on-disk mmap, currently each process loads independently); (3) promote AIC to a Container Apps Job analogous to D-046's Met job (this is the right long-term shape — the adapter is ready, only need Bicep + Brady's `az containerapp job start` button).

**Key insight:** Two sources, two completely different rate-limit shapes (Met 80 req/s per-IP, AIC 60 req/min per-IP), but **the same outer loop template** (museum-ingest-loop SKILL) works for both. The only Met-AIC-Rijks adapter divergence is along the 5 well-defined axes documented in the SKILL update. Adapter #4 (Harvard? Smithsonian? Cleveland?) should drop in at substantially less than a day of work.

## Learnings

- **Always check the source's published rate-limit doc BEFORE writing code.** Met's 80 req/s and AIC's 1 req/s drive completely different operational shapes; guessing wrong wastes a day. Doc-driven rate budget is the right starting point.
- **Listing endpoints with `fields=` projection beat list+detail patterns when available.** AIC's design saves half the request budget vs. Met's discovery+per-id pattern. Always probe `fields=` support on a listing endpoint first.
- **IIIF Image API 2.0 is becoming the norm** across major art museums (AIC, Yale, Cleveland, Getty). The `/full/{w},/0/default.jpg` URL pattern is portable across all of them. Future adapters should default to constructing IIIF URLs from `image_id` rather than expecting an absolute `image_url` field.
- **Description prose at-source matters.** AIC's `description` field is exactly the tier-(b) interpretive content D-024 identified as the Met gap. Worth a follow-up PR to promote it to a grounded column (with CC-BY-4.0 attribution UX) — would close the Met audit's "what does the lyre symbolize" ceiling at zero additional infra cost for the AIC slice of the catalog.
- **Concurrent SigLIP loads will OOM a 31 GB laptop.** Each `art-guide-ml` process peaks at ~3 GB during SigLIP load + ~200–500 MB steady-state. Two concurrent loaders + browser/Slack on a 31 GB machine = swap thrash → OOM kill. Workaround: stagger kickoffs OR run multi-source ingests in Container Apps Jobs (one replica per source). The 5 min CPU pre-check ("56% idle, fine to start") is insufficient — must also check available memory headroom (≥ 4 GB free per planned SigLIP load).

---

## 2026-05-17 — Path C: Rijksmuseum adapter built; live ingest blocked on Met hang (ml-retrieval-engineer-4)

**Tasks completed (Phases 1 + 2):**
1. **Rijks API research.** No API key needed for OAI-PMH, Search API, or Persistent ID resolver. No published per-IP rate limit. Classic key-based `/api/en/collection` returns HTTP 410 Gone (deprecated). Chose **OAI-PMH + EDM** for ingest: 50 fully-hydrated records per `ListRecords` call, all Concepts (medium, type, subjects) and Agents (creator name + birth/death dates) inlined in the same XML — zero per-entity follow-up requests.
2. **Field audit** (`docs/rijks-ingest-field-audit.md`). 8 diverse records audited (5 paintings + 3 sculpture, incl. Night Watch, Toorop, anonymous Tang Bodhisattva). Inventory of every EDM/DC field with mapping decisions. Schema gap finding: Rijks gives `dc:description xml:lang="en"` on ~50% of records — interpretive prose Met doesn't have anywhere. Per museum-ingest-loop rule, stashed at `raw_metadata.rijks.description_en` (don't promote columns reactively).
3. **Adapter shipped** (`services/ml/ml/ingest/rijks_db.py`, 734 LOC, stdlib XML only — no `lxml` dep). Mirrors `met_db.py` structure: `_list_records_page` + `_iter_records_from_xml` + pure `map_rijks_record(parsed_dict)` mapper + shared `format_pgvector` helper + same `_INSERT_SQL` ON CONFLICT pattern. Handles OAI resumption-token pagination across multiple sets (default: `261208` paintings + `26126` sculptures). 4,916 + 2,468 candidates, ~5K with images after filter.
4. **Tests** (`services/ml/tests/test_rijks_db.py`, 24 cases). Year-range regex parsing (`c. 1735 - c. 1745` → 1735/1745), text cleanup, artist-bio synthesis from `rdaGr2:dateOfBirth/Death`, OAI envelope parsing (deleted-marker skip, resumption-token extraction), happy/sad-path mapping (Night Watch happy path, no-image filter, restricted-rights filter, anonymous sparse-record graceful nulls, deduped tags). Full project suite: 94 passed (24 new + 70 pre-existing) in 13.93s.
5. **CLI integration** — `art-guide-ml ingest rijks` subcommand with `--limit/--dry-run/--database-url/--batch-commit-size/--set-specs/--request-delay`. Default request-delay 0.2s (~5 req/s); ingest subcommand metavar now `{met,rijks,aic}`.
6. **Skill update** — `.squad/skills/museum-ingest-loop/SKILL.md` now has a "Confirmed sources" table (Met + AIC + Rijks) and a Rijks-specific extensions section (OAI resumption-token paging; inline entity resolution; `xml:lang` is a preference not a guarantee). Confidence stays **high** (now validated across three independent API shapes: REST/JSON, paginated REST/JSON, OAI-PMH/RDF-XML).
7. **Decision file** at `.squad/decisions/inbox/ml-retrieval-engineer-4-rijks-adapter.md`.

**Phase 3 NOT executed (blocked on Met hang):**

Met ingest on the laptop has hung **twice** during this session:
- PID 42739 (started 21:08:42 PDT): 0% CPU, 0.6% memory, only the `"Opening asyncpg pool against ..."` log line printed. After ~10 min, PID disappeared with no traceback. Silent crash. Zero DB row growth (`source='met'` stayed at 100, the D-029 seed).
- PID 49204 (started 21:23:41 PDT — restart by Brady or external supervisor): same exact symptom. Same single log line. Still 0% CPU after 8+ min when this work ended.

Per the Path C constraint ("If Met has hit any throttle issues, DO NOT START Rijks") I did **not** kick off Rijks. Hypothesis (not verified): `asyncpg.create_pool(...)` against Azure Flexible Server is hanging silently, possibly an IP-allowlist drift or TLS handshake stall — the `"Opening pool"` log line is printed BEFORE `create_pool` returns. Suggested triage in the decision file: `psql "$DSN"` connectivity test from same shell; add `command_timeout=60` to `create_pool` to fail-fast instead of hang.

**Key learnings:**
- OAI-PMH with EDM is operationally simpler than Linked Art JSON when you need full entity context per record — 1 HTTP call returns 50 records + all referenced Concepts + Agents inline. Linked Art's beautiful normalization is a liability for an ingest pipeline that wants flat records.
- `xml:lang` tags on Rijks EDM responses are unreliable as a *content* signal — `<dc:title xml:lang="en">` may carry Dutch text. Always treat as a preference, not a guarantee.
- ~30–40% of records in Rijks's curated painting/sculpture sets have no `<edm:isShownBy>` element. Normal, not a bug; mapper returns None and the loop counts as skipped_filter.
- Rijks `dcterms:provenance` ≠ Met `creditLine`. Same column (`credit_line`), different semantics (ownership chain vs. donor/acquisition). Documented in adapter docstring + audit §3.
- Three independent museum APIs now share the same outer loop pattern (`museum-ingest-loop` skill). The mapper signature changes per source — JSON dict for Met/AIC, parsed XML dict for Rijks — but everything flows into the same `(source, source_id)` upsert. The skill is now genuinely battle-tested across REST/JSON, paginated REST/JSON, and OAI-PMH/RDF-XML.

**DB row count:** still 100 (Met seed unchanged; no Rijks rows yet — Phase 3 deferred).

---

## 2026-05-17 — Path D: Single-Worker Met Ingest at Published Rate Limit (D-053-inbox)

**Tasks completed:**
1. **Bicep revert from parallel to single worker.** `Microsoft.App/jobs/art-guide-prod-ingest` now: `parallelism: 1`, `replicaCompletionCount: 1`, command `art-guide-ml ingest met --limit 0 --request-delay 0.015 --batch-commit-size 64` (sharding flags removed entirely). `replicaTimeout: 14400` kept (4 hr ceiling for the ~2 hr expected runtime). Image still `art-guide-api:v4` (limit=0=unlimited + don't-retry-non-429 fixes already baked in).
2. **Deploy via `./deploy.sh` (D-038 what-if guard passed).** Bicep deployment `art-guide-prod-20260517T035459` succeeded. What-if confirmed only the ingest job changed (parallelism, command); API container env had AZURE_OPENAI_ENDPOINT briefly removed then re-added by deploy.sh Phase 4 — net no change.
3. **api-bearer-token bug recurred** (D-046/D-052 regression). Container App secret reverted to `PLACEHOLDER-must-be-set-before-live-traffic`. Restored from KV (which is preserved across Bicep deploys) via `az containerapp secret set` + `az containerapp revision restart` of the latest revision. Filed cross-agent note to backend-engineer to fix properly via KV secretRef.
4. **Decision file** written to `.squad/decisions/inbox/ml-retrieval-engineer-2-path-d-ingest.md`.

**Key insight:** Met API has a **published** 80 req/s per-IP limit (https://metmuseum.github.io/, "Please limit request rate to 80 requests per second"). Parallel design was solving for compute when the constraint was per-IP rate. Aggregate req/s — not per-replica — is what trips the throttle. Single worker at 66 req/s (under cap) finishes ~501K records in ~127 min fetch-bound; any throttled-parallel design produces 0 inserts during multi-hour cooldowns and loses every race.

**DB row count:** still 100 (D-029 seed). Job kickoff held until 04:51 UTC for 60-min cooldown from the 03:20 UTC 403-storm execution.

## Learnings

- Local laptop ingest pattern — useful for IP-throttle-recovery scenarios; CDN egress from residential IP unconstrained. Key gotchas: (a) Copilot CLI agent sessions kill detached child processes silently on turn transitions — must launch from a regular Terminal.app for multi-hour runs; (b) macOS amfid serializes code-signature verification of every Python `.so`/`.dylib` and the python.org Python install is "adhoc signed" → first cold start with no `transformers` `.pyc` cache is 5+ min before the first DB write; pre-populate the cache (`python -c 'import transformers'` once interactively) to drop subsequent starts to ~65 s.
- Met API published rate limit is 80 req/s — single worker at <80 req/s beats any parallel config.
- Per-IP rate limits without `Retry-After` semantics defeat adaptive backoff: the "don't retry non-429 4xx" fix is correct in isolation but produces a fast-fail storm under IP throttling that looks like normal record-level skips. Treat sustained 403 rate >10% as a throttle signal, not a record-coverage signal.
- Bicep's inline `value:` on Container App secrets is a recurring footgun (D-046, D-052, this work). The secret declared in template wins over `az containerapp secret set` mutations on every `Microsoft.App/containerApps` redeploy. Proper fix: declare as `keyVaultUrl` / `identity` secretRef so the source of truth lives in KV, not the template literal.

---

## 2026-05-17 — Full Catalog Ingest Job + Parallel Sharding + Eval Baseline (D-050, D-051, D-052)

**Tasks completed:**
1. **Full Met Catalog Job (D-051):** Added `Microsoft.App/jobs` to Bicep (`art-guide-prod-ingest`), manual trigger, replicaTimeout 7200s, command: `art-guide-ml ingest met --limit 0 --batch-commit-size 64`. Execution `art-guide-prod-ingest-brsioxp` started 2026-05-17T00:51:42Z (projected 23 hr).
2. **Eval Baseline (D-050):** Ran harness against 100-record prod catalog. Result: recall@1=0.000, status_accuracy=0.079 — all failures are expected catalog-coverage artifacts (dataset uses `met:435xxx`, catalog is `met:436xxx`, zero overlap). 3 false-exact cases (over-confidence in small homogeneous catalog), 3/3 out-of-catalog over-confident. Re-run after full 500K ingest. Gate: recall@1 ≥ 0.80.
3. **Parallel Ingest Infrastructure (D-052):** Parallelism design with Container Apps Jobs self-sharding via `--shard-index auto` (parsed from `CONTAINER_APP_REPLICA_NAME`). Three test executions revealed **Met API per-IP throttle is the binding constraint:** soft-throttle flips all responses to 403 (no 429, no Retry-After) after crossing ~27–160 req/s threshold; penalty lasts ≥ 40 min. Backed off to parallelism=4, request_delay=0.15s → realistic ETA 5–6 hr at ~27 req/s aggregate. **Code shipped (image v4):** CLI flags `--shard-index`, `--shard-count`, `--request-delay`; bugfix `limit<=0` now means unbounded (was silent ValueError); `_get_with_retry()` no longer retries permanent 4xx (saves 80% throughput loss at 30–50% 403 rates). **Superseded operationally by Path D above** — Met's published 80 req/s limit makes single-worker the correct shape; sharding infrastructure preserved in code for future use against APIs with per-key (not per-IP) rate limits.

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
