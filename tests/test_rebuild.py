# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Tests for rebuilding a database from a template + chosen dump tables (no server)."""

from __future__ import annotations

import gzip
from contextlib import contextmanager
from pathlib import Path

import pytest

import xdbtool.core.mysql_source as mysql_source
from xdbtool.core import rebuild, templates
from xdbtool.core.dump_source import (
    _INSERT_RE,
    SqlDumpSource,
    _convert,
    _iter_statements,
    _split_tuples,
)
from xdbtool.core.mysql_source import ConnectionConfig

FIXTURE = Path(__file__).parent / "fixtures" / "sample_xtream.sql"

# A newer "panel version": users lost `notes` and gained two columns (one generated),
# settings and bouquets ship default rows, `lines` is new and `streams` is gone.
TEMPLATE_SQL = """\
CREATE DATABASE IF NOT EXISTS `panel_tmpl`;
USE `panel_tmpl`;
CREATE TABLE IF NOT EXISTS `users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `username` varchar(255) NOT NULL,
  `password` varchar(255) NOT NULL,
  `email` varchar(255) DEFAULT NULL,
  `is_admin` tinyint(1) NOT NULL DEFAULT 0,
  `member_group_id` int(11) DEFAULT 1,
  `name_len` int(11) GENERATED ALWAYS AS (char_length(`username`)) VIRTUAL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS `settings` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `setting_key` varchar(255) DEFAULT NULL,
  `setting_value` text,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;
INSERT INTO `settings` (`id`, `setting_key`, `setting_value`) VALUES (1, 'panel_name', 'XUI');
CREATE TABLE IF NOT EXISTS `bouquets` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `bouquet_name` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;
INSERT INTO `bouquets` (`id`, `bouquet_name`) VALUES (1, 'Default');
CREATE TABLE IF NOT EXISTS `lines` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;
"""

EMPTY_SETTINGS = FIXTURE.read_text(encoding="utf-8").replace(
    "INSERT INTO `settings` VALUES (1,'panel_name','My Panel'),(2,'version','1.0');", "")


@pytest.fixture()
def tmpl(tmp_path, monkeypatch):
    monkeypatch.setenv("XDBTOOL_TEMPLATES", str(tmp_path / "templates"))
    sql = tmp_path / "base.sql"
    sql.write_text(TEMPLATE_SQL, encoding="utf-8")
    return templates.register_template(sql, name="Panel 2.0", panel_key="panel", version="2.0")


@pytest.fixture()
def src():
    with SqlDumpSource(FIXTURE) as s:
        yield s


def _inserted(statements) -> dict[str, list[dict]]:
    """Rows of ``INSERT INTO t (cols) VALUES …`` statements, keyed by table."""
    out: dict[str, list[dict]] = {}
    for stmt in statements:
        m = _INSERT_RE.match(stmt.strip())
        if not m or not m.group(2):
            continue
        cols = [c.strip().strip("`") for c in m.group(2).split(",")]
        for values in _split_tuples(stmt.strip()[m.end():]):
            out.setdefault(m.group(1), []).append(
                dict(zip(cols, [_convert(v) for v in values], strict=True)))
    return out


def test_template_schema_skips_db_directives_and_generated_columns(tmpl):
    schema = rebuild.load_template_schema(tmpl)
    assert not any(s.upper().startswith(("USE", "CREATE DATABASE")) for s in schema.statements)
    assert schema.columns["users"] == ["id", "username", "password", "email", "is_admin",
                                       "member_group_id"]
    assert schema.seeded == {"settings", "bouquets"}


def test_plan_maps_columns_and_reports_tables(tmpl, src):
    rp = rebuild.build_rebuild(src, tmpl.key, ["users", "streams", "settings"])
    users = next(m for m in rp.copied if m.table == "users")
    assert users.source_only == ["notes"] and users.target_only == ["member_group_id"]
    assert rp.plan.missing_in_target == ["streams"]
    assert rp.rows == {"users": 3, "settings": 2}
    assert rp.replaced_defaults == ["settings"]
    assert rp.kept_defaults == ["bouquets"]            # not chosen: template rows stay
    assert rp.left_empty == ["lines"]
    text = "\n".join(rp.summary_lines())
    assert "1 source-only cols dropped" in text and "1 new cols get their defaults" in text
    assert "Not in the template (skipped): streams" in text


def test_plan_rejects_unknown_or_no_tables(tmpl, src):
    with pytest.raises(rebuild.RebuildError):
        rebuild.build_rebuild(src, tmpl.key, ["nope"])
    with pytest.raises(rebuild.RebuildError):
        rebuild.build_rebuild(src, tmpl.key, [])
    with pytest.raises(rebuild.RebuildError):
        rebuild.build_rebuild(src, "missing-key", ["users"])


def test_empty_source_table_keeps_template_defaults(tmpl, tmp_path):
    dump = tmp_path / "empty_settings.sql"
    dump.write_text(EMPTY_SETTINGS, encoding="utf-8")
    with SqlDumpSource(dump) as s:
        rp = rebuild.build_rebuild(s, tmpl.key, ["settings"])
        assert rp.replaced_defaults == [] and "settings" in rp.kept_defaults
        out = rebuild.write_rebuild_script(s, rp, tmp_path / "r.sql", database="newdb")
    rows = _inserted(_iter_statements(out.read_text(encoding="utf-8")))
    assert rows["settings"] == [{"id": 1, "setting_key": "panel_name", "setting_value": "XUI"}]


def test_script_replaces_defaults_and_copies_rows_verbatim(tmpl, src, tmp_path):
    rp = rebuild.build_rebuild(src, tmpl.key, ["users", "settings"])
    out = rebuild.write_rebuild_script(src, rp, tmp_path / "rebuild.sql.gz", database="new`db")
    with gzip.open(out, "rt", encoding="utf-8") as fh:
        text = fh.read()
    assert "DROP DATABASE" not in text
    assert "CREATE DATABASE IF NOT EXISTS `new``db`" in text and "USE `new``db`;" in text
    assert "panel_tmpl" not in text                    # template's own db name never used
    rows = _inserted(_iter_statements(text))
    assert [r["setting_value"] for r in rows["settings"]] == ["My Panel", "1.0"]
    assert rows["bouquets"] == [{"id": 1, "bouquet_name": "Default"}]  # template default
    assert rows["users"][0] == {"id": 1, "username": "admin",
                                "password": "$2y$10$abcdefghijklmnopqrstuv",  # hash as-is
                                "email": "admin@example.com", "is_admin": 1}
    assert text.index("START TRANSACTION") < text.index("INSERT INTO `users`")


def test_script_drop_existing(tmpl, src, tmp_path):
    rp = rebuild.build_rebuild(src, tmpl.key, ["users"])
    out = rebuild.write_rebuild_script(src, rp, tmp_path / "r.sql", database="x",
                                       drop_existing=True)
    assert out.read_text(encoding="utf-8").count("DROP DATABASE IF EXISTS `x`;") == 1


def test_suggest_template(tmpl, src):
    meta, score = rebuild.suggest_template(src)
    assert meta.key == tmpl.key and score == pytest.approx(0.6)  # 3 shared of 5 names


class FakeTarget:
    """Records what rebuild_live runs instead of talking to a server."""

    instances: list[FakeTarget] = []
    existing: dict[str, int] = {}   # database -> table count
    fail_on: str | None = None

    def __init__(self, config):
        self.config, self.created, self.script, self.data = config, None, [], []
        self.committed = False
        FakeTarget.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_databases(self):
        return list(self.existing)

    def execute(self, sql, args=None):
        return [{"n": self.existing.get(args[0], 0)}]

    def create_database(self, name, drop_existing=False, allow_drop_panel=False, **_kw):
        self.created = (name, drop_existing, allow_drop_panel)

    def execute_script(self, statements, progress=None):
        self.script = list(statements)
        return len(self.script)

    @contextmanager
    def transaction(self):
        outer = self

        class Cur:
            def execute(self, sql):
                if outer.fail_on and outer.fail_on in sql:
                    raise RuntimeError("Duplicate entry")
                outer.data.append(sql)

        yield Cur()
        self.committed = True


@pytest.fixture()
def fake(monkeypatch):
    FakeTarget.instances.clear()
    FakeTarget.existing = {}
    FakeTarget.fail_on = None
    monkeypatch.setattr(mysql_source, "MySQLSource", FakeTarget)
    return FakeTarget


def test_live_rebuild(tmpl, src, fake):
    rp = rebuild.build_rebuild(src, tmpl.key, ["users", "settings", "bouquets"])
    cfg = ConnectionConfig(host="h", user="u", password="p", database="ignored")
    result = rebuild.rebuild_live(cfg, src, rp, "newdb")
    target = fake.instances[-1]
    assert target.config.database == "" and cfg.database == "ignored"  # caller's cfg intact
    assert target.created == ("newdb", False, False)
    assert target.script[0].startswith("SET SESSION sql_mode")
    assert not any("INSERT INTO `settings`" in s for s in target.script)  # replaced
    assert target.committed
    assert result["written"] == {"bouquets": 2, "settings": 2, "users": 3}
    assert len(_inserted(target.data)["users"]) == 3


def test_live_refuses_existing_database_with_tables(tmpl, src, fake):
    fake.existing = {"newdb": 5}
    rp = rebuild.build_rebuild(src, tmpl.key, ["users"])
    cfg = ConnectionConfig(host="h", user="u", password="p")
    with pytest.raises(rebuild.RebuildError, match="already exists"):
        rebuild.rebuild_live(cfg, src, rp, "newdb")
    assert fake.instances[-1].created is None
    rebuild.rebuild_live(cfg, src, rp, "newdb", drop_existing=True)  # explicit replace
    assert fake.instances[-1].created == ("newdb", True, False)


def test_live_failure_keeps_no_rows(tmpl, src, fake):
    fake.fail_on = "INSERT INTO `settings`"
    rp = rebuild.build_rebuild(src, tmpl.key, ["users", "settings"])
    cfg = ConnectionConfig(host="h", user="u", password="p")
    with pytest.raises(rebuild.RebuildError, match="none was kept"):
        rebuild.rebuild_live(cfg, src, rp, "newdb")
    assert not fake.instances[-1].committed


def test_run_command_handles_gzip():
    assert rebuild.run_command("/tmp/r.sql") == "mysql -u root -p < r.sql"
    assert rebuild.run_command("r.sql.gz") == "gunzip -c r.sql.gz | mysql -u root -p"
