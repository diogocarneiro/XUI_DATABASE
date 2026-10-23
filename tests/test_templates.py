# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
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


def test_templates_dir_never_follows_the_current_directory(monkeypatch, tmp_path):
    import sys

    from xdbtool.core import templates as tm
    from xdbtool.core.config import config_dir

    monkeypatch.delenv("XDBTOOL_TEMPLATES", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)   # packaged app
    assert tm.templates_dir() == config_dir() / "templates"
    assert not (tmp_path / "templates").exists()
    monkeypatch.setenv("XDBTOOL_TEMPLATES", str(tmp_path / "custom"))
    assert tm.templates_dir() == tmp_path / "custom"


DIRTY = """\
CREATE TABLE `settings` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(64) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4;
INSERT INTO `settings` VALUES (1,'Panel');
CREATE TABLE `streams` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `stream_display_name` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=13001 DEFAULT CHARSET=utf8mb4;
INSERT INTO `streams` VALUES (1,'Someone else''s channel'),(13000,'Another one');
CREATE TABLE `user_activity` (
  `activity_id` int(11) NOT NULL AUTO_INCREMENT,
  PRIMARY KEY (`activity_id`)
) ENGINE=InnoDB AUTO_INCREMENT=99 DEFAULT CHARSET=utf8mb4;
INSERT INTO `user_activity` VALUES (1),(2),(98);
"""


def test_seed_tables_suggests_panel_data(tmpl_env, tmp_path):
    dirty = tmp_path / "dirty.sql"
    dirty.write_text(DIRTY, encoding="utf-8")
    templates.register_template(dirty, name="Dirty", panel_key="xtream_ui", version="1")
    seeds = {s.name: s for s in templates.seed_tables("xtream_ui-1")}
    assert set(seeds) == {"settings", "streams", "user_activity"}
    assert seeds["streams"].suggested and seeds["user_activity"].suggested
    assert not seeds["settings"].suggested
    assert seeds["streams"].size > seeds["settings"].size


def test_clean_template_keeps_schema_and_chosen_rows(tmpl_env, tmp_path):
    dirty = tmp_path / "dirty.sql"
    dirty.write_text(DIRTY, encoding="utf-8")
    templates.register_template(dirty, name="Dirty", panel_key="xtream_ui", version="1")
    seen = []
    meta = templates.clean_template("xtream_ui-1", ["streams", "USER_ACTIVITY"],
                                    progress=lambda *a: seen.append(a))
    assert meta.key == "xtream_ui-1-clean" and meta.name == "Dirty (clean)"
    assert meta.tables == ["settings", "streams", "user_activity"]  # schema intact
    assert meta.seed_tables == ["settings"]
    text = meta.sql_path().read_text(encoding="utf-8")
    assert "Someone else" not in text
    assert "AUTO_INCREMENT=13001" not in text and "AUTO_INCREMENT=99" not in text
    assert "AUTO_INCREMENT=2" in text                 # kept table keeps its counter
    assert "`id` int(11) NOT NULL AUTO_INCREMENT" in text  # column flag untouched
    assert seen[-1] == ("cleaning", len(DIRTY), len(DIRTY))
    assert templates.get_template("xtream_ui-1").sql_path().read_text(encoding="utf-8") == DIRTY
    assert not list(tmpl_env.glob("*.part"))
    with pytest.raises(ValueError):
        templates.clean_template("xtream_ui-1", [], new_key="xtream_ui-1")
    with pytest.raises(ValueError):
        templates.clean_template("nope", [])


def test_create_from_template_ignores_its_own_database_and_relaxes_sql_mode(
        tmpl_env, tmp_path, monkeypatch):
    import xdbtool.core.mysql_source as mysql_source
    from xdbtool.core.mysql_source import ConnectionConfig

    sql = tmp_path / "with_use.sql"
    sql.write_text("CREATE DATABASE IF NOT EXISTS `other`;\nUSE `other`;\n" + DIRTY,
                   encoding="utf-8")
    templates.register_template(sql, name="With USE", panel_key="xtream_ui", version="2")
    ran = {}

    class Target:
        def __init__(self, config):
            ran["db"] = config.database

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def create_database(self, name, **_kw):
            ran["created"] = name

        def execute_script(self, statements, progress=None):
            ran["statements"] = list(statements)
            return len(ran["statements"])

    monkeypatch.setattr(mysql_source, "MySQLSource", Target)
    result = templates.create_from_template(ConnectionConfig(host="h", database="x"),
                                            "xtream_ui-2", "newdb")
    stmts = ran["statements"]
    assert ran == {"db": "", "created": "newdb", "statements": stmts}
    assert stmts[0].startswith("SET SESSION sql_mode")
    assert not any(s.upper().startswith(("USE", "CREATE DATABASE")) for s in stmts)
    assert result["statements"] == 6  # 3 CREATE TABLE + 3 INSERT, preamble not counted
