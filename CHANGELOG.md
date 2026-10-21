# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
