# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added
- **Direct server injection**: `restore` CLI command and a GUI **Migrate /
  Restore** tab that write straight to a live MySQL server — restore a `.sql`
  dump as-is (optionally creating the target DB) or run a live column-mapped
  migration. Live writes prompt for confirmation in the GUI.
- `core/restore.py` (`restore_dump`).
- Schema **templates**: register a panel's base (vanilla) `.sql` schema and
  recreate a fresh database from it on a target server (`templates`, `init-db`
  CLI commands and a "Templates / New DB" GUI tab).
- `MySQLSource.create_database` / `select_database` / `execute_script` for schema
  provisioning.
- Template **fingerprinting** — `info` reports the closest known template/version
  (Jaccard similarity). Base `.sql` stays local; only name-only signatures
  (`xdbtool/data/panel_signatures.json`) are committed.

## [0.1.0] - 2026-10-18
### Added
- Data-source abstraction with two backends: live MySQL/MariaDB (PyMySQL) and
  offline `mysqldump` `.sql` parsing.
- Heuristic panel detection for XUI.one / Xtream-UI / StreamCreed and table
  categorisation (accounts, streams, bouquets, settings, logs, …).
- Export to JSON, CSV and SQL, with optional column redaction and `SHOW CREATE
  TABLE` DDL for SQL exports.
- Maintenance operations (CHECK / REPAIR / ANALYZE / OPTIMIZE) and a
  check-then-repair-only-broken flow for live MySQL.
- Column-level migration engine (live execution or SQL dry-run).
- PySide6 GUI and an argparse-based CLI (`info`, `export`, `maint`, `migrate`).
- Offline pytest suite, ruff config and GitHub Actions CI.
