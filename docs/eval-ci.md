# Eval CI Integration Sketch

> **Status:** Design-only. Not yet wired. This document describes what the GitHub Actions workflow
> will look like when Phase 2 lands. Do not create `.github/workflows/eval.yml` until the Azure
> integration decisions in D-013 are resolved.

---

## Overview

The eval CI workflow turns every PR that touches `services/ml/`, `services/api/`, or
`packages/shared/api/` into a guarded gate: if recall@1 or any other threshold in
`services/ml/eval/thresholds.yaml` drops, the PR fails.

---

## When it fires

- Push / PR to `main` that modifies:
  - `services/ml/**`
  - `services/api/**`
  - `packages/shared/api/**`
  - `services/ml/eval/**` (the eval set itself changing is a signal worth running)

---

## What the workflow does (sketch)

```yaml
# .github/workflows/eval.yml  (DO NOT CREATE YET — design sketch only)
name: retrieval-eval

on:
  pull_request:
    paths:
      - 'services/ml/**'
      - 'services/api/**'
      - 'packages/shared/api/**'

jobs:
  eval:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: artguide
        ports: ['5432:5432']

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with: { python-version: '3.12' }

      - name: Install ML service
        run: pip install -e services/ml

      - name: Run DB migrations
        env:
          DATABASE_URL: postgresql://postgres:postgres@localhost:5432/artguide
          AUTO_MIGRATE: 'true'
        run: art-guide-ml ingest met --dry-run --limit 1  # triggers migration only

      - name: Ingest seed set (~20 records)
        env:
          DATABASE_URL: postgresql://postgres:postgres@localhost:5432/artguide
        run: |
          # Ingest a fixed, reproducible seed of 20 artworks from department 11.
          # Object IDs are stable — same 20 records every run, so eval scores are comparable.
          art-guide-ml ingest met --limit 20 --department-ids 11

      - name: Start API server
        env:
          DATABASE_URL: postgresql://postgres:postgres@localhost:5432/artguide
          RATE_LIMIT_REQUESTS: '200'
          RATE_LIMIT_WINDOW_SECONDS: '60'
        run: |
          cd services/api
          uvicorn app.main:app --host 0.0.0.0 --port 8000 &
          sleep 5  # wait for readiness

      - name: Run eval (in-catalog only, no delay needed in CI)
        env:
          EVAL_API_URL: http://localhost:8000
        run: |
          cd services/ml
          python -m eval.run_eval \
            --in-catalog-only \
            --request-delay-s 0 \
            --output-dir eval-output/

      - name: Post report as PR comment
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            const md = fs.readdirSync('services/ml/eval-output')
              .filter(f => f.endsWith('.md'))[0];
            const body = fs.readFileSync(`services/ml/eval-output/${md}`, 'utf8');
            github.rest.issues.createComment({
              ...context.repo,
              issue_number: context.issue.number,
              body: `## Retrieval Eval\n\n${body}`
            });

      - name: Upload report artifact
        uses: actions/upload-artifact@v4
        with:
          name: eval-report
          path: services/ml/eval-output/
```

---

## Why not Azure AI Foundry?

The original `now.md` phrase "Foundry CI integration" referred to **Azure AI Foundry** (formerly
Azure ML Studio). That is a Phase 2 item (see D-013 — fine-tuning, reranker, Azure deployment are
all out of scope for v1). The GitHub Actions sketch above is the **v1 CI form**: it is
self-contained, requires no Azure credentials, and runs on any GitHub-hosted runner.

When Phase 2 lands and the eval set grows large enough to justify Foundry's distributed eval
infrastructure, this file should be updated and `.github/workflows/eval.yml` created.

---

## Key parameters to pin for reproducibility

| Parameter | Value | Notes |
|-----------|-------|-------|
| Seed object IDs | Same 20 Met dept-11 IDs every run | Pin in a `eval/ci-seed.txt` file |
| Server rate limit | `200 req / 60 s` | No delay needed; CI runner IP is different from dev |
| Thresholds | `services/ml/eval/thresholds.yaml` | Permissive at bootstrap; tighten as catalog grows |
| Python version | 3.12 | Match `pyproject.toml` requires-python |

---

## Failure behaviour

- The `run_eval.py` main exits **non-zero** if any threshold is breached.
- GitHub Actions marks the step failed → PR is blocked from merging.
- The PR comment with the full markdown report is always posted (even on failure), so reviewers
  can see the exact metric that dropped.
