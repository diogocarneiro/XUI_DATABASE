# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""A dump copied into SQLite, so generated fix/renumber/edit SQL can really run in tests."""

from __future__ import annotations

import dataclasses
import sqlite3
from contextlib import contextmanager

from xdbtool.core.dump_source import SqlDumpSource
from xdbtool.core.mysql_source import MySQLSource
from xdbtool.core.source import DataSource, TableInfo


class SqliteSource(DataSource):
    """Tables and rows of a dump in an in-memory SQLite database.

    ``keys`` names each table's auto-increment primary key (reported as ``PRI`` /
    ``auto_increment`` like MySQL does); ``unique`` adds unique indexes.
    """

    def __init__(self, dump, unique: dict[str, tuple[str, ...]] | None = None,
                 keys: dict[str, str] | None = None):
        self.label = "sqlite"
        self.db = sqlite3.connect(":memory:")
        keys = keys or {}
        with SqlDumpSource(dump) as src:
            self.columns = {}
            for t in src.list_tables():
                cols = list(src.table_info(t).columns)
                if t in keys:
                    cols = [dataclasses.replace(c, key="PRI", extra="auto_increment")
                            if c.name == keys[t] else c for c in cols]
                self.columns[t] = cols
                # AUTOINCREMENT: never reuse a deleted id, like MySQL.
                defs = ", ".join(f"`{c.name}` INTEGER PRIMARY KEY AUTOINCREMENT"
                                 if c.key == "PRI"
                                 else f"`{c.name}`" for c in cols)
                self.db.execute(f"CREATE TABLE `{t}` ({defs})")
                marks = ", ".join("?" for _ in cols)
                self.db.executemany(f"INSERT INTO `{t}` VALUES ({marks})",
                                    [tuple(r.values()) for r in src.iter_rows(t)])
        # Unique keys are checked row by row, like MySQL does during an UPDATE.
        for n, (table, cols) in enumerate((unique or {}).items()):
            self.db.execute(f"CREATE UNIQUE INDEX u{n} ON `{table}` ({', '.join(cols)})")
        self.db.commit()  # a later rollback must not undo the set-up itself

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


class _MySQLishCursor:
    """Runs MySQL statements on SQLite: ``<=>`` → ``IS``, no ``FOR UPDATE``."""

    def __init__(self, cur):
        self.cur = cur

    def execute(self, sql, args=None):
        self.cur.execute(sql.replace(" <=> ", " IS ").replace(" FOR UPDATE", ""))
        return self.cur.rowcount

    def fetchall(self):
        return self.cur.fetchall()

    @property
    def lastrowid(self):
        return self.cur.lastrowid


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

    @contextmanager
    def transaction(self):
        with self.inner.db:  # commits on success, rolls back on an exception
            yield _MySQLishCursor(self.inner.db.cursor())

    def run_transaction(self, statements, progress=None):
        with self.inner.db:
            return sum(self.inner.db.execute(s).rowcount for s in statements)

    def execute(self, sql, args=None):
        self.executed.append(sql)  # e.g. ALTER TABLE … AUTO_INCREMENT (MySQL only)
        if sql.startswith("SELECT"):
            cur = self.inner.db.execute(sql)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
        return []
