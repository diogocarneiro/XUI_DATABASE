"""Migration engine: move data from a source panel DB into a target MySQL DB.

Strategy (schema-agnostic): for each table that exists on both sides, map columns
by name (case-insensitive). Only mapped columns are transferred, so the target's
own ``AUTO_INCREMENT`` / default columns are preserved and schema drift between
panel versions is handled gracefully. Credentials (password hashes) are copied
verbatim — never decrypted or regenerated.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .exporter import _sql_literal
from .logger import get_logger
from .mysql_source import MySQLSource
from .source import DataSource, SourceError

log = get_logger(__name__)

ProgressFn = Callable[[str, int, int], None]


@dataclass
class TableMapping:
    table: str
    columns: list[str]                       # columns present on BOTH sides
    source_only: list[str] = field(default_factory=list)
    target_only: list[str] = field(default_factory=list)
    transferable: bool = True

    @property
    def summary(self) -> str:
        return (
            f"{self.table}: {len(self.columns)} mapped, "
            f"{len(self.source_only)} source-only, {len(self.target_only)} target-only"
        )


@dataclass
class MigrationPlan:
    mappings: list[TableMapping]
    missing_in_target: list[str] = field(default_factory=list)  # source tables w/ no target

    @property
    def transferable(self) -> list[TableMapping]:
        return [m for m in self.mappings if m.transferable and m.columns]


def build_plan(
    source: DataSource, target: MySQLSource, tables: list[str] | None = None
) -> MigrationPlan:
    """Compute a column-level mapping between ``source`` and ``target``."""
    src_tables = {t.lower(): t for t in source.list_tables()}
    tgt_tables = {t.lower(): t for t in target.list_tables()}
    wanted = [t.lower() for t in (tables or source.list_tables())]

    mappings: list[TableMapping] = []
    missing: list[str] = []
    for low in wanted:
        if low not in src_tables:
            continue
        src_name = src_tables[low]
        if low not in tgt_tables:
            missing.append(src_name)
            continue
        tgt_name = tgt_tables[low]
        src_cols = {c.name.lower(): c.name for c in source.table_info(src_name).columns}
        tgt_cols = {c.name.lower(): c.name for c in target.table_info(tgt_name).columns}
        shared = [tgt_cols[c] for c in tgt_cols if c in src_cols]
        mappings.append(
            TableMapping(
                table=tgt_name,
                columns=shared,
                source_only=[src_cols[c] for c in src_cols if c not in tgt_cols],
                target_only=[tgt_cols[c] for c in tgt_cols if c not in src_cols],
                transferable=bool(shared),
            )
        )
    return MigrationPlan(mappings=mappings, missing_in_target=missing)


def _quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


@dataclass
class MigrationOptions:
    truncate_target: bool = False     # empty target table before inserting
    ignore_duplicates: bool = True    # INSERT IGNORE to skip PK/unique clashes
    batch_size: int = 500
    dry_run_sql_path: str | None = None  # if set, write SQL instead of executing


def execute_plan(
    source: DataSource,
    target: MySQLSource,
    plan: MigrationPlan,
    opts: MigrationOptions,
    progress: ProgressFn | None = None,
) -> dict[str, int]:
    """Transfer rows for every transferable table. Returns {table: rows_written}."""
    if opts.dry_run_sql_path:
        return _execute_to_sql(source, plan, opts, progress)
    return _execute_live(source, target, plan, opts, progress)


def _insert_prefix(opts: MigrationOptions) -> str:
    return "INSERT IGNORE INTO" if opts.ignore_duplicates else "INSERT INTO"


def _execute_live(source, target, plan, opts, progress) -> dict[str, int]:
    if target._conn is None:  # noqa: SLF001
        raise SourceError("Target is not connected.")
    written: dict[str, int] = {}
    conn = target._conn  # noqa: SLF001
    for m in plan.transferable:
        table = m.table
        total = _safe_count(source, table)
        if opts.truncate_target:
            with conn.cursor() as cur:
                cur.execute(f"TRUNCATE TABLE {_quote(table)}")
        collist = ", ".join(_quote(c) for c in m.columns)
        placeholders = ", ".join(["%s"] * len(m.columns))
        sql = f"{_insert_prefix(opts)} {_quote(table)} ({collist}) VALUES ({placeholders})"
        count = 0
        batch: list[tuple] = []
        with conn.cursor() as cur:
            for row in source.iter_rows(table, batch_size=opts.batch_size):
                batch.append(tuple(row.get(c) for c in m.columns))
                if len(batch) >= opts.batch_size:
                    cur.executemany(sql, batch)
                    count += len(batch)
                    batch.clear()
                    if progress:
                        progress(table, count, total)
            if batch:
                cur.executemany(sql, batch)
                count += len(batch)
        written[table] = count
        if progress:
            progress(table, count, total)
        log.info("Migrated %s rows into %s", count, table)
    return written


def _execute_to_sql(source, plan, opts, progress) -> dict[str, int]:
    written: dict[str, int] = {}
    with open(opts.dry_run_sql_path, "w", encoding="utf-8") as fh:
        fh.write("-- Migration SQL generated by XUI DB Tool\nSET FOREIGN_KEY_CHECKS=0;\n")
        for m in plan.transferable:
            table = m.table
            total = _safe_count(source, table)
            if opts.truncate_target:
                fh.write(f"TRUNCATE TABLE {_quote(table)};\n")
            collist = ", ".join(_quote(c) for c in m.columns)
            count = 0
            for row in source.iter_rows(table, batch_size=opts.batch_size):
                vals = ", ".join(_sql_literal(row.get(c)) for c in m.columns)
                fh.write(f"{_insert_prefix(opts)} {_quote(table)} ({collist}) VALUES ({vals});\n")
                count += 1
                if progress and count % opts.batch_size == 0:
                    progress(table, count, total)
            written[table] = count
            if progress:
                progress(table, count, total)
        fh.write("SET FOREIGN_KEY_CHECKS=1;\n")
    return written


def _safe_count(source: DataSource, table: str) -> int:
    try:
        return source.row_count(table)
    except Exception:  # noqa: BLE001
        return 0
