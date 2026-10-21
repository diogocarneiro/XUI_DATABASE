# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Tests for browsing rows: fetch_page (dump + MySQL SQL), secrets, the `view` command."""

from __future__ import annotations

import json

import pytest

from xdbtool.__main__ import main
from xdbtool.core.dump_source import SqlDumpSource
from xdbtool.core.exporter import is_secret_column
from xdbtool.core.mysql_source import ConnectionConfig, MySQLSource, _escape_like
from xdbtool.core.source import ColumnInfo, SourceError, TableInfo

DUMP = """\
CREATE TABLE `streams` (
  `id` int(11) NOT NULL,
  `stream_display_name` text,
  `category_id` text,
  `added` int(11)
);
INSERT INTO `streams` VALUES (300,'SPORT TV 1','[1]',50),(301,'News 24','[2]',NULL),\
(302,'sport tv 2','[1]',10),(303,'Movie: 100% Fun','[3]',30),(304,'Kids_TV','[4]',20);
"""


@pytest.fixture
def dump(tmp_path):
    path = tmp_path / "streams.sql"
    path.write_text(DUMP, encoding="utf-8")
    with SqlDumpSource(path) as src:
        yield src


def _ids(page):
    return [r[0] for r in page.rows]


def test_paging_without_search(dump):
    page = dump.fetch_page("streams", offset=2, limit=2)
    assert list(page.columns) == ["id", "stream_display_name", "category_id", "added"]
    assert _ids(page) == [302, 303] and page.total == 5 and page.offset == 2


def test_search_any_column_ignores_case(dump):
    page = dump.fetch_page("streams", search="SpOrT")
    assert _ids(page) == [300, 302] and page.total == 2
    assert _ids(dump.fetch_page("streams", search="[1]")) == [300, 302]


def test_search_one_column_and_wildcards_are_literal(dump):
    assert _ids(dump.fetch_page("streams", search="1", column="category_id")) == [300, 302]
    assert _ids(dump.fetch_page("streams", search="100%")) == [303]
    assert _ids(dump.fetch_page("streams", search="_")) == [304]


def test_order_nulls_first_and_numbers_numerically(dump):
    assert _ids(dump.fetch_page("streams", order_by="added")) == [301, 302, 304, 303, 300]
    page = dump.fetch_page("streams", order_by="added", descending=True, limit=2)
    assert _ids(page) == [300, 303] and page.total == 5
    names = dump.fetch_page("streams", order_by="stream_display_name", columns=["id"])
    assert list(names.columns) == ["id"] and _ids(names) == [304, 303, 301, 300, 302]


def test_unknown_columns_are_refused(dump):
    for kwargs in ({"column": "nope"}, {"order_by": "x`; DROP"}, {"columns": ["id", "zz"]}):
        with pytest.raises(SourceError, match="Unknown column"):
            dump.fetch_page("streams", **kwargs)


# ------------------------------------------------------------------- MySQL
class FakeCursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=None):
        self.log.append((sql, list(args or [])))

    def fetchone(self):
        return {"c": 42}

    def fetchall(self):
        return [{"id": 7, "stream_display_name": "x"}]


def _mysql(monkeypatch):
    src = MySQLSource(ConnectionConfig(database="xui"))
    log: list = []
    monkeypatch.setattr(src, "_cursor", lambda: FakeCursor(log))
    monkeypatch.setattr(src, "table_info", lambda t: TableInfo(t, (
        ColumnInfo("id", "int"), ColumnInfo("stream_display_name", "text"))))
    return src, log


def test_mysql_pages_in_sql(monkeypatch):
    src, log = _mysql(monkeypatch)
    page = src.fetch_page("streams", offset=100, limit=50, search="50%_off",
                          order_by="id", descending=True)
    assert page.total == 42 and page.rows == [(7, "x")] and page.offset == 100
    where = (" WHERE CAST(`id` AS CHAR) LIKE %s OR CAST(`stream_display_name` AS CHAR) "
             "LIKE %s")
    assert log == [
        ("SELECT COUNT(*) AS c FROM `streams`" + where, [r"%50\%\_off%"] * 2),
        ("SELECT `id`, `stream_display_name` FROM `streams`" + where
         + " ORDER BY `id` DESC LIMIT %s OFFSET %s", [r"%50\%\_off%"] * 2 + [50, 100]),
    ]


def test_mysql_without_search_or_order(monkeypatch):
    src, log = _mysql(monkeypatch)
    src.fetch_page("streams", column="id", columns=["id"])
    assert log[1] == ("SELECT `id` FROM `streams` LIMIT %s OFFSET %s", [100, 0])
    with pytest.raises(SourceError):
        src.fetch_page("streams", order_by="missing")


def test_escape_like():
    assert _escape_like(r"a\b%c_d") == r"a\\b\%c\_d"


@pytest.mark.parametrize("name, secret", [
    ("password", True), ("PASSWORD", True), ("ssh_password", True), ("api_key", True),
    ("play_token", True), ("access_token", True), ("key", True), ("key_auth", True),
    ("api_pass", True), ("recaptcha_v2_secret_key", True), ("tmdb_api_key", True),
    ("username", False), ("bypass_ua", False), ("output_key", False), ("type_key", False),
    ("device_key", False), ("stream_display_name", False),
])
def test_secret_columns(name, secret):
    assert is_secret_column(name) is secret


# --------------------------------------------------------------------- CLI
def test_cli_view_table(capsys):
    fixture = "tests/fixtures/sample_xtream.sql"
    assert main(["view", "users", "--dump", fixture, "--search", "QUOTES",
                 "--columns", "id,username,password"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["id", "username", "password"]
    assert out[2].split() == ["2", "reseller_a", "***"]
    assert out[-1] == "Rows 1-1 of 1 matching 'QUOTES'"


def test_cli_view_json_and_secrets(capsys):
    fixture = "tests/fixtures/sample_xtream.sql"
    main(["view", "users", "--dump", fixture, "--json", "--order", "id", "--desc",
          "--limit", "1"])
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{"id": 3, "username": "line_001", "password": "***", "email": None,
                     "is_admin": 0, "notes": "multi\nline\nnote"}]
    main(["view", "users", "--dump", fixture, "--json", "--limit", "1", "--show-secrets"])
    assert json.loads(capsys.readouterr().out)[0]["password"].startswith("$2y$10$")


def test_cli_view_truncates_and_shows_null(capsys):
    main(["view", "users", "--dump", "tests/fixtures/sample_xtream.sql", "--width", "8",
          "--columns", "email,notes"])
    out = capsys.readouterr().out.splitlines()
    assert out[2] == "admin@e…  owner a…"
    assert out[4] == "NULL      multi l…"
