# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Edit table rows on a live server: update, insert, delete, import from CSV/JSON.

Changes are collected first and applied together in one transaction. Every
``UPDATE`` also checks that the edited cells still hold the values that were loaded
(so a change made meanwhile by the panel is never overwritten silently), and an
undo script with the exact previous values is written next to the backups.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..i18n import _
from .exporter import _sql_literal
from .logger import get_logger
from .mysql_source import MySQLSource, _quote_ident
from .source import ColumnInfo, DataSource, SourceError, TableInfo, cell_text

log = get_logger(__name__)

UPDATE, INSERT, DELETE = "update", "insert", "delete"
INT_TYPES = frozenset({"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "year"})
NUM_TYPES = frozenset({"decimal", "numeric", "float", "double", "real"})
_INT_RE = re.compile(r"[+-]?\d+")


class EditError(Exception):
    """The changes cannot be applied (nothing was changed)."""


@dataclass
class Change:
    kind: str
    values: dict = field(default_factory=dict)  # UPDATE: new values; INSERT: given values
    key: dict = field(default_factory=dict)     # UPDATE/DELETE: primary key of the row
    old: dict = field(default_factory=dict)     # UPDATE: previous values; DELETE: whole row


# ----------------------------------------------------------------- columns
def key_columns(info: TableInfo) -> list[str]:
    """Primary-key columns ([] when the table has none: it cannot be edited)."""
    return [c.name for c in info.columns if c.key == "PRI"]


def auto_increment(info: TableInfo) -> str | None:
    return next((c.name for c in info.columns if "auto_increment" in c.extra.lower()), None)


def editable_reason(source: DataSource, info: TableInfo) -> str:
    """Why ``info`` cannot be edited on ``source`` ('' when it can)."""
    if not isinstance(source, MySQLSource):
        return _("Read-only: editing needs a live MySQL/MariaDB connection.")
    if not key_columns(info):
        return _("Read-only: this table has no primary key, so rows cannot be told apart.")
    return ""


def required_columns(info: TableInfo) -> list[str]:
    """Columns an INSERT must give: NOT NULL, no default, not auto-increment."""
    return [c.name for c in info.columns
            if not c.nullable and c.default is None and "auto_increment" not in c.extra.lower()]


def coerce(text: str | None, col: ColumnInfo) -> object:
    """The value typed for ``col`` (None = NULL); raises ValueError when invalid."""
    kind = col.data_type.lower()
    if text is None or (text.strip() == "" and kind in INT_TYPES | NUM_TYPES):
        if not col.nullable:
            raise ValueError(_("{column} cannot be empty (NULL).", column=col.name))
        return None
    if kind in INT_TYPES:
        if not _INT_RE.fullmatch(text.strip()):
            raise ValueError(_("{column} needs a whole number.", column=col.name))
        return int(text.strip())
    if kind in NUM_TYPES:
        try:
            return Decimal(text.strip())
        except InvalidOperation:
            raise ValueError(_("{column} needs a number.", column=col.name)) from None
    return text


def is_json_container(value: object) -> bool:
    """True when ``value`` is a JSON list or object (e.g. bouquet channel lists)."""
    text = cell_text(value).strip()
    if text[:1] not in ("[", "{"):
        return False
    try:
        return isinstance(json.loads(text), (list, dict))
    except ValueError:
        return False


def looks_hashed(value: object) -> bool:
    """True for crypt-style password hashes such as ``$6$salt$hash`` or ``$2y$10$…``."""
    text = cell_text(value)
    return text.startswith("$") and text.count("$") >= 3 and " " not in text


def check_value(col: ColumnInfo, old: object, new: object) -> tuple[str, str]:
    """(error, warning) for replacing ``old`` with ``new`` in ``col`` ('' when fine)."""
    if old is not None and is_json_container(old) and new is not None \
            and not is_json_container(new):
        return _("{column} holds a JSON list; the new value must be valid JSON too, e.g. "
                 "[1,2,3].", column=col.name), ""
    if looks_hashed(old) and new is not None and not looks_hashed(new):
        return "", _("{column} holds a password hash; text typed here is stored as is and "
                     "will not work as a password. Change it anyway?", column=col.name)
    return "", ""


# --------------------------------------------------------------------- SQL
def _where(key: dict) -> str:
    return " AND ".join(f"{_quote_ident(c)} = {_sql_literal(v)}" for c, v in key.items())


def check_sql(table: str, change: Change) -> str | None:
    """SELECT that locks the row and confirms it still holds the loaded values."""
    if change.kind == INSERT:
        return None
    same = "".join(f" AND {_quote_ident(c)} <=> {_sql_literal(change.old.get(c))}"
                   for c in change.values) if change.kind == UPDATE else ""
    return (f"SELECT 1 FROM {_quote_ident(table)} WHERE {_where(change.key)}{same} "
            "FOR UPDATE")


def change_sql(table: str, change: Change) -> str:
    t = _quote_ident(table)
    if change.kind == UPDATE:
        sets = ", ".join(f"{_quote_ident(c)} = {_sql_literal(v)}" for c, v in change.values.items())
        return f"UPDATE {t} SET {sets} WHERE {_where(change.key)}"
    if change.kind == DELETE:
        return f"DELETE FROM {t} WHERE {_where(change.key)}"
    if change.kind == INSERT:
        cols = ", ".join(_quote_ident(c) for c in change.values)
        vals = ", ".join(_sql_literal(v) for v in change.values.values())
        return f"INSERT INTO {t} ({cols}) VALUES ({vals})"
    raise ValueError(f"unknown change {change.kind!r}")


def undo_sql(table: str, change: Change, key: dict | None = None) -> str:
    """SQL that reverses ``change`` once applied (``key`` of an inserted row)."""
    t = _quote_ident(table)
    if change.kind == UPDATE:
        sets = ", ".join(f"{_quote_ident(c)} = {_sql_literal(change.old.get(c))}"
                         for c in change.values)
        return f"UPDATE {t} SET {sets} WHERE {_where(change.key)}"
    if change.kind == DELETE:
        return change_sql(table, Change(INSERT, dict(change.old)))
    if key:
        return f"DELETE FROM {t} WHERE {_where(key)}"
    return f"-- cannot undo an INSERT into {table}: its key is unknown"


def summary(changes: Sequence[Change]) -> dict[str, int]:
    return {kind: sum(1 for c in changes if c.kind == kind) for kind in (UPDATE, INSERT, DELETE)}


def apply_changes(source: DataSource, table: str, changes: Sequence[Change], *,
                  backup_dir: str | Path) -> dict:
    """Apply ``changes`` in one transaction; write the undo script; return the outcome."""
    if not isinstance(source, MySQLSource):
        raise SourceError(_("Read-only: editing needs a live MySQL/MariaDB connection."))
    info = source.table_info(table)
    keys, auto = key_columns(info), auto_increment(info)
    if not keys:
        raise EditError(_("Read-only: this table has no primary key, so rows cannot be told "
                          "apart."))
    for n, ch in enumerate(changes, 1):
        missing = [c for c in required_columns(info) if c not in ch.values]
        if ch.kind == INSERT and missing:
            raise EditError(_("New row {number}: fill in {columns} (they have no default).",
                              number=n, columns=", ".join(missing)))
    undo: list[str] = []
    with source.transaction() as cur:
        for ch in changes:
            check = check_sql(table, ch)
            if check is not None:
                cur.execute(check)
                if len(cur.fetchall()) != 1:
                    raise EditError(_(
                        "A row was changed or deleted by someone else since it was loaded "
                        "({where}); nothing was saved. Reload and try again.",
                        where=_where(ch.key)))
            sql = change_sql(table, ch)
            try:
                cur.execute(sql)
            except Exception as exc:  # noqa: BLE001 - reported, transaction rolled back
                raise EditError(_("The server refused a change, nothing was saved: {error}",
                                  error=exc) + f"\n--- SQL ---\n{sql[:300]}") from exc
            key = None
            if ch.kind == INSERT:
                key = {c: ch.values[c] for c in keys if c in ch.values}
                if len(key) < len(keys) and keys == [auto]:
                    key = {auto: cur.lastrowid}
            undo.append(undo_sql(table, ch, key))
    path = Path(backup_dir)
    path.mkdir(parents=True, exist_ok=True)
    path = path / f"edit-{table}-{time.strftime('%Y%m%d-%H%M%S')}.undo.sql"
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"-- Generated by XUI DB Tool: undo the changes saved to `{table}` on "
                 f"{source.label}.\nSTART TRANSACTION;\n")
        for stmt in reversed(undo):
            fh.write(stmt + (";\n" if not stmt.startswith("--") else "\n"))
        fh.write("COMMIT;\n")
    counts = summary(changes)
    log.info("Saved %s to %s; undo script %s", counts, table, path)
    return {"counts": counts, "undo": path}


def fetch_row(source: MySQLSource, table: str, key: dict) -> dict | None:
    rows = source.execute(f"SELECT * FROM {_quote_ident(table)} WHERE {_where(key)} LIMIT 2")
    if len(rows) > 1:
        raise EditError(_("The key {where} matches more than one row.", where=_where(key)))
    return rows[0] if rows else None


# ------------------------------------------------------------------ import
def read_rows_file(path: str | Path) -> list[dict]:
    """Rows of a CSV (header line; , ; or tab separated) or JSON (list of objects) file."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise EditError(_("The JSON file must hold a list of objects: one per row, with "
                              "the column names as keys."))
        return data
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return list(csv.DictReader(io.StringIO(text), dialect=dialect))


def _import_value(raw: object, col: ColumnInfo) -> object:
    if raw is None or isinstance(raw, str):
        return coerce(raw, col)
    if isinstance(raw, (list, dict)):  # JSON file: e.g. "bouquet_channels": [1, 2, 3]
        return json.dumps(raw, separators=(",", ":"))
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)) and col.data_type.lower() in INT_TYPES | NUM_TYPES:
        return coerce(str(raw), col)
    return str(raw)


def import_changes(rows: Iterable[dict], info: TableInfo) -> tuple[list[Change], list[str]]:
    """INSERT changes for ``rows`` (columns matched by name); also the unknown columns.

    Empty CSV cells are left out, so the column gets its default; JSON ``null`` is
    NULL. Raises EditError naming the first invalid row.
    """
    cols = {c.name.lower(): c for c in info.columns}
    unknown: dict[str, None] = {}
    changes = []
    for n, row in enumerate(rows, 1):
        values = {}
        for name, raw in row.items():
            col = cols.get(str(name).strip().lower())
            if col is None:
                unknown[str(name)] = None
                continue
            if raw == "":
                continue
            try:
                values[col.name] = _import_value(raw, col)
            except ValueError as exc:
                raise EditError(_("Row {number}: {error}", number=n, error=exc)) from None
        changes.append(Change(INSERT, values))
    return changes, list(unknown)
