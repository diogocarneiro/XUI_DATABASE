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
import string
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
from .passwords import random_secret, sha512_crypt
from .source import ColumnInfo, DataSource, TableInfo
from .xui_migrate import SESSION_PREAMBLE

log = get_logger(__name__)

XUI_DB = "xui"

# Server-specific configuration the user may choose to bring over, one by one.
# Replacing them gives the new panel the old admin URLs (access_codes), settings
# and server list/IPs (servers) — see :func:`build_import`.
OPTIONAL_SYSTEM_TABLES = ("access_codes", "settings", "servers")
# Runtime state of the target installation: always kept.
RUNTIME_TABLES = frozenset({"crontab", "migrations", "queue", "signals"})
SYSTEM_TABLES = frozenset(OPTIONAL_SYSTEM_TABLES) | RUNTIME_TABLES
ACCESS_CODE_TYPES = {0: "admin", 1: "reseller", 2: "ministra"}
XUI_TOOLS = "/home/xui/tools"
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


@dataclass(frozen=True)
class ContentGroup:
    """A kind of panel content the user can choose to import (or leave untouched)."""

    label: str
    tables: frozenset[str] = frozenset()        # tables owned entirely by this group
    stream_types: frozenset[int] = frozenset()  # `streams.type` values
    category_type: str = ""                     # `streams_categories.category_type`
    bouquet_column: str = ""                    # JSON id list in `bouquets`


# XUI.one keeps every stream kind in `streams`, told apart by `type` (see the
# `streams_types` table): 1 live, 2 movie, 3 created live, 4 radio, 5 series episode.
CONTENT_GROUPS: dict[str, ContentGroup] = {
    "live": ContentGroup("Live channels", frozenset({"recordings"}), frozenset({1, 3}),
                         "live", "bouquet_channels"),
    "movies": ContentGroup("Movies", stream_types=frozenset({2}), category_type="movie",
                           bouquet_column="bouquet_movies"),
    "series": ContentGroup("Series", frozenset({"streams_series", "streams_episodes"}),
                           frozenset({5}), "series", "bouquet_series"),
    "radio": ContentGroup("Radio stations", stream_types=frozenset({4}), category_type="radio",
                          bouquet_column="bouquet_radios"),
    "bouquets": ContentGroup("Bouquets", frozenset({"bouquets"})),
    "lines": ContentGroup("Lines, MAG & Enigma2 devices", frozenset({
        "lines", "mag_devices", "enigma2_devices", "mag_claims", "enigma2_actions"})),
    "resellers": ContentGroup("Admins, resellers, groups & packages", frozenset({
        "users", "users_groups", "users_packages", "users_credits_logs", "tickets",
        "tickets_replies"})),
    "epg": ContentGroup("EPG", frozenset({
        "epg", "epg_channels", "epg_data", "epg_api", "epg_languages"})),
    "security": ContentGroup("Blocked IPs/ISPs/ASNs/UAs, RTMP IPs, API keys", frozenset({
        "blocked_asns", "blocked_ips", "blocked_isps", "blocked_uas", "rtmp_ips",
        "hmac_keys"})),
    "other": ContentGroup("Everything else (providers, profiles, watch folders...)"),
}
STREAM_GROUPS = ("live", "movies", "series", "radio")
# Tables shared by all stream kinds; partially replaced when only some are chosen.
STREAM_TABLES = ("streams", "streams_servers", "streams_options", "streams_categories")
# Per-stream child tables whose own id nothing else references.
_STREAM_CHILDREN = {"streams_servers": "server_stream_id", "streams_options": "id"}


@dataclass
class ImportPlan:
    plan: MigrationPlan
    kept_target: list[str] = field(default_factory=list)    # system tables left alone
    skipped_logs: list[str] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)       # source rows per table
    append_only: set[str] = field(default_factory=set)       # add rows, do not DELETE first
    row_filters: dict[str, RowFilter] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    content: list[str] = field(default_factory=list)         # chosen CONTENT_GROUPS keys
    not_chosen: list[str] = field(default_factory=list)      # tables left as on the target
    delete_sql: dict[str, str] = field(default_factory=dict)  # partial DELETE per table
    # (table, id column, SQL condition selecting the target rows that are kept, ids
    # the import will insert) — checked against the live target before importing.
    keep_checks: list[tuple[str, str, str, set]] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        labels = ", ".join(CONTENT_GROUPS[g].label for g in self.content)
        lines = [f"Content: {labels or 'nothing'}",
                 f"Tables to replace: {len(self.plan.transferable)}"]
        for m in self.plan.transferable:
            extra = f", {len(m.source_only)} backup-only cols dropped" if m.source_only else ""
            how = " (added to the target's rows)" if m.table in self.append_only else ""
            if m.table in self.delete_sql:
                how = " (only the chosen kinds are replaced)"
            lines.append(f"  {m.table}: {self.rows.get(m.table, 0)} rows{extra}{how}")
        lines.extend(self.notes)
        if self.not_chosen:
            lines.append(f"Not chosen, left as they are: {', '.join(self.not_chosen)}")
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


def _chain(first: RowFilter | None, then: RowFilter) -> RowFilter:
    return then if first is None else (lambda rows: then(first(rows)))


def _group_of(table: str) -> str:
    for key, group in CONTENT_GROUPS.items():
        if table in group.tables:
            return key
    return "other"


def _apply_content(imp: ImportPlan, source: DataSource, chosen: set[str]) -> None:
    """Restrict the stream tables to the chosen stream kinds (partial replace)."""
    src_names = {t.lower(): t for t in source.list_tables()}
    groups = [CONTENT_GROUPS[g] for g in STREAM_GROUPS if g in chosen]
    types = sorted({t for g in groups for t in g.stream_types})
    cats = sorted({g.category_type for g in groups})
    type_list = ", ".join(str(t) for t in types)
    cat_list = ", ".join(_sql_literal(c) for c in cats)

    def keep_type(rows):
        return (r for r in rows if _int(r.get("type")) in types)

    ids = set()
    if "streams" in src_names:
        ids = {r.get("id") for r in keep_type(source.iter_rows(src_names["streams"]))}

    def by_stream(rows):
        return (r for r in rows if r.get("stream_id") in ids)

    names = {m.table.lower(): m for m in imp.plan.transferable}
    if "streams" in names:
        imp.row_filters["streams"] = _chain(keep_type, imp.row_filters.get("streams") or
                                            (lambda rows: rows))
        imp.delete_sql["streams"] = f"DELETE FROM `streams` WHERE `type` IN ({type_list})"
        imp.keep_checks.append(("streams", "id", f"`type` NOT IN ({type_list})", ids))
    for child, own_id in _STREAM_CHILDREN.items():
        m = names.get(child)
        if not m:
            continue
        imp.row_filters[child] = _chain(by_stream, imp.row_filters.get(child) or
                                        (lambda rows: rows))
        imp.delete_sql[m.table] = (
            f"DELETE x FROM {_quote_ident(m.table)} x JOIN `streams` s ON s.`id` = x.`stream_id` "
            f"WHERE s.`type` IN ({type_list})"
        )
        # Let AUTO_INCREMENT number the new rows so they cannot clash with kept ones.
        m.columns = [c for c in m.columns if c.lower() != own_id]
    m = names.get("streams_categories")
    if m:
        cat_ids = {r.get("id") for r in source.iter_rows(src_names["streams_categories"])
                   if r.get("category_type") in cats}
        imp.row_filters["streams_categories"] = (
            lambda rows: (r for r in rows if r.get("category_type") in cats))
        imp.delete_sql[m.table] = (
            f"DELETE FROM {_quote_ident(m.table)} WHERE `category_type` IN ({cat_list})")
        imp.keep_checks.append((m.table, "id", f"`category_type` NOT IN ({cat_list})", cat_ids))


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bouquet_filter(cleared: list[str]) -> RowFilter:
    def run(rows):
        for r in rows:
            yield {**r, **{c: "[]" for c in cleared if c in r}}
    return run


def build_import(
    source: DataSource,
    target_schema: dict[str, list[str]],
    *,
    import_tables: Iterable[str] = (),
    include_logs: bool = False,
    server_mode: str = "main",
    target_main_id: int = 1,
    target_server_ids: Iterable[int] = (1,),
    content: Iterable[str] | None = None,
) -> ImportPlan:
    """Map the backup onto the target schema, honouring the system/log exclusions.

    ``import_tables`` picks which of :data:`OPTIONAL_SYSTEM_TABLES` are replaced by
    the backup's rows; the others keep the target's. Importing ``servers`` replaces
    the whole server list, so streams keep their original servers and
    ``server_mode`` is ignored.

    ``content`` picks which :data:`CONTENT_GROUPS` to import (default: all). Tables
    of groups not chosen are left exactly as they are on the target. Choosing only
    some stream kinds (e.g. movies) replaces just those rows of ``streams`` and its
    child tables, keeping the target's other streams.
    """
    groups = set(CONTENT_GROUPS) if content is None else {g.lower() for g in content}
    bad = groups - set(CONTENT_GROUPS)
    if bad:
        raise ValueError(f"Unknown content {sorted(bad)}; choose from {list(CONTENT_GROUPS)}")
    stream_chosen = [g for g in STREAM_GROUPS if g in groups]
    partial_streams = 0 < len(stream_chosen) < len(STREAM_GROUPS)
    check_source(source)
    if "lines" not in {t.lower() for t in target_schema}:
        raise XuiImportError("The target database has no `lines` table — is it XUI.one?")
    if server_mode not in SERVER_MODES:
        raise ValueError(f"server_mode must be one of {SERVER_MODES}")
    chosen = {t.lower() for t in import_tables}
    unknown = chosen - set(OPTIONAL_SYSTEM_TABLES)
    if unknown:
        raise ValueError(f"Only {OPTIONAL_SYSTEM_TABLES} can be chosen, not {sorted(unknown)}")
    in_target = {t.lower() for t in target_schema}
    kept, logs, wanted, not_chosen = [], [], [], []
    for t in source.list_tables():
        low = t.lower()
        if low not in in_target:
            wanted.append(t)  # reported by build_plan as "not in target"
        elif low in SYSTEM_TABLES and low not in chosen:
            kept.append(t)
        elif low in LOG_TABLES and not include_logs:
            logs.append(t)
        elif low in SYSTEM_TABLES:
            wanted.append(t)
        elif (not stream_chosen) if low in STREAM_TABLES else (_group_of(low) not in groups):
            not_chosen.append(t)
        else:
            wanted.append(t)
    src_names = {t.lower(): t for t in source.list_tables()}
    servers_imported = "servers" in chosen and "servers" in src_names
    if server_mode == "import" and not servers_imported and "servers" in src_names:
        wanted.append(src_names["servers"])
        kept.remove(src_names["servers"])
    plan = build_plan(source, SchemaSource(target_schema), wanted)
    imp = ImportPlan(plan=plan, kept_target=kept, skipped_logs=logs, not_chosen=not_chosen,
                     content=[g for g in CONTENT_GROUPS if g in groups])

    if servers_imported:
        imp.notes.append("Servers replaced by the backup's (old server IPs; fix the main "
                         "server's IP in the panel if it changed).")
    elif server_mode == "main":
        imp.row_filters["streams_servers"], imp.row_filters["streams"] = _move_to_main(
            target_main_id)
        imp.notes.append(f"All streams are moved to this XUI's main server (id {target_main_id}).")
    elif "servers" in src_names:
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

    if partial_streams:
        _apply_content(imp, source, set(stream_chosen))
    if "bouquets" in groups and len(stream_chosen) < len(STREAM_GROUPS):
        cleared = [CONTENT_GROUPS[g].bouquet_column for g in STREAM_GROUPS
                   if g not in stream_chosen]
        imp.row_filters["bouquets"] = _bouquet_filter(cleared)
        imp.notes.append(f"Bouquets imported without {', '.join(cleared)} (content not chosen).")
    if "lines" in groups and "bouquets" not in groups:
        imp.notes.append("Note: lines keep the backup's bouquet ids; import bouquets too unless "
                         "this XUI has the same bouquets.")
    if "lines" in groups and "resellers" not in groups:
        imp.notes.append("Note: lines keep their backup owner (member_id) and package ids.")

    for m in plan.transferable:
        f = imp.row_filters.get(m.table.lower())
        rows = source.iter_rows(src_names[m.table.lower()])
        imp.rows[m.table] = sum(1 for _ in (f(rows) if f else rows))
    return imp


@dataclass
class RescueAccess:
    """A fresh admin access code plus an admin login, created after the import.

    Guarantees a way into the panel whatever was imported: the code and the user
    both point at the admin group that exists *after* the import.
    """

    code: str
    username: str
    password: str  # shown once to the operator; only its hash is written

    @classmethod
    def generate(cls) -> RescueAccess:
        return cls(
            code=random_secret(8, string.ascii_letters),
            username="rescue_" + random_secret(4, string.ascii_lowercase + string.digits),
            password=random_secret(16),
        )

    def sql(self, schema: dict[str, list[str]]) -> list[str]:
        admin_group = ("FROM `users_groups` WHERE `is_admin` = 1 "
                       "ORDER BY `group_id` LIMIT 1")
        out = [
            "INSERT INTO `access_codes` (`code`, `type`, `enabled`, `groups`, `whitelist`) "
            f"SELECT {_sql_literal(self.code)}, 0, 1, CONCAT('[', `group_id`, ']'), '[]' "
            + admin_group
        ]
        values = {
            "username": _sql_literal(self.username),
            "password": _sql_literal(sha512_crypt(self.password)),
            "email": "''", "ip": "''", "date_registered": "UNIX_TIMESTAMP()",
            "member_group_id": "`group_id`", "credits": "0", "status": "1", "owner_id": "0",
            "notes": "'Rescue admin created by XUI DB Tool'",
        }
        cols = [c for c in values if c in schema.get("users", [])]
        out.append(
            f"INSERT INTO `users` ({', '.join(_quote_ident(c) for c in cols)}) "
            f"SELECT {', '.join(values[c] for c in cols)} " + admin_group
        )
        return out


def write_import_script(
    source: DataSource,
    imp: ImportPlan,
    out_path: str | Path,
    *,
    database: str = XUI_DB,
    batch_size: int = 500,
    rescue: RescueAccess | None = None,
    target_schema: dict[str, list[str]] | None = None,
    progress=None,
) -> Path:
    """Write the import as SQL (gzip if ``out_path`` ends in ``.gz``).

    Each mapped table is emptied with ``DELETE`` and refilled, all inside a single
    transaction: if the ``mysql`` client stops on an error, nothing is committed.
    ``rescue`` adds an admin access code and admin user in the same transaction.
    """
    out_path = Path(out_path)
    opener = gzip.open if out_path.suffix == ".gz" else open
    src_names = {t.lower(): t for t in source.list_tables()}
    with opener(out_path, "wt", encoding="utf-8", newline="\n") as fh:
        fh.write("-- Generated by XUI DB Tool: import an XUI.one backup into XUI.\n")
        fh.write(f"USE {_quote_ident(database)};\n")
        for s in (*SESSION_PREAMBLE, "SET FOREIGN_KEY_CHECKS = 0", "START TRANSACTION"):
            fh.write(s + ";\n")
        # Delete phase first: partial child DELETEs join `streams`, so they must run
        # before `streams` itself loses the rows they match on.
        deletes = [imp.delete_sql.get(m.table, f"DELETE FROM {_quote_ident(m.table)}")
                   for m in imp.plan.transferable if m.table not in imp.append_only]
        for stmt in sorted(deletes, key=lambda s: " JOIN " not in s):
            fh.write(stmt + ";\n")
        for m in imp.plan.transferable:
            table, total = _quote_ident(m.table), imp.rows.get(m.table, 0)
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
        if rescue:
            for s in rescue.sql(target_schema or {}):
                fh.write(s + ";\n")
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
    import_tables: Iterable[str] = (),
    content: Iterable[str] | None = None,
    include_logs: bool = False,
    server_mode: str = "main",
    backup: bool = True,
    create_rescue: bool = True,
    refresh_codes: bool = True,
    plan_only: bool = False,
    progress=None,
    message=None,
) -> dict:
    """Import an XUI.one backup into ``database`` on the server reached over SSH.

    With ``create_rescue`` a new admin access code and admin user are added (see
    :class:`RescueAccess`). With ``refresh_codes`` XUI's own ``tools access`` is run
    so nginx serves the access codes now in the database. The result lists every
    access code with its panel URL.
    """
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
            imp = build_import(source, schema, import_tables=import_tables,
                               include_logs=include_logs, server_mode=server_mode,
                               target_main_id=main_id, target_server_ids=ids or [main_id],
                               content=content)
            for line in imp.summary_lines():
                say(line)
            _check_kept_rows(ssh, database, imp)
            rescue = RescueAccess.generate() if create_rescue else None
            if rescue and not {"access_codes", "users", "users_groups"} <= set(schema):
                raise XuiImportError("Target lacks access_codes/users/users_groups; "
                                     "cannot create the rescue access.")
            result = {"database": database, "plan": imp, "backup": None, "row_counts": {},
                      "rescue": None, "access_codes": [], "codes_refresh": None}
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
                                            database=database, rescue=rescue,
                                            target_schema=schema, progress=progress)
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
            if rescue:
                lit = _sql_literal(rescue.username)
                found = ssh.mysql_query(f"SELECT COUNT(*) FROM {_quote_ident(database)}.`users` "
                                        f"WHERE `username` = {lit}")
                if not found or found[0][0] == "0":
                    raise XuiImportError("Data imported, but no admin group (users_groups."
                                         "is_admin = 1) exists, so no rescue access was made.")
                result["rescue"] = rescue
            if refresh_codes:
                result["codes_refresh"] = _refresh_access_codes(ssh)
            port = _main_http_port(ssh, database, schema)
            base = f"http://{ssh_config.host}{'' if port in (None, 80) else f':{port}'}"
            result["panel_base"] = base
            result["access_codes"] = [
                {"code": c, "type": ACCESS_CODE_TYPES.get(int(t), f"type {t}"),
                 "enabled": e == "1", "url": f"{base}/{c}/"}
                for c, t, e in ssh.mysql_query(
                    f"SELECT `code`, `type`, `enabled` FROM {_quote_ident(database)}"
                    ".`access_codes` ORDER BY `id`")
            ]
    log.info("Imported %s into %s on %s", Path(dump_path).name, database, ssh_config.host)
    return result


def _check_kept_rows(ssh, database: str, imp: ImportPlan) -> None:
    """Refuse a partial import whose ids collide with target rows that are kept."""
    for table, id_col, kept_where, incoming in imp.keep_checks:
        if not incoming:
            continue
        rows = ssh.mysql_query(f"SELECT {_quote_ident(id_col)} FROM {_quote_ident(database)}."
                               f"{_quote_ident(table)} WHERE {kept_where}")
        clash = sorted(i for i in {_int(r[0]) for r in rows} & {_int(i) for i in incoming}
                       if i is not None)
        if clash:
            shown = ", ".join(str(i) for i in clash[:10]) + (" ..." if len(clash) > 10 else "")
            raise XuiImportError(
                f"{len(clash)} id(s) in `{table}` ({shown}) are used on this XUI by content "
                "you are keeping. Import those kinds too, or remove them from this XUI first."
            )


def report_lines(result: dict) -> list[str]:
    """Human-readable outcome of :func:`import_via_ssh` (shared by CLI and GUI)."""
    lines: list[str] = []
    if result.get("backup"):
        lines.append(f"Previous `{result['database']}` saved on the server: {result['backup']}")
    if result.get("row_counts"):
        lines.append("Rows now in the panel:")
        lines += [f"  {t:18} {n:>8}" for t, n in result["row_counts"].items()]
    if result.get("access_codes"):
        lines.append("Access codes (panel URLs):")
        for c in result["access_codes"]:
            state = "" if c["enabled"] else "  [disabled]"
            lines.append(f"  {c['type']:9} {c['url']}{state}")
    rescue = result.get("rescue")
    if rescue:
        lines += [
            "RESCUE ACCESS (save it now, the password is not stored anywhere):",
            f"  URL      : {result.get('panel_base', '')}/{rescue.code}/",
            f"  Username : {rescue.username}",
            f"  Password : {rescue.password}",
            "  Delete this rescue user/code in the panel once you are in.",
        ]
    if result.get("codes_refresh"):
        lines.append(f"Access-code refresh: {result['codes_refresh']}")
    return lines


def _main_http_port(ssh, database: str, schema: dict[str, list[str]]) -> int | None:
    if not {"is_main", "http_broadcast_port"} <= set(schema.get("servers", [])):
        return None
    rows = ssh.mysql_query(f"SELECT `http_broadcast_port` FROM {_quote_ident(database)}"
                           ".`servers` WHERE `is_main` = 1 LIMIT 1")
    return int(rows[0][0]) if rows and rows[0][0].isdigit() else None


def _refresh_access_codes(ssh) -> str:
    """Ask XUI to rebuild its access-code web config from the database."""
    code, _, _ = ssh.run(f"test -x {XUI_TOOLS} && grep -qw access {XUI_TOOLS}", check=False)
    if code != 0:
        return (f"{XUI_TOOLS} not found: regenerate access codes from the panel or "
                "restart XUI so new/imported codes are served.")
    code, out, err = ssh.run(f"timeout 120 {XUI_TOOLS} access </dev/null", check=False)
    text = (out.strip() or err.strip())[-400:]
    return f"`tools access` {'OK' if code == 0 else f'exit {code}'}: {text}"
