# CI

Phase 1 — three jobs, all gating on `main`:

| Job             | What it runs                       | Why                                                       |
|-----------------|------------------------------------|-----------------------------------------------------------|
| `pytest`        | `uv run pytest tests/`             | The Python test suite (CME, llm_provider, infra)          |
| `web typecheck` | `cd web && npx tsc --noEmit`       | Catches TS errors before merge                            |
| `web lint`      | `cd web && npx eslint .`           | Catches lint errors (warnings allowed)                    |

All three must be green for `main` to accept a merge — this is wired
via GitHub Settings → Branches → `main` → Require status checks.

## Reproducing CI locally

Before pushing, run the same commands CI runs:

```bash
# Python — from repo root
uv sync --frozen
uv run pytest tests/

# Web — from web/
cd web
npm ci                    # or `npm install` if you've added/removed deps
npx tsc --noEmit
npx eslint .
```

If any of these are red locally, CI will be red too.

## Integration tests

Network-touching tests are marked `@pytest.mark.integration` and are
**not** part of the default `pytest tests/` invocation. They require
real `GEMINI_KEY` / `GROQ_API_KEY` env vars and hit the real APIs.
Run them on demand:

```bash
uv run pytest tests/ --integration
```

These intentionally don't run in CI yet (would burn API quota on
every PR and need real secrets in GH).

## Adding tests later

Per the test plan in [IMPROVEMENTS.md](IMPROVEMENTS.md), modules
without coverage today get one focused test file each in subsequent
PRs. The CI infrastructure here already handles them — just add the
test file under `tests/` (Python) or `web/**/*.test.ts` (frontend,
once Vitest is wired in Phase 3).

## Why a single `ci.yml` and not split files

Easier to reason about — one file, three jobs, parallel by default
because GH Actions runs sibling jobs concurrently. Splitting into
`ci-py.yml` / `ci-web.yml` would just double the YAML.

## What this does NOT cover

- **Build smoke** — `next build` is not in CI. We'd add it once we
  actually deploy somewhere; for a local-only single-user app it's
  not worth the ~2 min on every PR.
- **Coverage reports** — no codecov hookup yet. Single-user app
  doesn't have the reviewer pressure that makes coverage badges
  useful.
- **Format check** — no prettier check. The repo doesn't have a
  prettier config and tsc/eslint catch the things that matter.
