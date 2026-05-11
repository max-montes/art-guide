# SKILL: python-venv-isolation

**When to use:** any Python service in this repo whose install pulls heavy ML dependencies (`torch`, `transformers`, `tensorflow`, `jax`, `onnxruntime`, CUDA wheels, audio stacks, etc.), or any service whose `pip install -e .` could plausibly upgrade a top-level package that other projects on a contributor's machine pin to an older version. If a `pip install` step in your README could silently break an unrelated project on someone's laptop, this skill applies.

**Rule of thumb:** if you ever wrote `pip install -e ../something-with-torch` in a README without a `python -m venv` step right above it, you owe yourself this script.

## The pattern

1. **Ship a `.venv-bootstrap.sh` next to the service's `pyproject.toml`.** Not `setup.sh`, not `install.sh` — the dot-prefix groups it with `.env.example`/`.gitignore` and signals "tooling, not source." Make it executable and reference it as the *first* install step in the README.

2. **Script must be idempotent.** Re-running on a healthy checkout should be a no-op (or near-no-op) — contributors will run it after every `git pull`. Two cheap layers:
   - If `.venv/` exists, reuse it instead of recreating.
   - If the target imports already succeed inside the venv (`python -c "import pkg_a, pkg_b"`), fast-skip the `pip install` calls entirely.

3. **Always start with `set -euo pipefail` and `cd "$(dirname "$0")"`.** The `cd` makes the script work from any cwd; `pipefail` makes silent failures impossible.

4. **Sanity-check that activation actually worked** before running pip. `source .venv/bin/activate` failures are silent. After activating, assert `command -v python` resolves into `$(pwd)/.venv/bin/python`. If not, bail. This is the difference between "the script created a venv" and "the script installed into the venv."

5. **Pin the Python version check to whatever `pyproject.toml` says.** Read `requires-python` and assert it. Don't rely on the system `python3` being new enough; let contributors override with `PYTHON_BIN=/path/to/python3.11 ./.venv-bootstrap.sh` if their default `python3` is too old.

6. **Install heavy deps first.** If service A depends on local package B (which pulls torch/transformers), run `pip install -e ../B` *before* `pip install -e .`. Pip will resolve once with the heavier constraints rather than re-resolving and possibly downgrading mid-install.

7. **End with a clear next-step printout.** A `cat <<'EOF' ... EOF` block with the exact `source .venv/bin/activate && <run command>` so the contributor's eyes land on copy-paste-ready text. Don't make them scroll back to the README.

8. **Do not forget the README "Why a venv?" callout.** Three sentences, explaining the specific cross-project conflict you're guarding against (e.g. "SigLIP needs torch>=2.2, pyannote-audio pins torch==2.1.2, pip will upgrade torch globally and silently break the other project"). Future contributors will skip the script unless they understand *why*. Name names — vague "best practices" warnings get ignored.

## Why not poetry / uv / pipenv / hatch?

A 60-line bash script solves the actual problem (don't pollute global Python) without any of these costs:

- No lockfile to maintain or regenerate on every dep bump.
- No CI plumbing changes — `pip install -e .` still works in CI, and CI containers are already isolated.
- No contributor onboarding tax (no "first install our tool to install our deps").
- No upstream tool risk (the install step is `python3 -m venv` and `pip install`, both stdlib-stable).

Reach for `uv` (or similar) when you actually need *cross-package version pinning* — i.e. when "torch resolved differently on two laptops" becomes a recurring debugging cost. Don't reach for it just to wrap `python -m venv`.

## Skeleton

```bash
#!/usr/bin/env bash
# Bootstrap the project-local venv for <service>.
#
# Why this exists: <one sentence naming the specific cross-project conflict>.
# See <service>/README.md → "Why a venv?".

set -euo pipefail
cd "$(dirname "$0")"

REQUIRED_MAJOR=3
REQUIRED_MINOR=11   # match <service>/pyproject.toml::requires-python
PYTHON_BIN="${PYTHON_BIN:-python3}"

command -v "$PYTHON_BIN" >/dev/null 2>&1 \
  || { echo "error: '$PYTHON_BIN' not found" >&2; exit 1; }

"$PYTHON_BIN" -c "import sys; raise SystemExit(0 if sys.version_info >= (${REQUIRED_MAJOR}, ${REQUIRED_MINOR}) else 1)" \
  || { echo "error: Python ${REQUIRED_MAJOR}.${REQUIRED_MINOR}+ required" >&2; exit 1; }

[ -d .venv ] || "$PYTHON_BIN" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

# Hard-fail if activation didn't take.
[ "$(command -v python)" = "$(pwd)/.venv/bin/python" ] \
  || { echo "error: venv activation failed" >&2; exit 1; }

# Fast-skip if everything already imports.
if python -c "import <pkg_a>, <pkg_b>" >/dev/null 2>&1; then
  echo "==> Already installed (fast-skip)"
else
  python -m pip install --upgrade pip
  pip install -e ../<heavy-local-dep>   # heavy first
  pip install -e ".[dev]"
fi

cat <<'EOF'

✅ <service> venv ready.

Next steps:
  source .venv/bin/activate
  <run command>
EOF
```

## Reference implementation

`services/api/.venv-bootstrap.sh` (art-guide). Heavy dep is `services/ml/` (SigLIP + torch). Conflict it guards against: `pyannote-audio`'s `torch==2.1.2` pin.

## Tripwire history

- 2026-05-10 — first hit: max-montes ran `pip install -e ../ml` without a venv; pip upgraded global torch from 2.1.2 → 2.2+, broke a local `pyannote-audio` install. Recovery (run outside the art-guide venv): `pip install 'torch==2.1.2' 'torchaudio==2.1.2' 'torchvision==0.16.2'`. This skill is the postmortem fix.
