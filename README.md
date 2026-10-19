# XUI DB Tool

A desktop + CLI tool to **inspect, back up, repair and migrate** the databases of
IPTV panels — **XUI.one**, **Xtream-UI** and **StreamCreed** — from a live
MySQL/MariaDB connection or from an offline `mysqldump` `.sql` file.

> **Intended use.** This tool is for administrators working on databases they own
> or are **explicitly authorised** to manage (e.g. migrating your own panel, or a
> client's panel with their permission). It migrates stored credentials *verbatim*
> — password hashes are copied as-is and are **never** decrypted or cracked. Do not
> use it against databases you do not control.

---

## Features

- **Two data sources, one interface**
  - Live **MySQL / MariaDB** connection (full read + maintenance).
  - Offline **`.sql` dump** parsing (read + export + migrate), no server needed.
- **Panel detection** — heuristically identifies XUI.one / Xtream-UI / StreamCreed
  and groups tables by category (accounts, streams, bouquets, settings, logs…).
- **Schema-agnostic** — everything is read by live introspection of
  `INFORMATION_SCHEMA`, so version/fork differences are handled gracefully.
- **Export / backup** to **JSON**, **CSV** or **SQL** (`INSERT`s, optionally with
  `SHOW CREATE TABLE` DDL). Optional redaction of password/secret-like columns.
- **Repair / maintenance** — `CHECK`, `REPAIR`, `ANALYZE`, `OPTIMIZE`, plus a
  one-click *check-then-repair-only-broken* flow (live MySQL only).
- **Migration** — column-level mapping between a source and a target database;
  transfers only columns present on both sides, so target auto-increment/defaults
  are preserved. Can execute live or emit a `.sql` script (dry run).
- **Schema templates** — register a panel's base (vanilla) schema once, then
  **recreate a fresh, clean database** for that panel on any target server. Base
  `.sql` files stay local; only name-only *signatures* are shipped, and they also
  fingerprint an unknown database to its closest known template/version.
- **GUI** (PySide6) **and** a scriptable **CLI**.

## Install

Requires **Python 3.10+**.

```bash
git clone https://github.com/diogocarneiro/XUI_DATABASE.git
cd XUI_DATABASE
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
```

## Run

### GUI

```bash
python -m xdbtool          # or: python -m xdbtool gui
```

1. Pick a source (live MySQL or a `.sql` dump) and click **Connect / Load**.
2. The detected panel and table list appear; tick the tables you want.
3. Use the **Export / Backup** or **Maintenance** tab.

### CLI

```bash
# Detect panel + list tables
python -m xdbtool info --dump backup.sql
python -m xdbtool info --host 127.0.0.1 --user root --password secret --database xtream_iptv

# Back up all tables to SQL
python -m xdbtool export --host 127.0.0.1 --user root --password secret \
    --database xtream_iptv --format sql --out ./backup

# Check and auto-repair broken tables (live only)
python -m xdbtool maint --host 127.0.0.1 --user root --password secret \
    --database xtream_iptv --operation auto

# Migrate from an old dump into a fresh panel DB (dry run → SQL file)
python -m xdbtool migrate --dump old_panel.sql \
    --target-host 127.0.0.1 --target-user root --target-password secret \
    --target-database new_panel --sql-out migration.sql --plan-only
```

### Templates — recreate a fresh panel database

```bash
# Register a base (vanilla) schema once; the .sql is copied into ./templates (local)
python -m xdbtool templates --register path/to/base.sql \
    --panel xui_one --version 1.5.13 --name "XUI.one 1.5.13"
python -m xdbtool templates                      # list registered templates

# Create a brand-new, clean database from a template on the target server
python -m xdbtool init-db --template xui_one-1.5.13 \
    --target-host 127.0.0.1 --target-user root --target-password secret \
    --target-database new_panel

# Typical workflow: create a clean DB, then migrate your data into it
python -m xdbtool init-db   --template xui_one-1.5.13 --target-host H --target-user U \
    --target-password P --target-database new_panel
python -m xdbtool migrate   --dump old_backup.sql    --target-host H --target-user U \
    --target-password P --target-database new_panel
```

### Inject directly into a server

Both migration and restore write **straight to a live MySQL server** (no files):

```bash
# Restore a full .sql dump into the server (optionally creating the DB first)
python -m xdbtool restore backup.sql --target-host 127.0.0.1 --target-user root \
    --target-password secret --target-database restored_panel --create

# Live migration: inject the source's rows directly into an existing target DB
python -m xdbtool migrate --dump old_backup.sql --target-host 127.0.0.1 \
    --target-user root --target-password secret --target-database new_panel
```

In the GUI these are on the **Migrate / Restore** tab. Live writes are
destructive — the GUI asks for confirmation, and you should always back up the
target first (`export --format sql`).

The base `.sql` files live under `./templates/` and are **git-ignored**. Only
name-only signatures (`xdbtool/data/panel_signatures.json`) are committed, which
is what `info` uses to report the *closest template* for an unknown database.

Always take a backup (`export --format sql`) **before** running `repair` or a live
migration.

### XUI.one migration — stage the `xui_migrate` database

XUI.one's **Database Migration** page converts an old **Xtream UI, ZapX,
StreamCreed or Xtream Codes v2** database itself, but it reads it from a schema
called `xui_migrate` on the XUI server. `xui-migrate` puts your old dump there:

- removes the dump's own `CREATE DATABASE` / `USE` lines, so everything lands in
  `xui_migrate` and the live `xui` database is never touched;
- skips log table rows by default (XUI does not migrate logs) — `--keep-logs` to
  load them;
- relaxes `sql_mode` so MySQL 5.x dumps load on XUI's MariaDB;
- accepts `.sql` and `.sql.gz`, and refuses dumps that are already XUI.one
  (`--force` to override).

```bash
# 1. Check the dump
python -m xdbtool xui-migrate old_panel.sql.gz --check-only

# 2a. Recommended: over SSH. XUI's MySQL refuses remote logins (error 1130), so the
#     tool uploads a script and runs the server's own mysql client. Blank
#     --mysql-user = root via unix socket; otherwise use XUI's user from config.ini.
python -m xdbtool xui-migrate old_panel.sql.gz --ssh-host 192.168.1.106 \
    --ssh-user root --ssh-password secret --drop

# 2b. Direct MySQL (only if the server accepts your IP, or through an SSH tunnel:
#     ssh -L 3307:127.0.0.1:3306 root@xui-server)
python -m xdbtool xui-migrate old_panel.sql.gz --target-host 127.0.0.1 --target-port 3307 \
    --target-user root --target-password secret --drop

# 2c. …or write a script, copy it to the server and run it there
python -m xdbtool xui-migrate old_panel.sql.gz --sql-out xui_migrate.sql --drop
#     (on the server)  mysql -u root -p < xui_migrate.sql
```

Then open the XUI panel → **Database Migration** → **Migrate Database**. If XUI's
own MySQL user cannot read `xui_migrate`, add `--grant user@localhost` (the user is
in XUI's `config.ini`). The same steps are in the GUI under **XUI Migration**.

## Project layout

```
xdbtool/
  core/           # GUI-independent logic
    source.py       # DataSource interface
    mysql_source.py # live MySQL/MariaDB (PyMySQL)
    dump_source.py  # offline mysqldump .sql parser
    profiles.py     # panel detection + table categories
    exporter.py     # JSON / CSV / SQL export
    repair.py       # CHECK / REPAIR / ANALYZE / OPTIMIZE
    migrator.py     # column-level migration engine
    restore.py      # execute a .sql dump on a live server
    xui_migrate.py  # stage an old panel dump as `xui_migrate` for XUI.one
    ssh_remote.py   # SSH/SFTP helper: run the server's mysql client remotely
    config.py       # connection profiles (user config dir)
  gui/            # PySide6 interface
  __main__.py     # CLI + GUI entry point
tests/            # pytest suite (runs fully offline)
```

## Development

```bash
pip install -r requirements-dev.txt
pytest          # run tests (no database required)
ruff check .    # lint
```

## Build a standalone executable

```bash
pip install -r requirements-dev.txt
pyinstaller --noconfirm --windowed --name XUI-DB-Tool -p . xdbtool/__main__.py
# result in dist/XUI-DB-Tool/
```

## License

[MIT](LICENSE) — Copyright (c) 2026 Diogo Carneiro.

## Disclaimer

Provided "as is", without warranty. You are responsible for ensuring you have the
right to access and process any database you point this tool at, and for complying
with the laws and licenses that apply to you.
