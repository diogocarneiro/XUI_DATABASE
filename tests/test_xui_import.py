# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Tests for importing an XUI.one backup into another XUI.one server (offline)."""

from __future__ import annotations

import gzip
from collections import Counter

import pytest

from xdbtool.__main__ import main
from xdbtool.core.dump_source import SqlDumpSource
from xdbtool.core.passwords import sha512_crypt
from xdbtool.core.xui_import import (
    STREAM_TABLES,
    RescueAccess,
    XuiImportError,
    build_import,
    import_via_ssh,
    report_lines,
    write_import_script,
)

BACKUP = """\
CREATE TABLE `servers` (
  `id` int(11) NOT NULL,
  `server_name` varchar(255),
  `is_main` tinyint(4)
);
INSERT INTO `servers` VALUES (1,'Main Server',1),(2,'CANAIS',0),(3,'VIDEO CLUB',0);
CREATE TABLE `settings` (
  `id` int(11) NOT NULL,
  `server_name` varchar(255)
);
INSERT INTO `settings` VALUES (1,'old panel');
CREATE TABLE `access_codes` (
  `id` int(11) NOT NULL,
  `code` varchar(255)
);
INSERT INTO `access_codes` VALUES (1,'OLDCODE');
CREATE TABLE `lines` (
  `id` int(11) NOT NULL,
  `username` varchar(255),
  `password` varchar(255),
  `fork_only_col` int(11)
);
INSERT INTO `lines` VALUES (1,'alice','$6$hash',7),(2,'bob','x;y',8);
CREATE TABLE `streams` (
  `id` int(11) NOT NULL,
  `stream_display_name` text,
  `tv_archive_server_id` int(11)
);
INSERT INTO `streams` VALUES (10,'Channel 1',3),(11,'Movie',0);
CREATE TABLE `streams_servers` (
  `server_stream_id` int(11) NOT NULL,
  `stream_id` int(11),
  `server_id` int(11),
  `parent_id` int(11)
);
INSERT INTO `streams_servers` VALUES (1,10,2,NULL),(2,10,3,2),(3,11,3,NULL);
CREATE TABLE `cluster_nodes` (`id` int(11) NOT NULL);
INSERT INTO `cluster_nodes` VALUES (1);
CREATE TABLE `lines_logs` (`id` int(11) NOT NULL);
INSERT INTO `lines_logs` VALUES (1),(2);
"""

TARGET = {
    "servers": ["id", "server_name", "is_main"],
    "settings": ["id", "server_name"],
    "access_codes": ["id", "code"],
    "lines": ["id", "username", "password", "new_target_col"],
    "streams": ["id", "stream_display_name", "tv_archive_server_id"],
    "streams_servers": ["server_stream_id", "stream_id", "server_id", "parent_id"],
    "lines_logs": ["id"],
}

TARGET_WITH_USERS = {
    **TARGET,
    "servers": [*TARGET["servers"], "http_broadcast_port"],
    "users":["id", "username", "password", "member_group_id", "status", "date_registered"],
    "users_groups": ["group_id", "group_name", "is_admin"],
}


@pytest.fixture
def backup(tmp_path):
    path = tmp_path / "backup.sql"
    path.write_text(BACKUP, encoding="utf-8")
    with SqlDumpSource(path) as src:
        yield src


def _script(src, imp, tmp_path):
    out = write_import_script(src, imp, tmp_path / "import.sql.gz")
    text = gzip.open(out, "rt", encoding="utf-8").read()
    return text, SqlDumpSource(out)


def test_default_keeps_system_tables_and_moves_streams_to_main(backup, tmp_path):
    imp = build_import(backup, TARGET, target_main_id=1)
    tables = {m.table for m in imp.plan.transferable}
    assert tables == {"lines", "streams", "streams_servers"}
    assert {"servers", "settings", "access_codes"} <= set(imp.kept_target)
    assert imp.skipped_logs == ["lines_logs"]
    assert imp.plan.missing_in_target == ["cluster_nodes"]
    assert imp.rows["streams_servers"] == 2           # one row per stream after re-homing

    text, script = _script(backup, imp, tmp_path)
    assert "OLDCODE" not in text and "old panel" not in text   # admin URL/settings untouched
    assert text.index("START TRANSACTION") < text.index("DELETE FROM `lines`")
    assert text.rstrip().endswith("SET FOREIGN_KEY_CHECKS = 1;")
    with script:
        ss = list(script.iter_rows("streams_servers"))
        assert Counter(r["server_id"] for r in ss) == {1: 2}
        assert all(r["parent_id"] is None for r in ss)
        archive = {r["id"]: r["tv_archive_server_id"] for r in script.iter_rows("streams")}
        assert archive == {10: 1, 11: 0}
        # Hashes copied verbatim; fork-only column dropped; `;` inside data survives.
        assert [r["password"] for r in script.iter_rows("lines")] == ["$6$hash", "x;y"]
        assert "fork_only_col" not in text


def test_import_load_balancers_as_extra_servers(backup, tmp_path):
    imp = build_import(backup, TARGET, server_mode="import", target_server_ids=[1])
    text, script = _script(backup, imp, tmp_path)
    assert "DELETE FROM `servers`" not in text        # target's main server row is kept
    with script:
        assert [r["id"] for r in script.iter_rows("servers")] == [2, 3]
        assert Counter(r["server_id"] for r in script.iter_rows("streams_servers")) == {3: 2, 2: 1}


def test_import_mode_refuses_server_id_clash(backup):
    with pytest.raises(XuiImportError, match=r"\[2\]"):
        build_import(backup, TARGET, server_mode="import", target_server_ids=[1, 2])


def test_include_system_and_logs(backup):
    imp = build_import(backup, TARGET, import_tables=("access_codes", "settings", "servers"),
                       include_logs=True)
    tables = {m.table for m in imp.plan.transferable}
    assert {"servers", "settings", "access_codes", "lines_logs"} <= tables
    # Servers come from the backup, so stream→server links are kept as they were.
    assert "streams_servers" not in imp.row_filters
    assert imp.rows["streams_servers"] == 3


def test_rejects_non_xui_source(tmp_path):
    path = tmp_path / "xc.sql"
    path.write_text("CREATE TABLE `users` (`id` int(11));\n", encoding="utf-8")
    with SqlDumpSource(path) as src, pytest.raises(XuiImportError, match="xui_migrate"):
        build_import(src, TARGET)


class FakeSsh:
    last: FakeSsh | None = None

    def __init__(self, config):
        self.commands: list[str] = []
        self.uploaded = ""
        FakeSsh.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_mysql_credentials(self, user="", password=""):
        self.creds = (user, password)

    def temp_path(self, suffix):
        return f"/tmp/xdbtool-test{suffix}"

    def mysql_cmd(self, *args, program="mysql"):
        return " ".join([program, *args])

    def run(self, command, check=True):
        self.commands.append(command)
        return (1, "", "") if command.startswith("test -x") else (0, "", "")

    def upload(self, local, remote, progress=None):
        self.uploaded = gzip.open(local, "rt", encoding="utf-8").read()

    def mysql_query(self, sql):
        if "information_schema.COLUMNS" in sql:
            return [[t, c] for t, cols in TARGET_WITH_USERS.items() for c in cols]
        if "http_broadcast_port" in sql:
            return [["8080"]]
        if "is_main" in sql:
            return [["1", "1"]]
        if "WHERE `username`" in sql:
            return [["1"]]
        if "FROM `xui`.`access_codes`" in sql:
            return [["KEEPme12", "0", "1"], ["RsLr0001", "1", "0"]]
        return [["lines", "2"], ["streams", "2"]]


def test_import_via_ssh_backs_up_first(tmp_path, monkeypatch):
    import xdbtool.core.ssh_remote as ssh_remote
    from xdbtool.core.ssh_remote import SshConfig

    monkeypatch.setattr(ssh_remote, "SshSession", FakeSsh)
    path = tmp_path / "backup.sql"
    path.write_text(BACKUP, encoding="utf-8")
    result = import_via_ssh(SshConfig(host="xui.example"), path, mysql_user="u",
                            mysql_password="pw")
    ssh = FakeSsh.last
    assert ssh.commands[-1].startswith("test -x /home/xui/tools")
    assert ssh.commands[0].startswith("bash -o pipefail -c 'mysqldump --single-transaction")
    assert ssh.commands[1] == "gunzip -c /tmp/xdbtool-test.sql.gz | mysql"
    rescue = result["rescue"]
    assert rescue.password not in ssh.uploaded             # only the hash is sent
    assert f"'{rescue.username}'" in ssh.uploaded and "$6$rounds=20000$" in ssh.uploaded
    assert ssh.uploaded.index(rescue.code) < ssh.uploaded.index("COMMIT;")
    assert result["access_codes"][0] == {"code": "KEEPme12", "type": "admin", "enabled": True,
                                         "url": "http://xui.example:8080/KEEPme12/"}
    report = "\n".join(report_lines(result))
    assert f"http://xui.example:8080/{rescue.code}/" in report
    assert rescue.password in report and "[disabled]" in report
    assert not any("pw" in c for c in ssh.commands)
    assert "USE `xui`;" in ssh.uploaded
    assert result["backup"].startswith("/root/xui-before-import-")
    assert result["row_counts"] == {"lines": 2, "streams": 2}

    plan = import_via_ssh(SshConfig(host="xui.example"), path, plan_only=True)
    assert plan["backup"] is None and FakeSsh.last.commands == []
    assert plan["rescue"] is None


def test_each_system_table_is_chosen_individually(backup, tmp_path):
    imp = build_import(backup, TARGET, import_tables=["access_codes"])
    tables = {m.table for m in imp.plan.transferable}
    assert "access_codes" in tables and not {"settings", "servers"} & tables
    assert "streams_servers" in imp.row_filters            # servers kept → streams re-homed
    text, _ = _script(backup, imp, tmp_path)
    assert "OLDCODE" in text and "old panel" not in text
    with pytest.raises(ValueError):
        build_import(backup, TARGET, import_tables=["crontab"])


def test_rescue_sql_targets_admin_group_and_hashes_password():
    r = RescueAccess.generate()
    assert len(r.code) == 8 and r.code.isalpha() and r.username.startswith("rescue_")
    code_sql, user_sql = r.sql(TARGET_WITH_USERS)
    assert "`is_admin` = 1" in code_sql and "CONCAT('[', `group_id`, ']')" in code_sql
    assert "`member_group_id`" in user_sql and r.password not in user_sql
    stored = user_sql.split("'")[3]
    salt = stored.split("$")[3]
    assert sha512_crypt(r.password, salt) == stored      # verifies like PHP crypt()
    assert "`email`" not in user_sql                      # only columns the target has


@pytest.mark.parametrize("password,salt,rounds,expected", [
    ("Hello world!", "saltstringsaltstring", 10000,
     "$6$rounds=10000$saltstringsaltst$OW1/O6BYHV6BcXZu8QVeXbDWra3Oeqh0sbHbbMCVNSnCM/"
     "UrjmM0Dp8vOuZeHBy/YTBmSK6H9qs/y3RnOaw5v."),
    ("This is just a test", "toolongsaltstring", 5000,
     "$6$rounds=5000$toolongsaltstrin$lQ8jolhgVRVhY4b5pZKaysCLi0QBxGoNeKQzQ3glMhwllF7oGDZxUhx"
     "1yxdYcz/e1JSbq3y6JMxxl8audkUEm0"),
])
def test_sha512_crypt_spec_vectors(password, salt, rounds, expected):
    assert sha512_crypt(password, salt, rounds) == expected


CONTENT_BACKUP = """\
CREATE TABLE `lines` (
  `id` int(11) NOT NULL,
  `username` varchar(255),
  `bouquet` text
);
INSERT INTO `lines` VALUES (1,'alice','[1]');
CREATE TABLE `streams` (
  `id` int(11) NOT NULL,
  `type` int(11),
  `stream_display_name` text
);
INSERT INTO `streams` VALUES (1,1,'RTP1'),(2,2,'Movie A'),(3,2,'Movie B'),(4,5,'S01E01');
CREATE TABLE `streams_servers` (
  `server_stream_id` int(11) NOT NULL,
  `stream_id` int(11),
  `server_id` int(11),
  `parent_id` int(11)
);
INSERT INTO `streams_servers` VALUES (10,1,1,NULL),(11,2,1,NULL),(12,3,1,NULL),(13,4,1,NULL);
CREATE TABLE `streams_categories` (
  `id` int(11) NOT NULL,
  `category_type` varchar(255),
  `category_name` varchar(255)
);
INSERT INTO `streams_categories` VALUES (1,'live','News'),(2,'movie','Action'),(3,'series','Drama');
CREATE TABLE `streams_series` (
  `id` int(11) NOT NULL,
  `title` varchar(255)
);
INSERT INTO `streams_series` VALUES (1,'Show');
CREATE TABLE `bouquets` (
  `id` int(11) NOT NULL,
  `bouquet_name` varchar(255),
  `bouquet_channels` text,
  `bouquet_movies` text,
  `bouquet_series` text,
  `bouquet_radios` text
);
INSERT INTO `bouquets` VALUES (1,'PT','[1]','[2,3]','[1]','[]');
"""

CONTENT_TARGET = {
    "lines": ["id", "username", "bouquet"],
    "streams": ["id", "type", "stream_display_name"],
    "streams_servers": ["server_stream_id", "stream_id", "server_id", "parent_id"],
    "streams_categories": ["id", "category_type", "category_name"],
    "streams_series": ["id", "title"],
    "bouquets": ["id", "bouquet_name", "bouquet_channels", "bouquet_movies",
                 "bouquet_series", "bouquet_radios"],
}


@pytest.fixture
def content_backup(tmp_path):
    path = tmp_path / "content.sql"
    path.write_text(CONTENT_BACKUP, encoding="utf-8")
    with SqlDumpSource(path) as src:
        yield src


def test_movies_only_replaces_just_the_movies(content_backup, tmp_path):
    imp = build_import(content_backup, CONTENT_TARGET, content=["movies"])
    tables = {m.table for m in imp.plan.transferable}
    assert tables == {"streams", "streams_servers", "streams_categories"}
    assert {"lines", "bouquets", "streams_series"} <= set(imp.not_chosen)
    text, script = _script(content_backup, imp, tmp_path)
    deletes = [line for line in text.splitlines() if line.startswith("DELETE")]
    assert deletes == [
        "DELETE x FROM `streams_servers` x JOIN `streams` s ON s.`id` = x.`stream_id` "
        "WHERE s.`type` IN (2);",
        "DELETE FROM `streams` WHERE `type` IN (2);",
        "DELETE FROM `streams_categories` WHERE `category_type` IN ('movie');",
    ]
    assert "server_stream_id" not in text       # child ids left to AUTO_INCREMENT
    with script:
        assert [r["id"] for r in script.iter_rows("streams")] == [2, 3]
        assert [r["stream_id"] for r in script.iter_rows("streams_servers")] == [2, 3]
        assert [r["category_name"] for r in script.iter_rows("streams_categories")] == ["Action"]
    assert ("streams", "id", "`type` NOT IN (2)", {2, 3}) in imp.keep_checks


def test_live_with_bouquets_clears_other_bouquet_lists(content_backup, tmp_path):
    imp = build_import(content_backup, CONTENT_TARGET, content=["live", "bouquets"])
    _, script = _script(content_backup, imp, tmp_path)
    with script:
        b = next(script.iter_rows("bouquets"))
        assert b["bouquet_channels"] == "[1]"
        assert b["bouquet_movies"] == b["bouquet_series"] == "[]"
        assert [r["type"] for r in script.iter_rows("streams")] == [1]


def test_series_brings_series_tables_and_episodes(content_backup):
    imp = build_import(content_backup, CONTENT_TARGET, content=["series"])
    assert {m.table for m in imp.plan.transferable} >= {"streams", "streams_series"}
    assert imp.rows["streams"] == 1 and imp.rows["streams_categories"] == 1


def test_no_stream_kind_leaves_all_stream_tables(content_backup):
    imp = build_import(content_backup, CONTENT_TARGET, content=["lines"])
    assert {m.table for m in imp.plan.transferable} == {"lines"}
    assert set(STREAM_TABLES) & set(CONTENT_TARGET) <= set(imp.not_chosen)
    assert any("bouquet ids" in n for n in imp.notes)


def test_all_content_is_a_full_replace(content_backup):
    imp = build_import(content_backup, CONTENT_TARGET)
    assert imp.delete_sql == {} and imp.keep_checks == []


def test_unknown_content_rejected(content_backup):
    with pytest.raises(ValueError, match="films"):
        build_import(content_backup, CONTENT_TARGET, content=["films"])


def test_kept_rows_clash_is_refused(content_backup):
    from xdbtool.core.xui_import import _check_kept_rows

    imp = build_import(content_backup, CONTENT_TARGET, content=["movies"])

    class Target:
        def mysql_query(self, sql):
            return [["3"], ["99"]] if "`type` NOT IN (2)" in sql else []

    with pytest.raises(XuiImportError, match=r"`streams` \(3\)"):
        _check_kept_rows(Target(), "xui", imp)


def test_cli_rejects_unknown_content(capsys):
    with pytest.raises(SystemExit):
        main(["xui-import", "x.sql", "--content", "movies,films"])
    assert "films" in capsys.readouterr().err
