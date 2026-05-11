---
name: "sql-migration-runner"
description: "Plain-SQL migration runner for an asyncpg-only FastAPI service: discovery, ordering, idempotency, transactional apply, schema_migrations table, optional startup auto-apply gated by env."
domain: "backend-architecture"
confidence: "high"
source: "earned (art-guide services/api 0001_init schema landing)"
---

## Context

Use this when a Python service needs Postgres schema migrations and:

- The stack is asyncpg-only — no SQLAlchemy ORM is otherwise in the dependency tree.
- You have one or a small handful of migrations and don't yet need branching, autogeneration, or downgrades.
- You want a runner you can audit in five minutes and that doesn't add a new top-level dependency.

If migrations grow into the dozens, or if you need branching/downgrades, swap to Alembic. The `schema_migrations` table below renames cleanly to Alembic's `alembic_version` in a single migration of its own.

## Patterns

### 1. File layout

```
services/<svc>/app/migrations/
├── __init__.py     # the runner
├── __main__.py     # `python -m app.migrations` entrypoint
├── 0001_init.sql
└── 0002_<name>.sql
```

- One subpackage per service holds both SQL files and the runner.
- Filename grammar: `^\d{4}[_-][A-Za-z0-9_-]+\.sql$`. The 4-digit prefix is the sort key; the rest is documentation.
- The filename **stem** (e.g. `0001_init`) is the version id stored in `schema_migrations`.
- Never edit a migration after it ships — write a new one.

### 2. The `schema_migrations` table

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
```

The runner ensures this exists on every invocation, before reading the applied set. PK on `version` makes double-apply impossible at the DB level even if two runners race.

### 3. Discovery (sort + ignore noise)

```python
import re
_MIGRATION_FILE_RE = re.compile(r"^(?P<v>\d{4})[_\-][A-Za-z0-9_\-]+\.sql$")

def discover_migrations(directory):
    if not directory.is_dir():
        return []
    files = [p for p in directory.iterdir()
             if p.is_file() and _MIGRATION_FILE_RE.match(p.name)]
    files.sort(key=lambda p: p.name)
    return files
```

Matters: ignore `README.md`, editor swap files, and anything else without the `NNNN_` prefix. A stray file should never break startup.

### 4. Apply: per-migration transaction wrapping the version row

```python
async def apply_migrations(pool, *, directory=MIGRATIONS_DIR):
    files = discover_migrations(directory)
    async with pool.acquire() as conn:
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (...)")
        applied = {r["version"] for r in
                   await conn.fetch("SELECT version FROM schema_migrations")}
        new = []
        for path in files:
            version = path.stem
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations(version) VALUES ($1)", version
                )
            new.append(version)
        return new
```

Critical detail: the migration body **and** the `INSERT INTO schema_migrations` row are in the **same** transaction. A half-applied migration rolls back atomically and the runner is safe to re-invoke.

### 5. CLI entrypoint

`__main__.py`:

```python
from . import main
if __name__ == "__main__":
    main()
```

Where `main()` runs `asyncio.run(_amain())` and `_amain()` opens a short-lived pool, calls `apply_migrations`, prints a summary, and closes the pool. This is the explicit deploy-step interface; tests use `apply_migrations(pool)` directly.

### 6. Optional startup auto-apply, off by default

Add `AUTO_MIGRATE: bool = False` to your `Settings`. In the FastAPI `lifespan`, after the pool opens:

```python
if pool_open and settings.AUTO_MIGRATE:
    try:
        await apply_migrations(get_pool())
    except Exception:
        logger.exception("auto-migrate failed at startup")
```

Default off **everywhere**, even `local`. Local devs opt in via `.env`. Prod stays off — migrations are a deliberate deploy step. Failures log but don't crash startup, so `/readyz` keeps reporting the truth.

### 7. Testing without a real Postgres

Three layers:

1. **Unit tests** with an in-process fake `Pool`/`Connection` (just an `acquire()` async-context-manager and an `execute`/`fetch`/`transaction()` surface that records calls). Cover: discovery sort, ignore-noise, missing-dir, runner happy path, idempotency on re-run, skip-already-applied, no-files-still-creates-history-table.
2. **DDL contract tests** that read your `0001_init.sql` and grep for the load-bearing strings: `CREATE EXTENSION ...`, exact column types (e.g. `vector(768)`), index method (`USING hnsw (... vector_cosine_ops)`), uniqueness constraints. This catches "someone changed the embedding dimension and forgot the index" before the integration suite ever runs.
3. **Integration test** marked `@pytest.mark.integration` that runs against a real Postgres in a throwaway schema (`CREATE SCHEMA art_guide_test_migr; ... search_path=art_guide_test_migr; ... DROP SCHEMA ... CASCADE`). Skipped by default via `addopts = "-m 'not integration'"`.

The unit + contract tests run in <2 seconds with no external dependencies; the integration test is the live verification path.

## Anti-patterns

- **Editing a shipped migration.** Always add a new file. The version table tracks what's applied; rewriting history desyncs deployed databases from the repo.
- **Auto-applying on every prod boot.** Surprise schema changes during a rolling deploy are how outages happen. Make it an explicit step.
- **A single file with all DDL re-run on every boot using `IF NOT EXISTS`.** Loses you the "what's been applied where" answer and silently masks intentional schema changes.
- **Putting the body and the version-row insert in separate transactions.** A failure between them leaves the system in a half-applied state.
- **One transaction wrapping all migrations.** A failure mid-batch rolls back work that succeeded; harder to recover.
- **Skipping the file-grammar regex.** Editor swap files (`.0001_init.sql.swp`), `README.md`, and `0002_bad_name` (no extension) all sneak in otherwise.

## When NOT to use this

- You need branching/downgrades or autogeneration → use Alembic from day one.
- The team standard is Django/SQLAlchemy with ORM models → use the ORM's migration tool to keep models and DDL in sync.
- You have hundreds of migrations and need merge-conflict-friendly version naming → Alembic's per-revision-hash naming is better than 4-digit counters.
