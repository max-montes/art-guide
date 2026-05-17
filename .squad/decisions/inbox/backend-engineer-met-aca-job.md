# D-NNN candidate: Met ingest via Azure Container Apps Job (met-dump path)

**Date:** 2026-05-17  
**Author:** backend-engineer  
**Status:** Inbox — awaiting Brady review and D-number assignment

---

## Decision

Switch the existing `art-guide-prod-ingest` ACA Job from the v1 API path (`ingest met`) to the v2 CSV dump path (`ingest met-dump`) and add `--resume-skip-existing` support to `met_csv.py`.

**Entrypoint command:**
```
art-guide-ml ingest met-dump \
  --limit 0 \
  --request-delay 0.015 \
  --batch-commit-size 64 \
  --batch-size 4 \
  --resume-skip-existing
```

---

## Why

- **Laptop is blocked.** Met's per-IP throttle (~80 req/s) plus multi-hour cooldowns make residential-IP ingest unreliable at 501K-record scale.
- **Azure egress IP was also in penalty box** from prior D-052 parallel-ingest experiments. The ACA Job is the right long-term answer.
- **v2 CSV dump path cuts API calls ~50%.** The `MetObjects.csv` pre-filter rejects non-PD + denylist records before any `/objects/{id}` call, reducing required API calls from ~501K to ~250K.
- **`--resume-skip-existing` is now ported to `met_csv.py`** (D-060 pattern). A restarted job skips already-embedded IDs, making multi-restart runs cheap.

---

## What changes

| File | Change |
|---|---|
| `services/ml/ml/ingest/met_csv.py` | Added `_load_existing_source_ids()`, `resume_skip_existing` param to `ingest_met_csv_to_db()`, skip logic in the CSV loop, `skipped_existing` counter in `CSVIngestStats` |
| `services/ml/ml/cli.py` | Added `--resume-skip-existing` arg to `met-dump` parser, pass flag to `ingest_met_csv_to_db()`, include `skipped_existing` in summary output |
| `infra/azure/main.bicep` | **Not yet changed** — Brady reviews `docs/met-aca-job.md` first. Change: `command` array from `ingest met ...` → `ingest met-dump ... --resume-skip-existing`, image from `v4` → `latest` |
| `docs/met-aca-job.md` | New operator runbook (this session) |

---

## Constraints honored

- D-006 (catalog over model): new coverage via new ingest adapter, not retraining ✓
- D-013 (two environments only): job runs in `prod` env only ✓
- D-028 (SigLIP offline): `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` set in job env ✓
- D-053 (single-worker rate discipline): parallelism=1, request_delay=0.015 s ✓
- D-060 (resume-skip pattern): ported to `met-dump` in this session ✓
- Hard rule #3 (no raw images stored): image bytes held in-memory only, never written to disk ✓
- Hard rule #4 (one image pipeline): all embedding via `ml.imageops.prepare_for_embedding` ✓

---

## Open questions for Brady

1. **Confirm the Bicep `command` change** before running `az deployment group create`. See diff in `docs/met-aca-job.md` Step 2.
2. **Confirm the `dbUrl` parameter** in `parameters.prod.json` has the correct prod Postgres DSN. The job injects it as the `database-url` secret.
3. **IP cooldown check** (T-5 in the runbook): verify Azure egress is not still in Met's penalty box before starting.
4. **Image build**: confirm new image with `--resume-skip-existing` support is pushed to ACR before job start. The flag is in code as of this session's commit.

---

## Cost impact

~$1.10–1.60 per full run (2 vCPU × 3.5 hr). Well within $100/mo budget alert.
