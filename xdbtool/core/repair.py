# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Database maintenance: CHECK / REPAIR / ANALYZE / OPTIMIZE for live MySQL sources."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .logger import get_logger
from .mysql_source import MySQLSource
from .source import DataSource, SourceError

log = get_logger(__name__)

ProgressFn = Callable[[str, int, int], None]


@dataclass
class MaintenanceResult:
    table: str
    operation: str
    status: str      # e.g. "OK", "error"
    message: str = ""
    ok: bool = True


def _quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _run(source: MySQLSource, op: str, table: str) -> MaintenanceResult:
    verb = {
        "check": "CHECK TABLE",
        "repair": "REPAIR TABLE",
        "analyze": "ANALYZE TABLE",
        "optimize": "OPTIMIZE TABLE",
    }[op]
    try:
        with source._cursor() as cur:  # noqa: SLF001 - intentional internal use
            cur.execute(f"{verb} {_quote(table)}")
            rows = list(cur.fetchall())
        # MySQL returns one or more rows with Msg_type / Msg_text.
        msg_type = ""
        msg_text = ""
        for r in rows:
            msg_type = (r.get("Msg_type") or msg_type)
            msg_text = r.get("Msg_text") or msg_text
            if (r.get("Msg_type") or "").lower() == "error":
                return MaintenanceResult(table, op, "error", r.get("Msg_text", ""), ok=False)
        ok = msg_type.lower() in ("", "status", "note", "info") or msg_text.lower() == "ok"
        return MaintenanceResult(table, op, msg_type or "OK", msg_text or "OK", ok=ok)
    except Exception as exc:  # noqa: BLE001
        return MaintenanceResult(table, op, "error", str(exc), ok=False)


def run_maintenance(
    source: DataSource,
    tables: list[str],
    operation: str = "check",
    progress: ProgressFn | None = None,
) -> list[MaintenanceResult]:
    """Run a maintenance ``operation`` over ``tables``. MySQL sources only."""
    if not isinstance(source, MySQLSource):
        raise SourceError("Maintenance is only available on a live MySQL/MariaDB connection.")
    if operation not in ("check", "repair", "analyze", "optimize"):
        raise ValueError(f"Unknown operation: {operation}")

    results: list[MaintenanceResult] = []
    total = len(tables)
    for i, table in enumerate(tables, 1):
        log.info("%s %s", operation, table)
        results.append(_run(source, operation, table))
        if progress:
            progress(table, i, total)
    return results


def check_and_repair(
    source: DataSource,
    tables: list[str],
    progress: ProgressFn | None = None,
) -> list[MaintenanceResult]:
    """CHECK every table, then REPAIR only those that did not report OK."""
    checked = run_maintenance(source, tables, "check", progress)
    broken = [r.table for r in checked if not r.ok]
    if not broken:
        return checked
    log.warning("Repairing %d table(s): %s", len(broken), ", ".join(broken))
    repaired = run_maintenance(source, broken, "repair", progress)
    return checked + repaired
