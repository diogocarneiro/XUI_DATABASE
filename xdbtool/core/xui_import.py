# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Import an XUI.one backup into the live ``xui`` database of another XUI.one server.

XUI's own *Database Migration* page refuses XUI-to-XUI ("Can't migrate from XUI to
XUI"), and restoring a backup as-is would replace the target's schema with the
source's — breaking the panel when the backup comes from another version or fork.
This module instead:

* reads the target's real schema over SSH and copies only the tables and columns
  that exist on both sides (fork-only tables/columns are reported and skipped);
* keeps the target's *system* tables (``servers``, ``settings``, ``access_codes``
  …) unless asked otherwise, so the new server keeps its own IP, settings and
  admin URL;
* skips log/statistics tables by default;
* dumps the current ``xui`` database on the server before changing anything, and
  replaces table contents inside one transaction.

Credentials are copied verbatim (password hashes are never decrypted).
"""

from __future__ import annotations

import gzip
import shlex
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .dump_source import SqlDumpSource
from .exporter import _sql_literal
from .logger import get_logger
from .migrator import MigrationPlan, build_plan
from .mysql_source import _quote_ident
from .source import ColumnInfo, DataSource, TableInfo
from .xui_migrate import SESSION_PREAMBLE

log = get_logger(__name__)

XUI_DB = "xui"

# Server-specific configuration: replacing these would point the new panel at the
# old server's IP, settings and admin URL (access code).
SYSTEM_TABLES = frozenset({
    "servers", "settings", "access_codes", "crontab", "migrations", "queue", "signals",
})
LOG_TABLES = frozenset({
    "lines_logs", "lines_activity", "lines_live", "lines_divergence", "login_logs",
    "mag_logs", "mag_events", "panel_logs", "panel_stats", "streams_logs", "streams_errors",
    "streams_stats", "servers_stats", "users_logs", "detect_restream_logs", "watch_logs",
    "mysql_syslog", "syskill_log",
})
KEY_TABLES = (
    "users", "users_groups", "users_packages", "lines", "streams", "streams_categories",
    "streams_series", "streams_episodes", "bouquets", "mag_devices", "enigma2_devices",
    "epg", "providers",
)


class XuiImportError(Exception):
    """The backup cannot be imported into the target XUI database."""


class SchemaSource(DataSource):
    """A table→columns schema with no rows (the target, read over SSH)."""

    def __init__(self, schema: dict[str, list[str]], label: str = "target schema"):
        self.schema = schema
        self.label = label

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def list_tables(self) -> list[str]:
        return sorted(self.schema)

    def table_info(self, table: str) -> TableInfo:
        cols = tuple(ColumnInfo(name=c, data_type="") for c in self.schema.get(table, []))
        return TableInfo(name=table, columns=cols, row_count=None)

    def row_count(self, table: str) -> int:
        return 0

    def iter_rows(self, table: str, batch_size: int = 1000) -> Iterator[dict]:
        return iter(())


RowFilter = Callable[[Iterable[dict]], Iterator[dict]]

# Where streams run after the import:
#   "main"   - every stream is moved to the target's main server (single-server setup);
#   "import" - the backup's load-balancer servers are added to the target as extra
#              servers (they must then be reinstalled against the new main server).
SERVER_MODES = ("main", "import")


@dataclass
class ImportPlan:
    plan: MigrationPlan
    kept_target: list[str] = field(default_factory=list)    # system tables left alone
    skipped_logs: list[str] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)       # source rows per table
    append_only: set[str] = field(default_factory=set)       # add rows, do not DELETE first
    row_filters: dict[str, RowFilter] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        lines = [f"Tables to replace: {len(self.plan.transferable)}"]
        for m in self.plan.transferable:
            extra = f", {len(m.source_only)} backup-only cols dropped" if m.source_only else ""
            how = " (added to the target's rows)" if m.table in self.append_only else ""
            lines.append(f"  {m.table}: {self.rows.get(m.table, 0)} rows{extra}{how}")
        lines.extend(self.notes)
        if self.kept_target:
            lines.append(f"Kept from the target (system): {', '.join(self.kept_target)}")
        if self.skipped_logs:
            lines.append(f"Logs/stats skipped: {', '.join(self.skipped_logs)}")
        if self.plan.missing_in_target:
            lines.append("Not in target XUI (skipped): " + ", ".join(self.plan.missing_in_target))
        return lines


def check_source(source: DataSource) -> None:
    tables = {t.lower() for t in source.list_tables()}
    if "lines" not in tables:
        raise XuiImportError(
            "This dump has no `lines` table, so it is not an XUI.one backup. For Xtream UI / "
            "ZapX / StreamCreed / XC v2 dumps use the xui_migrate staging instead."
        )


def _move_to_main(main_id: int) -> tuple[RowFilter, RowFilter]:
    """Row filters that re-home every stream on the target's main server."""

    def streams_servers(rows: Iterable[dict]) -> Iterator[dict]:
        seen: set[object] = set()
        for row in rows:
            if row.get("stream_id") in seen:  # one row per stream on a single server
                continue
            seen.add(row.get("stream_id"))
            yield {**row, "server_id": main_id, "parent_id": None}

    def streams(rows: Iterable[dict]) -> Iterator[dict]:
        for row in rows:
            fixed = {k: main_id for k in ("tv_archive_server_id", "vframes_server_id")
                     if row.get(k) not in (None, 0, "0", "")}
            yield {**row, **fixed} if fixed else row

    return streams_servers, streams


def build_import(
    source: DataSource,
    target_schema: dict[str, list[str]],
    *,
    include_system: bool = False,
    include_logs: bool = False,
    server_mode: str = "main",
    target_main_id: int = 1,
    target_server_ids: Iterable[int] = (1,),
) -> ImportPlan:
    """Map the backup onto the target schema, honouring the system/log exclusions."""
    check_source(source)
    if "lines" not in {t.lower() for t in target_schema}:
        raise XuiImportError("The target database has no `lines` table — is it XUI.one?")
    if server_mode not in SERVER_MODES:
        raise ValueError(f"server_mode must be one of {SERVER_MODES}")
    kept, logs, wanted = [], [], []
    for t in source.list_tables():
        low = t.lower()
        if low in SYSTEM_TABLES and not include_system:
            kept.append(t)
        elif low in LOG_TABLES and not include_logs:
            logs.append(t)
        else:
            wanted.append(t)
    src_names = {t.lower(): t for t in source.list_tables()}
    servers_imported = include_system and "servers" in src_names
    if server_mode == "import" and not servers_imported and "servers" in src_names:
        wanted.append(src_names["servers"])
        kept.remove(src_names["servers"])
    plan = build_plan(source, SchemaSource(target_schema), wanted)
    imp = ImportPlan(plan=plan, kept_target=kept, skipped_logs=logs)

    if not servers_imported and server_mode == "main":
        imp.row_filters["streams_servers"], imp.row_filters["streams"] = _move_to_main(
            target_main_id)
        imp.notes.append(f"All streams are moved to this XUI's main server (id {target_main_id}).")
    elif not servers_imported and "servers" in src_names:
        taken = set(target_server_ids)
        extra = [r for r in source.iter_rows(src_names["servers"]) if not r.get("is_main")]
        clash = sorted(r.get("id") for r in extra if r.get("id") in taken)
        if clash:
            raise XuiImportError(
                f"Load-balancer ids {clash} already exist on the target XUI; remove those "
                "servers there first, or move the streams to the main server instead."
            )
        imp.append_only.add(next(m.table for m in plan.mappings if m.table.lower() == "servers"))
        imp.row_filters["servers"] = lambda rows: (r for r in rows if not r.get("is_main"))
        names = ", ".join(str(r.get("server_name") or r.get("id")) for r in extra)
        imp.notes.append(f"Load balancers added as extra servers (reinstall them): {names}")

    for m in plan.transferable:
        f = imp.row_filters.get(m.table.lower())
        rows = source.iter_rows(src_names[m.table.lower()])
        imp.rows[m.table] = sum(1 for _ in (f(rows) if f else rows))
    return imp


def write_import_script(
    source: DataSource,
    imp: ImportPlan,
    out_path: str | Path,
    *,
    database: str = XUI_DB,
    batch_size: int = 500,
    progress=None,
) -> Path:
    """Write the import as SQL (gzip if ``out_path`` ends in ``.gz``).

    Each mapped table is emptied with ``DELETE`` and refilled, all inside a single
    transaction: if the ``mysql`` client stops on an error, nothing is committed.
    """
    out_path = Path(out_path)
    opener = gzip.open if out_path.suffix == ".gz" else open
    src_names = {t.lower(): t for t in source.list_tables()}
    with opener(out_path, "wt", encoding="utf-8", newline="\n") as fh:
        fh.write("-- Generated by XUI DB Tool: import an XUI.one backup into XUI.\n")
        fh.write(f"USE {_quote_ident(database)};\n")
        for s in (*SESSION_PREAMBLE, "SET FOREIGN_KEY_CHECKS = 0", "START TRANSACTION"):
            fh.write(s + ";\n")
        for m in imp.plan.transferable:
            table, total = _quote_ident(m.table), imp.rows.get(m.table, 0)
            if m.table not in imp.append_only:
                fh.write(f"DELETE FROM {table};\n")
            collist = ", ".join(_quote_ident(c) for c in m.columns)
            batch: list[str] = []
            done = 0
            rows = source.iter_rows(src_names[m.table.lower()])
            row_filter = imp.row_filters.get(m.table.lower())
            for row in row_filter(rows) if row_filter else rows:
                low = {k.lower(): v for k, v in row.items()}
                if not any(c.lower() in low for c in m.columns):
                    raise XuiImportError(
                        f"Rows of `{m.table}` do not match its CREATE TABLE column list in "
                        "the backup; refusing to import them as NULLs."
                    )
                batch.append("(" + ", ".join(_sql_literal(low.get(c.lower())) for c in m.columns)
                             + ")")
                if len(batch) >= batch_size:
                    fh.write(f"INSERT INTO {table} ({collist}) VALUES {','.join(batch)};\n")
                    done += len(batch)
                    batch.clear()
                    if progress:
                        progress(m.table, done, total)
            if batch:
                fh.write(f"INSERT INTO {table} ({collist}) VALUES {','.join(batch)};\n")
                done += len(batch)
            if progress:
                progress(m.table, done, total)
        fh.write("COMMIT;\nSET FOREIGN_KEY_CHECKS = 1;\n")
    log.info("Wrote XUI import script %s (%d tables)", out_path, len(imp.plan.transferable))
    return out_path


def _remote_schema(ssh, database: str) -> dict[str, list[str]]:
    lit = "'" + database.replace("\\", "\\\\").replace("'", "\\'") + "'"
    rows = ssh.mysql_query(
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA = {lit} AND EXTRA NOT LIKE '%GENERATED%' "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION"
    )
    schema: dict[str, list[str]] = {}
    for table, column in rows:
        schema.setdefault(table, []).append(column)
    return schema


def import_via_ssh(
    ssh_config,
    dump_path: str | Path,
    *,
    mysql_user: str = "",
    mysql_password: str = "",
    database: str = XUI_DB,
    include_system: bool = False,
    include_logs: bool = False,
    server_mode: str = "main",
    backup: bool = True,
    plan_only: bool = False,
    progress=None,
    message=None,
) -> dict:
    """Import an XUI.one backup into ``database`` on the server reached over SSH."""
    from .ssh_remote import SshError, SshSession

    say = message or (lambda _m: None)
    with SqlDumpSource(dump_path) as source:
        check_source(source)
        with SshSession(ssh_config) as ssh:
            ssh.set_mysql_credentials(mysql_user, mysql_password)
            try:
                schema = _remote_schema(ssh, database)
            except SshError as exc:
                raise XuiImportError(f"Cannot run mysql on the server: {exc}") from exc
            if not schema:
                raise XuiImportError(f"Database `{database}` not found or empty on the server.")
            servers = ssh.mysql_query(
                f"SELECT id, is_main FROM {_quote_ident(database)}.`servers` ORDER BY id"
            ) if "is_main" in schema.get("servers", []) else []
            ids = [int(r[0]) for r in servers]
            main_id = next((int(r[0]) for r in servers if r[1] == "1"), ids[0] if ids else 1)
            imp = build_import(source, schema, include_system=include_system,
                               include_logs=include_logs, server_mode=server_mode,
                               target_main_id=main_id, target_server_ids=ids or [main_id])
            for line in imp.summary_lines():
                say(line)
            result = {"database": database, "plan": imp, "backup": None, "row_counts": {}}
            if plan_only:
                return result

            if backup:
                path = f"/root/xui-before-import-{time.strftime('%Y%m%d-%H%M%S')}.sql.gz"
                say(f"Backing up the current `{database}` to {path} on the server…")
                dump = ssh.mysql_cmd("--single-transaction", "--routines", database,
                                     program="mysqldump")
                cmd = shlex.quote(f"{dump} | gzip > {path}")
                code, _, err = ssh.run(f"bash -o pipefail -c {cmd}", check=False)
                if code != 0:
                    raise XuiImportError(f"Backup failed, nothing was changed: {err.strip()}")
                result["backup"] = path

            with tempfile.TemporaryDirectory(prefix="xdbtool-") as tmp:
                local = write_import_script(source, imp, Path(tmp) / "xui_import.sql.gz",
                                            database=database, progress=progress)
                remote = ssh.temp_path(".sql.gz")
                say(f"Uploading import script ({local.stat().st_size // 1024} KiB)…")
                ssh.upload(local, remote, progress)
            say(f"Importing into `{database}`…")
            code, out, err = ssh.run(f"gunzip -c {remote} | {ssh.mysql_cmd()}", check=False)
            if code != 0:
                raise XuiImportError(
                    f"mysql failed on the server; the transaction was rolled back: "
                    f"{err.strip() or out.strip()}"
                )
            present = [t for t in KEY_TABLES if t in {x.lower() for x in schema}]
            if present:
                db = _quote_ident(database)
                q = " UNION ALL ".join(
                    f"SELECT '{t}', COUNT(*) FROM {db}.{_quote_ident(t)}" for t in present
                )
                result["row_counts"] = {r[0]: int(r[1]) for r in ssh.mysql_query(q)}
    log.info("Imported %s into %s on %s", Path(dump_path).name, database, ssh_config.host)
    return result
