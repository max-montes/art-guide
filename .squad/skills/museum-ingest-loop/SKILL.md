---
name: "museum-ingest-loop"
description: "Reusable fetch → preprocess → embed → upsert template for museum Open Access sources"
domain: "ml-retrieval"
confidence: "high"
source: "earned (art-guide Met Open Access 2026-05-10; AIC + Rijks 2026-05-17 — three independent sources now share the same outer loop)"
---

## Context

A retrieval-first art system needs to populate its vector catalog from N museum Open Access feeds (Met, Rijksmuseum, Harvard, Smithsonian, AIC, Cleveland, …). Each feed has its own JSON shape and quirks, but the **outer pipeline is identical**: discover candidate IDs, fetch per-record details, filter to public-domain in-scope records, download the image into memory, embed once, upsert into Postgres+pgvector with `ON CONFLICT (source, source_id) DO UPDATE`. Rewriting that loop per source means N copies of subtle bugs around image-byte lifetime, idempotency, retry/backoff, and confidence-vector drift. This skill captures the loop so each new source is just an API client + a field mapper.

## Patterns

- **Two functions per source, one shared loop.** Each museum source provides:
  - `discover_ids(client) -> list[str|int]` (whatever the API exposes for "all candidate ids")
  - `fetch_record(client, id) -> dict` (the museum-specific per-id GET)
  - `map_record(raw_dict) -> Optional[dict]` (museum JSON → canonical row dict). Pure function, no I/O — unit-testable in isolation.
  Everything else (download, embed, batch-commit, retry/backoff, stats) is shared and lives in the orchestrator.

- **Classify out-of-scope records in `map_record`, not the loop.** The mapper returns `None` for any record that fails a project-level filter (not public domain, no image, wrong classification). The loop just counts skips. Keeps the loop generic across sources and keeps filter logic with the source-specific mapping it depends on.

- **Image bytes live in memory, then die.** Download → `embed_bytes(b)` → `b = None` in a `finally`. Never write to disk, never log. This is non-negotiable for any system whose privacy stance forbids retaining user (or museum) image bytes (D-012-style rule). The orchestrator should make this lifecycle visible — explicit `None` assignment, not "the GC will get it eventually".

- **Embedding goes through the project's single image pipeline.** Never call a model-bundled processor (`AutoProcessor`, `CLIPProcessor`) on raw bytes. Use the `get_embedder()` seam so swapping models is a one-character change and preprocessing parity between ingest and query is by construction. (Pairs with the `embedder-factory` skill.)

- **Idempotency via `(source, source_id)` UPSERT.** The catalog table has a UNIQUE on `(source, source_id)` and a namespaced `id = '{source}:{source_id}'` PK. Every ingest is `INSERT … ON CONFLICT (source, source_id) DO UPDATE SET … embedding = EXCLUDED.embedding`. Re-runs refresh the embedding instead of duplicating. Use `RETURNING (xmax = 0) AS inserted` to count inserts vs. updates without a second roundtrip.

- **`raw_metadata jsonb` is the safety valve.** Anything the source publishes that doesn't map to a canonical column (thumbnails, license URLs, dimensions, exhibit history, source-specific tags) goes into a single `raw_metadata` jsonb blob. **Do not add columns reactively.** Promote to a column only after >1 source needs it AND a query path needs to filter on it.

- **pgvector via text cast is enough.** Format the vector as `'[v1,v2,...]'` and pass it as a `text` parameter; the SQL casts to `::vector`. Avoids dragging in the `pgvector` Python adapter just for type registration. (Switch to the adapter if you need per-row roundtrip vector reads in tight loops; ingest doesn't.)

- **Batch commits sized for the bottleneck, not the DB.** When per-record cost is HTTP fetch + image download + CPU embed (hundreds of ms each), batch sizes of 25–50 are plenty. Going to 500/txn just buys you a longer rollback if anything fails. The DB write is microseconds — it's never the bottleneck.

- **`asyncio.to_thread(embed_bytes, …)` to keep the loop responsive.** With an `httpx.AsyncClient` driving I/O concurrently and a sync PyTorch embedder in the middle, offloading the embed call lets the event loop continue draining HTTP responses for the next records.

- **Polite by default, retried by default — but only on transient errors.**
  Identify yourself in the User-Agent. Floor the request rate
  (e.g. 0.15 s ≈ 6 req/s ceiling per replica). Exponential backoff with
  jitter on **429s, 5xx, and network errors**; capped retries.
  **Do NOT retry permanent 4xx (everything except 429).** Per-record
  403/404 (a museum-restricted record, a retired id) wastes the polite
  request budget — 5 retries × backoff is 31 s of sleep per bad record.
  At single-replica / 6 req/s this is invisible; at 8 replicas /
  20 req/s/replica with 30-50% 403 rates it's catastrophic
  (~80% throughput loss). Skip immediately and `continue`.
  Single-record failures are logged at WARNING and counted in stats;
  never abort the whole run.

- **Stats object as the only run summary.** A small `IngestStats` dataclass with `candidate_ids`, `fetched`, `skipped_filter`, `skipped_image_error`, `skipped_embed_error`, `skipped_db_error`, `inserted`, `updated`, `rate_limited_events` covers every failure mode the operator needs to see. Print it at the end; that's the operator UI.

- **`--dry-run` is the no-DB path.** Same fetch + download + embed; skip the DB transaction. Lets you smoke-test on a laptop without bringing up Postgres and lets CI exercise the loop end-to-end without infra.

- **Test the mapper, not the loop.** The mapper is pure and the only source-specific code that can have bugs that survive integration; cover every filter rejection branch and every nullable optional field. The loop itself is exercised by the `--dry-run` smoke test against the live API; don't bother mocking httpx + torch + asyncpg in unit tests.

## Examples

✓ **Correct (template):**

```python
# services/ml/ml/ingest/{source}_db.py

INSERT_SQL = """
INSERT INTO artworks (id, source, source_id, …, embedding)
VALUES ($1, $2, $3, …, $N::vector)
ON CONFLICT (source, source_id) DO UPDATE SET
    …, embedding = EXCLUDED.embedding
RETURNING (xmax = 0) AS inserted
"""

def map_{source}_record(raw: dict) -> dict | None:
    if not _passes_project_filter(raw):
        return None
    return {
        "id": f"{SLUG}:{raw['id']}",
        "source": SLUG,
        "source_id": str(raw["id"]),
        # … canonical columns …
        "raw_metadata": {
            "license": …, "license_url": …,
            SLUG: raw,                # full original payload, forward-compat
        },
    }

async def ingest_{source}_to_db(*, pool, limit, embedder=None, dry_run=False):
    embedder = embedder or get_embedder()
    stats = IngestStats()
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": UA}) as client:
        ids = await discover_ids(client, …)
        batch = []
        for oid in ids:
            if stats.embedded >= limit: break
            await asyncio.sleep(REQUEST_DELAY)
            raw = await fetch_record(client, oid)
            mapped = map_{source}_record(raw)
            if mapped is None: stats.skipped_filter += 1; continue
            try: img = await client.get(mapped["image_url"]); img.raise_for_status()
            except httpx.HTTPError: stats.skipped_image_error += 1; continue
            try: vec = await asyncio.to_thread(embedder.embed_bytes, img.content)
            except Exception: stats.skipped_embed_error += 1; continue
            finally: img = None  # bytes never outlive this iteration
            mapped["embedding"] = vec
            if dry_run: stats.inserted += 1; continue
            batch.append(mapped)
            if len(batch) >= BATCH_SIZE:
                await _commit_batch(pool, batch, stats); batch.clear()
        if batch and not dry_run:
            await _commit_batch(pool, batch, stats)
    return stats
```

✗ **Incorrect:**

- Writing image bytes to a `data/{source}/cache/` directory "for resume". Now you have a privacy commitment to honor across every source and every codepath. Don't.
- Calling `AutoProcessor.from_pretrained(...)(image)` inside the loop. Splits the image pipeline; ingest-time and query-time preprocessing will silently drift.
- Adding `thumbnail_url` / `license_url` columns to the schema for the first source that has them. Use `raw_metadata`. Promote later if and only if a query needs to filter on them.
- One commit per record. Wastes round-trips with no benefit.
- Hard-failing the whole run on a single 403 or a single mapper exception. Log + count + skip; one bad record can't take down a 50K-row job.

## Anti-Patterns

- **Reusing the source-specific JSONL adapter as the orchestrator.** The "fetch + normalize" adapter and the "fetch + embed + upsert" orchestrator are different concerns with different lifetimes (one is offline data exploration; the other is the production catalog). Compose, don't merge.
- **Pretending `pgvector` reads need the Python adapter at write time.** Text cast is fine for write. Reach for the adapter only when you actually need typed vector results back from queries.
- **Logging the image URL plus a random hash "for traceability".** Source IDs already give traceability; don't accidentally log enough to reidentify a user-uploaded query image when the same loop is reused on a query path.
- **Discovering all IDs once and assuming the order is meaningful.** Most museum APIs return the global ID list in arbitrary order. Don't slice; iterate, filter, and stop at `limit`.

## Container Apps Job Pattern (added 2026-05-16)

When the ingest run is too long to drive from a developer's machine (e.g., ~500K records at 6 req/s ≈ 23 hours), promote it to a **Container Apps Job** on the same environment as the API. This avoids building a separate image and keeps the IAM/secrets model consistent.

**Key choices:**
- **Same image as the API** — do not build a separate ingest image. Override CMD in the job spec to run `art-guide-ml ingest met --limit 0 --batch-commit-size 64` instead of uvicorn.
- **Trigger type: Manual** — not scheduled. Brady initiates via CLI; no cron risk of double-ingest.
- **replicaTimeout: 7200** — Azure's provisioning operation timeout (not total job runtime). The actual job runs until completion regardless.
- **replicaRetryLimit: 1** — no automatic retry on failure; ingest is idempotent so re-run manually.
- **CPU/memory: 2 vCPU / 4 Gi** — SigLIP model load at runtime (even with `HF_HUB_OFFLINE=1` it needs to deserialize weights) peaks at ~3 Gi.
- **DATABASE_URL via secretRef** — same secret as the API container. Never inline the DSN.
- **HF_HUB_OFFLINE=1 + TRANSFORMERS_OFFLINE=1** — mandatory when SigLIP weights are baked into the image (D-028). Without these, the job will attempt an HuggingFace download on cold start.
- **System-assigned identity with AcrPull + KV Secrets User** — same RBAC pattern as the API container app. Add as separate `Microsoft.Authorization/roleAssignments` with deterministic `guid(acr.id, ingestJob.id, roleId)` names.

**Bicep resource type:** `Microsoft.App/jobs@2023-05-01`

```bicep
resource ingestJob 'Microsoft.App/jobs@2023-05-01' = {
  name: '${prefix}-ingest'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    environmentId: containerAppsEnv.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 7200
      replicaRetryLimit: 1
      registries: [{ server: '${acrName}.azurecr.io', identity: 'system' }]
      secrets: [{ name: 'database-url', value: dbUrl }]
    }
    template: {
      containers: [{
        name: 'ingest'
        image: '${acrName}.azurecr.io/art-guide-api:v1'
        resources: { cpu: json('2'), memory: '4Gi' }
        command: ['art-guide-ml', 'ingest', 'met', '--limit', '0', '--batch-commit-size', '64']
        env: [
          { name: 'DATABASE_URL',         secretRef: 'database-url' }
          { name: 'ENV',                  value: 'prod' }
          { name: 'HF_HUB_OFFLINE',       value: '1' }
          { name: 'TRANSFORMERS_OFFLINE', value: '1' }
          { name: 'HF_HOME',              value: '/opt/hf-cache' }
        ]
      }]
    }
  }
}
```

**Monitor a job execution:**
```bash
# Start
az containerapp job start -n art-guide-prod-ingest -g art-guide-prod-rg

# List executions
az containerapp job execution list -n art-guide-prod-ingest -g art-guide-prod-rg -o table

# Stream logs (requires containerapp-helper extension)
az containerapp job logs show \
  -n art-guide-prod-ingest -g art-guide-prod-rg \
  --execution <exec-name> --container ingest --follow
```

**Transient "Operation expired" failure:** Azure Container Apps Job provisioning can time out on first creation (provisioningState = "Failed") while the job resource itself is usable. If this happens, attempt `az containerapp job start` anyway — the execution will succeed. Then re-deploy Bicep in incremental mode to reconcile state.

**Corpus count (2026-05-16):** Met public-domain objects: **501,696** (URL: `https://collectionapi.metmuseum.org/public/collection/v1/objects?isPublicDomain=true`). This drifts upward as Met adds records. Re-check before each full-catalog run.


## Confirmed sources (as of 2026-05-17)

| Source | Adapter | Discovery API | Per-record format | Public-domain catalog | Notes |
|---|---|---|---|---|---|
| The Metropolitan Museum of Art | `ml.ingest.met_db` | `/objects?isPublicDomain=true` (single JSON of 501K ids) | JSON `/objects/{id}` | ~501,696 | 80 req/s per-IP cap; use single-worker; image CDN needs Safari UA + image Accept |
| Art Institute of Chicago | `ml.ingest.aic_db` | `/artworks?fields=...&page=N` paginated | JSON | ~100K (estimate) | Published 60 req/min cap; do not exceed |
| Rijksmuseum (Amsterdam) | `ml.ingest.rijks_db` | OAI-PMH `ListRecords?set=...&metadataPrefix=edm` (50 records/page incl. inline Concepts + Agents) | EDM RDF/XML | ~5,000 (paintings + sculpture sets, after no-image filter) | No API key, no published rate limit; adopt 5 req/s. Images via IIIF CDN at `iiif.micr.io`. Rijks gives `dc:description` (interpretive text Met doesn't have) — stashed in `raw_metadata.description_en`. |

Three sources now share the same outer loop (`discover → map → image → embed → upsert`). The mapper's signature changes per source — JSON dict for Met/AIC, parsed XML dict for Rijks — but **all flow into the same `(source, source_id)` upsert against `artworks`**, so retrieval is single-table. The `museum-ingest-loop` skill is now **high confidence** (validated across three independent API shapes: REST/JSON, paginated REST/JSON, OAI-PMH/RDF-XML).

### Rijks-specific extensions to the loop

* **OAI-PMH resumption-token pagination.** Unlike REST APIs, OAI-PMH's "next page" is a base64 token returned in the previous response (NOT a `?page=N` parameter). The mapper code must remember to send `?verb=ListRecords&resumptionToken=...` *without* re-sending `set` or `metadataPrefix` (spec requirement). See `_list_records_page()` in `rijks_db.py`.
* **Inline entity resolution.** Rijks EDM responses inline all `<edm:Agent>` and `<skos:Concept>` entities referenced by URI from inside `<edm:ProvidedCHO>` — so creator names, medium labels, and subject labels are all available without follow-up HTTP requests. Build per-record `concepts: dict[URI, {en, nl}]` and `agents: dict[URI, {names, birth, death}]` maps when parsing the record, then resolve all references locally.
* **Multi-set discovery is N sequential resumption-token loops.** Each `setSpec` is harvested independently. The orchestrator loops over the set tuple and within each set walks the resumption tokens until exhausted. Idempotent upsert keeps this safe across re-runs.
* **`xml:lang` is a preference, not a guarantee.** Rijks emits the same field with multiple `xml:lang` tags but the *language tag does not reliably match the content* — the "en" entry may carry Dutch text. Treat language tag as preference, fall back to whatever's present.
* **Schema gap: descriptions.** Rijks has free-text `dc:description xml:lang="en"` on ~50% of records — Met has nothing equivalent. Per the skill rule "don't promote columns reactively," v1 stashes this in `raw_metadata.rijks.description_en`. Backend can later promote to a `description` column when (a) a second source needs it (AIC has `description` too — already covered by the AIC adapter's `raw_metadata`), and (b) a query path needs it (grounded LLM prompt is the obvious one).

## AIC-specific deltas (added 2026-05-17, multi-source validation)

When porting the Met template to the Art Institute of Chicago, these are
the dimensions where adapters diverge. Future adapters (Rijksmuseum,
Harvard, Smithsonian, Cleveland) will sit somewhere on each axis.

### Axis 1 — Discovery shape (listing vs. listing + detail)

| Source | Discovery | Per-record detail | Total budget |
| --- | --- | --- | --- |
| Met | `/objects?isPublicDomain=true` returns a single huge array of ~500K ids | `/objects/{id}` per record (mandatory; the listing has no fields) | ~501K + ~501K image = ~1M requests |
| AIC | `/artworks?page=N&limit=100&fields=...` returns full records in batches of 100 | **None — listing already has every field we need** | ~1.3K listing + ~62K image = ~63K requests |

**The AIC pattern is strictly better when the source supports it** — half
the request budget vs. Met. Always check the source docs for a
`fields=` (or equivalent) parameter on the listing endpoint before
defaulting to the Met pattern. If listing supports filtering + field
projection, skip the detail call entirely. The adapter still keeps a
`_fetch_object(id)` helper for ad-hoc reingest / debugging, but the
steady-state walk does not invoke it.

### Axis 2 — Image URL construction

| Source | Pattern | CDN headers needed? |
| --- | --- | --- |
| Met | `raw['primaryImage']` (museum-hosted absolute URL) | **Yes** — `images.metmuseum.org` WAF rejects non-browser UA; needs `_IMAGE_CDN_HEADERS` override per request |
| AIC | IIIF: `{config.iiif_url}/{image_id}/full/843,/0/default.jpg` | **No** — Cloudflare CDN with `Access-Control-Allow-Origin: *`, plain client works |

**IIIF URLs are constructed, not fetched.** Most modern museum APIs
expose an `image_id` (UUID-ish) + an `iiif_url` config field. The
constructed URL pattern `/full/{w},/0/default.jpg` is IIIF Image API 2.0
standard and works against AIC, Yale Center for British Art, Cleveland
Museum, Getty, and others. Hardcode the width to the museum's
recommended cached size (AIC: 843px) for best cache hit rate. Read the
`config.iiif_url` field from a response if you want to be paranoid about
museum CDN migrations; otherwise pin the constant in code with a
test that asserts the spec.

### Axis 3 — Rate limit (the constraint that drives parallelism design)

| Source | Published cap | Single-IP enforcement | Right shape |
| --- | --- | --- | --- |
| Met | 80 req/s | Yes (per D-052: 403 storm on overage, ≥40 min cooldown, no `Retry-After`) | **Single worker, `request_delay ≈ 0.015s` (66 req/s, 82% of cap)** |
| AIC | 60 req/min ≈ 1 req/s | Yes — docs explicitly say "no parallel scrapers, 1 second between requests" | **Single worker, `request_delay ≈ 1.05s` (57 req/min, 95% of cap)** |
| Rijksmuseum (TBD) | Per-key, 10,000/day default | Per-API-key | Parallel-N with one key per replica (different from Met/AIC pattern) |

**Rule:** parse the source's published rate-limit doc BEFORE writing
any code. If the cap is per-IP, single worker at the cap (single-worker
rate-limit-aware pattern in `prod-catalog-ingest`). If per-key, parallel
shards with key isolation (parallel-sharded pattern, same skill). Never
guess.

### Axis 4 — Classification filter width

| Source | Project filter | Rationale |
| --- | --- | --- |
| Met | `_classification_matches` accepts only "Paintings" / "Sculpture" / variants | Met catalog is enormous and skewed; wider filter pulls in coins/photographs/prints that hurt eval signal |
| AIC | `_AIC_ACCEPTED_TYPES` accepts Painting / Sculpture / Print / Drawing / Photograph / Vessel / Textile / Costume / Book / Manuscript / Coin / ... | AIC's strength is non-Western, Asian, decorative arts, prints — these are exactly the gaps the Met adapter leaves; AGENTS.md hard rule #5 ("catalog over model") prefers wide |

**Both are correct per their context.** The filter width is a
PRODUCT decision per-source, not a template-level rule. Document the
acceptance set as a module-level frozenset (not a regex) so it's
introspectable and unit-testable.

### Axis 5 — Field naming + nullability

Met returns empty strings (`""`) for unknown fields. AIC returns JSON
`null`. The mapper's `raw.get("x") or None` idiom collapses both into
canonical `None`, so the upstream consumer never needs to know which
convention the source uses. **Always normalize to `None` in the mapper**,
never propagate empty strings or 0-as-placeholder dates downstream.

### What stays identical across Met / AIC (and will stay identical for Rijks)

1. **`map_<source>_record(raw) -> dict | None`** — pure function, no I/O.
2. **`_INSERT_SQL`** — verbatim copy. Same column set, same ON CONFLICT,
   same `xmax = 0` insert-vs-update detection.
3. **`_commit_batch(pool, batch, *, stats)`** — verbatim copy.
4. **`IngestStats`** dataclass — identical counters. (AIC adds
   `pages_walked` since the listing has explicit pagination; Met doesn't
   have an equivalent because it discovers ids upfront.)
5. **`_get_with_retry()`** — verbatim copy. Same retry-on-429/5xx,
   bail-on-permanent-4xx logic. Universal HTTP discipline.
6. **`asyncio.to_thread(embedder.embed_bytes, img_bytes)`** — keeps the
   event loop responsive for in-flight HTTP while the sync PyTorch
   embedder runs.
7. **`finally: img_bytes = None`** — D-012 / hard rule #3 enforcement.
   Image bytes never outlive the iteration that produced them.

### Anti-patterns observed during multi-source porting

1. **Copy-pasting the full Met `_get_with_retry`'s docstring without
   editing the source-name placeholder.** Looks fine in code review;
   confuses operators reading log warnings. The warning message must
   reference the right source.
2. **Forgetting to update the `museum=` constant.** Bug only visible
   downstream (LLM grounding pulls the wrong museum name). Add a unit
   test that asserts `out["museum"] == AIC_MUSEUM_NAME` per source.
3. **Copying the Met classification filter set verbatim.** Almost
   certainly the wrong project filter for the new source — AIC's filter
   is intentionally wider. Always reconsider the filter set per-source.
4. **Hardcoding the IIIF base URL when the source exposes
   `config.iiif_url`.** Future migration risk. Pin the constant in code
   AND assert it matches the live API response in an integration test
   (not done in v1 — flagged as a follow-up).
