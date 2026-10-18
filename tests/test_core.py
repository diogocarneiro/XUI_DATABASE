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
