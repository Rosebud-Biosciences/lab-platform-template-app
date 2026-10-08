# Contributing

Issues and pull requests are welcome. Security problems go through
[SECURITY.md](SECURITY.md), never a public issue. Everyone taking part
follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## The checks CI runs

```bash
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run ty check
# Row-level security is PostgreSQL's; without TEST_POSTGRES_URL its tests skip.
TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres uv run pytest

tofu fmt -check -recursive infra
tofu -chdir=infra/preview init -backend=false && tofu -chdir=infra/preview validate
```

CI also imports each deployable with its runtime dependencies only
(`ci.yml`), which catches a dependency that only the dev group provides.

## Copies of this template

Deployments that keep tracking this template copy the paths in
[`.github/template-drift/shared-paths`](.github/template-drift/shared-paths)
verbatim, and their drift check fails when a copy differs. A fix to any of
those paths lands here first.
