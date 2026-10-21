# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Tests for renumbering stream ids (offline: dumps + SQLite)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from sqlite_db import FakeMySQL, SqliteSource

from xdbtool.__main__ import main
from xdbtool.core import renumber as R
from xdbtool.core.dump_source import SqlDumpSource
from xdbtool.core.source import SourceError

# Stream 2 survives from the old numbering; 300… are the newer streams. Compacting
# gives 2→1, 300→2, 301→3, 305→4, 310→5, so "[300]" becomes "[2]" while "[2]"
# itself becomes "[1]" — values that must not be rewritten twice.
XUI = """\
CREATE TABLE `lines` (
  `id` int(11) NOT NULL,
  `username` varchar(50)
);
INSERT INTO `lines` VALUES (1,'alice');
CREATE TABLE `servers` (
  `id` int(11) NOT NULL
);
INSERT INTO `servers` VALUES (1),(2);
CREATE TABLE `streams` (
  `id` int(11) NOT NULL,
  `type` int(11) NOT NULL,
  `stream_display_name` text
);
INSERT INTO `streams` VALUES (2,2,'Old movie'),(300,1,'News'),(301,1,'Sport'),(305,4,'Radio'),\
(310,5,'Episode');
CREATE TABLE `streams_servers` (
  `server_stream_id` int(11) NOT NULL,
  `stream_id` int(11),
  `server_id` int(11)
);
INSERT INTO `streams_servers` VALUES (1,2,1),(2,300,1),(3,301,1),(4,305,1),(5,310,1),(6,300,2);
CREATE TABLE `streams_episodes` (
  `id` int(11) NOT NULL,
  `series_id` int(11),
  `stream_id` int(11)
);
INSERT INTO `streams_episodes` VALUES (1,7,310);
CREATE TABLE `bouquets` (
  `id` int(11) NOT NULL,
  `bouquet_channels` text,
  `bouquet_movies` text
);
INSERT INTO `bouquets` VALUES (1,'[300,301]','[2]'),(2,'[301,999]','["2"]'),(3,'[]','[]');
CREATE TABLE `mag_devices` (
  `mag_id` int(11) NOT NULL,
  `user_id` int(11),
  `fav_channels` text,
  `last_itv_id` int(11)
);
INSERT INTO `mag_devices` VALUES (1,1,'[300,301]',300),\
(2,1,'{"live":["301"],"movie":[2]}',4),(3,1,'[3,300]',0),(4,1,'',NULL);
CREATE TABLE `lines_activity` (
  `activity_id` int(11) NOT NULL,
  `user_id` int(11),
  `stream_id` int(11) NOT NULL
);
INSERT INTO `lines_activity` VALUES (1,1,300),(2,1,5),(3,1,77);
CREATE TABLE `providers_streams` (
  `id` int(11) NOT NULL,
  `provider_id` int(11),
  `stream_id` int(11)
);
INSERT INTO `providers_streams` VALUES (1,1,300);
"""
UNIQUE = {"streams": ("id",), "streams_servers": ("stream_id", "server_id")}


@pytest.fixture
def dump(tmp_path):
    path = tmp_path / "xui.sql"
    path.write_text(XUI, encoding="utf-8")
    return path


def _plan(path, **kw):
    with SqlDumpSource(path) as src:
        return R.build_renumber(src, **kw)


def test_plan_maps_ids_in_order_and_finds_every_reference(dump):
    plan = _plan(dump)
    assert plan.family == "xui_one" and plan.total == 5
    assert plan.mapping == {2: 1, 300: 2, 301: 3, 305: 4, 310: 5}
    assert (plan.old_range, plan.new_range) == ((2, 310), (1, 5))
    refs = {r.where: r for r in plan.refs}
    assert {w: (r.rows, dict(r.orphans), r.policy) for w, r in refs.items()} == {
        "streams_servers.stream_id": (6, {}, R.REFUSE),
        "streams_episodes.stream_id": (1, {}, R.REFUSE),
        "bouquets.bouquet_channels": (2, {}, R.REFUSE),
        "bouquets.bouquet_movies": (2, {}, R.REFUSE),
        "mag_devices.fav_channels": (3, {3: 1}, R.PRUNE),
        "mag_devices.last_itv_id": (1, {4: 1}, R.CLEAR),
        "lines_activity.stream_id": (1, {5: 1}, R.CLEAR),
    }
    # The provider's own catalogue ids are not this panel's stream ids.
    assert "providers_streams.stream_id" not in refs
    assert plan.problems == []
    assert plan.tables == ["streams", "bouquets", "lines_activity", "mag_devices",
                           "streams_episodes", "streams_servers"]
    text = "\n".join(plan.summary_lines())
    assert "Ids 2–310 become 1–5." in text
    assert "Streams that change id: 5 (live: 2, movie: 1, radio: 1, episode: 1)" in text


def test_live_first_order(dump):
    plan = _plan(dump, order="type")
    assert plan.mapping == {300: 1, 301: 2, 305: 3, 2: 4, 310: 5}


def test_start_and_already_compact(dump):
    assert _plan(dump, start=10).mapping[2] == 10
    plan = _plan("tests/fixtures/sample_xtream.sql", start=10)  # one stream, id 10
    assert plan.mapping == {} and R.renumber_statements(plan) == []
    assert "nothing to renumber" in plan.summary_lines()[-1]


def test_renumber_runs_and_updates_every_reference(dump, tmp_path):
    src = FakeMySQL(SqliteSource(dump, UNIQUE))
    plan = R.build_renumber(src)
    result = R.apply_renumber(src, plan, backup_dir=tmp_path / "bk")
    db = src.inner
    assert db.column("streams", "id") == [1, 2, 3, 4, 5]
    assert db.column("streams", "stream_display_name") == [
        "Old movie", "News", "Sport", "Radio", "Episode"]
    assert db.column("streams_servers", "stream_id") == [1, 2, 3, 4, 5, 2]
    assert db.column("streams_episodes", "stream_id") == [5]
    assert db.column("bouquets", "bouquet_channels") == ["[2,3]", "[3,999]", "[]"]
    assert db.column("bouquets", "bouquet_movies") == ["[1]", '["1"]', "[]"]
    favs = db.column("mag_devices", "fav_channels")
    assert favs[0] == "[2,3]" and favs[2] == "[2]" and favs[3] == ""
    assert json.loads(favs[1]) == {"live": ["3"], "movie": [1]}
    assert db.column("mag_devices", "last_itv_id") == [2, None, 0, None]
    # Activity of the deleted stream 5 no longer points at the new stream 5.
    assert db.column("lines_activity", "stream_id") == [2, 0, 77]
    assert db.column("providers_streams", "stream_id") == [300]
    assert src.executed == ["ALTER TABLE `streams` AUTO_INCREMENT = 6"]
    assert sorted(p.name for p in result["backup"].iterdir()) == [
        f"{t}.sql" for t in sorted(plan.tables)]
    assert "VALUES (300, 1, 'News')" in (result["backup"] / "streams.sql").read_text()
    # A second plan finds nothing left to do.
    assert R.build_renumber(src).mapping == {}


def test_orphans_that_would_be_reused_block_the_renumbering(tmp_path):
    path = tmp_path / "orphan.sql"
    path.write_text(XUI.replace("(6,300,2);", "(6,300,2),(7,3,1);")
                    .replace("(3,'[]','[]')", "(3,'[4]','not json')"), encoding="utf-8")
    plan = _plan(path)
    assert len(plan.problems) == 3
    assert "streams_servers.stream_id: 1 row(s) point to deleted streams whose ids (3)" in (
        plan.problems[0])
    assert "bouquets.bouquet_channels" in plan.problems[1]
    assert "bouquets.bouquet_movies: 1 value(s) are not valid id lists" in plan.problems[2]
    with pytest.raises(R.RenumberError):
        R.renumber_statements(plan)
    assert main(["renumber", "--dump", str(path)]) == 2


def test_remap_json():
    m = {300: 2, 301: 3}
    assert R.remap_json("[300, 5]", m) == "[2,5]"
    assert R.remap_json("[5]", m) == "[5]"
    assert R.remap_json('["300"]', m) == '["2"]'
    assert R.remap_json('{"live":[301],"vod":"x"}', m) == '{"live":[3],"vod":"x"}'
    assert R.remap_json("300", m) == "2" and R.remap_json(300, m) == 2
    assert R.remap_json(" 7 ", m) == " 7 "
    assert R.remap_json("[3,300]", m, frozenset({3})) == "[2]"
    for empty in (None, "", "0", "null"):
        assert R.remap_json(empty, m) == empty
    with pytest.raises(ValueError):
        R.remap_json('["x"]', m)


def test_runs_group_consecutive_ids():
    assert R._runs({300: 1, 301: 2, 302: 3, 305: 4, 306: 5, 400: 7}) == [
        (300, 302, -299), (305, 306, -301), (400, 400, -393)]


def test_apply_refuses_a_dump(dump, tmp_path):
    with SqlDumpSource(dump) as src:
        plan = R.build_renumber(src)
        with pytest.raises(SourceError, match="live MySQL"):
            R.apply_renumber(src, plan, backup_dir=tmp_path)


def test_cli_writes_sql_and_files_scripts(dump, tmp_path, capsys):
    sql, sh = tmp_path / "renumber.sql", tmp_path / "files.sh"
    assert main(["renumber", "--dump", str(dump), "--sql-out", str(sql),
                 "--files-script", str(sh)]) == 0
    out = capsys.readouterr().out
    assert "Streams that change id: 5" in out and "Restart XUI" in out
    text = sql.read_text(encoding="utf-8")
    assert "START TRANSACTION;\n" in text
    assert text.endswith("COMMIT;\nALTER TABLE `streams` AUTO_INCREMENT = 6;\n"
                         "SET FOREIGN_KEY_CHECKS = 1;\n")
    assert "UPDATE `streams` SET `id` = `id` - 1000000000 WHERE `id` >= 1000000000;" in text
    script = sh.read_bytes()
    assert b"\r\n" not in script and b"/home/xui/content/vod" in script
    assert b"\n300 2\n" in script


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="needs Linux bash")
def test_files_script_renames_by_id(dump, tmp_path):
    plan = _plan(dump)
    vod = tmp_path / "vod"
    vod.mkdir()
    for name in ("2.mp4", "300.mkv", "300.srt", "3000.mp4", "301"):
        (vod / name).write_text(name)
    (vod / "310").mkdir()
    script = tmp_path / "files.sh"
    script.write_text(R.files_script(plan, (str(vod),)), encoding="utf-8")
    subprocess.run(["bash", str(script)], check=True, capture_output=True)
    assert sorted(p.name for p in vod.iterdir()) == [
        "1.mp4", "2.mkv", "2.srt", "3", "3000.mp4", "5"]
    assert (vod / "1.mp4").read_text() == "2.mp4" and (vod / "3").read_text() == "301"
