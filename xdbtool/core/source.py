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


class SourceError(RuntimeError):
    """Raised for any recoverable data-source problem (connection, parse, query)."""


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

    # -- optional server info ---------------------------------------------
    def server_info(self) -> dict[str, str]:
        """Return a small dict of server/source facts for display."""
        return {"label": self.label}
