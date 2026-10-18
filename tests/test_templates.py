"""Tests for the template registry and matching (offline, no MySQL)."""

from __future__ import annotations

from pathlib import Path

import pytest

from xdbtool.core import templates

FIXTURE = Path(__file__).parent / "fixtures" / "sample_xtream.sql"


@pytest.fixture()
def tmpl_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDBTOOL_TEMPLATES", str(tmp_path))
    return tmp_path


def test_register_and_list(tmpl_env):
    meta = templates.register_template(
        FIXTURE, name="Sample", panel_key="xtream_ui", version="test"
    )
    assert meta.key == "xtream_ui-test"
    assert meta.table_count == 4
    assert set(meta.seed_tables) == {"users", "streams", "bouquets", "settings"}
    assert (tmpl_env / "xtream_ui-test.sql").is_file()  # copied in
    assert (tmpl_env / "manifest.json").is_file()

    listed = templates.list_templates()
    assert len(listed) == 1 and listed[0].key == "xtream_ui-test"


def test_match_template(tmpl_env):
    templates.register_template(FIXTURE, name="Sample", panel_key="xtream_ui")
    meta, score = templates.match_template(["users", "streams", "bouquets", "settings"])
    assert meta is not None and meta.panel_key == "xtream_ui"
    assert score == pytest.approx(1.0)


def test_match_partial(tmpl_env):
    templates.register_template(FIXTURE, name="Sample", panel_key="xtream_ui")
    # Two of four tables present -> Jaccard 2/4 = 0.5
    meta, score = templates.match_template(["users", "streams"])
    assert meta is not None
    assert score == pytest.approx(0.5)


def test_export_signatures(tmpl_env, tmp_path):
    templates.register_template(FIXTURE, name="Sample", panel_key="xtream_ui")
    out = templates.export_signatures(tmp_path / "sigs.json")
    assert out.is_file()
    data = out.read_text(encoding="utf-8")
    assert "xtream_ui" in data and "sample_xtream" not in data  # names only, no SQL
