# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Stage an old panel database as ``xui_migrate`` for XUI.one's built-in migration.

XUI.one's *Database Migration* page reads a previous Xtream UI / ZapX /
StreamCreed / Xtream Codes v2 database from a schema named ``xui_migrate`` on the
XUI server and converts it itself. This module prepares that schema from a
``mysqldump`` file:

* the dump's own ``CREATE DATABASE`` / ``USE`` / ``DROP DATABASE`` directives are
  removed, so every table lands in ``xui_migrate`` and nothing else on the server
  (in particular the live ``xui`` database) can be touched;
* row data of log tables is skipped by default — XUI does not migrate logs, and
  they are usually the bulk of the dump;
* the session ``sql_mode`` is relaxed the way ``mysqldump`` expects, so old MySQL
  5.x dumps (zero dates, ``id = 0`` rows) load on XUI's MariaDB.

It can load live over a MySQL connection, or write a ready-to-run ``.sql`` script
for ``mysql < xui_migrate.sql`` on the server when MySQL is not reachable
remotely. Credentials are copied verbatim, as in the rest of the tool.
"""

from __future__ import annotations

import dataclasses
import gzip
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .dump_source import _CREATE_RE, _INSERT_RE, _iter_statements, read_sql_text
from .logger import get_logger
from .mysql_source import _quote_ident

log = get_logger(__name__)

MIGRATE_DB = "xui_migrate"

# Tables of the Xtream Codes v2 family whose rows XUI ignores during migration.
LOG_TABLES = frozenset({
    "client_logs", "stream_logs", "panel_logs", "user_activity", "user_activity_now",
    "mag_logs", "login_logs", "login_flood", "reg_userlog", "epg_logs",
})

# Tables that should exist in any Xtream Codes v2-family database.
REQUIRED_TABLES = ("users", "streams")
# Tables reported after loading so the user can sanity-check the content.
KEY_TABLES = (
    "users", "reg_users", "member_groups", "packages", "streams", "stream_categories",
    "series", "series_episodes", "bouquets", "streaming_servers", "mag_devices",
    "enigma2_devices",
)

SESSION_PREAMBLE = (
    "SET SESSION sql_mode = 'NO_AUTO_VALUE_ON_ZERO,NO_ENGINE_SUBSTITUTION'",
    "SET SESSION unique_checks = 0",
)

_DB_DIRECTIVE = re.compile(r"^(CREATE\s+(DATABASE|SCHEMA)|DROP\s+(DATABASE|SCHEMA)|USE)\b", re.I)
_DB_NAME = re.compile(r"(?:DATABASE|SCHEMA|USE)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?`?([^`\s;]+)`?",
                      re.I)
_LOCKS = re.compile(r"^(LOCK\s+TABLES|UNLOCK\s+TABLES)\b", re.I)


class XuiMigrateError(Exception):
    """The dump cannot be staged for XUI migration."""


@dataclass
class PreparedDump:
    """A dump rewritten for ``xui_migrate``, plus what was changed and why."""

    statements: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    source_databases: list[str] = field(default_factory=list)
    removed_directives: int = 0
    skipped_inserts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        lines = [f"Tables in dump : {len(self.tables)}",
                 f"Statements     : {len(self.statements)}"]
        if self.source_databases:
            lines.append(f"Original DB    : {', '.join(self.source_databases)} "
                         f"(redirected to `{MIGRATE_DB}`)")
        if self.skipped_inserts:
            skipped = ", ".join(f"{t} ({n})" for t, n in sorted(self.skipped_inserts.items()))
            lines.append(f"Log data skipped (INSERT statements): {skipped}")
        lines.extend(f"WARNING: {w}" for w in self.warnings)
        return lines


def prepare_dump(text: str, *, skip_logs: bool = True, force: bool = False) -> PreparedDump:
    """Rewrite dump ``text`` so it loads cleanly into the ``xui_migrate`` schema.

    Raises :class:`XuiMigrateError` when the dump is clearly not an Xtream Codes
    v2-family database (e.g. it is already an XUI.one dump), unless ``force``.
    """
    out = PreparedDump()
    seen: set[str] = set()
    for stmt in _iter_statements(text):
        s = stmt.strip()
        if not s:
            continue
        if s[:9].upper() == "DELIMITER":
            raise XuiMigrateError(
                "The dump contains stored routines/triggers (DELIMITER). Re-export it "
                "without --routines/--triggers; XUI does not need them."
            )
        if _DB_DIRECTIVE.match(s):
            m = _DB_NAME.search(s)
            if m and m.group(1) not in out.source_databases:
                out.source_databases.append(m.group(1))
            out.removed_directives += 1
            continue
        if _LOCKS.match(s):
            continue
        head = s[:12].upper()
        if head.startswith("CREATE TABLE"):
            m = _CREATE_RE.match(s)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                out.tables.append(m.group(1))
        elif skip_logs and head.startswith("INSERT"):
            m = _INSERT_RE.match(s)
            if m and m.group(1).lower() in LOG_TABLES:
                out.skipped_inserts[m.group(1)] = out.skipped_inserts.get(m.group(1), 0) + 1
                continue
        out.statements.append(s)

    present = {t.lower() for t in out.tables}
    if not out.tables:
        raise XuiMigrateError("No CREATE TABLE statements found — is this a mysqldump file?")
    problems: list[str] = []
    if "lines" in present and "users" not in present:
        problems.append("This looks like an XUI.one database (`lines` table, no `users`). "
                        "xui_migrate expects an Xtream UI / ZapX / StreamCreed / XC v2 dump; "
                        "use `restore` or `migrate` for XUI-to-XUI copies.")
    missing = [t for t in REQUIRED_TABLES if t not in present]
    if missing:
        problems.append(f"Missing core Xtream Codes tables: {', '.join(missing)}.")
    if problems and not force:
        raise XuiMigrateError(" ".join(problems) + " (use force to load it anyway)")
    out.warnings.extend(problems)
    return out


def load_dump(dump_path: str | Path, **kwargs) -> PreparedDump:
    path = Path(dump_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    return prepare_dump(read_sql_text(path), **kwargs)


def _parse_grantee(grant_to: str) -> tuple[str, str]:
    user, _, host = grant_to.partition("@")
    if not user:
        raise XuiMigrateError(f"Invalid grant user {grant_to!r}; expected user@host.")
    return user, host or "localhost"


def write_script(
    prepared: PreparedDump,
    out_path: str | Path,
    *,
    database: str = MIGRATE_DB,
    drop_existing: bool = True,
    grant_to: str | None = None,
) -> Path:
    """Write a self-contained script: ``mysql -u root -p < out_path`` on the server.

    A ``.gz`` ``out_path`` is gzip-compressed (run it with ``gunzip -c f | mysql``).
    """
    db = _quote_ident(database)
    out_path = Path(out_path)
    if out_path.suffix == ".gz":
        opener = gzip.open(out_path, "wt", encoding="utf-8", newline="\n")
    else:
        opener = out_path.open("w", encoding="utf-8", newline="\n")
    with opener as fh:
        fh.write(f"-- Generated by XUI DB Tool: stage an old panel DB as {db} for XUI.one.\n")
        fh.write(f"-- Run on the XUI server:  mysql -u root -p < {out_path.name}\n\n")
        if drop_existing:
            fh.write(f"DROP DATABASE IF EXISTS {db};\n")
        fh.write(f"CREATE DATABASE IF NOT EXISTS {db} CHARACTER SET utf8mb4;\n")
        fh.write(f"USE {db};\n")
        for s in (*SESSION_PREAMBLE, "SET FOREIGN_KEY_CHECKS = 0"):
            fh.write(s + ";\n")
        for s in prepared.statements:
            fh.write(s + ";\n")
        fh.write("SET FOREIGN_KEY_CHECKS = 1;\n")
        if grant_to:
            user, host = _parse_grantee(grant_to)
            esc = lambda v: v.replace("\\", "\\\\").replace("'", "\\'")  # noqa: E731
            fh.write(f"GRANT ALL PRIVILEGES ON {db}.* TO '{esc(user)}'@'{esc(host)}';\n")
            fh.write("FLUSH PRIVILEGES;\n")
    log.info("Wrote xui_migrate script %s (%d statements)", out_path, len(prepared.statements))
    return out_path


def stage_live(
    target_config,
    prepared: PreparedDump,
    *,
    database: str = MIGRATE_DB,
    drop_existing: bool = False,
    grant_to: str | None = None,
    progress=None,
) -> dict:
    """Load ``prepared`` into ``database`` on the target server and verify it.

    Refuses to overwrite a non-empty ``database`` unless ``drop_existing``.
    Returns per-table row counts of :data:`KEY_TABLES` and whether the server
    looks like an XUI.one host (has an ``xui`` schema).
    """
    from .mysql_source import MySQLSource  # local import keeps core import-light

    cfg = dataclasses.replace(target_config, database="")
    with MySQLSource(cfg) as target:
        existing = target.list_databases()
        if database in existing and not drop_existing:
            rows = target.execute(
                "SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s",
                (database,),
            )
            if rows and int(rows[0]["n"]) > 0:
                raise XuiMigrateError(
                    f"`{database}` already exists and has tables. Enable 'drop existing' "
                    "to replace it."
                )
        target.create_database(database, drop_existing=drop_existing)
        count = target.execute_script([*SESSION_PREAMBLE, *prepared.statements], progress)

        loaded = {t.lower(): t for t in target.list_tables()}
        counts = {t: target.row_count(loaded[t]) for t in KEY_TABLES if t in loaded}
        if grant_to:
            user, host = _parse_grantee(grant_to)
            target.execute(f"GRANT ALL PRIVILEGES ON {_quote_ident(database)}.* TO %s@%s",
                           (user, host))
            target.execute("FLUSH PRIVILEGES")

    log.info("Staged %d statements into %s on %s", count, database, cfg.host)
    return {
        "database": database,
        "statements": count,
        "tables": len(loaded),
        "row_counts": counts,
        "xui_server": "xui" in existing,
        "granted": grant_to,
    }


def stage_via_ssh(
    ssh_config,
    prepared: PreparedDump,
    *,
    mysql_user: str = "",
    mysql_password: str = "",
    database: str = MIGRATE_DB,
    drop_existing: bool = False,
    grant_to: str | None = None,
    progress=None,
    message=None,
) -> dict:
    """Load ``prepared`` by uploading a script over SSH and running the server's ``mysql``.

    This works when MySQL only accepts local connections (the XUI.one default).
    Blank ``mysql_user`` uses the server's default login (``root`` via unix socket).
    Returns the same shape as :func:`stage_live`.
    """
    from .ssh_remote import SshError, SshSession

    say = message or (lambda _m: None)
    db_lit = "'" + database.replace("\\", "\\\\").replace("'", "\'") + "'"
    with SshSession(ssh_config) as ssh:
        ssh.set_mysql_credentials(mysql_user, mysql_password)
        try:
            dbs = {r[0] for r in ssh.mysql_query("SHOW DATABASES")}
        except SshError as exc:
            raise XuiMigrateError(f"Cannot run mysql on the server: {exc}") from exc
        if database in dbs and not drop_existing:
            rows = ssh.mysql_query("SELECT COUNT(*) FROM information_schema.TABLES "
                                   f"WHERE TABLE_SCHEMA = {db_lit}")
            if rows and int(rows[0][0]) > 0:
                raise XuiMigrateError(
                    f"`{database}` already exists and has tables. Enable 'drop existing' "
                    "to replace it."
                )

        with tempfile.TemporaryDirectory(prefix="xdbtool-") as tmp:
            local = write_script(prepared, Path(tmp) / "xui_migrate.sql.gz", database=database,
                                 drop_existing=drop_existing, grant_to=grant_to)
            remote = ssh.temp_path(".sql.gz")
            say(f"Uploading script ({local.stat().st_size // 1024} KiB)…")
            ssh.upload(local, remote, progress)
        say("Running the script with the server's mysql client…")
        code, out, err = ssh.run(f"gunzip -c {remote} | {ssh.mysql_cmd()}", check=False)
        if code != 0:
            raise XuiMigrateError(f"mysql failed on the server: {err.strip() or out.strip()}")

        loaded = {r[0].lower(): r[0] for r in ssh.mysql_query(
            f"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = {db_lit}")}
        present = [t for t in KEY_TABLES if t in loaded]
        counts: dict[str, int] = {}
        if present:
            q = " UNION ALL ".join(
                f"SELECT '{t}', COUNT(*) FROM {_quote_ident(database)}.{_quote_ident(loaded[t])}"
                for t in present
            )
            counts = {r[0]: int(r[1]) for r in ssh.mysql_query(q)}

    log.info("Staged %s over SSH on %s", database, ssh_config.host)
    return {
        "database": database,
        "statements": len(prepared.statements),
        "tables": len(loaded),
        "row_counts": counts,
        "xui_server": "xui" in dbs,
        "granted": grant_to,
    }
