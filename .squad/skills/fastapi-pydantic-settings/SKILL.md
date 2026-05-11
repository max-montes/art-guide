---
name: "fastapi-pydantic-settings"
description: "Env-driven config + asyncpg pool + DB-aware /readyz pattern for a FastAPI service running in two environments."
domain: "backend-architecture"
confidence: "high"
source: "earned (art-guide services/api Phase-1 refactor)"
---

## Context

Use this when a FastAPI service needs to:

- Run in exactly two environments (`local` via Docker Compose, `prod` via a managed cloud) with the same image.
- Be strict about prod secrets without making local dev annoying.
- Open and ping a Postgres connection pool from app startup.
- Survive Postgres being briefly down without failing liveness probes.

## Patterns

### 1. Single `Settings` class via `pydantic-settings`

Put all runtime config in one `BaseSettings` subclass. Field name == env var name. Use `case_sensitive=False`, `extra="ignore"`, and an `.env` file fallback for local.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")
    ENV: Literal["local", "prod"] = "local"
    DATABASE_URL: str = "postgresql://app:app@localhost:5432/app"
    API_BEARER_TOKEN: str | None = Field(
        default=None,
        validation_alias=AliasChoices("API_BEARER_TOKEN", "API_KEY"),  # legacy alias
    )
```

Cache the instance with `@lru_cache(maxsize=1) def get_settings(): return Settings()`. Tests reset it with `get_settings.cache_clear()`.

### 2. `model_validator` enforces prod strictness

Defaults are sized for local dev. A single `mode="after"` validator collects all the prod-required fields and raises with **all** missing names at once — much friendlier than failing on the first one.

```python
@model_validator(mode="after")
def _prod_requires_secrets(self) -> "Settings":
    if self.ENV != "prod":
        return self
    missing = [k for k in (...) if not getattr(self, k)]
    # also reject local-default DATABASE_URL in prod
    if self.DATABASE_URL.startswith("postgresql://app:app@localhost"):
        missing.append("DATABASE_URL")
    if missing:
        raise ValueError("ENV=prod requires explicit values for: " + ", ".join(missing))
    return self
```

### 3. asyncpg pool managed by `lifespan`, errors logged not raised

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    try:
        await init_pool(settings)
    except Exception:
        logger.exception("failed to open Postgres pool at startup")
    try:
        yield
    finally:
        await close_pool()
```

Why catch the open error: if Postgres is briefly unavailable at boot, the app should still come up so liveness answers and readiness reports the truth. Otherwise platform restart loops mask the underlying issue.

### 4. Liveness ≠ readiness

- `/healthz`: process is up. **Never** touch dependencies. Always 200.
- `/readyz`: dependencies are usable. Returns 200 when `SELECT 1` succeeds, 503 otherwise. Body is additive (`{status, db}`) so adding more checks (LLM, cache, …) later is non-breaking.

### 5. Middleware reads from `Settings`, not `os.environ`

Middleware constructors take an optional override (`limiter=None`, `api_key=None`) and fall back to `get_settings()`. Tests inject overrides; runtime gets the live config.

### 6. Test-time isolation

`conftest.py` strips all of the service's env vars in an autouse fixture so a stray `.env` in the dev's CWD can't bleed into `Settings(_env_file=None)` calls. Tests opt-in with `monkeypatch.setenv`.

For TestClient tests against the app, stub `init_pool`/`close_pool`/`ping` so the lifespan doesn't try to dial real Postgres.

## Examples

✓ **Correct:**
- `Settings(_env_file=None)` in tests bypasses any `.env` so the test environment is purely what `monkeypatch.setenv` set.
- `validation_alias=AliasChoices("NEW_NAME", "OLD_NAME")` for env var renames keeps existing dev `.env` files working through the transition.
- `pg_isready` healthcheck on the Postgres container so the app can wait via `docker compose up -d --wait`.

✗ **Incorrect:**
- Reading `os.environ` deep in routes/middleware (untestable, no validation).
- `/readyz` mirroring `/healthz` once dependencies exist (defeats the purpose of two probes).
- Failing app startup when Postgres is briefly down (probes can't tell you what's wrong).
- Hard-renaming an env var without an alias (silently breaks every dev `.env`).

## Anti-patterns

- One settings class per module. Centralize.
- Prod-required fields scattered across many validators. One `model_validator(mode="after")` collects everything in a single error message.
- Coupling tests to live infra. Stub the pool; assert the contract.
