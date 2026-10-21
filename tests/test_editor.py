# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Tests for editing rows: values, SQL, transactions, undo scripts, import, the GUI model."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlite_db import FakeMySQL, SqliteSource

from xdbtool.__main__ import main
from xdbtool.core import editor as E
from xdbtool.core.dump_source import SqlDumpSource
from xdbtool.core.source import ColumnInfo, SourceError, TableInfo

DUMP = """\
CREATE TABLE `streams` (
  `id` int(11) NOT NULL,
  `type` int(11),
  `stream_display_name` text,
  `category_id` text,
  `rating` decimal(4,2)
);
INSERT INTO `streams` VALUES (1,1,'News','[1]',NULL),(2,1,'Sport','[1,2]',7.5),\
(3,2,'Movie','[3]',NULL);
CREATE TABLE `users` (
  `id` int(11) NOT NULL,
  `username` varchar(50) NOT NULL,
  `password` varchar(255)
);
INSERT INTO `users` VALUES (1,'admin','$6$salt$abcdef');
CREATE TABLE `logs` (
  `stream_id` int(11),
  `msg` text
);
"""
KEYS = {"streams": "id", "users": "id"}


@pytest.fixture
def dump(tmp_path):
    path = tmp_path / "db.sql"
    path.write_text(DUMP, encoding="utf-8")
    return path


@pytest.fixture
def live(dump):
    return FakeMySQL(SqliteSource(dump, unique={"users": ("username",)}, keys=KEYS))


def col(name, kind="text", nullable=True):
    return ColumnInfo(name, kind, nullable)


# ------------------------------------------------------------------ values
@pytest.mark.parametrize("text, column, expected", [
    ("42", col("n", "int"), 42), (" -7 ", col("n", "bigint"), -7), ("", col("n", "int"), None),
    (None, col("t"), None), ("", col("t"), ""), ("3.50", col("d", "decimal"), Decimal("3.50")),
    ("hello", col("t", "varchar"), "hello"),
])
def test_coerce(text, column, expected):
    assert E.coerce(text, column) == expected


@pytest.mark.parametrize("text, column, message", [
    ("4.5", col("n", "int"), "whole number"), ("x", col("d", "double"), "needs a number"),
    (None, col("t", nullable=False), "cannot be empty"), ("", col("n", "int", False), "empty"),
])
def test_coerce_refuses(text, column, message):
    with pytest.raises(ValueError, match=message):
        E.coerce(text, column)


def test_check_value_json_and_hash():
    assert "valid JSON" in E.check_value(col("bouquet_channels"), "[1,2]", "1,2")[0]
    assert E.check_value(col("bouquet_channels"), "[1,2]", "[1]") == ("", "")
    assert "password hash" in E.check_value(col("password"), "$6$s$h", "secret")[1]
    assert E.check_value(col("password"), "$6$s$h", "$6$t$i") == ("", "")
    assert E.check_value(col("notes"), "plain", "other") == ("", "")


def test_editable_reason(dump, live):
    with SqlDumpSource(dump) as src:
        assert "live MySQL" in E.editable_reason(src, src.table_info("streams"))
    assert E.editable_reason(live, live.table_info("streams")) == ""
    assert "no primary key" in E.editable_reason(live, live.table_info("logs"))
    info = live.table_info("streams")
    assert (E.key_columns(info), E.auto_increment(info)) == (["id"], "id")
    assert E.required_columns(live.table_info("users")) == ["username"]


# --------------------------------------------------------------------- SQL
def test_sql_and_undo():
    up = E.Change(E.UPDATE, {"stream_display_name": "SPORT HD", "rating": None}, {"id": 2},
                  {"stream_display_name": "Sport", "rating": Decimal("7.5")})
    assert E.check_sql("streams", up) == (
        "SELECT 1 FROM `streams` WHERE `id` = 2 AND `stream_display_name` <=> 'Sport' "
        "AND `rating` <=> 7.5 FOR UPDATE")
    assert E.change_sql("streams", up) == (
        "UPDATE `streams` SET `stream_display_name` = 'SPORT HD', `rating` = NULL WHERE `id` = 2")
    assert E.undo_sql("streams", up) == (
        "UPDATE `streams` SET `stream_display_name` = 'Sport', `rating` = 7.5 WHERE `id` = 2")
    ins = E.Change(E.INSERT, {"stream_display_name": "New"})
    assert E.change_sql("streams", ins) == (
        "INSERT INTO `streams` (`stream_display_name`) VALUES ('New')")
    assert E.undo_sql("streams", ins, {"id": 9}) == "DELETE FROM `streams` WHERE `id` = 9"
    dele = E.Change(E.DELETE, key={"id": 3}, old={"id": 3, "stream_display_name": "Movie"})
    assert E.check_sql("streams", dele) == "SELECT 1 FROM `streams` WHERE `id` = 3 FOR UPDATE"
    assert E.undo_sql("streams", dele) == (
        "INSERT INTO `streams` (`id`, `stream_display_name`) VALUES (3, 'Movie')")


# ------------------------------------------------------------------- apply
def _rows(live, table="streams"):
    return list(live.inner.db.execute(f"SELECT * FROM `{table}` ORDER BY id"))


def test_apply_changes_and_undo_script(live, tmp_path):
    before = _rows(live)
    changes = [
        E.Change(E.UPDATE, {"stream_display_name": "SPORT HD"}, {"id": 2},
                 {"stream_display_name": "Sport"}),
        E.Change(E.DELETE, key={"id": 3}, old=dict(zip(
            ("id", "type", "stream_display_name", "category_id", "rating"), before[2],
            strict=True))),
        E.Change(E.INSERT, {"type": 1, "stream_display_name": "Kids", "category_id": "[4]"}),
    ]
    result = E.apply_changes(live, "streams", changes, backup_dir=tmp_path)
    assert result["counts"] == {"update": 1, "insert": 1, "delete": 1}
    assert _rows(live) == [(1, 1, "News", "[1]", None), (2, 1, "SPORT HD", "[1,2]", 7.5),
                           (4, 1, "Kids", "[4]", None)]
    undo = result["undo"].read_text(encoding="utf-8")
    assert result["undo"].parent == tmp_path and result["undo"].name.endswith(".undo.sql")
    assert undo.splitlines()[1:] == [
        "START TRANSACTION;",
        "DELETE FROM `streams` WHERE `id` = 4;",
        "INSERT INTO `streams` (`id`, `type`, `stream_display_name`, `category_id`, `rating`) "
        "VALUES (3, 2, 'Movie', '[3]', NULL);",
        "UPDATE `streams` SET `stream_display_name` = 'Sport' WHERE `id` = 2;",
        "COMMIT;",
    ]
    live.inner.db.executescript(undo.replace("START TRANSACTION;", "BEGIN;"))
    assert _rows(live) == before


def test_row_changed_meanwhile_saves_nothing(live, tmp_path):
    changes = [
        E.Change(E.UPDATE, {"stream_display_name": "A"}, {"id": 1},
                 {"stream_display_name": "News"}),
        E.Change(E.UPDATE, {"stream_display_name": "B"}, {"id": 2},
                 {"stream_display_name": "edited by the panel"}),
    ]
    with pytest.raises(E.EditError, match="changed or deleted by someone else"):
        E.apply_changes(live, "streams", changes, backup_dir=tmp_path / "bk")
    assert [r[2] for r in _rows(live)] == ["News", "Sport", "Movie"]  # first one rolled back
    gone = E.Change(E.DELETE, key={"id": 99}, old={"id": 99})
    with pytest.raises(E.EditError):
        E.apply_changes(live, "streams", [gone], backup_dir=tmp_path / "bk")
    assert not (tmp_path / "bk").exists()  # no undo script for nothing saved


def test_server_refusal_and_required_columns(live, tmp_path):
    clash = E.Change(E.INSERT, {"username": "admin"})
    with pytest.raises(E.EditError, match="server refused"):
        E.apply_changes(live, "users", [E.Change(E.INSERT, {"username": "new"}), clash],
                        backup_dir=tmp_path)
    assert [r[1] for r in _rows(live, "users")] == ["admin"]
    with pytest.raises(E.EditError, match="fill in username"):
        E.apply_changes(live, "users", [E.Change(E.INSERT, {"password": "x"})],
                        backup_dir=tmp_path)


def test_apply_refuses_a_dump_and_keyless_tables(dump, live, tmp_path):
    with SqlDumpSource(dump) as src, pytest.raises(SourceError):
        E.apply_changes(src, "streams", [], backup_dir=tmp_path)
    with pytest.raises(E.EditError, match="no primary key"):
        E.apply_changes(live, "logs", [], backup_dir=tmp_path)


def test_fetch_row(live):
    assert E.fetch_row(live, "users", {"id": 1})["username"] == "admin"
    assert E.fetch_row(live, "users", {"id": 5}) is None


# ------------------------------------------------------------------ import
def test_import_csv_semicolons_and_unknown_columns(tmp_path, live):
    path = tmp_path / "rows.csv"
    path.write_text("﻿stream_display_name;type;rating;extra\nCanal A;1;;x\n"
                    "Canal B;2;8.25;y\n", encoding="utf-8")
    changes, unknown = E.import_changes(E.read_rows_file(path), live.table_info("streams"))
    assert unknown == ["extra"]
    assert [c.values for c in changes] == [
        {"stream_display_name": "Canal A", "type": 1},
        {"stream_display_name": "Canal B", "type": 2, "rating": Decimal("8.25")}]
    E.apply_changes(live, "streams", changes, backup_dir=tmp_path)
    assert [r[2] for r in _rows(live)][-2:] == ["Canal A", "Canal B"]


def test_import_json(tmp_path, live):
    path = tmp_path / "rows.json"
    path.write_text('[{"stream_display_name": "X", "category_id": [1, 2], "type": 1, '
                    '"rating": null}]', encoding="utf-8")
    changes, unknown = E.import_changes(E.read_rows_file(path), live.table_info("streams"))
    assert unknown == [] and changes[0].values == {
        "stream_display_name": "X", "category_id": "[1,2]", "type": 1, "rating": None}
    path.write_text('{"not": "a list"}', encoding="utf-8")
    with pytest.raises(E.EditError, match="list of objects"):
        E.read_rows_file(path)


def test_import_names_the_bad_row(tmp_path, live):
    path = tmp_path / "rows.csv"
    path.write_text("type\n1\nabc\n", encoding="utf-8")
    with pytest.raises(E.EditError, match="Row 2: type needs a whole number"):
        E.import_changes(E.read_rows_file(path), live.table_info("streams"))


def test_cli_edit_needs_a_live_server(dump, capsys):
    assert main(["edit", "streams", "--dump", str(dump), "--key", "id=1",
                 "--set", "type=2"]) == 2
    assert "live MySQL" in capsys.readouterr().err


# ---------------------------------------------------------------- GUI model
@pytest.fixture
def model(live):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from xdbtool.gui.data_viewer import RowsModel

    QApplication.instance() or QApplication([])
    m = RowsModel(live.table_info("users"))
    m.set_page(live.inner.fetch_page("users"), show_secrets=False)
    return m


def test_model_turns_edits_into_changes(model):
    from PySide6.QtCore import Qt

    errors, confirms = [], []
    model.on_error, model.on_confirm = errors.append, lambda m: confirms.append(m) or False
    assert not model.flags(model.index(0, 1)) & Qt.ItemIsEditable  # edit mode off
    model.edit_mode = True
    assert not model.flags(model.index(0, 0)) & Qt.ItemIsEditable  # primary key
    assert not model.flags(model.index(0, 2)) & Qt.ItemIsEditable  # masked password
    assert model.setData(model.index(0, 1), "root")
    model.set_show_secrets(True)
    assert not model.setData(model.index(0, 2), "plain")  # hash warning declined
    assert confirms and model.pending() == 1
    row = model.add_row({"username": "bob", "id": 77})
    assert not model.flags(model.index(row, 0)) & Qt.ItemIsEditable  # auto-increment id
    assert model.flags(model.index(row, 1)) & Qt.ItemIsEditable
    model.toggle_delete([0])
    changes = model.changes()
    assert [(c.kind, c.values, c.key) for c in changes] == [
        ("delete", {}, {"id": 1}), ("insert", {"username": "bob"}, {})]
    assert changes[0].old == {"id": 1, "username": "admin", "password": "$6$salt$abcdef"}
    model.toggle_delete([0])  # restore: the edit counts again
    assert [c.kind for c in model.changes()] == ["update", "insert"]
    model.toggle_delete([row])  # a new row is simply dropped
    assert model.rowCount() == 1 and model.pending() == 1
    model.discard()
    assert model.pending() == 0 and model.changes() == []
    assert errors == []


def test_model_rejects_invalid_values(model):
    errors = []
    model.on_error = errors.append
    model.edit_mode = True
    assert not model.setData(model.index(0, 1), None)  # username is NOT NULL
    assert errors and "cannot be empty" in errors[0]
    assert model.set_null([model.index(0, 1)]) == ["username"]


def test_info_for_keyless_rows():
    info = TableInfo("t", (ColumnInfo("a", "int"),))
    assert E.key_columns(info) == [] and E.auto_increment(info) is None
