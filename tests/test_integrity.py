# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Tests for the integrity check and its fixes (offline: dumps + SQLite)."""

from __future__ import annotations

import sqlite3

import pytest

from xdbtool.__main__ import main
from xdbtool.core import integrity as I
from xdbtool.core.dump_source import SqlDumpSource
from xdbtool.core.mysql_source import MySQLSource
from xdbtool.core.source import ColumnInfo, DataSource, SourceError, TableInfo

BROKEN_XUI = """\
CREATE TABLE `servers` (
  `id` int(11) NOT NULL,
  `server_name` varchar(255)
);
INSERT INTO `servers` VALUES (1,'Main');
CREATE TABLE `streams_categories` (
  `id` int(11) NOT NULL,
  `category_name` varchar(255),
  `parent_id` int(11) NOT NULL DEFAULT 0
);
INSERT INTO `streams_categories` VALUES (1,'News',0),(2,'Films',9);
CREATE TABLE `streams` (
  `id` int(11) NOT NULL,
  `type` int(11),
  `category_id` text,
  `tv_archive_server_id` int(11) NOT NULL DEFAULT 0
);
INSERT INTO `streams` VALUES (300,1,'[1]',0),(301,1,'[1,7]',4),(302,2,'2',0),(303,5,'8',0);
CREATE TABLE `streams_servers` (
  `server_stream_id` int(11) NOT NULL,
  `stream_id` int(11),
  `server_id` int(11),
  `parent_id` int(11)
);
INSERT INTO `streams_servers` VALUES (1,300,1,NULL),(2,301,1,5),(3,250,1,NULL),(4,302,3,NULL);
CREATE TABLE `streams_episodes` (
  `id` int(11) NOT NULL,
  `series_id` int(11),
  `stream_id` int(11)
);
CREATE TABLE `streams_series` (
  `id` int(11) NOT NULL
);
CREATE TABLE `bouquets` (
  `id` int(11) NOT NULL,
  `bouquet_channels` text,
  `bouquet_movies` text
);
INSERT INTO `bouquets` VALUES (1,'[300,301,301,12,13]','["302","400"]'),(2,'not json','[]');
CREATE TABLE `lines` (
  `id` int(11) NOT NULL,
  `username` varchar(50),
  `bouquet` text
);
INSERT INTO `lines` VALUES (1,'alice','[1,2]'),(2,'Alice','[1,5]'),(3,'bob','[1,5]');
CREATE TABLE `mag_devices` (
  `mag_id` int(11) NOT NULL,
  `user_id` int(11),
  `mac` varchar(50)
);
INSERT INTO `mag_devices` VALUES (1,1,'AA'),(2,99,'BB');
CREATE TABLE `mag_events` (
  `id` int(11) NOT NULL,
  `mag_device_id` int(11)
);
INSERT INTO `mag_events` VALUES (1,2);
"""


@pytest.fixture
def broken(tmp_path):
    path = tmp_path / "broken.sql"
    path.write_text(BROKEN_XUI, encoding="utf-8")
    return path


def _issues(report):
    return {(i.kind, f"{i.table}.{i.column}"): i for i in report.issues
            if i.kind != I.CHILDLESS}


def test_finds_every_planted_problem(broken):
    with SqlDumpSource(broken) as src:
        report = I.check_integrity(src)
    assert report.family == "xui_one"
    got = _issues(report)
    expect = {
        (I.ORPHAN, "streams.category_id"): (2, I.PRUNE, [7, 8]),
        (I.ORPHAN, "streams.tv_archive_server_id"): (1, I.CLEAR, [4]),
        (I.ORPHAN, "streams_servers.stream_id"): (1, I.DELETE, [250]),
        (I.ORPHAN, "streams_servers.server_id"): (1, I.DELETE, [3]),
        (I.ORPHAN, "streams_servers.parent_id"): (1, I.CLEAR, [5]),
        (I.ORPHAN, "streams_categories.parent_id"): (1, I.CLEAR, [9]),
        (I.ORPHAN, "bouquets.bouquet_channels"): (1, I.PRUNE, [12, 13]),
        (I.ORPHAN, "bouquets.bouquet_movies"): (1, I.PRUNE, [400]),
        (I.REPEATED, "bouquets.bouquet_channels"): (1, I.DEDUPE, []),
        (I.INVALID, "bouquets.bouquet_channels"): (1, "", ["not json"]),
        (I.ORPHAN, "lines.bouquet"): (2, I.PRUNE, [5]),
        (I.ORPHAN, "mag_devices.user_id"): (1, I.DELETE, [99]),
        (I.DUPLICATE, "lines.username"): (1, "", ["alice"]),
    }
    assert {k: (i.rows, i.fix, i.sample) for k, i in got.items()} == expect
    # mag_events → mag_devices is fine (device 2 exists until the fix deletes it).
    assert (I.ORPHAN, "mag_events.mag_device_id") not in got
    # Stream 303 runs on no server, and as an episode (type 5) belongs to no series.
    lonely = [(i.target, i.rows, i.sample, i.fix) for i in report.issues
              if i.kind == I.CHILDLESS]
    assert lonely == [("streams_servers", 1, [303], ""), ("streams_episodes", 1, [303], "")]
    assert lonely_text(report) == ["1 stream(s) not assigned to any server (they cannot play)",
                                   "1 series episode(s) not linked to any series"]


def lonely_text(report):
    return [i.describe() for i in report.issues if i.kind == I.CHILDLESS]


def test_fix_statements(broken):
    with SqlDumpSource(broken) as src:
        report = I.check_integrity(src)
    assert I.fix_statements(report) == [
        "UPDATE `streams` SET `category_id` = '[1]' WHERE `category_id` = '[1,7]'",
        "UPDATE `streams` SET `category_id` = NULL WHERE `category_id` = '8'",
        "UPDATE `bouquets` SET `bouquet_channels` = '[300,301]' "
        "WHERE `bouquet_channels` = '[300,301,301,12,13]'",
        "UPDATE `bouquets` SET `bouquet_movies` = '[\"302\"]' "
        "WHERE `bouquet_movies` = '[\"302\",\"400\"]'",
        "UPDATE `lines` SET `bouquet` = '[1]' WHERE `bouquet` = '[1,5]'",
        "UPDATE `streams` SET `tv_archive_server_id` = 0 WHERE `tv_archive_server_id` IN (4)",
        "DELETE FROM `streams_servers` WHERE `stream_id` IN (250)",
        "DELETE FROM `streams_servers` WHERE `server_id` IN (3)",
        "UPDATE `streams_servers` SET `parent_id` = NULL WHERE `parent_id` IN (5)",
        "UPDATE `streams_categories` SET `parent_id` = 0 WHERE `parent_id` IN (9)",
        "DELETE FROM `mag_devices` WHERE `user_id` IN (99)",
    ]


def test_only_chosen_issues_are_fixed(broken):
    with SqlDumpSource(broken) as src:
        report = I.check_integrity(src)
    got = _issues(report)
    dedupe_only = I.fix_statements(report, [got[(I.REPEATED, "bouquets.bouquet_channels")]])
    assert dedupe_only == ["UPDATE `bouquets` SET `bouquet_channels` = '[300,301,12,13]' "
                           "WHERE `bouquet_channels` = '[300,301,301,12,13]'"]
    # Report-only issues never produce SQL.
    assert I.fix_statements(report, [got[(I.DUPLICATE, "lines.username")]]) == []


@pytest.mark.parametrize("raw, expected", [
    (None, ([], True)), ("", ([], True)), (0, ([], False)), ("0", ([], False)),
    (7, ([7], False)), ("7", ([7], False)), ("[1,2]", ([1, 2], True)),
    ('["1","2"]', (["1", "2"], True)), ("[]", ([], True)),
    ("not json", None), ('{"a":1}', None), ('["x"]', None), ("[[1]]", None),
])
def test_parse_id_list(raw, expected):
    assert I.parse_id_list(raw) == expected


def test_clean_id_list_keeps_unchanged_values_and_text_ids():
    gone = frozenset({5})
    assert I.clean_id_list("[1, 2]", gone, prune=True, dedupe=True, empty_scalar=None) == "[1, 2]"
    assert I.clean_id_list('["1","5","1"]', gone, prune=True, dedupe=True,
                           empty_scalar=None) == '["1"]'
    assert I.clean_id_list("5", gone, prune=True, dedupe=False, empty_scalar=0) == 0
    assert I.clean_id_list("[5]", gone, prune=True, dedupe=False, empty_scalar=0) == "[]"
    assert I.clean_id_list("junk", gone, prune=True, dedupe=True, empty_scalar=0) == "junk"


def test_empty_target_table_is_reported_but_never_fixed(tmp_path):
    dump = tmp_path / "partial.sql"
    dump.write_text(BROKEN_XUI.replace("INSERT INTO `servers` VALUES (1,'Main');\n", ""),
                    encoding="utf-8")
    with SqlDumpSource(dump) as src:
        report = I.check_integrity(src)
    got = _issues(report)
    assert got[(I.ORPHAN, "streams_servers.server_id")].rows == 4
    assert got[(I.ORPHAN, "streams_servers.server_id")].fix == ""
    assert "DELETE FROM `streams_servers` WHERE `server_id`" not in "\n".join(
        I.fix_statements(report))


def test_xtream_family_uses_its_own_tables(tmp_path):
    dump = tmp_path / "xtream.sql"
    dump.write_text("""\
CREATE TABLE `bouquets` (
  `id` int(11) NOT NULL,
  `bouquet_channels` text
);
INSERT INTO `bouquets` VALUES (1,'[10,11]');
CREATE TABLE `streams` (
  `id` int(11) NOT NULL,
  `category_id` int(11)
);
INSERT INTO `streams` VALUES (10,3);
CREATE TABLE `stream_categories` (
  `id` int(11) NOT NULL
);
INSERT INTO `stream_categories` VALUES (1);
CREATE TABLE `users` (
  `id` int(11) NOT NULL,
  `username` varchar(50),
  `bouquet` text
);
INSERT INTO `users` VALUES (1,'line1','[1,2]');
""", encoding="utf-8")
    with SqlDumpSource(dump) as src:
        report = I.check_integrity(src)
    assert report.family == "xtream"
    got = _issues(report)
    assert got[(I.ORPHAN, "bouquets.bouquet_channels")].sample == [11]
    assert got[(I.ORPHAN, "streams.category_id")].sample == [3]
    assert got[(I.ORPHAN, "users.bouquet")].sample == [2]
    assert "UPDATE `streams` SET `category_id` = NULL WHERE `category_id` = 3" in (
        I.fix_statements(report))


def test_clean_fixture_has_no_problems():
    with SqlDumpSource("tests/fixtures/sample_xtream.sql") as src:
        report = I.check_integrity(src)
    assert report.issues == []
    assert report.summary_lines()[-1] == "No problems found."


# --------------------------------------------------------- applying the fixes
class SqliteSource(DataSource):
    """A dump copied into SQLite, so the generated fix SQL can really run."""

    def __init__(self, dump):
        self.label = "sqlite"
        self.db = sqlite3.connect(":memory:")
        with SqlDumpSource(dump) as src:
            self.columns = {t: list(src.table_info(t).columns) for t in src.list_tables()}
            for t, cols in self.columns.items():
                self.db.execute(f"CREATE TABLE `{t}` ({', '.join(f'`{c.name}`' for c in cols)})")
                marks = ", ".join("?" for _ in cols)
                self.db.executemany(f"INSERT INTO `{t}` VALUES ({marks})",
                                    [tuple(r.values()) for r in src.iter_rows(t)])

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


class FakeMySQL(MySQLSource):
    """MySQLSource over SQLite: apply_fixes only accepts live MySQL sources."""

    def __init__(self, inner: SqliteSource):  # no real connection
        self.inner, self.label, self._conn = inner, "fake-mysql", None

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


def test_fixes_run_and_a_second_check_finds_the_uncovered_orphan(broken, tmp_path):
    src = FakeMySQL(SqliteSource(broken))
    report = I.check_integrity(src)
    result = I.apply_fixes(src, report, backup_dir=tmp_path / "bk")
    assert result["statements"] == 11 and result["changed"] == 12
    backup = result["backup"]
    assert sorted(p.name for p in backup.iterdir()) == [
        "bouquets.sql", "lines.sql", "mag_devices.sql", "streams.sql",
        "streams_categories.sql", "streams_servers.sql"]
    assert "VALUES (2, 99, 'BB')" in (backup / "mag_devices.sql").read_text()

    again = I.check_integrity(src)
    left = _issues(again)
    # Deleting device 2 orphaned its event: found on the second pass.
    assert left[(I.ORPHAN, "mag_events.mag_device_id")].sample == [2]
    assert {k for k, i in left.items() if i.fixable} == {(I.ORPHAN, "mag_events.mag_device_id")}
    I.apply_fixes(src, again, backup_dir=tmp_path / "bk")
    assert all(not i.fixable for i in I.check_integrity(src).issues)
    db = src.inner.db
    assert db.execute("SELECT bouquet_channels FROM bouquets WHERE id = 1").fetchone() == (
        "[300,301]",)
    assert db.execute("SELECT category_id FROM streams WHERE id = 303").fetchone() == (None,)


def test_apply_fixes_refuses_a_dump(broken, tmp_path):
    with SqlDumpSource(broken) as src:
        report = I.check_integrity(src)
        with pytest.raises(SourceError, match="live MySQL"):
            I.apply_fixes(src, report, backup_dir=tmp_path)


def test_cli_check(broken, tmp_path, capsys):
    out = tmp_path / "fix.sql"
    assert main(["check", "--dump", str(broken), "--fix-sql", str(out)]) == 1
    text = capsys.readouterr().out
    assert "Problems found: 15 (11 can be fixed automatically)" in text
    script = out.read_text(encoding="utf-8")
    assert script.startswith("-- Generated by XUI DB Tool: integrity fixes for broken.sql.")
    assert "START TRANSACTION;\n" in script and script.endswith("COMMIT;\n")
    assert "DELETE FROM `mag_devices` WHERE `user_id` IN (99);" in script
    assert main(["check", "--dump", "tests/fixtures/sample_xtream.sql"]) == 0


def test_dump_columns_know_not_null(broken):
    with SqlDumpSource(broken) as src:
        cols = {c.name: c for c in src.table_info("streams").columns}
    assert cols["id"].nullable is False and cols["category_id"].nullable is True
    assert isinstance(cols["id"], ColumnInfo)
