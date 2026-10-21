# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Integrity check: find broken links between panel tables, and repair them.

XUI.one and Xtream-UI declare no foreign keys. A stream points at its category, a
bouquet at its streams (a JSON id list), a line at its bouquets — but nothing stops
the panel, or a manual ``DELETE``, from removing the other side. This module checks
those implicit links plus a few uniqueness rules, and builds the SQL that repairs
what can be repaired safely. Only links whose tables and columns exist are checked
(live introspection), so forks and versions with fewer tables are handled.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..i18n import N_, _
from .exporter import ExportOptions, _sql_literal, export_tables
from .logger import get_logger
from .mysql_source import MySQLSource, _quote_ident
from .source import DataSource, SourceError

log = get_logger(__name__)

ProgressFn = Callable[[str, int, int], None]

# Issue kinds.
ORPHAN = "orphan"        # points at an id that no longer exists
REPEATED = "repeated"    # an id list names the same id twice
INVALID = "invalid"      # an id list that is not a JSON list of ids
DUPLICATE = "duplicate"  # a value that should be unique is shared by several rows
CHILDLESS = "childless"  # a row that is missing its required companion row

# Fixes.
DELETE = "delete"        # delete the rows holding the broken reference
CLEAR = "clear"          # set the reference to NULL (or 0 when NOT NULL)
PRUNE = "prune"          # drop the missing ids from the id list
DEDUPE = "dedupe"        # drop the repeated ids from the id list

SAMPLE_SIZE = 10
_CHUNK = 500


@dataclass(frozen=True)
class Link:
    """``table.column`` refers to ``target.target_column``."""

    table: str
    column: str
    target: str
    target_column: str = "id"
    fix: str = ""        # DELETE / CLEAR / PRUNE, or "" = report only
    many: bool = False   # a JSON id list (a single id is accepted too)

    def __str__(self) -> str:
        return f"{self.table}.{self.column} → {self.target}.{self.target_column}"


@dataclass(frozen=True)
class Required:
    """Every ``table`` row (optionally of some ``streams.type``) needs a ``child`` row."""

    table: str
    child: str
    child_column: str
    label: str
    types: frozenset[int] = frozenset()


L = Link
# XUI.one keeps every stream kind in `streams` (type 1 live, 2 movie, 3 created
# live, 4 radio, 5 episode); `users` are admins/resellers and `lines` subscribers.
XUI_ONE_LINKS = (
    L("streams", "category_id", "streams_categories", fix=PRUNE, many=True),
    L("streams", "tv_archive_server_id", "servers", fix=CLEAR),
    L("streams", "vframes_server_id", "servers", fix=CLEAR),
    L("streams", "transcode_profile_id", "profiles", "profile_id", fix=CLEAR),
    L("streams", "epg_id", "epg", fix=CLEAR),
    L("streams_servers", "stream_id", "streams", fix=DELETE),
    L("streams_servers", "server_id", "servers", fix=DELETE),
    L("streams_servers", "parent_id", "servers", fix=CLEAR),
    L("streams_options", "stream_id", "streams", fix=DELETE),
    L("streams_options", "argument_id", "streams_arguments", fix=DELETE),
    L("streams_episodes", "stream_id", "streams", fix=DELETE),
    L("streams_episodes", "series_id", "streams_series", fix=DELETE),
    L("streams_series", "category_id", "streams_categories", fix=PRUNE, many=True),
    L("streams_categories", "parent_id", "streams_categories", fix=CLEAR),
    L("bouquets", "bouquet_channels", "streams", fix=PRUNE, many=True),
    L("bouquets", "bouquet_movies", "streams", fix=PRUNE, many=True),
    L("bouquets", "bouquet_radios", "streams", fix=PRUNE, many=True),
    L("bouquets", "bouquet_series", "streams_series", fix=PRUNE, many=True),
    L("lines", "bouquet", "bouquets", fix=PRUNE, many=True),
    L("lines", "allowed_outputs", "output_formats", "access_output_id", fix=PRUNE, many=True),
    L("lines", "member_id", "users"),
    L("lines", "package_id", "users_packages"),
    L("lines", "force_server_id", "servers", fix=CLEAR),
    L("mag_devices", "user_id", "lines", fix=DELETE),
    L("enigma2_devices", "user_id", "lines", fix=DELETE),
    L("enigma2_actions", "device_id", "enigma2_devices", "device_id", fix=DELETE),
    L("mag_claims", "mag_id", "mag_devices", "mag_id", fix=DELETE),
    L("mag_events", "mag_device_id", "mag_devices", "mag_id", fix=DELETE),
    L("users", "member_group_id", "users_groups", "group_id"),
    L("users", "owner_id", "users"),
    L("users_packages", "bouquets", "bouquets", fix=PRUNE, many=True),
    L("users_packages", "groups", "users_groups", "group_id", fix=PRUNE, many=True),
    L("users_packages", "output_formats", "output_formats", "access_output_id", fix=PRUNE,
      many=True),
    L("access_codes", "groups", "users_groups", "group_id", fix=PRUNE, many=True),
    L("epg_channels", "epg_id", "epg", fix=DELETE),
    L("epg_data", "epg_id", "epg", fix=DELETE),
    L("providers_streams", "provider_id", "providers", fix=DELETE),
    L("tickets", "member_id", "users"),
    L("tickets_replies", "ticket_id", "tickets", fix=DELETE),
    L("watch_folders", "server_id", "servers"),
)

# Xtream-UI / StreamCreed: `users` are the subscriber lines, `reg_users` the
# admins/resellers, and streams run on `streaming_servers` through `streams_sys`.
XTREAM_LINKS = (
    L("streams", "category_id", "stream_categories", fix=PRUNE, many=True),
    L("streams", "tv_archive_server_id", "streaming_servers", fix=CLEAR),
    L("streams", "transcode_profile_id", "transcoding_profiles", "profile_id", fix=CLEAR),
    L("streams", "epg_id", "epg", fix=CLEAR),
    L("streams_sys", "stream_id", "streams", fix=DELETE),
    L("streams_sys", "server_id", "streaming_servers", fix=DELETE),
    L("streams_sys", "parent_id", "streaming_servers", fix=CLEAR),
    L("streams_options", "stream_id", "streams", fix=DELETE),
    L("streams_options", "argument_id", "streams_arguments", fix=DELETE),
    L("series_episodes", "stream_id", "streams", fix=DELETE),
    L("series_episodes", "series_id", "series", fix=DELETE),
    L("series", "category_id", "stream_categories", fix=PRUNE, many=True),
    L("stream_categories", "parent_id", "stream_categories", fix=CLEAR),
    L("bouquets", "bouquet_channels", "streams", fix=PRUNE, many=True),
    L("bouquets", "bouquet_series", "series", fix=PRUNE, many=True),
    L("users", "bouquet", "bouquets", fix=PRUNE, many=True),
    L("users", "member_id", "reg_users"),
    L("users", "force_server_id", "streaming_servers", fix=CLEAR),
    L("user_output", "user_id", "users", fix=DELETE),
    L("user_output", "access_output_id", "access_output", "access_output_id", fix=DELETE),
    L("mag_devices", "user_id", "users", fix=DELETE),
    L("enigma2_devices", "user_id", "users", fix=DELETE),
    L("enigma2_actions", "device_id", "enigma2_devices", "device_id", fix=DELETE),
    L("mag_claims", "mag_id", "mag_devices", "mag_id", fix=DELETE),
    L("mag_events", "mag_device_id", "mag_devices", "mag_id", fix=DELETE),
    L("reg_users", "member_group_id", "member_groups", "group_id"),
    L("reg_users", "owner_id", "reg_users"),
    L("packages", "bouquets", "bouquets", fix=PRUNE, many=True),
    L("packages", "groups", "member_groups", "group_id", fix=PRUNE, many=True),
    L("epg_data", "epg_id", "epg", fix=DELETE),
    L("tickets", "member_id", "reg_users"),
    L("tickets_replies", "ticket_id", "tickets", fix=DELETE),
)
del L

XUI_ONE_UNIQUE = (("lines", "username"), ("users", "username"), ("mag_devices", "mac"),
                  ("enigma2_devices", "mac"), ("access_codes", "code"))
XTREAM_UNIQUE = (("users", "username"), ("reg_users", "username"), ("mag_devices", "mac"),
                 ("enigma2_devices", "mac"))

_NO_SERVER = N_("{rows} stream(s) not assigned to any server (they cannot play)")
_NO_SERIES = N_("{rows} series episode(s) not linked to any series")
XUI_ONE_REQUIRED = (
    Required("streams", "streams_servers", "stream_id", _NO_SERVER),
    Required("streams", "streams_episodes", "stream_id", _NO_SERIES, frozenset({5})),
)
XTREAM_REQUIRED = (
    Required("streams", "streams_sys", "stream_id", _NO_SERVER),
    Required("streams", "series_episodes", "stream_id", _NO_SERIES, frozenset({5})),
)

FAMILIES = {
    "xui_one": ("XUI.one", XUI_ONE_LINKS, XUI_ONE_UNIQUE, XUI_ONE_REQUIRED),
    "xtream": ("Xtream-UI / StreamCreed", XTREAM_LINKS, XTREAM_UNIQUE, XTREAM_REQUIRED),
}

_FIX_LABELS = {
    DELETE: N_("delete these rows"),
    CLEAR: N_("clear the reference"),
    PRUNE: N_("remove the missing ids from the list"),
    DEDUPE: N_("remove the repeated ids"),
    "": N_("report only — review by hand"),
}


@dataclass
class Issue:
    kind: str
    table: str
    column: str
    rows: int
    target: str = ""                       # "streams.id" (ORPHAN) / child table (CHILDLESS)
    fix: str = ""
    sample: list = field(default_factory=list)
    missing: frozenset = frozenset()       # every missing id (ORPHAN)
    label: str = ""                        # CHILDLESS description

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.table}.{self.column}:{self.target}"

    @property
    def fixable(self) -> bool:
        return bool(self.fix)

    def describe(self) -> str:
        where = f"{self.table}.{self.column}"
        if self.kind == ORPHAN:
            return _("{rows} row(s) of {where} point to missing {target} ids",
                     rows=self.rows, where=where, target=self.target)
        if self.kind == REPEATED:
            return _("{rows} row(s) of {where} list the same id more than once",
                     rows=self.rows, where=where)
        if self.kind == INVALID:
            return _("{rows} row(s) of {where} are not a valid id list", rows=self.rows,
                     where=where)
        if self.kind == DUPLICATE:
            return _("{count} value(s) of {where} are shared by several rows",
                     count=self.rows, where=where)
        return _(self.label, rows=self.rows)

    def fix_label(self) -> str:
        return _(_FIX_LABELS[self.fix])

    def sample_text(self) -> str:
        text = ", ".join(str(v) for v in self.sample[:SAMPLE_SIZE])
        return text + (" …" if len(self.sample) > SAMPLE_SIZE else "")


@dataclass
class IntegrityReport:
    family: str
    issues: list[Issue] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)   # rules checked
    skipped: list[str] = field(default_factory=list)   # rules whose tables/columns are absent
    # Distinct values of each id-list column with their row counts, kept so the
    # fix can rewrite every distinct value once (UPDATE … WHERE col = old).
    lists: dict[tuple[str, str], Counter] = field(default_factory=dict)
    nullable: dict[tuple[str, str], bool] = field(default_factory=dict)

    @property
    def fixable(self) -> list[Issue]:
        return [i for i in self.issues if i.fixable]

    def summary_lines(self) -> list[str]:
        lines = [_("Panel family: {family}", family=FAMILIES[self.family][0]),
                 _("Rules checked: {count}", count=len(self.checked))]
        if not self.issues:
            lines.append(_("No problems found."))
            return lines
        lines.append(_("Problems found: {count} ({fixable} can be fixed automatically)",
                       count=len(self.issues), fixable=len(self.fixable)))
        for i in self.issues:
            text = f"  [{i.kind}] {i.describe()} — {i.fix_label()}"
            if i.sample:
                text += "  " + _("(e.g. {values})", values=i.sample_text())
            lines.append(text)
        return lines


# -------------------------------------------------------------------- helpers
def _key(value) -> object | None:
    """Normalise a scalar reference: None for "no reference" (NULL, '', 0)."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    s = str(value).strip()
    if s in ("", "0"):
        return None
    try:
        return int(s)
    except ValueError:
        return s


def _ordered(keys: Iterable) -> list:
    """Ids in numeric order (text keys, if any, after the numbers)."""
    return sorted(keys, key=lambda k: (0, k, "") if isinstance(k, int) else (1, 0, str(k)))


def _item_key(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def parse_id_list(raw) -> tuple[list, bool] | None:
    """``(items, is_list)`` for an id-list value; None when it is not one.

    Accepts a JSON list (``[1,2]`` or ``["1","2"]``) or a single id (old forks keep
    a scalar ``category_id``). NULL, ``''`` and ``0`` are an empty reference.
    """
    if raw is None:
        return [], True
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return ([raw] if raw else []), False
    s = str(raw).strip()
    if s in ("", "0", "null"):
        return [], s != "0"
    if s.lstrip("-").isdigit():
        return [int(s)], False
    try:
        value = json.loads(s)
    except ValueError:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return ([value] if value else []), False
    if not isinstance(value, list) or any(_item_key(v) is None for v in value):
        return None
    return value, True


def clean_id_list(raw, missing: frozenset, *, prune: bool, dedupe: bool, empty_scalar):
    """The value ``raw`` becomes after the fix (``raw`` itself when unchanged)."""
    parsed = parse_id_list(raw)
    if parsed is None:
        return raw
    items, is_list = parsed
    out, seen = [], set()
    for item in items:
        k = _item_key(item)
        if (prune and k in missing) or (dedupe and k in seen):
            continue
        seen.add(k)
        out.append(item)
    if out == items:
        return raw
    if is_list:
        return json.dumps(out, separators=(",", ":"))
    return out[0] if out else empty_scalar


# ----------------------------------------------------------------------- check
class _Schema:
    def __init__(self, source: DataSource):
        self.source = source
        self.names = {t.lower(): t for t in source.list_tables()}
        self._cols: dict[str, dict] = {}
        self._ids: dict[tuple[str, str], set] = {}

    def table(self, name: str) -> str | None:
        return self.names.get(name.lower())

    def columns(self, table: str) -> dict:
        real = self.table(table)
        if real is None:
            return {}
        if real not in self._cols:
            self._cols[real] = {c.name.lower(): c for c in self.source.table_info(real).columns}
        return self._cols[real]

    def column(self, table: str, column: str) -> str | None:
        info = self.columns(table).get(column.lower())
        return info.name if info else None

    def has(self, table: str, *columns: str) -> bool:
        return all(self.column(table, c) for c in columns)

    def ids(self, table: str, column: str) -> set:
        key = (table.lower(), column.lower())
        if key not in self._ids:
            values = self.source.iter_columns(self.table(table), [self.column(table, column)])
            self._ids[key] = {k for (v,) in values if (k := _key(v)) is not None}
        return self._ids[key]


def detect_family(tables: Iterable[str]) -> str:
    return "xui_one" if "lines" in {t.lower() for t in tables} else "xtream"


def check_integrity(source: DataSource, progress: ProgressFn | None = None) -> IntegrityReport:
    """Check every applicable link, uniqueness rule and required row of the panel."""
    schema = _Schema(source)
    family = detect_family(schema.names)
    _name, links, uniques, required = FAMILIES[family]
    report = IntegrityReport(family=family)
    total = len(links) + len(uniques) + len(required)
    step = 0

    def tick(label: str) -> None:
        nonlocal step
        step += 1
        if progress:
            progress(label, step, total)

    by_table: dict[str, list[Link]] = {}
    for link in links:
        if schema.has(link.table, link.column) and schema.has(link.target, link.target_column):
            by_table.setdefault(link.table, []).append(link)
            report.checked.append(str(link))
        else:
            report.skipped.append(str(link))
            tick(link.table)
    for table, table_links in by_table.items():
        _check_links(schema, report, table, table_links)
        for link in table_links:
            tick(link.table)

    for table, column in uniques:
        rule = _("unique {where}", where=f"{table}.{column}")
        if schema.has(table, column):
            report.checked.append(rule)
            _check_unique(schema, report, table, column)
        else:
            report.skipped.append(rule)
        tick(table)

    for req in required:
        rule = f"{req.table} ⇐ {req.child}.{req.child_column}"
        needs = ("id", "type") if req.types else ("id",)
        if schema.has(req.table, *needs) and schema.has(req.child, req.child_column):
            report.checked.append(rule)
            _check_required(schema, report, req)
        else:
            report.skipped.append(rule)
        tick(req.table)
    log.info("Integrity check (%s): %d rule(s), %d issue(s)", family, len(report.checked),
             len(report.issues))
    return report


def _check_links(schema: _Schema, report: IntegrityReport, table: str,
                 links: list[Link]) -> None:
    real = schema.table(table)
    columns = [schema.column(table, link.column) for link in links]
    scalars: list[Counter] = [Counter() for _ in links]
    lists: list[Counter] = [Counter() for _ in links]
    for row in schema.source.iter_columns(real, columns):
        for n, link in enumerate(links):
            if link.many:
                lists[n][row[n]] += 1
            elif (k := _key(row[n])) is not None:
                scalars[n][k] += 1
    for n, link in enumerate(links):
        valid = schema.ids(link.target, link.target_column)
        target = f"{schema.table(link.target)}.{link.target_column}"
        # An empty target table (e.g. a partial dump) would make every reference look
        # broken; report it, but never "fix" it by deleting/clearing everything.
        fix = link.fix if valid else ""
        col_info = schema.columns(table)[link.column.lower()]
        report.nullable[(real, columns[n])] = col_info.nullable
        if not link.many:
            missing = {k: c for k, c in scalars[n].items() if k not in valid}
            if missing:
                report.issues.append(Issue(
                    ORPHAN, real, columns[n], sum(missing.values()), target, fix,
                    _ordered(missing), frozenset(missing)))
            continue
        report.lists[(real, columns[n])] = lists[n]
        orphan_rows = repeated_rows = invalid_rows = 0
        missing_ids: set = set()
        invalid_sample = []
        for raw, count in lists[n].items():
            parsed = parse_id_list(raw)
            if parsed is None:
                invalid_rows += count
                invalid_sample.append(str(raw)[:40])
                continue
            keys = [_item_key(i) for i in parsed[0]]
            gone = {k for k in keys if k not in valid}
            if gone:
                orphan_rows += count
                missing_ids |= gone
            if len(set(keys)) != len(keys):
                repeated_rows += count
        if orphan_rows:
            report.issues.append(Issue(
                ORPHAN, real, columns[n], orphan_rows, target, fix,
                _ordered(missing_ids), frozenset(missing_ids)))
        if repeated_rows:
            report.issues.append(Issue(REPEATED, real, columns[n], repeated_rows,
                                       fix=DEDUPE if link.fix else ""))
        if invalid_rows:
            report.issues.append(Issue(INVALID, real, columns[n], invalid_rows,
                                       sample=invalid_sample))


def _check_unique(schema: _Schema, report: IntegrityReport, table: str, column: str) -> None:
    real, col = schema.table(table), schema.column(table, column)
    counts: Counter = Counter()
    for (value,) in schema.source.iter_columns(real, [col]):
        if value not in (None, ""):
            counts[str(value).strip().lower()] += 1
    shared = sorted(v for v, c in counts.items() if c > 1)
    if shared:
        report.issues.append(Issue(DUPLICATE, real, col, len(shared), sample=shared))


def _check_required(schema: _Schema, report: IntegrityReport, req: Required) -> None:
    real = schema.table(req.table)
    have = schema.ids(req.child, req.child_column)
    if req.types:
        cols = [schema.column(req.table, "id"), schema.column(req.table, "type")]
        rows = ((i, t) for i, t in schema.source.iter_columns(real, cols)
                if _item_key(t) in req.types)
    else:
        rows = ((i, None) for (i,) in schema.source.iter_columns(
            real, [schema.column(req.table, "id")]))
    lonely = _ordered(k for i, _t in rows if (k := _key(i)) is not None and k not in have)
    if lonely:
        report.issues.append(Issue(CHILDLESS, real, schema.column(req.table, "id"),
                                   len(lonely), schema.table(req.child), sample=lonely,
                                   label=req.label))


# ------------------------------------------------------------------------- fix
def _in_list(keys: Iterable) -> Iterable[str]:
    keys = _ordered(keys)
    for start in range(0, len(keys), _CHUNK):
        yield ", ".join(_sql_literal(k) for k in keys[start:start + _CHUNK])


def fix_statements(report: IntegrityReport, issues: Iterable[Issue] | None = None) -> list[str]:
    """SQL that repairs ``issues`` (default: every fixable issue of ``report``)."""
    chosen = [i for i in (report.fixable if issues is None else issues) if i.fixable]
    stmts: list[str] = []
    # Id lists: prune and/or dedupe each distinct value once.
    list_fixes: dict[tuple[str, str], dict] = {}
    for issue in chosen:
        if issue.fix in (PRUNE, DEDUPE):
            spec = list_fixes.setdefault((issue.table, issue.column),
                                         {"missing": frozenset(), "dedupe": False})
            if issue.fix == PRUNE:
                spec["missing"] = issue.missing
            else:
                spec["dedupe"] = True
    for (table, column), spec in list_fixes.items():
        empty = None if report.nullable.get((table, column), True) else 0
        t, c = _quote_ident(table), _quote_ident(column)
        for raw in report.lists.get((table, column), {}):
            new = clean_id_list(raw, spec["missing"], prune=bool(spec["missing"]),
                                dedupe=spec["dedupe"], empty_scalar=empty)
            if new != raw:
                stmts.append(f"UPDATE {t} SET {c} = {_sql_literal(new)} "
                             f"WHERE {c} = {_sql_literal(raw)}")
    for issue in chosen:
        t, c = _quote_ident(issue.table), _quote_ident(issue.column)
        if issue.fix == DELETE:
            stmts += [f"DELETE FROM {t} WHERE {c} IN ({keys})" for keys in _in_list(issue.missing)]
        elif issue.fix == CLEAR:
            empty = "NULL" if report.nullable.get((issue.table, issue.column), True) else "0"
            stmts += [f"UPDATE {t} SET {c} = {empty} WHERE {c} IN ({keys})"
                      for keys in _in_list(issue.missing)]
    return stmts


def write_fix_script(statements: list[str], path: str | Path, label: str = "") -> Path:
    """Write the fix as one transaction, to run with the ``mysql`` client."""
    path = Path(path)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("-- Generated by XUI DB Tool: integrity fixes"
                 + (f" for {label}" if label else "") + ".\n"
                 "-- Back up the database before running it.\n"
                 "START TRANSACTION;\n")
        for stmt in statements:
            fh.write(stmt + ";\n")
        fh.write("COMMIT;\n")
    log.info("Wrote %d integrity fix statement(s) to %s", len(statements), path)
    return path


def apply_fixes(
    source: DataSource,
    report: IntegrityReport,
    issues: Iterable[Issue] | None = None,
    *,
    backup_dir: str | Path,
    progress: ProgressFn | None = None,
    message: Callable[[str], None] | None = None,
) -> dict:
    """Back up the affected tables as SQL, then apply the fixes in one transaction."""
    if not isinstance(source, MySQLSource):
        raise SourceError(_("Fixes can only be applied to a live MySQL/MariaDB connection; "
                            "save the fix script instead."))
    say = message or (lambda _m: None)
    chosen = [i for i in (report.fixable if issues is None else issues) if i.fixable]
    stmts = fix_statements(report, chosen)
    if not stmts:
        return {"backup": None, "statements": 0, "changed": 0}
    tables = sorted({i.table for i in chosen})
    folder = Path(backup_dir) / f"integrity-{time.strftime('%Y%m%d-%H%M%S')}"
    say(_("Backing up {tables} to {folder}…", tables=", ".join(tables), folder=folder))
    export_tables(source, tables, ExportOptions(fmt="sql", out_dir=folder), progress)
    say(_("Applying {count} fix statement(s)…", count=len(stmts)))
    changed = source.run_transaction(stmts, progress)
    log.info("Applied %d integrity fix statement(s), %d row(s) changed", len(stmts), changed)
    return {"backup": folder, "statements": len(stmts), "changed": changed}
