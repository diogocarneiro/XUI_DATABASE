# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Tests for staging an old panel dump as ``xui_migrate`` (offline, no server)."""

from __future__ import annotations

import gzip

import pytest

import xdbtool.core.mysql_source as mysql_source
from xdbtool.__main__ import main
from xdbtool.core.mysql_source import ConnectionConfig
from xdbtool.core.xui_migrate import (
    XuiMigrateError,
    load_dump,
    prepare_dump,
    stage_live,
    write_script,
)

XC_DUMP = """\
-- MySQL dump 10.13
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
CREATE DATABASE /*!32312 IF NOT EXISTS*/ `xtream_iptvpro` /*!40100 DEFAULT CHARACTER SET utf8 */;
USE `xtream_iptvpro`;
DROP TABLE IF EXISTS `users`;
CREATE TABLE `users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `username` varchar(50) NOT NULL,
  `password` varchar(50) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
LOCK TABLES `users` WRITE;
INSERT INTO `users` VALUES (1,'alice','pw;1'),(2,'bob','x');
UNLOCK TABLES;
CREATE TABLE `streams` (`id` int(11) NOT NULL, `stream_display_name` text);
INSERT INTO `streams` VALUES (1,'Channel; One');
CREATE TABLE `user_activity` (`activity_id` int(11) NOT NULL, `user_id` int(11));
INSERT INTO `user_activity` VALUES (1,1),(2,2);
INSERT INTO `user_activity` VALUES (3,1);
"""

# XUI.one keeps `users` too (admins/resellers), alongside `lines` (subscribers).
XUI_ONE_DUMP = """\
CREATE TABLE `access_codes` (`id` int(11) NOT NULL);
CREATE TABLE `lines` (`id` int(11) NOT NULL);
CREATE TABLE `users` (`id` int(11) NOT NULL);
CREATE TABLE `users_groups` (`group_id` int(11) NOT NULL);
CREATE TABLE `streams` (`id` int(11) NOT NULL);
"""


def test_prepare_redirects_database_and_skips_logs():
    p = prepare_dump(XC_DUMP)
    joined = "\n".join(p.statements)
    assert p.source_databases == ["xtream_iptvpro"]
    assert p.removed_directives == 2
    assert "USE " not in joined and "CREATE DATABASE" not in joined
    assert "LOCK TABLES" not in joined
    assert p.tables == ["users", "streams", "user_activity"]
    assert p.skipped_inserts == {"user_activity": 2}
    # Log table schema is kept (XUI may expect it); only its rows are dropped.
    assert any(s.startswith("CREATE TABLE `user_activity`") for s in p.statements)
    # Semicolons inside string literals do not split statements.
    assert "'pw;1'" in joined and "'Channel; One'" in joined


def test_keep_logs():
    p = prepare_dump(XC_DUMP, skip_logs=False)
    assert p.skipped_inserts == {}
    assert sum(s.startswith("INSERT INTO `user_activity`") for s in p.statements) == 2


def test_rejects_xui_one_dump_unless_forced():
    with pytest.raises(XuiMigrateError, match="XUI.one backup"):
        prepare_dump(XUI_ONE_DUMP)
    # Even without `lines`, two XUI-only tables are enough to tell.
    with pytest.raises(XuiMigrateError, match="XUI.one backup"):
        prepare_dump(XUI_ONE_DUMP.replace("`lines`", "`lines_x`"))
    p = prepare_dump(XUI_ONE_DUMP, force=True)
    assert p.warnings


def test_rejects_routines():
    with pytest.raises(XuiMigrateError, match="DELIMITER"):
        prepare_dump(XC_DUMP + "DELIMITER ;;\nCREATE TRIGGER t BEFORE INSERT ON users ;;\n")


def test_load_gzip(tmp_path):
    path = tmp_path / "old.sql.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(XC_DUMP)
    assert load_dump(path).tables == ["users", "streams", "user_activity"]


def test_write_script(tmp_path):
    out = write_script(prepare_dump(XC_DUMP), tmp_path / "xui_migrate.sql",
                       grant_to="xui_user@localhost")
    text = out.read_text(encoding="utf-8")
    assert text.index("DROP DATABASE IF EXISTS `xui_migrate`;") < text.index("USE `xui_migrate`;")
    assert "NO_AUTO_VALUE_ON_ZERO" in text
    assert "xtream_iptvpro" not in text.split("\n", 2)[2]  # only xui_migrate is addressed
    assert "GRANT ALL PRIVILEGES ON `xui_migrate`.* TO 'xui_user'@'localhost';" in text


class FakeServer:
    """Stands in for MySQLSource: records what stage_live does to the server."""

    databases: list[str] = []
    tables_in_existing = 0
    last: FakeServer | None = None

    def __init__(self, config):
        self.config = config
        self.calls: list[tuple] = []
        FakeServer.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_databases(self):
        return list(FakeServer.databases)

    def execute(self, sql, args=None):
        self.calls.append(("execute", sql, args))
        return [{"n": FakeServer.tables_in_existing}]

    def create_database(self, name, charset="utf8mb4", drop_existing=False, **_kw):
        self.calls.append(("create", name, drop_existing))

    def execute_script(self, statements, progress=None):
        self.statements = list(statements)
        return len(self.statements)

    def list_tables(self):
        return ["streams", "user_activity", "users"]

    def row_count(self, table):
        return {"users": 2, "streams": 1}.get(table, 0)


@pytest.fixture
def fake_server(monkeypatch):
    FakeServer.databases = ["xui"]
    FakeServer.tables_in_existing = 0
    monkeypatch.setattr(mysql_source, "MySQLSource", FakeServer)
    return FakeServer


def test_stage_live(fake_server):
    cfg = ConnectionConfig(host="xui.example", database="xui")
    result = stage_live(cfg, prepare_dump(XC_DUMP), drop_existing=True, grant_to="u@localhost")
    srv = fake_server.last
    assert srv.config.database == ""          # never connects into the live `xui` schema
    assert cfg.database == "xui"              # caller's config is not mutated
    assert ("create", "xui_migrate", True) in srv.calls
    assert srv.statements[0].startswith("SET SESSION sql_mode")
    assert result["row_counts"] == {"users": 2, "streams": 1}
    assert result["xui_server"] is True
    assert any("GRANT" in c[1] and c[2] == ("u", "localhost") for c in srv.calls)


def test_stage_live_refuses_to_overwrite(fake_server):
    fake_server.databases = ["xui", "xui_migrate"]
    fake_server.tables_in_existing = 12
    with pytest.raises(XuiMigrateError, match="already exists"):
        stage_live(ConnectionConfig(host="h"), prepare_dump(XC_DUMP))


def test_cli_check_only_and_script(tmp_path, capsys):
    dump = tmp_path / "old.sql"
    dump.write_text(XC_DUMP, encoding="utf-8")
    assert main(["xui-migrate", str(dump), "--check-only"]) == 0
    assert "xtream_iptvpro" in capsys.readouterr().out
    out = tmp_path / "stage.sql"
    assert main(["xui-migrate", str(dump), "--sql-out", str(out), "--drop"]) == 0
    assert out.is_file()
    bad = tmp_path / "xui.sql"
    bad.write_text(XUI_ONE_DUMP, encoding="utf-8")
    assert main(["xui-migrate", str(bad), "--check-only"]) == 2


class FakeSsh:
    """Stands in for SshSession: answers the remote mysql queries stage_via_ssh makes."""

    last: FakeSsh | None = None
    databases = ["xui", "xui_migrate"]
    fail_script = False

    def __init__(self, config):
        self.config = config
        self.commands: list[str] = []
        self.uploaded: str | None = None
        self.creds = None
        FakeSsh.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_mysql_credentials(self, user="", password=""):
        self.creds = (user, password)

    def temp_path(self, suffix):
        return f"/tmp/xdbtool-test{suffix}"

    def mysql_cmd(self, *args):
        return "mysql " + " ".join(args)

    def upload(self, local, remote, progress=None):
        with gzip.open(local, "rt", encoding="utf-8") as fh:
            self.uploaded = fh.read()

    def run(self, command, check=True):
        self.commands.append(command)
        return (1, "", "ERROR 1064 at line 9") if FakeSsh.fail_script else (0, "", "")

    def mysql_query(self, sql):
        if sql == "SHOW DATABASES":
            return [[d] for d in FakeSsh.databases]
        if sql.startswith("SELECT TABLE_NAME"):
            return [["users"], ["streams"], ["user_activity"]]
        if "UNION ALL" in sql or "FROM `xui_migrate`" in sql:
            return [["users", "2"], ["streams", "1"]]
        return [["5"]]  # tables already in xui_migrate


@pytest.fixture
def fake_ssh(monkeypatch):
    import xdbtool.core.ssh_remote as ssh_remote

    FakeSsh.fail_script = False
    monkeypatch.setattr(ssh_remote, "SshSession", FakeSsh)
    return FakeSsh


def test_stage_via_ssh(fake_ssh):
    from xdbtool.core.ssh_remote import SshConfig
    from xdbtool.core.xui_migrate import stage_via_ssh

    result = stage_via_ssh(SshConfig(host="xui.example"), prepare_dump(XC_DUMP),
                           mysql_user="xuiuser", mysql_password="s3cret", drop_existing=True)
    ssh = fake_ssh.last
    assert ssh.creds == ("xuiuser", "s3cret")
    assert "DROP DATABASE IF EXISTS `xui_migrate`;" in ssh.uploaded
    assert ssh.commands == ["gunzip -c /tmp/xdbtool-test.sql.gz | mysql "]
    assert not any("s3cret" in c for c in ssh.commands)   # password never on a command line
    assert result["row_counts"] == {"users": 2, "streams": 1}
    assert result["xui_server"] is True


def test_stage_via_ssh_refuses_overwrite_and_reports_mysql_errors(fake_ssh):
    from xdbtool.core.ssh_remote import SshConfig
    from xdbtool.core.xui_migrate import stage_via_ssh

    with pytest.raises(XuiMigrateError, match="already exists"):
        stage_via_ssh(SshConfig(host="h"), prepare_dump(XC_DUMP))
    fake_ssh.fail_script = True
    with pytest.raises(XuiMigrateError, match="1064"):
        stage_via_ssh(SshConfig(host="h"), prepare_dump(XC_DUMP), drop_existing=True)


def test_mysql_cnf_escapes_password():
    from xdbtool.core.ssh_remote import _mysql_cnf

    assert _mysql_cnf("u", r'a"b\c') == '[client]\nuser="u"\n' + r'password="a\"b\\c"' + "\n"


def test_write_script_gzip(tmp_path):
    out = write_script(prepare_dump(XC_DUMP), tmp_path / "x.sql.gz")
    with gzip.open(out, "rt", encoding="utf-8") as fh:
        assert "USE `xui_migrate`;" in fh.read()
