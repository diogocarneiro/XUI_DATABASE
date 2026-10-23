# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Rebuild a panel database: a fresh template schema plus the chosen tables of a dump.

Restoring an old or damaged dump as-is brings its schema along — the wrong version,
missing tables, broken indexes. This module instead:

* creates the new database from a registered template (the clean schema of the
  panel version wanted, with its default rows);
* copies the rows of the tables the user picked from the source, matching columns
  by name: dump-only columns are dropped, template-only columns keep their defaults;
* tables picked from the dump replace the template's rows; ``template_rows``
  decides which other tables keep the template's own rows: by default only the
  tables the source does not have at all (new in the template's version), so a
  template made from a real panel never brings its streams or logs along;
* loads the data inside one transaction, or writes it all as a ``.sql`` script for
  ``mysql < file`` on the server.

Credentials are copied verbatim (password hashes are never decrypted).
"""

from __future__ import annotations

import dataclasses
import gzip
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..i18n import _
from .dump_source import _CREATE_RE, _INSERT_RE, _iter_statements, _parse_columns, read_sql_text
from .exporter import _sql_literal
from .logger import get_logger
from .migrator import MigrationPlan, TableMapping, build_plan
from .mysql_source import PANEL_DATA_TABLES, _quote_ident
from .source import DataSource
from .templates import TemplateMeta, get_template, list_templates
from .xui_import import LOG_TABLES as XUI_LOG_TABLES
from .xui_import import SchemaSource
from .xui_migrate import _DB_DIRECTIVE, _LOCKS, SESSION_PREAMBLE
from .xui_migrate import LOG_TABLES as XC_LOG_TABLES

log = get_logger(__name__)

# Below this table-name similarity the dump is probably of another panel.
LOW_MATCH = 0.5
# Which tables keep the template's own rows when the source does not fill them:
#   "missing" - only tables the source does not have (default);
#   "all"     - every table not filled from the source;
#   "none"    - none: the template gives the schema only.
TEMPLATE_ROWS = ("missing", "all", "none")
# Rows here mean the template is a real panel's database, not a clean install.
DATA_TABLES = frozenset(PANEL_DATA_TABLES) | XUI_LOG_TABLES | XC_LOG_TABLES | {
    "mag_devices", "enigma2_devices", "user_activity", "user_activity_now", "streams_sys",
    "epg_data", "streams_episodes", "series_episodes", "streams_series", "series",
}
# Rows suggested for removal when cleaning a template: a panel's data plus its accounts,
# server addresses and block lists. Clean installs seed a few of these (an admin, the
# main server), so they only *suggest* - the user decides (templates.clean_template).
CLEAN_TABLES = DATA_TABLES | {
    "reg_users", "users", "streaming_servers", "access_codes", "packages", "users_packages",
    "stream_categories", "streams_categories", "streams_options", "streams_servers",
    "credits_log", "users_credits_logs", "tickets", "tickets_replies", "blocked_ips",
    "blocked_user_agents", "blocked_uas", "blocked_isps", "rtmp_ips", "hmac_keys", "epg",
    "epg_channels", "providers", "providers_streams", "recordings", "watch_folders",
    "mag_claims", "enigma2_actions", "devices_logs", "server_activity", "created",
    "isp_addon", "reseller_imex", "tmdb_async", "licence",
}
_GENERATED = re.compile(r"\bGENERATED\s+ALWAYS\b|\bAS\s*\(.*\)\s*(VIRTUAL|STORED|PERSISTENT)\b",
                        re.I)


class RebuildError(Exception):
    """The database cannot be rebuilt as asked."""


@dataclass
class TemplateSchema:
    """A template's statements (database directives removed) and its column lists."""

    statements: list[str] = field(default_factory=list)
    columns: dict[str, list[str]] = field(default_factory=dict)
    seed_bytes: dict[str, int] = field(default_factory=dict)  # lower name -> INSERT size

    @property
    def seeded(self) -> set[str]:
        """Lower-case names of the tables the template has rows for."""
        return set(self.seed_bytes)


def load_template_schema(meta: TemplateMeta) -> TemplateSchema:
    path = meta.sql_path()
    if not path.is_file():
        raise RebuildError(_("The .sql file of template '{key}' is missing: {path}",
                             key=meta.key, path=path))
    out = TemplateSchema()
    for stmt in _iter_statements(read_sql_text(path)):
        s = stmt.strip()
        if not s or _DB_DIRECTIVE.match(s) or _LOCKS.match(s):
            continue
        head = s[:12].upper()
        if head.startswith("CREATE TABLE"):
            m = _CREATE_RE.match(s)
            if m:
                body = {ln.strip().split()[0].strip("`"): ln for ln in s.splitlines()
                        if ln.strip().startswith("`")}
                out.columns[m.group(1)] = [
                    c.name for c in _parse_columns(s)
                    if not _GENERATED.search(body.get(c.name, ""))
                ]
        elif head.startswith("INSERT"):
            m = _INSERT_RE.match(s)
            if m:
                low = m.group(1).lower()
                out.seed_bytes[low] = out.seed_bytes.get(low, 0) + len(s)
        out.statements.append(s)
    if not out.columns:
        raise RebuildError(_("Template '{key}' has no CREATE TABLE statements.", key=meta.key))
    return out


def format_size(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


@dataclass
class RebuildPlan:
    template: TemplateMeta
    schema: TemplateSchema
    plan: MigrationPlan
    match: float = 0.0                                  # table-name similarity (Jaccard)
    rows: dict[str, int] = field(default_factory=dict)  # source rows per copied table
    source_tables: set[str] = field(default_factory=set)  # lower names of every source table
    template_rows: str = "missing"                      # see TEMPLATE_ROWS

    @property
    def copied(self) -> list[TableMapping]:
        return self.plan.transferable

    def _name(self, low: str) -> str:
        return next((t for t in self.schema.columns if t.lower() == low), low)

    @property
    def replaced_defaults(self) -> list[str]:
        """Template tables with rows of their own that the source's rows replace."""
        return [m.table for m in self.copied
                if m.table.lower() in self.schema.seeded and self.rows.get(m.table)]

    @property
    def kept_defaults(self) -> list[str]:
        """Template tables whose own rows go into the new database."""
        replaced = {t.lower() for t in self.replaced_defaults}
        return sorted(self._name(low) for low in self.schema.seeded - replaced
                      if self.template_rows == "all"
                      or (self.template_rows == "missing" and low not in self.source_tables
                          and low not in DATA_TABLES))  # never another panel's data

    @property
    def dropped_defaults(self) -> list[str]:
        """Template tables whose own rows are left out (neither kept nor replaced)."""
        done = {t.lower() for t in (*self.replaced_defaults, *self.kept_defaults)}
        return sorted(self._name(t) for t in self.schema.seeded - done)

    @property
    def left_empty(self) -> list[str]:
        filled = {m.table.lower() for m in self.copied if self.rows.get(m.table)}
        filled |= {t.lower() for t in self.kept_defaults}
        return sorted(t for t in self.schema.columns if t.lower() not in filled)

    @property
    def template_bytes(self) -> int:
        """Size of the template rows that go into the new database."""
        return sum(self.schema.seed_bytes[t.lower()] for t in self.kept_defaults)

    def _sized(self, tables: Iterable[str]) -> str:
        sizes = self.schema.seed_bytes
        return ", ".join(f"{t} ({format_size(sizes[t.lower()])})" for t in tables)

    def summary_lines(self) -> list[str]:
        lines = [_("Template: {name} ({count} tables); the source matches it {score:.0%}.",
                   name=self.template.name, count=len(self.schema.columns), score=self.match)]
        if self.match < LOW_MATCH:
            lines.append(_("WARNING: the source has little in common with this template; "
                           "choose the template of the source's panel."))
        data = sorted(self._name(t) for t in self.schema.seeded & DATA_TABLES)
        if data:
            size = sum(self.schema.seed_bytes[t.lower()] for t in data)
            lines.append(_("WARNING: this template is not a clean installation - it holds a "
                           "panel's data ({size} in {tables}). Register a clean install as "
                           "template, or keep its rows out.", size=format_size(size),
                           tables=", ".join(data)))
        lines.append(_("Tables copied from the source: {count}", count=len(self.copied)))
        seeded, kept = set(self.replaced_defaults), set(self.kept_defaults)
        for m in self.copied:
            text = _("{table}: {count} rows", table=m.table, count=self.rows.get(m.table, 0))
            if m.source_only:
                text += ", " + _("{count} source-only cols dropped", count=len(m.source_only))
            if m.target_only:
                text += ", " + _("{count} new cols get their defaults", count=len(m.target_only))
            if m.table in seeded:
                text += " " + _("(replaces the template's default rows)")
            elif m.table in kept:
                text += " " + _("(empty in the source: the template's default rows are kept)")
            lines.append("  " + text)
        not_copied = [m.table for m in self.plan.mappings if not m.transferable]
        if not_copied:
            lines.append(_("No column in common (skipped): {tables}",
                           tables=", ".join(not_copied)))
        if self.plan.missing_in_target:
            lines.append(_("Not in the template (skipped): {tables}",
                           tables=", ".join(self.plan.missing_in_target)))
        if self.kept_defaults:
            lines.append(_("Template rows kept ({size}): {tables}", size=format_size(
                self.template_bytes), tables=self._sized(self.kept_defaults)))
        if self.dropped_defaults:
            lines.append(_("Template rows left out: {tables}",
                           tables=self._sized(self.dropped_defaults)))
        if self.left_empty:
            lines.append(_("Left empty: {tables}", tables=", ".join(self.left_empty)))
        return lines


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    x, y = {t.lower() for t in a}, {t.lower() for t in b}
    return round(len(x & y) / len(x | y), 3) if x | y else 0.0


def suggest_template(source: DataSource) -> tuple[TemplateMeta | None, float]:
    """The registered template (with its .sql on disk) closest to the source's tables."""
    tables = source.list_tables()
    best: tuple[TemplateMeta | None, float] = (None, 0.0)
    for meta in list_templates():
        score = _jaccard(tables, meta.tables)
        if meta.sql_path().is_file() and score > best[1]:
            best = (meta, score)
    return best


def build_rebuild(source: DataSource, template_key: str,
                  tables: Iterable[str] | None = None, *,
                  template_rows: str = "missing") -> RebuildPlan:
    """Map the chosen source ``tables`` (default: all) onto the template's schema.

    ``template_rows`` (see :data:`TEMPLATE_ROWS`) picks the tables that keep the
    template's own rows when the source does not fill them.
    """
    if template_rows not in TEMPLATE_ROWS:
        raise ValueError(f"template_rows must be one of {TEMPLATE_ROWS}")
    meta = get_template(template_key)
    if meta is None:
        raise RebuildError(_("No such template: {key}", key=template_key))
    schema = load_template_schema(meta)
    all_tables = source.list_tables()
    by_lower = {t.lower(): t for t in all_tables}
    wanted = list(all_tables) if tables is None else list(tables)
    unknown = [t for t in wanted if t.lower() not in by_lower]
    if unknown:
        raise RebuildError(_("Not in the source: {tables}", tables=", ".join(unknown)))
    if not wanted:
        raise RebuildError(_("Choose at least one table to copy."))
    chosen = list(dict.fromkeys(by_lower[t.lower()] for t in wanted))
    plan = build_plan(source, SchemaSource(schema.columns), chosen)
    rp = RebuildPlan(template=meta, schema=schema, plan=plan,
                     match=_jaccard(all_tables, schema.columns),
                     source_tables=set(by_lower), template_rows=template_rows)
    for m in plan.transferable:
        rp.rows[m.table] = source.row_count(by_lower[m.table.lower()])
    return rp


def _insert_batches(source: DataSource, m: TableMapping, batch_size: int
                    ) -> Iterator[tuple[str, int]]:
    """``INSERT … VALUES`` statements for one copied table, with their row counts.

    Only the columns a row really has are inserted, so a column its dump INSERT
    left out gets the template's default instead of NULL.
    """
    by_lower = {t.lower(): t for t in source.list_tables()}
    table = _quote_ident(m.table)
    batch: list[str] = []
    cols: tuple[str, ...] = ()

    def flush() -> tuple[str, int]:
        collist = ", ".join(_quote_ident(c) for c in cols)
        return f"INSERT INTO {table} ({collist}) VALUES {','.join(batch)}", len(batch)

    for row in source.iter_rows(by_lower[m.table.lower()]):
        low = {k.lower(): v for k, v in row.items()}
        present = tuple(c for c in m.columns if c.lower() in low)
        if not present:
            raise RebuildError(_("Rows of `{table}` do not match its CREATE TABLE column list "
                                 "in the source; refusing to copy them as NULLs.",
                                 table=m.table))
        if batch and (present != cols or len(batch) >= batch_size):
            yield flush()
            batch = []
        cols = present
        batch.append("(" + ", ".join(_sql_literal(low[c.lower()]) for c in cols) + ")")
    if batch:
        yield flush()


def _schema_statements(rp: RebuildPlan) -> list[str]:
    """The template's statements, with only the rows of the tables that keep them."""
    kept = {t.lower() for t in rp.kept_defaults}
    out = []
    for s in rp.schema.statements:
        if s[:6].upper() == "INSERT":
            m = _INSERT_RE.match(s)
            if m and m.group(1).lower() not in kept:
                continue
        out.append(s)
    return out


def run_command(path: str | Path, database: str = "") -> str:
    """The shell command that loads a rebuild file on the server.

    A file written without a database name loads into the database named on the
    command line (``DATABASE`` is the placeholder to replace).
    """
    name = Path(path).name
    target = f" {database}" if database else ""
    if name.endswith(".gz"):
        return f"gunzip -c {name} | mysql -u root -p{target}"
    return f"mysql -u root -p{target} < {name}"


def write_rebuild_script(source: DataSource, rp: RebuildPlan, out_path: str | Path, *,
                         database: str = "", drop_existing: bool = False,
                         batch_size: int = 500, progress=None) -> Path:
    """Write the whole rebuilt database as one ``.sql`` file (gzip if it ends in ``.gz``).

    With ``database`` the file creates that database itself (``drop_existing`` adds
    ``DROP DATABASE``); run it with :func:`run_command`. Without it the file is a
    portable dump with no ``CREATE DATABASE``/``USE``: import it into any empty
    database (``mysql … DATABASE < file``, phpMyAdmin → Import, or *Restore*), or
    open it again in this tool as a dump source.
    """
    out_path = Path(out_path)
    db = _quote_ident(database) if database else ""
    opener = gzip.open if out_path.suffix == ".gz" else open
    with opener(out_path, "wt", encoding="utf-8", newline="\n") as fh:
        fh.write(f"-- Generated by XUI DB Tool: {db or 'database'} rebuilt from template "
                 f"'{rp.template.key}' + {source.label}.\n")
        if db:
            fh.write(f"-- Run on the server:  {run_command(out_path)}\n\n")
            if drop_existing:
                fh.write(f"DROP DATABASE IF EXISTS {db};\n")
            fh.write(f"CREATE DATABASE IF NOT EXISTS {db} CHARACTER SET utf8mb4;\n")
            fh.write(f"USE {db};\n")
        else:
            fh.write("-- Import it into an EMPTY database:  "
                     f"{run_command(out_path, 'DATABASE')}\n\n")
        for s in (*SESSION_PREAMBLE, "SET FOREIGN_KEY_CHECKS = 0"):
            fh.write(s + ";\n")
        for s in _schema_statements(rp):
            fh.write(s + ";\n")
        fh.write("START TRANSACTION;\n")
        for m in rp.copied:
            done, total = 0, rp.rows.get(m.table, 0)
            for stmt, n in _insert_batches(source, m, batch_size):
                fh.write(stmt + ";\n")
                done += n
                if progress:
                    progress(m.table, done, total)
        fh.write("COMMIT;\nSET FOREIGN_KEY_CHECKS = 1;\n")
    log.info("Wrote rebuilt database %s (%d tables copied)", out_path, len(rp.copied))
    return out_path


def rebuild_live(target_config, source: DataSource, rp: RebuildPlan, database: str, *,
                 drop_existing: bool = False, allow_drop_panel: bool = False,
                 batch_size: int = 500, progress=None, message=None) -> dict:
    """Create ``database`` on the target server from the template, then copy the rows.

    Refuses a database that already has tables unless ``drop_existing``; dropping one
    that holds panel data also needs ``allow_drop_panel`` (``DropRefused``). The rows
    are copied in one transaction: on error none of them is kept.
    """
    from .mysql_source import MySQLSource  # local import keeps core import-light

    say = message or (lambda _m: None)
    cfg = dataclasses.replace(target_config, database="")
    with MySQLSource(cfg) as target:
        if database in target.list_databases() and not drop_existing:
            rows = target.execute(
                "SELECT COUNT(*) AS n FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s",
                (database,))
            if rows and int(rows[0]["n"]) > 0:
                raise RebuildError(_("`{db}` already exists and has tables. Choose another "
                                     "name, or replace it (DROP).", db=database))
        target.create_database(database, drop_existing=drop_existing,
                               allow_drop_panel=allow_drop_panel)
        say(_("Creating the tables of template '{key}'…", key=rp.template.key))
        target.execute_script([*SESSION_PREAMBLE, *_schema_statements(rp)], progress)
        written: dict[str, int] = {}
        stmt = ""
        try:
            with target.transaction() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS = 0")
                for m in rp.copied:
                    say(_("Copying {table}…", table=m.table))
                    done, total = 0, rp.rows.get(m.table, 0)
                    for stmt, n in _insert_batches(source, m, batch_size):
                        cur.execute(stmt)
                        done += n
                        if progress:
                            progress(m.table, done, total)
                    written[m.table] = done
                cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        except RebuildError:
            raise
        except Exception as exc:  # noqa: BLE001 - report which statement failed
            raise RebuildError(_("Copying the rows failed, none was kept (`{db}` has the "
                                 "template only): {error}", db=database, error=exc)
                               + f"\n--- SQL ---\n{stmt[:300]}") from exc
    log.info("Rebuilt %s from template %s (%d tables copied)", database, rp.template.key,
             len(written))
    return {"database": database, "template": rp.template.key, "written": written}
