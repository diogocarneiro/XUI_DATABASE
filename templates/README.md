# Local templates

This folder holds **base (vanilla) panel schema dumps** and a local `manifest.json`.
It is **git-ignored on purpose** — the `.sql` files are the panel software's own
schema, not your data, and are kept local rather than redistributed.

Only name-only *signatures* derived from these templates are committed to the repo,
under `xdbtool/data/panel_signatures.json`, to help panel detection.

## Register a base schema

```bash
python -m xdbtool templates --register path/to/base.sql --panel xui_one --version 1.5.13 --name "XUI.one 1.5.13"
python -m xdbtool templates           # list registered templates
```

## Recreate a fresh database from a template

```bash
python -m xdbtool init-db --template xui_one-1.5.13 \
    --target-host 127.0.0.1 --target-user root --target-password secret \
    --target-database new_panel
```

## Refresh the committed signatures (maintainers)

```bash
python -m xdbtool templates --export-signatures
```
