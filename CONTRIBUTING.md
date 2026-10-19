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

Releases are automatic — no tags by hand. The version lives only in
`xdbtool/__init__.py` (`__version__`; `pyproject.toml` reads it). To publish:

1. Set `__version__` (e.g. `0.3.0`) and turn `## [Unreleased]` in `CHANGELOG.md`
   into `## [0.3.0] - YYYY-MM-DD` (its text becomes the release notes; keep an
   empty `## [Unreleased]` above it).
2. Commit and push to `main`.

On every push `.github/workflows/release.yml` reads the version; if `v0.3.0` is
not released yet it runs the tests, builds the three formats — Windows GUI zip,
Linux CLI single binary (built on Debian 11 / glibc 2.31 so it runs on Ubuntu
20.04+), wheel + sdist — smoke-tests each binary, writes `SHA256SUMS.txt` and
publishes the release, creating the tag. A version without its CHANGELOG section
fails on purpose. Pushes that do not change the version finish in seconds.
To retry a failed release: *Actions → Release → Run workflow*.

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
