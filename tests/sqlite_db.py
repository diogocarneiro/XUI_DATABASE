# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""A dump copied into SQLite, so generated fix/renumber SQL can really run in tests."""

from __future__ import annotations

import sqlite3

from xdbtool.core.dump_source import SqlDumpSource
from xdbtool.core.mysql_source import MySQLSource
from xdbtool.core.source import DataSource, TableInfo


class SqliteSource(DataSource):
    """Tables and rows of a dump in an in-memory SQLite database."""

    def __init__(self, dump, unique: dict[str, tuple[str, ...]] | None = None):
        self.label = "sqlite"
        self.db = sqlite3.connect(":memory:")
        with SqlDumpSource(dump) as src:
            self.columns = {t: list(src.table_info(t).columns) for t in src.list_tables()}
            for t, cols in self.columns.items():
                self.db.execute(f"CREATE TABLE `{t}` ({', '.join(f'`{c.name}`' for c in cols)})")
                marks = ", ".join("?" for _ in cols)
                self.db.executemany(f"INSERT INTO `{t}` VALUES ({marks})",
                                    [tuple(r.values()) for r in src.iter_rows(t)])
        # Unique keys are checked row by row, like MySQL does during an UPDATE.
        for n, (table, cols) in enumerate((unique or {}).items()):
            self.db.execute(f"CREATE UNIQUE INDEX u{n} ON `{table}` ({', '.join(cols)})")

    def open(self):
        pass

    def close(self):
        pass

    def list_tables(self):
        return sorted(self.columns)

    def table_info(self, table):
        return TableInfo(table, tuple(self.columns[table]))

    def row_count(self, table):
        return self.db.execute(f"SELECT COUNT(*) FROM `{table}`").fetchone()[0]

    def iter_rows(self, table, batch_size=1000):
        names = [c.name for c in self.columns[table]]
        for row in self.db.execute(f"SELECT * FROM `{table}`"):
            yield dict(zip(names, row, strict=True))

    def column(self, table, column, order="rowid"):
        return [r[0] for r in self.db.execute(
            f"SELECT `{column}` FROM `{table}` ORDER BY {order}")]


class FakeMySQL(MySQLSource):
    """MySQLSource over SQLite: the apply functions only accept live MySQL sources."""

    def __init__(self, inner: SqliteSource):  # no real connection
        self.inner, self.label, self._conn = inner, "fake-mysql", None
        self.executed: list[str] = []

    def list_tables(self):
        return self.inner.list_tables()

    def table_info(self, table):
        return self.inner.table_info(table)

    def row_count(self, table):
        return self.inner.row_count(table)

    def iter_rows(self, table, batch_size=1000):
        return self.inner.iter_rows(table, batch_size)

    def iter_columns(self, table, columns, batch_size=1000):
        return self.inner.iter_columns(table, columns, batch_size)

    def run_transaction(self, statements, progress=None):
        with self.inner.db:
            return sum(self.inner.db.execute(s).rowcount for s in statements)

    def execute(self, sql, args=None):
        self.executed.append(sql)  # e.g. ALTER TABLE … AUTO_INCREMENT (MySQL only)
        return []
