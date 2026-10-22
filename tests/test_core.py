# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Tests for the offline parser, panel detection and exporters (no DB needed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xdbtool.core.dump_source import SqlDumpSource
from xdbtool.core.exporter import ExportOptions, export_table
from xdbtool.core.profiles import categorise_tables, detect_panel

FIXTURE = Path(__file__).parent / "fixtures" / "sample_xtream.sql"


@pytest.fixture()
def source():
    with SqlDumpSource(FIXTURE) as src:
        yield src


def test_lists_tables(source):
    assert set(source.list_tables()) == {"users", "streams", "bouquets", "settings"}


def test_parses_columns(source):
    cols = [c.name for c in source.table_info("users").columns]
    assert cols == ["id", "username", "password", "email", "is_admin", "notes"]


def test_row_counts(source):
    assert source.row_count("users") == 3
    assert source.row_count("bouquets") == 2
    assert source.row_count("streams") == 1


def test_handles_quotes_commas_nulls_and_escapes(source):
    rows = list(source.iter_rows("users"))
    assert rows[1]["notes"] == "line with 'quotes' and, commas"
    assert rows[2]["email"] is None
    assert rows[2]["notes"] == "multi\nline\nnote"
    assert rows[0]["is_admin"] == 1


def test_detects_xtream_family(source):
    result = detect_panel(source.list_tables())
    assert result.profile.key in {"xtream_ui", "streamcreed", "xui_one"}
    assert result.confidence > 0.3


def test_categorise(source):
    cats = categorise_tables(source.list_tables())
    assert "streams" in cats and "bouquets" in cats and "settings" in cats


def test_export_json_roundtrip(source, tmp_path):
    opts = ExportOptions(fmt="json", out_dir=tmp_path)
    path = export_table(source, "users", opts)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) == 3
    assert data[0]["username"] == "admin"


def test_export_csv(source, tmp_path):
    opts = ExportOptions(fmt="csv", out_dir=tmp_path)
    path = export_table(source, "bouquets", opts)
    text = path.read_text(encoding="utf-8")
    assert "bouquet_name" in text and "Starter" in text


def test_export_sql_escapes(source, tmp_path):
    opts = ExportOptions(fmt="sql", out_dir=tmp_path, include_schema=False)
    path = export_table(source, "users", opts)
    text = path.read_text(encoding="utf-8")
    assert "INSERT INTO `users`" in text
    assert "\\'quotes\\'" in text  # single quotes escaped for MySQL


def test_redaction(source, tmp_path):
    opts = ExportOptions(fmt="json", out_dir=tmp_path, redact_columns=frozenset({"password"}))
    path = export_table(source, "users", opts)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert all(r["password"] == "***" for r in data)


def test_insert_with_partial_or_reordered_column_list(tmp_path):
    """phpMyAdmin/XC_VM dumps name the columns; some INSERTs leave columns out."""
    dump = tmp_path / "partial.sql"
    dump.write_text(
        "CREATE TABLE `servers` (\n  `id` int(11) NOT NULL,\n  `name` varchar(64),\n"
        "  `port` int(11) DEFAULT 80,\n  PRIMARY KEY (`id`)\n);\n"
        "INSERT INTO `servers` (`id`, `name`) VALUES (1, 'Main');\n"
        "INSERT INTO `servers` (`port`, `name`, `id`) VALUES (8080, 'LB', 2);\n"
        "INSERT INTO `servers` VALUES (3, 'Old', 25461);\n", encoding="utf-8")
    with SqlDumpSource(dump) as src:
        assert list(src.iter_rows("servers")) == [
            {"id": 1, "name": "Main"},
            {"port": 8080, "name": "LB", "id": 2},
            {"id": 3, "name": "Old", "port": 25461},
        ]
        page = src.fetch_page("servers", order_by="id")
        assert page.rows == [(1, "Main", None), (2, "LB", 8080), (3, "Old", 25461)]
