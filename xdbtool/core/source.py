# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Data-source abstraction.

A :class:`DataSource` exposes a panel database uniformly, whether it comes from
a live MySQL/MariaDB server or from an offline ``mysqldump`` ``.sql`` file. The
GUI and the export/migrate/repair engines only ever talk to this interface.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnInfo:
    """Description of a single column."""

    name: str
    data_type: str
    nullable: bool = True
    key: str = ""           # "PRI", "MUL", "UNI" or ""
    default: str | None = None
    extra: str = ""         # e.g. "auto_increment"


@dataclass(frozen=True)
class TableInfo:
    """Description of a table and (when known) its row count."""

    name: str
    columns: Sequence[ColumnInfo] = field(default_factory=tuple)
    row_count: int | None = None
    engine: str | None = None
    collation: str | None = None


@dataclass(frozen=True)
class Page:
    """One page of a table's rows, as returned by :meth:`DataSource.fetch_page`."""

    columns: Sequence[str]
    rows: Sequence[tuple]
    total: int       # rows matching the search (all pages)
    offset: int = 0


class SourceError(RuntimeError):
    """Raised for any recoverable data-source problem (connection, parse, query)."""


def cell_text(value: object) -> str:
    """A value as searchable/displayable text (bytes decoded, None as '')."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def _sort_key(value: object) -> tuple:
    """Order NULLs first, numbers numerically, everything else as text."""
    if value is None:
        return (0, 0, "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (1, value, "")
    return (2, 0, cell_text(value).lower())


class DataSource(abc.ABC):
    """Read interface common to every source.

    Implementations must be usable as context managers so resources (sockets,
    file handles) are released deterministically.
    """

    #: Human-readable label for the UI, e.g. "MySQL 10.5 @ host" or a file name.
    label: str = "source"

    #: True when the source supports server-side maintenance (CHECK/REPAIR).
    supports_repair: bool = False

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> DataSource:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @abc.abstractmethod
    def open(self) -> None:
        ...

    @abc.abstractmethod
    def close(self) -> None:
        ...

    # -- introspection -----------------------------------------------------
    @abc.abstractmethod
    def list_tables(self) -> list[str]:
        """Return table names in the database."""

    @abc.abstractmethod
    def table_info(self, table: str) -> TableInfo:
        """Return column metadata and, if cheap, the row count for ``table``."""

    @abc.abstractmethod
    def row_count(self, table: str) -> int:
        ...

    # -- data --------------------------------------------------------------
    @abc.abstractmethod
    def iter_rows(self, table: str, batch_size: int = 1000) -> Iterator[dict]:
        """Yield rows of ``table`` as ``{column: value}`` dicts, streaming."""

    def iter_columns(self, table: str, columns: Sequence[str],
                     batch_size: int = 1000) -> Iterator[tuple]:
        """Yield only ``columns`` of each row, as tuples (sources may read less)."""
        for row in self.iter_rows(table, batch_size):
            yield tuple(row.get(c) for c in columns)

    def _page_columns(self, table: str, columns, *named) -> tuple[list[str], list[str]]:
        """(all columns, shown columns); raises for an unknown column name."""
        every = [c.name for c in self.table_info(table).columns]
        shown = list(columns) if columns else every
        unknown = [c for c in (*shown, *named) if c and c not in every]
        if unknown:
            raise SourceError(f"Unknown column(s) in `{table}`: {', '.join(unknown)}")
        return every, shown

    def fetch_page(
        self,
        table: str,
        *,
        offset: int = 0,
        limit: int = 100,
        search: str = "",
        column: str | None = None,
        columns: Sequence[str] | None = None,
        order_by: str | None = None,
        descending: bool = False,
    ) -> Page:
        """Rows ``offset``..``offset+limit`` of ``table``.

        ``search`` keeps rows where ``column`` (or any column) contains the text,
        ignoring case; ``order_by`` sorts by a column; ``columns`` picks the columns
        returned. This default reads the rows in Python; live sources do it in SQL.
        """
        _every, shown = self._page_columns(table, columns, column, order_by)
        needle = search.lower()
        if not needle and not order_by:
            rows = []
            for n, row in enumerate(self.iter_rows(table)):
                if n >= offset + limit:
                    break
                if n >= offset:
                    rows.append(tuple(row.get(c) for c in shown))
            return Page(shown, rows, self.row_count(table), offset)
        matched = []
        for row in self.iter_rows(table):
            if needle:
                values = [row.get(column)] if column else row.values()
                if not any(needle in cell_text(v).lower() for v in values):
                    continue
            matched.append(row)
        if order_by:
            matched.sort(key=lambda r: _sort_key(r.get(order_by)), reverse=descending)
        rows = [tuple(r.get(c) for c in shown) for r in matched[offset:offset + limit]]
        return Page(shown, rows, len(matched), offset)

    # -- optional server info ---------------------------------------------
    def server_info(self) -> dict[str, str]:
        """Return a small dict of server/source facts for display."""
        return {"label": self.label}
