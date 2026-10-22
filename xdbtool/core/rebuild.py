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
* tables picked from the dump replace the template's default rows; the other
  template tables keep their defaults (or stay empty);
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
from .mysql_source import _quote_ident
from .source import DataSource
from .templates import TemplateMeta, get_template, list_templates
from .xui_import import SchemaSource
from .xui_migrate import _DB_DIRECTIVE, _LOCKS, SESSION_PREAMBLE

log = get_logger(__name__)

# Below this table-name similarity the dump is probably of another panel.
LOW_MATCH = 0.5
_GENERATED = re.compile(r"\bGENERATED\s+ALWAYS\b|\bAS\s*\(.*\)\s*(VIRTUAL|STORED|PERSISTENT)\b",
                        re.I)


class RebuildError(Exception):
    """The database cannot be rebuilt as asked."""


@dataclass
class TemplateSchema:
    """A template's statements (database directives removed) and its column lists."""

    statements: list[str] = field(default_factory=list)
    columns: dict[str, list[str]] = field(default_factory=dict)
    seeded: set[str] = field(default_factory=set)  # tables the template fills


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
                out.seeded.add(m.group(1).lower())
        out.statements.append(s)
    if not out.columns:
        raise RebuildError(_("Template '{key}' has no CREATE TABLE statements.", key=meta.key))
    return out


@dataclass
class RebuildPlan:
    template: TemplateMeta
    schema: TemplateSchema
    plan: MigrationPlan
    match: float = 0.0                                  # table-name similarity (Jaccard)
    rows: dict[str, int] = field(default_factory=dict)  # source rows per copied table

    @property
    def copied(self) -> list[TableMapping]:
        return self.plan.transferable

    @property
    def replaced_defaults(self) -> list[str]:
        """Template tables with default rows that the source's rows replace.

        A table that is empty in the source keeps the template's rows, so ticking
        e.g. an empty ``settings`` never leaves the new panel without its defaults.
        """
        return [m.table for m in self.copied
                if m.table.lower() in self.schema.seeded and self.rows.get(m.table)]

    @property
    def kept_defaults(self) -> list[str]:
        replaced = {t.lower() for t in self.replaced_defaults}
        return sorted(t for t in self.schema.columns
                      if t.lower() in self.schema.seeded and t.lower() not in replaced)

    @property
    def left_empty(self) -> list[str]:
        copied = {m.table.lower() for m in self.copied if self.rows.get(m.table)}
        return sorted(t for t in self.schema.columns
                      if t.lower() not in self.schema.seeded and t.lower() not in copied)

    def summary_lines(self) -> list[str]:
        lines = [_("Template: {name} ({count} tables); the source matches it {score:.0%}.",
                   name=self.template.name, count=len(self.schema.columns), score=self.match)]
        if self.match < LOW_MATCH:
            lines.append(_("WARNING: the source has little in common with this template; "
                           "choose the template of the source's panel."))
        lines.append(_("Tables copied from the source: {count}", count=len(self.copied)))
        seeded = set(self.replaced_defaults)
        for m in self.copied:
            text = _("{table}: {count} rows", table=m.table, count=self.rows.get(m.table, 0))
            if m.source_only:
                text += ", " + _("{count} source-only cols dropped", count=len(m.source_only))
            if m.target_only:
                text += ", " + _("{count} new cols get their defaults", count=len(m.target_only))
            if m.table in seeded:
                text += " " + _("(replaces the template's default rows)")
            elif m.table.lower() in self.schema.seeded:
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
            lines.append(_("Template default rows kept: {tables}",
                           tables=", ".join(self.kept_defaults)))
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
                  tables: Iterable[str] | None = None) -> RebuildPlan:
    """Map the chosen source ``tables`` (default: all) onto the template's schema."""
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
                     match=_jaccard(all_tables, schema.columns))
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
    """The template's statements minus the default rows of tables taken from the source."""
    replaced = {t.lower() for t in rp.replaced_defaults}
    out = []
    for s in rp.schema.statements:
        if s[:6].upper() == "INSERT":
            m = _INSERT_RE.match(s)
            if m and m.group(1).lower() in replaced:
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
