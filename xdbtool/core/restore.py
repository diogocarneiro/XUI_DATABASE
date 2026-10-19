# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Restore / import a ``.sql`` dump directly into a live MySQL/MariaDB server.

This executes the dump's statements as-is (schema + data), optionally creating the
target database first. Use it to inject a backup straight into a server instead of
producing files. It is destructive by nature — always confirm the target.
"""

from __future__ import annotations

from pathlib import Path

from .dump_source import _iter_statements, read_sql_text
from .logger import get_logger

log = get_logger(__name__)


def restore_dump(
    target_config,
    dump_path: str | Path,
    *,
    database: str | None = None,
    create: bool = False,
    drop_existing: bool = False,
    allow_drop_panel: bool = False,
    progress=None,
) -> dict:
    """Execute a ``.sql`` dump against the target server.

    ``target_config`` is a :class:`ConnectionConfig`.
    - ``database`` selects (or, with ``create``, creates) the schema to load into.
      If omitted, the dump is expected to contain its own ``CREATE/USE DATABASE``.
    - ``create`` creates ``database`` first (with ``drop_existing`` to replace it).
    """
    from .mysql_source import MySQLSource  # local import keeps core import-light

    dump_path = Path(dump_path)
    if not dump_path.is_file():
        raise FileNotFoundError(dump_path)

    cfg = target_config
    # Connect without pre-selecting a schema when we must create it first.
    cfg.database = "" if create else (database or cfg.database)

    sql_text = read_sql_text(dump_path)
    statements = [s for s in _iter_statements(sql_text) if s.strip()]

    with MySQLSource(cfg) as target:
        if create:
            if not database:
                raise ValueError("create=True requires a database name.")
            target.create_database(database, drop_existing=drop_existing,
                                   allow_drop_panel=allow_drop_panel)
        elif database:
            target.select_database(database)
        count = target.execute_script(statements, progress=progress)

    log.info("Restored %s into %s (%d statements)", dump_path.name, database or cfg.database, count)
    return {"database": database or cfg.database, "statements": count, "source": dump_path.name}
