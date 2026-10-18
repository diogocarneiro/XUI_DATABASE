# Contributing

Thanks for your interest in improving XUI DB Tool.

## Development setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Before opening a PR

- `ruff check .` — lint must pass (run `ruff check . --fix` to auto-fix).
- `pytest -q` — the suite runs fully offline (no database required).
- Add or update tests for behaviour you change. The parser/export/detection
  logic is covered by `tests/test_core.py` using the fixture in
  `tests/fixtures/`.

## Guidelines

- Keep the `core/` package free of any GUI (`PySide6`) imports — the CLI and the
  test suite must work without the GUI toolkit installed.
- New panel support goes through `core/profiles.py` (signature tables), not by
  hard-coding column names — data is read by live introspection.
- Treat panel data as the owner's. Do not add features that decrypt, crack or
  otherwise defeat stored credentials; migration copies hashes verbatim.

## Adding support for another panel

1. Add a `PanelProfile` to `PROFILES` in `core/profiles.py` with its signature
   tables.
2. If it introduces new logical groupings, extend `TABLE_CATEGORIES`.
3. Add a small anonymised fixture under `tests/fixtures/` and a detection test.
