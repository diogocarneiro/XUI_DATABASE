# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added
- **Translations**: Portuguese (default), English, French, Spanish and Italian.
  GUI menu *Idioma / Language* (saved in `settings.json`, window rebuilt on
  switch); CLI `--lang` (CLI default stays English). New `xdbtool/i18n` package
  (`_()` / `N_()`, JSON catalogs with named placeholders, `extract.py` checker);
  reports, notes and connection hints from the core are translated too;
  `tests/test_i18n.py` enforces complete catalogs and matching placeholders.
- **SSH + server MySQL socket** connection mode (GUI *Connect via*, CLI
  `--ssh-mysql-socket [PATH]`): relays each connection to MySQL's unix socket
  through a Python (or socat / nc -U) bridge run on the server, so `root` with
  unix_socket authentication logs in like `mysql -uroot`. Socket auto-detected
  from `my_print_defaults` and the usual paths. Profiles remember the mode.

### Fixed
- SSH mode: a MySQL host equal to the SSH host is treated as 127.0.0.1 (MariaDB
  saw a remote login from the server's LAN IP, e.g. `root@192.168.1.106`); the
  GUI moves it automatically; 1045 hints now point to the socket mode.
- SSH tunnel on **Templates / New DB** and **Migrate / Restore** (shared
  `gui/ssh_fields.py` widget, also used by Data source); CLI `--ssh-*` on
  `init-db`/`restore`, `--target-ssh-*` on `migrate`.
- **Drop guard**: `create_database(drop_existing=True)` refuses to drop a
  database holding panel data (`DropRefused` with row counts) unless
  `allow_drop_panel`; GUI asks to type the database name, CLI needs
  `--allow-drop-panel`. `xui_migrate` staging replacement stays allowed.
- **SSH tunnel for the main connection**: *Data source → Connect via → SSH
  tunnel* (CLI `--ssh-host` … on `info`/`export`/`maint`) forwards a local port
  to the server's MySQL, so tables, Export and Maintenance work with servers whose
  MySQL only accepts local connections. `SshTunnel` + `connect_client()` in
  `core/ssh_remote.py`; `ConnectionConfig.ssh`; SSH settings stored in profiles
  (SSH password only with *Save password*); SSH-specific 1045/1130 hints.
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
