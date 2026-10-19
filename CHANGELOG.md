# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added
- **XUI.one → XUI.one import** (`xui-import` CLI, GUI section in **XUI
  Migration**): imports an XUI.one backup into another server's live `xui` over
  SSH. Reads the target schema and copies only shared tables/columns; keeps the
  target's servers/settings/access codes; skips logs; moves streams to the
  target's main server or adds the old load balancers; backs up `xui` with
  `mysqldump` first and replaces data in one transaction. Offline `--sql-out`
  with a template as the target schema. New `core/xui_import.py`.
- XUI import: **choose the content to restore** (`--content`, GUI checkboxes):
  live channels, movies, series, radio, bouquets, lines/devices, resellers, EPG,
  security lists, other. Partial stream kinds replace only their `streams.type`
  rows, categories, server links and options; colliding ids with kept target
  rows are refused before any change; bouquets drop lists of unchosen kinds.
- XUI import: choose **access codes / settings / servers** individually; create a
  **rescue access** (admin access code + admin user, random password) in the same
  transaction; report all access-code URLs and the rescue login at the end; run
  XUI's `tools access` to refresh served codes. New `core/passwords.py`
  (SHA-512 crypt for new accounts only, checked against the spec vectors).
- **XUI.one migration staging**: `xui-migrate` CLI command and an **XUI
  Migration** GUI tab load an old Xtream UI / ZapX / StreamCreed / Xtream Codes v2
  dump into the `xui_migrate` database, ready for XUI's *Database Migration*
  page. Strips the dump's `CREATE DATABASE`/`USE` directives, skips log rows by
  default, relaxes `sql_mode` for MySQL 5.x dumps, refuses XUI.one dumps, can
  write a script to run on the server, and optionally grants XUI's DB user.
- **SSH mode** for `xui-migrate` (`--ssh-host` …, default in the GUI): uploads a
  gzipped script over SFTP and runs the server's own `mysql`, so servers whose
  MySQL refuses remote logins (error 1130, the XUI.one default) work unchanged.
  MySQL credentials go in a temporary `chmod 600` option file, never on the
  command line. New `core/ssh_remote.py`; new dependency `paramiko`.
- Connection errors 1130/1045 now explain the cause and the alternatives.
- `core/xui_migrate.py`; `.sql.gz` dump support (`read_sql_text`);
  `MySQLSource.execute`.
- Copyright header on every source file, enforced by `tests/test_copyright.py`.
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
