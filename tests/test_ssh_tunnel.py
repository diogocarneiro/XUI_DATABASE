# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""SSH tunnel and MySQL-over-SSH wiring, with the SSH side faked (no server)."""

from __future__ import annotations

import socket
import threading

import pytest

import xdbtool.core.mysql_source as mysql_source
import xdbtool.core.ssh_remote as ssh_remote
from xdbtool.core.config import ConnectionProfile, Store, load_store, save_store
from xdbtool.core.mysql_source import ConnectionConfig, MySQLSource
from xdbtool.core.source import SourceError
from xdbtool.core.ssh_remote import SshConfig, SshError, SshTunnel


class EchoTransport:
    """open_channel() returns a socket whose far end echoes, like a remote MySQL."""

    def __init__(self):
        self.opened: list[tuple] = []

    def open_channel(self, kind, dest, origin):
        self.opened.append((kind, dest))
        near, far = socket.socketpair()

        def echo():
            with far:
                while data := far.recv(4096):
                    far.sendall(data.upper())

        threading.Thread(target=echo, daemon=True).start()
        return near


class FakeClient:
    def __init__(self):
        self.transport = EchoTransport()
        self.closed = False

    def get_transport(self):
        return self.transport

    def close(self):
        self.closed = True


def test_tunnel_forwards_bytes(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(ssh_remote, "connect_client", lambda cfg: client)
    with SshTunnel(SshConfig(host="xui.example"), "127.0.0.1", 3306) as tunnel:
        assert tunnel.local_port > 0
        with socket.create_connection(("127.0.0.1", tunnel.local_port), timeout=5) as s:
            s.sendall(b"select 1")
            assert s.recv(100) == b"SELECT 1"
    assert client.transport.opened == [("direct-tcpip", ("127.0.0.1", 3306))]
    assert client.closed


def test_mysql_source_connects_through_tunnel(monkeypatch):
    class FakeTunnel:
        stopped = False

        def __init__(self, cfg, host, port):
            self.target = (host, port)

        def start(self):
            return 40001

        def stop(self):
            FakeTunnel.stopped = True

    seen = {}

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(ssh_remote, "SshTunnel", FakeTunnel)
    monkeypatch.setattr(mysql_source.pymysql, "connect", lambda **kw: seen.update(kw) or Conn())
    cfg = ConnectionConfig(host="127.0.0.1", port=3306, user="xui", password="p",
                           database="xui", ssh=SshConfig(host="192.0.2.10", password="s"))
    src = MySQLSource(cfg)
    assert src.label == "xui@192.0.2.10 (SSH)/xui"
    src.open()
    assert (seen["host"], seen["port"]) == ("127.0.0.1", 40001)
    src.close()
    assert FakeTunnel.stopped
    assert "s" not in str(cfg.masked()["ssh"])  # SSH password never in logs


def test_mysql_source_reports_ssh_failure(monkeypatch):
    class BadTunnel:
        def __init__(self, *a):
            pass

        def start(self):
            raise SshError("SSH connection to root@h:22 failed: auth")

        def stop(self):
            pass

    monkeypatch.setattr(ssh_remote, "SshTunnel", BadTunnel)
    src = MySQLSource(ConnectionConfig(ssh=SshConfig(host="h")))
    with pytest.raises(SourceError, match="auth"):
        src.open()


def test_profile_keeps_ssh_settings_but_not_unsaved_passwords():
    store = Store([ConnectionProfile(name="xui", user="u", password="pw", use_ssh=True,
                                     ssh_host="192.0.2.10", ssh_password="sshpw")])
    save_store(store)
    loaded = load_store().profiles[0]
    assert loaded.use_ssh and loaded.ssh_host == "192.0.2.10"
    assert loaded.password == "" and loaded.ssh_password == ""


class _Cursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=None):
        self.log.append(sql)


def _source_with_panel(rows):
    src = MySQLSource(ConnectionConfig())
    executed: list[str] = []
    src._cursor = lambda: _Cursor(executed)
    src.panel_rows = lambda name: rows
    return src, executed


def test_drop_of_a_panel_database_is_refused():
    from xdbtool.core.mysql_source import DropRefused

    src, executed = _source_with_panel({"lines": 485, "streams": 6263})
    with pytest.raises(DropRefused) as info:
        src.create_database("xui", drop_existing=True)
    assert info.value.rows == {"lines": 485, "streams": 6263}
    assert not any("DROP" in s for s in executed)          # nothing touched

    src.create_database("xui", drop_existing=True, allow_drop_panel=True)
    assert executed[0] == "DROP DATABASE IF EXISTS `xui`"


def test_drop_of_an_empty_database_needs_no_extra_confirmation():
    src, executed = _source_with_panel({})
    src.create_database("xui_test", drop_existing=True)
    assert executed[0] == "DROP DATABASE IF EXISTS `xui_test`"


def test_cli_init_db_stops_on_panel_drop(monkeypatch, capsys):
    import xdbtool.core.templates as templates
    from xdbtool.__main__ import main
    from xdbtool.core.mysql_source import DropRefused

    seen = {}

    def fake_create(cfg, key, db, **kw):
        seen.update(kw, ssh=cfg.ssh)
        raise DropRefused(db, {"lines": 3})

    monkeypatch.setattr(templates, "create_from_template", fake_create)
    rc = main(["init-db", "--template", "t", "--target-host", "127.0.0.1",
               "--target-database", "xui", "--drop", "--ssh-host", "192.0.2.10"])
    assert rc == 2 and "--allow-drop-panel" in capsys.readouterr().err
    assert seen["allow_drop_panel"] is False and seen["ssh"].host == "192.0.2.10"
