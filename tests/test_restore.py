"""Tests for restore_dump orchestration using a fake MySQL target (no server)."""

from __future__ import annotations

from pathlib import Path

import pytest

import xdbtool.core.mysql_source as mysql_source
from xdbtool.core.mysql_source import ConnectionConfig
from xdbtool.core.restore import restore_dump

FIXTURE = Path(__file__).parent / "fixtures" / "sample_xtream.sql"


class FakeTarget:
    """Records the calls restore_dump makes instead of talking to a server."""

    instances: list[FakeTarget] = []

    def __init__(self, config):
        self.config = config
        self.created = None
        self.selected = None
        self.executed = None
        FakeTarget.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def create_database(self, name, charset="utf8mb4", drop_existing=False):
        self.created = (name, drop_existing)
        self.selected = name

    def select_database(self, name):
        self.selected = name

    def execute_script(self, statements, progress=None):
        self.executed = list(statements)
        return len(self.executed)


@pytest.fixture(autouse=True)
def _patch_target(monkeypatch):
    FakeTarget.instances.clear()
    monkeypatch.setattr(mysql_source, "MySQLSource", FakeTarget)
    yield


def test_restore_with_create():
    cfg = ConnectionConfig(host="h", user="u", password="p")
    result = restore_dump(cfg, FIXTURE, database="newdb", create=True, drop_existing=True)
    target = FakeTarget.instances[-1]
    assert target.created == ("newdb", True)
    assert target.selected == "newdb"
    # The fixture has 4 CREATE + 4 DROP + 4 INSERT statements => 12 executed.
    assert result["statements"] == len(target.executed) >= 12
    assert result["database"] == "newdb"


def test_restore_into_existing_db():
    cfg = ConnectionConfig(host="h", user="u", password="p")
    restore_dump(cfg, FIXTURE, database="existing", create=False)
    target = FakeTarget.instances[-1]
    assert target.created is None            # not created
    assert target.selected == "existing"     # just selected
    assert target.executed


def test_restore_requires_name_when_creating():
    cfg = ConnectionConfig(host="h", user="u", password="p")
    with pytest.raises(ValueError):
        restore_dump(cfg, FIXTURE, database=None, create=True)
