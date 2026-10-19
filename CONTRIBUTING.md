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

## Releases

The version lives only in `xdbtool/__init__.py` (`__version__`); `pyproject.toml`
reads it. To publish:

1. Set `__version__` (e.g. `0.2.0`) and rename `## [Unreleased]` in
   `CHANGELOG.md` to `## [0.2.0] - YYYY-MM-DD` (its text becomes the release notes).
2. Commit, then tag and push the tag:
   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```
3. `.github/workflows/release.yml` runs the tests, refuses a tag that does not
   match `__version__`, builds the Windows app, and publishes the GitHub Release
   with `XUI-DB-Tool-0.2.0-windows.zip` and `SHA256SUMS.txt` — the files the
   in-app updater downloads and verifies.

## Translations

User-facing text is written in English and wrapped in `_()` (from `xdbtool.i18n`);
module-level constants use `N_()` and are translated where shown. Placeholders are
named, never positional: `_("{table}: {count} rows", table=t, count=n)`.

After adding or changing text:

```bash
python -m xdbtool.i18n.extract            # lists missing/unused per language
python -m xdbtool.i18n.extract --update   # adds missing keys (English) to each catalog
```

then translate the new entries in `xdbtool/i18n/locales/{pt,fr,es,it}.json`.
`tests/test_i18n.py` fails if any message is untranslated or a placeholder differs.

## Adding support for another panel

1. Add a `PanelProfile` to `PROFILES` in `core/profiles.py` with its signature
   tables.
2. If it introduces new logical groupings, extend `TABLE_CATEGORIES`.
3. Add a small anonymised fixture under `tests/fixtures/` and a detection test.
