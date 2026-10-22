# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Offline data source: read tables and rows from a ``mysqldump`` ``.sql`` file.

This handles the common output of ``mysqldump`` (both extended and one-row-per-
statement inserts). It is a pragmatic parser, not a full SQL engine: it extracts
``CREATE TABLE`` column lists and ``INSERT ... VALUES`` tuples, respecting single-
quoted strings with backslash escapes. The whole dump is parsed into memory on
``open()``; for very large dumps use the live MySQL connection instead.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from .logger import get_logger
from .source import ColumnInfo, DataSource, SourceError, TableInfo

log = get_logger(__name__)

_CREATE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([^`\s(]+)`?\s*\(", re.I)
_INSERT_RE = re.compile(
    r"INSERT\s+(?:IGNORE\s+)?INTO\s+`?([^`\s(]+)`?\s*(?:\(([^)]*)\))?\s*VALUES", re.I
)
_COL_RE = re.compile(r"^\s*`([^`]+)`\s+([a-zA-Z0-9_]+)", re.M)

# Column-definition lines start with a backtick name; these keywords start
# constraint/index lines we must skip when reading a CREATE TABLE body.
_NON_COLUMN = re.compile(
    r"^\s*(PRIMARY\s+KEY|UNIQUE\s+KEY|UNIQUE|KEY|INDEX|CONSTRAINT|FOREIGN\s+KEY|FULLTEXT|SPATIAL)\b",
    re.I,
)


def read_sql_text(path: str | Path, encoding: str = "utf-8") -> str:
    """Read a ``.sql`` dump, transparently decompressing ``.gz`` files."""
    path = Path(path)
    with path.open("rb") as fh:
        gz = fh.read(2) == b"\x1f\x8b"
    if gz:
        import gzip

        with gzip.open(path, "rt", encoding=encoding, errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding=encoding, errors="replace")


def _iter_statements(text: str) -> Iterator[str]:
    """Yield top-level SQL statements, respecting quotes, escapes and comments."""
    buf: list[str] = []
    i, n = 0, len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:          # backslash-escaped char
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote and i + 1 < n and text[i + 1] == quote:  # doubled quote
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        # not inside a string
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and text[i : i + 2] == "--":  # line comment
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch == "/" and text[i : i + 2] == "/*":  # block comment
            j = text.find("*/", i)
            i = n if j == -1 else j + 2
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                yield stmt
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        yield tail


def _parse_columns(create_stmt: str) -> list[ColumnInfo]:
    """Extract column names/types from a CREATE TABLE statement body."""
    start = create_stmt.find("(")
    if start == -1:
        return []
    body = create_stmt[start + 1 :]
    cols: list[ColumnInfo] = []
    for line in body.splitlines():
        if _NON_COLUMN.match(line):
            continue
        m = _COL_RE.match(line)
        if m:
            cols.append(ColumnInfo(name=m.group(1), data_type=m.group(2).lower(),
                                   nullable="NOT NULL" not in line.upper()))
    return cols


def _convert(raw: str) -> object:
    """Convert a single raw SQL literal to a Python value."""
    s = raw.strip()
    if s == "NULL":
        return None
    if s and s[0] == "'" and s[-1] == "'":
        inner = s[1:-1]
        return _unescape(inner)
    # numeric
    try:
        if "." in s or "e" in s or "E" in s:
            return float(s)
        return int(s)
    except ValueError:
        return _unescape(s)


_ESCAPES = {
    "0": "\0", "n": "\n", "r": "\r", "t": "\t", "b": "\b",
    "Z": "\x1a", "\\": "\\", "'": "'", '"': '"',
}


def _unescape(s: str) -> str:
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            out.append(_ESCAPES.get(s[i + 1], s[i + 1]))
            i += 2
            continue
        if ch == "'" and i + 1 < n and s[i + 1] == "'":  # doubled single quote
            out.append("'")
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_tuples(values_blob: str) -> Iterator[list[str]]:
    """Split a VALUES blob ``(a,b),(c,d)`` into lists of raw value strings."""
    i, n = 0, len(values_blob)
    while i < n:
        while i < n and values_blob[i] != "(":
            i += 1
        if i >= n:
            break
        i += 1  # skip '('
        values: list[str] = []
        cur: list[str] = []
        quote: str | None = None
        while i < n:
            ch = values_blob[i]
            if quote:
                cur.append(ch)
                if ch == "\\" and i + 1 < n:
                    cur.append(values_blob[i + 1])
                    i += 2
                    continue
                if ch == quote and i + 1 < n and values_blob[i + 1] == quote:  # doubled quote
                    cur.append(values_blob[i + 1])
                    i += 2
                    continue
                if ch == quote:
                    quote = None
                i += 1
                continue
            if ch == "'":
                quote = ch
                cur.append(ch)
                i += 1
                continue
            if ch == ",":
                values.append("".join(cur))
                cur = []
                i += 1
                continue
            if ch == ")":
                values.append("".join(cur))
                i += 1
                yield values
                break
            cur.append(ch)
            i += 1


class SqlDumpSource(DataSource):
    """Read-only source parsed from a mysqldump .sql file (loaded into memory)."""

    supports_repair = False

    def __init__(self, path: str | Path, encoding: str = "utf-8"):
        self.path = Path(path)
        self.encoding = encoding
        self.label = self.path.name
        self._columns: dict[str, list[ColumnInfo]] = {}
        # Each row keeps the column list of its INSERT (None: the CREATE TABLE order),
        # so INSERTs naming only some columns, or in another order, read correctly.
        self._rows: dict[str, list[tuple[tuple[str, ...] | None, list[object]]]] = {}
        self._loaded = False

    def open(self) -> None:
        if self._loaded:
            return
        if not self.path.is_file():
            raise SourceError(f"Dump file not found: {self.path}")
        try:
            text = read_sql_text(self.path, self.encoding)
        except OSError as exc:
            raise SourceError(f"Could not read dump: {exc}") from exc

        for stmt in _iter_statements(text):
            head = stmt.lstrip()[:12].upper()
            if head.startswith("CREATE TABLE"):
                m = _CREATE_RE.match(stmt.lstrip())
                if m:
                    self._columns[m.group(1)] = _parse_columns(stmt)
            elif head.startswith("INSERT"):
                m = _INSERT_RE.match(stmt.lstrip())
                if not m:
                    continue
                table = m.group(1)
                explicit_cols = m.group(2)
                blob = stmt[m.end() :]
                names = (tuple(c.strip().strip("`") for c in explicit_cols.split(","))
                         if explicit_cols else None)
                rows = self._rows.setdefault(table, [])
                for values in _split_tuples(blob):
                    rows.append((names, [_convert(v) for v in values]))
                if names and table not in self._columns:
                    self._columns[table] = [ColumnInfo(name=x, data_type="") for x in names]
        self._loaded = True
        log.info("Parsed dump %s: %d tables", self.label, len(self.list_tables()))

    def close(self) -> None:
        self._columns.clear()
        self._rows.clear()
        self._loaded = False

    def list_tables(self) -> list[str]:
        return sorted(set(self._columns) | set(self._rows))

    def table_info(self, table: str) -> TableInfo:
        cols = self._columns.get(table, [])
        return TableInfo(name=table, columns=tuple(cols), row_count=len(self._rows.get(table, [])))

    def row_count(self, table: str) -> int:
        return len(self._rows.get(table, []))

    def iter_rows(self, table: str, batch_size: int = 1000) -> Iterator[dict]:
        create = tuple(c.name for c in self._columns.get(table, []))
        for names, row in self._rows.get(table, []):
            keys = names or create
            if keys and len(keys) == len(row):
                yield dict(zip(keys, row, strict=True))
            else:  # fall back to positional column names
                yield {f"col_{idx}": v for idx, v in enumerate(row)}

    def server_info(self) -> dict[str, str]:
        return {"label": self.label, "source": "sql-dump", "path": str(self.path)}
