"""Export tables from any :class:`DataSource` to JSON, CSV or SQL files."""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .logger import get_logger
from .source import DataSource

log = get_logger(__name__)

ProgressFn = Callable[[str, int, int], None]  # (table, rows_done, rows_total)


@dataclass
class ExportOptions:
    fmt: str = "json"                     # "json" | "csv" | "sql"
    out_dir: Path = Path("exports")
    batch_size: int = 1000
    # Columns whose values are replaced with ``redact_with`` (case-insensitive,
    # matched by exact column name). Empty = no redaction.
    redact_columns: frozenset[str] = frozenset()
    redact_with: str = "***"
    # For SQL export: prefix each table with DROP/CREATE from the live server.
    include_schema: bool = True


def _json_default(obj: object) -> object:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", "replace")
    return str(obj)


def _redact(row: dict, cols: frozenset[str], placeholder: str) -> dict:
    if not cols:
        return row
    return {k: (placeholder if k.lower() in cols else v) for k, v in row.items()}


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return "'" + value.isoformat(sep=" ") + "'"
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    s = str(value)
    s = (
        s.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x00", "\\0")
        .replace("\x1a", "\\Z")
    )
    return f"'{s}'"


def _resolve_total(source: DataSource, table: str) -> int:
    try:
        return source.row_count(table)
    except Exception:  # noqa: BLE001 - count is best-effort for progress
        info = source.table_info(table)
        return info.row_count or 0


def export_table(
    source: DataSource,
    table: str,
    opts: ExportOptions,
    progress: ProgressFn | None = None,
) -> Path:
    """Export a single table; returns the written file path."""
    opts.out_dir.mkdir(parents=True, exist_ok=True)
    redact = frozenset(c.lower() for c in opts.redact_columns)
    total = _resolve_total(source, table)
    rows = source.iter_rows(table, batch_size=opts.batch_size)

    if opts.fmt == "json":
        return _write_json(source, table, rows, opts, redact, total, progress)
    if opts.fmt == "csv":
        return _write_csv(source, table, rows, opts, redact, total, progress)
    if opts.fmt == "sql":
        return _write_sql(source, table, rows, opts, redact, total, progress)
    raise ValueError(f"Unknown export format: {opts.fmt!r}")


def _write_json(source, table, rows, opts, redact, total, progress) -> Path:
    path = opts.out_dir / f"{table}.json"
    done = 0
    with path.open("w", encoding="utf-8") as fh:
        fh.write("[\n")
        first = True
        for row in rows:
            row = _redact(row, redact, opts.redact_with)
            encoded = json.dumps(row, ensure_ascii=False, default=_json_default)
            fh.write(("" if first else ",\n") + encoded)
            first = False
            done += 1
            if progress and done % opts.batch_size == 0:
                progress(table, done, total)
        fh.write("\n]\n")
    if progress:
        progress(table, done, total)
    return path


def _write_csv(source, table, rows, opts, redact, total, progress) -> Path:
    path = opts.out_dir / f"{table}.csv"
    done = 0
    writer = None
    with path.open("w", encoding="utf-8", newline="") as fh:
        for row in rows:
            row = _redact(row, redact, opts.redact_with)
            if writer is None:
                writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
                writer.writeheader()
            writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
            done += 1
            if progress and done % opts.batch_size == 0:
                progress(table, done, total)
        if writer is None:  # empty table -> header from introspection
            cols = [c.name for c in source.table_info(table).columns]
            csv.writer(fh).writerow(cols)
    if progress:
        progress(table, done, total)
    return path


def _write_sql(source, table, rows, opts, redact, total, progress) -> Path:
    path = opts.out_dir / f"{table}.sql"
    done = 0
    with path.open("w", encoding="utf-8") as fh:
        fh.write(f"-- Export of `{table}` from {source.label}\n")
        fh.write("SET FOREIGN_KEY_CHECKS=0;\n")
        if opts.include_schema:
            ddl = _schema_ddl(source, table)
            if ddl:
                fh.write(f"DROP TABLE IF EXISTS `{table}`;\n{ddl};\n")
        cols: list[str] | None = None
        for row in rows:
            row = _redact(row, redact, opts.redact_with)
            if cols is None:
                cols = list(row.keys())
            collist = ", ".join(f"`{c}`" for c in cols)
            vals = ", ".join(_sql_literal(row.get(c)) for c in cols)
            fh.write(f"INSERT INTO `{table}` ({collist}) VALUES ({vals});\n")
            done += 1
            if progress and done % opts.batch_size == 0:
                progress(table, done, total)
        fh.write("SET FOREIGN_KEY_CHECKS=1;\n")
    if progress:
        progress(table, done, total)
    return path


def _schema_ddl(source: DataSource, table: str) -> str | None:
    """Return ``SHOW CREATE TABLE`` DDL when the source is a live MySQL server."""
    getter = getattr(source, "_cursor", None)
    if getter is None:
        return None
    try:
        with getter() as cur:
            cur.execute(f"SHOW CREATE TABLE `{table.replace('`', '``')}`")
            row = cur.fetchone()
        if row:
            return row.get("Create Table") or next(
                (v for k, v in row.items() if k.lower().startswith("create")), None
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read DDL for %s: %s", table, exc)
    return None


def export_tables(
    source: DataSource,
    tables: Iterable[str],
    opts: ExportOptions,
    progress: ProgressFn | None = None,
) -> list[Path]:
    """Export several tables; returns written file paths."""
    written: list[Path] = []
    for table in tables:
        log.info("Exporting %s -> %s", table, opts.fmt)
        written.append(export_table(source, table, opts, progress))
    return written
