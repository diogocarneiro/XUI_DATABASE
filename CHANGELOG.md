# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.10.0] - 2026-10-24
### Added
- **Clean template** — *Templates / Nova BD → Limpar template…*: makes a clean copy of a
  template registered from a real panel's database. The dialog lists every table with
  rows and its size; tables with a panel's data (streams, lines, accounts, logs, server
  addresses, block lists…) come ticked for removal, configuration tables (settings,
  groups, stream types…) keep their rows. The copy keeps the whole schema, emptied
  tables start their ids at 1 again, and it is registered as a new template
  (`KEY-clean`); the original is not changed. `xui-1.5.5`: 174 MB → 61 KB.
- CLI: `templates --rows KEY` lists the tables with rows; `templates --clean KEY`
  (`--keep-rows`, `--drop-rows`, `--key`, `--name`) makes the clean copy.
- User guide: *Templates / Nova BD* gains *Limpar um template*.

### Fixed
- *Criar base de dados a partir do template* (`init-db`) failed on MySQL 5.7+/8 with
  templates that use zero-date defaults (`Invalid default value`): the session now uses
  the relaxed `sql_mode` mysqldump expects, as the rebuild already did.
- `init-db` ignored the template's own `CREATE DATABASE` / `USE` lines, which could send
  the tables to another database.

## [0.9.0] - 2026-10-22
### Fixed
- **Rebuild: tables you did not tick no longer bring the template's rows.** A template
  registered from a real panel's database (e.g. `xui-1.5.5`, 174 MB) filled every
  unticked table with that panel's streams, devices and logs, so a 16 MB dump became a
  166 MB database. Now an unticked table stays empty, and a ticked table that is empty
  in the dump stays empty too. Same 7 tables from `xui-1.5.5`: 174.5 MB → 7.4 MB.

### Added
- *Linhas do próprio template* (Templates / Nova BD; CLI `rebuild --template-rows`):
  *Só nas tabelas que a origem não tem* (`missing`, default — fills only tables new in
  the template's version, never a template's lines/streams/logs), *Em todas as tabelas
  que não vêm da origem* (`all`, the previous behaviour) or *Nenhuma* (`none`, schema
  only).
- The rebuild preview shows the size of the template rows kept and left out, and warns
  when the template is not a clean installation (holds a panel's data).
- User guide: *Reconstruir uma base de dados a partir de um dump* explains the choice.

## [0.8.0] - 2026-10-22
### Added
- **Save the rebuilt database as a local file** — *Guardar como ficheiro .sql local…*
  (Templates / Nova BD) writes the new database (template schema + the ticked tables'
  rows) to a `.sql` or `.sql.gz` file on this computer, without any server. With a
  *Nova base de dados* name the file creates that database; left blank it is a portable
  dump with no database name inside, to import into any empty database (phpMyAdmin,
  *Migrar / Restaurar*, `mysql -u root -p DATABASE < file`) or to open again here as a
  dump source. The file dialog starts in the export folder.
- CLI `rebuild --sql-out FILE` no longer needs `--target-database`: without it the file
  is portable. `--plan-only` with `--target-host` needs no name either.
- User guide: *Reconstruir uma base de dados a partir de um dump* explains the local file.

### Changed
- *Guardar script de reconstrução…* is now *Guardar como ficheiro .sql local…*; the hint
  for `.sql.gz` files says `gunzip -c file | mysql …` (mysql cannot read gzip itself).

## [0.7.0] - 2026-10-22
### Added
- **Rebuild from a dump** — *Templates / Nova BD → Reconstruir a partir da origem
  carregada*: creates a new database from a registered template (the clean schema of
  the panel version wanted, with its default rows) and copies into it the rows of the
  tables ticked in the list. Columns are matched by name: dump-only columns are dropped,
  new template columns get their defaults. Ticked tables replace the template's default
  rows (an empty one keeps them); unticked tables keep them; tables the template lacks
  are skipped. *Pré-visualizar reconstrução* lists all of this in the log.
- *Reconstruir → criar a nova base de dados* loads it on the target server (also over
  SSH); *Guardar script de reconstrução…* writes a `.sql`/`.sql.gz` to run with
  `mysql -u root -p < file`.
- The closest registered template is pre-selected when a source is loaded.
- CLI `rebuild`: `--template` (default: closest), `--tables`, `--exclude`,
  `--plan-only`, `--sql-out`, `--target-*`, `--drop`, `--allow-drop-panel`.
- User guide: *Templates / Nova BD* gains the *Reconstruir uma base de dados a partir
  de um dump* steps.

### Fixed
- Dumps whose `INSERT`s name only some columns, or list them in another order
  (phpMyAdmin, XC_VM — e.g. `servers`), were read with the values under the wrong
  columns (`col_0`, `col_1`…). Each row now follows its own `INSERT` column list, in
  the viewer, export, migration and integrity check too.

### Safety
- The rows are copied in one transaction (none is kept if one fails); a database that
  already has tables is refused unless *DROP* is ticked, and dropping one with panel
  data still asks for its name. Password hashes are copied verbatim.

## [0.6.0] - 2026-10-21
### Added
- **Edit data** — *Modo de edição* in the data viewer (live MySQL, tables with a
  primary key): change cells, *Adicionar linha*, *Duplicar* (copy rows as new ones),
  *Apagar / repor*, *Pôr NULL* and *Importar CSV/JSON…* (header-line CSV with comma,
  semicolon or tab; JSON list of objects; columns matched by name). Edited cells are
  yellow, new rows green, deleted rows red until *Guardar alterações…*, which shows
  the SQL first and saves everything in one transaction.
- An undo script with the exact previous values (`edit-TABLE-DATE.undo.sql`) is
  written to the export folder after every save.
- CLI `edit <table>`: `--key COL=VAL` with `--set`/`--null` or `--delete`, `--insert`,
  `--import FILE`, `--dry-run`, `--backup-dir`.
- User guide: *Ver e editar dados* section rewritten with the editing steps.

### Safety
- Every changed or deleted row is locked and re-checked against the values that were
  loaded; if the panel changed it meanwhile, nothing is saved.
- Primary keys are not editable; numbers, NOT NULL and JSON lists (bouquet channels,
  categories) are validated as you type; replacing a password hash with plain text
  asks first (CLI: `--force`).
- Unsaved changes are never lost silently: changing page, closing the window or
  connecting to another source asks first.

## [0.5.0] - 2026-10-21
### Added
- **Data viewer** — double-click a table (or *Ver dados…*) to browse its rows in a
  separate window; several can be open at once. Search in every column or one
  (ignoring case), sort by clicking a column header, pages of 50–1000 rows, full row
  details with JSON lists laid out readably, Ctrl+C copies the selected cells as
  tab-separated text. On live databases search, sorting and paging run in SQL, so
  tables with millions of rows open instantly; dumps work too.
- Passwords, hashes, tokens and keys are masked (••••••) until *Mostrar
  passwords/chaves* is ticked.
- CLI `view <table>`: `--search`, `--column`, `--columns`, `--order`/`--desc`,
  `--limit`/`--offset`, `--json`; secrets masked unless `--show-secrets`.

## [0.4.0] - 2026-10-21
### Added
- **Renumber stream ids** — new *Renumerar IDs* tab and `renumber` command. After
  old channels are deleted, gives every stream (channels, movies, radios, episodes)
  consecutive ids again from 1 (or any start), keeping the current order or putting
  live channels first, and updates every reference: `streams_servers`, episodes,
  options, bouquet lists, MAG favourites and last channel, and every `stream_id`
  column (activity logs, queue…). XUI.one and Xtream-UI / StreamCreed.
- *Preview* shows the old/new id range and the rows changed per table. *Renumber
  now* (`--apply`, live MySQL; type the database name to confirm) backs up every
  affected table as SQL, then runs in one transaction and resets `AUTO_INCREMENT`;
  *Save SQL script…* (`--sql-out`) does the same as a script.
- *Save file-rename script…* (`--files-script`): a bash script for the server that
  renames movie/episode files and TV archive folders named by stream id.

### Safety
- Refuses to renumber while bouquets, servers or episodes still point to deleted
  streams whose ids would be reused (run the Integrity fixes first); such
  references in logs are cleared and removed from MAG favourites, so nothing ends
  up pointing at a different stream.
- Ids move through a temporary offset, so unique keys never clash mid-update.

## [0.3.0] - 2026-10-21
### Added
- **Integrity check** — new *Integridade* tab and `check` command. Finds rows that
  point to ids that no longer exist (streams of a deleted category, bouquets listing
  deleted streams, lines with deleted bouquets, devices of deleted lines, episodes of
  a deleted series, streams on a deleted server…), the same id repeated in a bouquet
  or line list, invalid id lists, duplicate usernames/MACs/access codes, streams
  assigned to no server and episodes linked to no series — for XUI.one and for
  Xtream-UI / StreamCreed.
- Fixes: delete the orphan rows, clear the reference, or remove the missing/repeated
  ids from the list. *Save fix script…* (`--fix-sql`) writes them as one transaction;
  *Apply selected fixes* (`--fix`, live MySQL) first backs up the affected tables as
  SQL, then applies everything in one transaction and checks again. A problem whose
  referenced table is empty (partial dump) is reported but never fixed automatically.

## [0.2.1] - 2026-10-19
### Added
- **User guide** — *Ajuda → Guia de utilização* (F1): how every tab and feature
  works, SSH modes, XUI migration and import step by step, and fixes for common
  errors (1130, 1045, "Can't migrate from XUI to XUI"), in all five languages.
- **Developer contacts** — *Ajuda → Contactar o programador (Telegram)*
  (<https://t.me/diogoncarneiro>), *Reportar um problema / sugestão (GitHub)*, and
  a new About window with clickable Telegram, email and GitHub links.

### Fixed
- Section titles such as "Origem dos dados" sat on the card border and cut into
  the header; they are now a header row inside each card.
- With *SQL dump file* selected, the Data source card kept the taller MySQL
  form's height and left a large empty gap; it now fits the visible form.

## [0.2.0] - 2026-10-19
### Downloads
- **Windows** — `XUI-DB-Tool-0.2.0-windows.zip`: the desktop app; unzip and run
  `XUI-DB-Tool.exe`.
- **Linux servers** — `xui-db-tool-0.2.0-linux-x86_64`: the command line in one
  file (no GUI), runs on Ubuntu 20.04 and newer: `chmod +x` and run it.
- **Python** — wheel / sdist: `pip install xui-db-tool-0.2.0-py3-none-any.whl`
  (CLI) or with `[gui]` for the desktop app; any OS with Python 3.10+.
- `SHA256SUMS.txt` — checksums of all of the above.

### Added
- **XUI.one migration**
  - Stage an old Xtream UI / ZapX / StreamCreed / Xtream Codes v2 dump as the
    `xui_migrate` database for XUI's own *Database Migration* page (`xui-migrate`).
  - Import an XUI.one backup into another XUI server (`xui-import`), choosing what
    to restore — live channels, movies, series, radio, bouquets, lines and devices,
    resellers, EPG, security lists — and whether to bring access codes, settings
    and servers. Only the chosen kinds are replaced; the server's own data is
    backed up first and everything runs in one transaction.
  - A **rescue access** (admin access code + admin user) is created at the end and
    every panel URL is listed, so you can always log in.
- **SSH everywhere** — every tab and CLI command can reach servers whose MySQL
  only accepts local logins: SSH tunnel to MySQL's port, or the server's MySQL
  socket (root without a password, like `mysql -uroot` on the server).
- **Templates / new databases** — register a panel's base schema and create clean
  databases from it; fingerprint unknown databases against known versions.
- **Direct restore** of `.sql` / `.sql.gz` dumps into a server.
- **Languages** — Português (default), English, Français, Español, Italiano.
- **Themes** — modern design with Light, Dark and Automatic (follows the system).
- **Updates** — *Ajuda → Procurar actualizações* (and a quiet check at start-up):
  downloads the new version, verifies its SHA-256 and installs it (Windows app,
  Linux binary, pip); CLI `update [--check]`.

### Safety
- Refuses to `DROP` a database that holds panel data (lines, users, streams…)
  unless you type its name to confirm.
- Credentials are always copied verbatim — password hashes are never decrypted.
- Connection errors 1045/1130 explain the cause and what to do.

### Changed
- `pip install xui-db-tool` installs the CLI only; the desktop GUI is the `gui`
  extra. The unused `sqlparse` dependency was removed.
- The packaged app keeps its templates in the user's config folder instead of
  the current directory.

### Fixed
- CI never ran a test before: plain `pytest` could not import the package.
- Releases are now created automatically when the version changes on `main`.

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
