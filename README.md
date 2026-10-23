# XUI DB Tool

[![CI](https://github.com/diogocarneiro/XUI_DATABASE/actions/workflows/ci.yml/badge.svg)](https://github.com/diogocarneiro/XUI_DATABASE/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/diogocarneiro/XUI_DATABASE?include_prereleases&sort=semver)](https://github.com/diogocarneiro/XUI_DATABASE/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

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
- **Data viewer** — browse any table's rows in a separate window: search (all
  columns or one), sort by clicking a header, page through millions of rows (done in
  SQL on live servers), full row details with readable JSON, copy cells;
  passwords/tokens/keys masked until you ask. CLI: `view <table>`.
- **Edit data** — on a live server, change cells, add, duplicate and delete rows,
  set NULL, or import rows from CSV/JSON; changes are highlighted until saved in one
  transaction, rows changed meanwhile by the panel are never overwritten, and an undo
  script with the previous values is written. CLI: `edit <table>`.
- **Integrity check** — finds rows pointing at deleted streams, categories, bouquets,
  servers or lines (bouquet and line id lists included), repeated ids, duplicate
  usernames/MACs and streams without a server; fixes them in one transaction after
  backing up the affected tables, or writes the fix as a `.sql` script (dumps too).
- **Renumber stream ids** — after old channels are deleted, compacts `streams.id`
  to 1..N again (keeping the order, or live channels first) and updates every
  reference: servers, episodes, bouquets, MAG favourites, logs. Backup + one
  transaction, or a `.sql` script; plus a server script that renames movie files and
  TV archive folders named by id.
- **Migration** — column-level mapping between a source and a target database;
  transfers only columns present on both sides, so target auto-increment/defaults
  are preserved. Can execute live or emit a `.sql` script (dry run).
- **Schema templates** — register a panel's base (vanilla) schema once, then
  **recreate a fresh, clean database** for that panel on any target server. Base
  `.sql` files stay local; only name-only *signatures* are shipped, and they also
  fingerprint an unknown database to its closest known template/version.
  **Clean template** turns one made from a real panel's database into a clean install
  (same schema, configuration rows only).
- **Rebuild from a dump** — create a new database from a template (the clean schema
  of the panel version you want) and copy into it only the tables you pick from a
  dump, matching columns by name; live in one transaction or as a `.sql` script.
- **XUI.one migration** — stage an old Xtream UI / ZapX / StreamCreed / XC v2 dump
  as `xui_migrate` for XUI's own *Database Migration* page, or import an XUI.one
  backup into another XUI server choosing what to restore (channels, movies,
  series, lines, resellers, EPG…), with automatic backup and a rescue login.
- **SSH everywhere** — reach panel servers whose MySQL only accepts local logins,
  through an SSH tunnel or the server's MySQL socket (root without password).
- **Safety** — refuses to `DROP` a database holding panel data unless confirmed.
- **GUI** (PySide6) **and** a scriptable **CLI**; **Português** (default),
  English, Français, Español, Italiano; light / dark / automatic theme;
  in-app updates from GitHub Releases; a built-in **user guide** (*Ajuda → Guia
  de utilização*, F1) in every language.

**Developer:** Diogo Carneiro — Telegram [t.me/diogoncarneiro](https://t.me/diogoncarneiro)
· [issues & suggestions](https://github.com/diogocarneiro/XUI_DATABASE/issues).

## Install

Every [release](https://github.com/diogocarneiro/XUI_DATABASE/releases/latest)
publishes one format per audience, all listed in `SHA256SUMS.txt`:

| File | For | How |
|---|---|---|
| `XUI-DB-Tool-<v>-windows.zip` | desktop app (Windows) | unzip, run `XUI-DB-Tool.exe` |
| `xui-db-tool-<v>-linux-x86_64` | command line on Linux servers (Ubuntu 20.04+) | `chmod +x` and run |
| `xui_db_tool-<v>-py3-none-any.whl` | any OS with Python 3.10+ | `pip install <file>` (CLI) or `pip install "<file>[gui]"` |

```bash
# Linux server, e.g. the XUI host itself
curl -LO https://github.com/diogocarneiro/XUI_DATABASE/releases/latest/download/SHA256SUMS.txt
curl -LO https://github.com/diogocarneiro/XUI_DATABASE/releases/download/v0.10.0/xui-db-tool-0.10.0-linux-x86_64
sha256sum --check --ignore-missing SHA256SUMS.txt
chmod +x xui-db-tool-0.10.0-linux-x86_64 && ./xui-db-tool-0.10.0-linux-x86_64 --help
```

All three update themselves: *Ajuda → Procurar actualizações* in the app, or
`update` on the command line.

**From source** (any OS) — requires **Python 3.10+**:

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

# Integrity check: list broken links; write the fix as SQL, or (live) back up and apply it
python -m xdbtool check --dump backup.sql --fix-sql fix.sql
python -m xdbtool check --host 127.0.0.1 --user root --password secret     --database xui --fix --backup-dir ./backups

# Browse rows: search, sort, page (passwords masked unless --show-secrets)
python -m xdbtool view streams --dump backup.sql --search sport \
    --columns id,stream_display_name,category_id --order id --limit 50

# Edit rows on a live server (undo script in ./backups); --dry-run only prints the SQL
python -m xdbtool edit streams --host 127.0.0.1 --user root --password secret \
    --database xui --key id=300 --set stream_display_name="SPORT TV 1 HD"
python -m xdbtool edit streams --host 127.0.0.1 --user root --password secret \
    --database xui --import new_channels.csv

# Renumber stream ids to 1..N (preview, then SQL + file-rename scripts, or apply live)
python -m xdbtool renumber --dump backup.sql --sql-out renumber.sql --files-script files.sh
python -m xdbtool renumber --host 127.0.0.1 --user root --password secret \
    --database xui --apply --backup-dir ./backups

# Migrate from an old dump into a fresh panel DB (dry run → SQL file)
python -m xdbtool migrate --dump old_panel.sql \
    --target-host 127.0.0.1 --target-user root --target-password secret \
    --target-database new_panel --sql-out migration.sql --plan-only
```

### Connect through SSH (MySQL that only accepts local connections)

XUI.one's MariaDB refuses remote logins (error 1130). Choose **Connect via → SSH
tunnel** in the *Data source* box (or pass `--ssh-host` on the CLI): the tool opens
SSH and forwards a local port to the server's `127.0.0.1:3306`, so every tab —
tables, Export, **Maintenance** (CHECK/REPAIR/…) — works unchanged. Use the panel's
MySQL user from `/home/xui/config/config.ini`; SSH settings are saved in profiles
(passwords only with *Save password*).

```bash
python -m xdbtool maint --host 127.0.0.1 --user XUI_DB_USER --password XUI_DB_PASS \
    --database xui --ssh-host 192.168.1.106 --ssh-password secret --operation auto
```

**Root without a password?** If `mysql -uroot` works on the server, root uses
MariaDB's *unix_socket* authentication, which only works through the local socket
— never over TCP. Choose **SSH + server MySQL socket** (CLI `--ssh-mysql-socket`):
each connection is relayed to MySQL's socket (auto-detected) by a small Python
bridge run on the server as the SSH user, so it logs in exactly like `mysql -uroot`
there. Leave the MySQL password blank.

The same *Connect via* choice is on **Templates / New DB** and **Migrate / Restore**
(CLI: `--ssh-*` on `init-db`/`restore`, `--target-ssh-*` on `migrate`/`rebuild`).

**Drop guard:** `--drop` / "DROP database first" refuses to drop a database that
holds panel data (`lines`, `users`, `streams`, …) — e.g. the live `xui` — and
shows its row counts. The GUI then asks you to type the database name; the CLI
needs `--allow-drop-panel`. Use a new name (e.g. `xui_test`) for fresh databases.

### Templates — recreate a fresh panel database

```bash
# Register a base (vanilla) schema once; the .sql is copied into ./templates (local)
python -m xdbtool templates --register path/to/base.sql \
    --panel xui_one --version 1.5.13 --name "XUI.one 1.5.13"
python -m xdbtool templates                      # list registered templates

# Clean a template registered from a real panel's database (streams, lines, logs…):
# same schema, config rows only; registered as a new template (default: KEY-clean)
python -m xdbtool templates --rows xui-1.5.5              # tables with rows + size
python -m xdbtool templates --clean xui-1.5.5 --keep-rows streaming_servers

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

### Rebuild — new database from a template + chosen tables of a dump

The new database gets the template's clean schema and the **data** of the tables you
choose. Columns are matched by name (dump-only columns are dropped, new template columns
get their defaults); tables you do not tick stay empty. The template's own rows are used
only in tables the dump does not have (`--template-rows missing`, the default — never a
template's lines/streams/logs), in every unfilled table (`all`) or nowhere (`none`); the
plan shows their size and warns when a template is a real panel's database rather than a
clean install. In the GUI: load the dump, tick the tables, then **Templates / New DB →
Rebuild from the loaded source**.

```bash
# See what would happen (template defaults to the closest registered one)
python -m xdbtool rebuild --dump old_backup.sql --exclude lines_logs panel_logs --plan-only

# Create it on a server (rows copied in one transaction; refuses a DB that has tables)
python -m xdbtool rebuild --dump old_backup.sql --template xui_one-1.5.13 \
    --tables lines users streams streams_categories bouquets \
    --target-host 127.0.0.1 --target-user root --target-password secret \
    --target-database xui_new

# ...or save it as a local file: with --target-database the file creates that DB
# (gunzip -c xui_new.sql.gz | mysql -u root -p) ...
python -m xdbtool rebuild --dump old_backup.sql --sql-out xui_new.sql.gz \
    --target-database xui_new
# ... without it, a portable dump: import into any empty DB (phpMyAdmin, `restore`,
# mysql -u root -p DATABASE < new_panel.sql) or open it again with --dump
python -m xdbtool rebuild --dump old_backup.sql --sql-out new_panel.sql
```

In the GUI, **Save as local .sql file…** does the same — no server needed; leave
*New database* blank for the portable dump.

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

### XUI.one → XUI.one — import a backup into another XUI server

XUI's migration page refuses XUI backups ("Can't migrate from XUI to XUI"), and
restoring a backup from another version or fork as-is would replace the target's
schema. `xui-import` instead works over SSH against the live `xui` database:

- copies only the tables/columns both sides have (fork-only ones are listed and skipped);
- **keeps the target's `servers`, `settings` and `access_codes`** (its IP, settings
  and admin URL) — bring any of them over individually with `--import-access-codes`,
  `--import-settings`, `--import-servers`; skips logs unless `--include-logs`;
- **choose what to restore** with `--content` (default: everything): `live`,
  `movies`, `series`, `radio`, `bouquets`, `lines`, `resellers`, `epg`, `security`,
  `other`. Content not chosen is left exactly as it is. Picking only some stream
  kinds replaces just those rows (`streams.type`, their categories, server links
  and options) and keeps the target's other streams — ids that would collide with
  kept rows are detected before anything changes. Bouquets imported without some
  stream kinds get those lists emptied;
- creates a **rescue access** at the end (new admin access code + `rescue_xxxx`
  admin user with a random password, hashed as SHA-512 crypt like XUI does) and
  prints every access code's panel URL; runs XUI's `/home/xui/tools access` so
  nginx serves the codes (`--no-rescue`, `--no-refresh-codes` to skip);
- `--servers main` (default) moves every stream to the target's main server;
  `--servers import` adds the backup's load balancers as extra servers instead;
- takes a `mysqldump` of the current `xui` to `/root/xui-before-import-*.sql.gz`
  first, and replaces the data inside one transaction.

```bash
python -m xdbtool xui-import backup.sql --ssh-host 192.168.1.106 --ssh-password secret --plan-only
python -m xdbtool xui-import backup.sql --ssh-host 192.168.1.106 --ssh-password secret
# only movies and series, leaving channels, lines, bouquets… untouched
python -m xdbtool xui-import backup.sql --ssh-host 192.168.1.106 --ssh-password secret \
    --content movies,series
```

Admin/reseller accounts (`users`) come from the backup, so log in with the old
panel's admin credentials afterwards. Restart XUI on the server so it reloads its
caches. The same is in the GUI **XUI Migration** tab (*Import an XUI.one backup*).

## Themes

Menu *Tema / Theme*: **Automatic** (default — follows the operating system's
light/dark setting live), **Light** or **Dark**. The choice is remembered. Main
actions are blue; actions that write to a server (import, restore, migrate,
repair) are red. Theme colours live in `xdbtool/gui/theme.py` (`TOKENS`) and are
checked for WCAG contrast by the tests.

## Languages

The interface is available in **Português** (default), **English**, **Français**,
**Español** and **Italiano** — switch in the GUI menu *Idioma / Language*; the
choice is remembered. The CLI prints English (stable for scripts and logs) unless
`--lang pt|en|fr|es|it` is given:

```bash
python -m xdbtool --lang pt xui-import backup.sql --ssh-host 192.168.1.106 --plan-only
```

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
    rebuild.py      # new DB from a template + chosen tables of a dump
    xui_migrate.py  # stage an old panel dump as `xui_migrate` for XUI.one
    ssh_remote.py   # SSH/SFTP helper: run the server's mysql client remotely
    xui_import.py   # XUI.one → XUI.one import (schema-aware, keeps system tables)
    config.py       # connection profiles (user config dir)
  gui/            # PySide6 interface (theme.py: light/dark/auto tokens + stylesheet)
  i18n/           # translations: _() helper, locales/{pt,fr,es,it}.json, extract.py
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
# from the repository root (so --collect-data finds the translations):
python -m PyInstaller --noconfirm --windowed --name XUI-DB-Tool \
    --collect-data xdbtool --paths . packaging/xui_db_tool.py
# result in dist/XUI-DB-Tool/
```

Tagged releases build this automatically (see *Releases* in CONTRIBUTING.md).

## Updates

*Ajuda → Procurar actualizações…* (and, by default, a silent check at startup)
compares the installed version with the latest GitHub Release and shows its
notes. **Update now** then:

- **Windows app** — downloads `XUI-DB-Tool-<version>-windows.zip`, verifies it
  against the release's `SHA256SUMS.txt` (a mismatch is refused), and restarts
  into the new version once the app has closed;
- **pip install** — installs the release's source with `pip install --upgrade`;
- **git checkout** — nothing is changed; use `git pull`.

CLI: `python -m xdbtool update --check` (report only) or `update` (install).
No account or token is needed (the repository is public). Behind a shared IP
that hits GitHub's hourly API limit, an optional token can be given with
`XDBTOOL_GITHUB_TOKEN` or `update --token`.

## License

[MIT](LICENSE) — Copyright (c) 2026 Diogo Carneiro.

## Disclaimer

Provided "as is", without warranty. You are responsible for ensuring you have the
right to access and process any database you point this tool at, and for complying
with the laws and licenses that apply to you.
