# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Before opening an issue

Use the bug report template and attach the (redacted) diagnostics file and debug logs. Never post
credentials or tokens.

## Development setup

```bash
uv venv --python 3.14
source .venv/bin/activate
uv pip install -r requirements_dev.txt
```

## Checks (all run in CI)

```bash
ruff check . && ruff format --check .
mypy
pytest --cov --cov-report=term   # coverage must stay at or above 95 %
```

- Every change ships with unit/integration tests; do not lower the coverage threshold.
- Test fixtures must contain fake values only. Never commit real portal responses, HAR files or
  captures (they are git-ignored).
- Code, comments and docs are in English; user-facing strings go in `translations/en.json` and
  `translations/fr.json`.
- Update `CHANGELOG.md` under "Unreleased".
- The integration follows the Home Assistant integration conventions (quality scale rules in
  `custom_components/edf_enr/quality_scale.yaml`).

## Pull requests

Keep them focused, describe the behavior change, and make sure CI is green.
