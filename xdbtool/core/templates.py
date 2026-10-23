# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Panel schema templates: register base ``.sql`` schemas and recreate fresh DBs.

A *template* is a vanilla (freshly-installed) panel database dump. Templates let
the tool:

* recreate a clean database for a panel on a target MySQL server, and
* fingerprint an unknown database by comparing its table set to known templates.

The ``.sql`` files themselves stay **local** (they are the panel software's own
schema, not the user's data, and are git-ignored). Only lightweight derived
metadata — table names, which tables carry seed rows — is kept in the manifest.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .dump_source import _CREATE_RE, _INSERT_RE, SqlDumpSource, _iter_statements, read_sql_text
from .logger import get_logger

log = get_logger(__name__)

_AUTO_INC = re.compile(r"\s*\bAUTO_INCREMENT\s*=\s*\d+", re.I)  # table option, not the column flag


def templates_dir() -> Path:
    """Local directory holding registered template ``.sql`` files + manifest.

    ``$XDBTOOL_TEMPLATES`` wins; a source checkout uses its own ``templates/``; the
    packaged app and pip installs use the user's config dir — never the current
    directory, which would scatter ``templates/`` folders wherever the CLI is run.
    """
    override = os.environ.get("XDBTOOL_TEMPLATES")
    repo = Path(__file__).resolve().parents[2] / "templates"
    if override:
        base = Path(override)
    elif not getattr(sys, "frozen", False) and (repo / "README.md").is_file():
        base = repo
    else:
        from .config import config_dir

        base = config_dir() / "templates"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _manifest_path() -> Path:
    return templates_dir() / "manifest.json"


@dataclass
class TemplateMeta:
    key: str                       # unique id, e.g. "xui_one-1.5.13"
    name: str                      # display name
    panel_key: str                 # profile key, e.g. "xui_one"
    version: str = ""
    sql_filename: str = ""         # file inside templates_dir()
    table_count: int = 0
    tables: list[str] = field(default_factory=list)
    seed_tables: list[str] = field(default_factory=list)  # tables with INSERT rows

    def sql_path(self) -> Path:
        return templates_dir() / self.sql_filename


def load_manifest() -> list[TemplateMeta]:
    path = _manifest_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [TemplateMeta(**t) for t in data.get("templates", [])]
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read templates manifest: %s", exc)
        return []


def save_manifest(templates: list[TemplateMeta]) -> None:
    path = _manifest_path()
    path.write_text(
        json.dumps({"templates": [asdict(t) for t in templates]}, indent=2),
        encoding="utf-8",
    )


def _slugify(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text).strip("_")


def analyse_schema(sql_path: str | Path) -> tuple[list[str], list[str]]:
    """Return (all_tables, seed_tables) for a schema dump."""
    with SqlDumpSource(sql_path) as src:
        tables = src.list_tables()
        seed = [t for t in tables if src.row_count(t) > 0]
    return tables, seed


def register_template(
    sql_path: str | Path,
    *,
    name: str,
    panel_key: str,
    version: str = "",
    key: str | None = None,
    copy: bool = True,
) -> TemplateMeta:
    """Parse a base schema, copy it into the templates dir and record metadata."""
    sql_path = Path(sql_path)
    if not sql_path.is_file():
        raise FileNotFoundError(sql_path)

    key = key or _slugify(f"{panel_key}-{version}" if version else panel_key)
    tables, seed = analyse_schema(sql_path)

    dest_name = f"{key}.sql"
    if copy:
        dest = templates_dir() / dest_name
        if sql_path.resolve() != dest.resolve():
            shutil.copy2(sql_path, dest)
    else:
        dest_name = sql_path.name

    meta = TemplateMeta(
        key=key, name=name, panel_key=panel_key, version=version,
        sql_filename=dest_name, table_count=len(tables),
        tables=tables, seed_tables=seed,
    )
    manifest = [t for t in load_manifest() if t.key != key]
    manifest.append(meta)
    save_manifest(manifest)
    log.info("Registered template %s (%d tables, %d seeded)", key, len(tables), len(seed))
    return meta


def get_template(key: str) -> TemplateMeta | None:
    return next((t for t in load_manifest() if t.key == key), None)


def list_templates() -> list[TemplateMeta]:
    return load_manifest()


def _sql_of(key: str) -> tuple[TemplateMeta, str]:
    meta = get_template(key)
    if meta is None:
        raise ValueError(f"No such template: {key}")
    if not meta.sql_path().is_file():
        raise FileNotFoundError(f"Template SQL missing on disk: {meta.sql_path()}")
    return meta, read_sql_text(meta.sql_path())


@dataclass
class SeedTable:
    """A template table that has rows, and whether cleaning suggests removing them."""

    name: str
    size: int          # bytes of its INSERT statements
    suggested: bool    # holds a panel's data, accounts, addresses or block lists


def seed_tables(key: str) -> list[SeedTable]:
    """The template's tables with rows, largest first."""
    from .rebuild import CLEAN_TABLES  # local import: rebuild imports this module

    _meta, text = _sql_of(key)
    sizes: dict[str, int] = {}
    for stmt in _iter_statements(text):
        m = _INSERT_RE.match(stmt.lstrip())
        if m:
            sizes[m.group(1)] = sizes.get(m.group(1), 0) + len(stmt)
    return sorted((SeedTable(t, n, t.lower() in CLEAN_TABLES) for t, n in sizes.items()),
                  key=lambda s: (-s.size, s.name))


def clean_template(key: str, drop_rows, *, new_key: str | None = None, name: str | None = None,
                   progress=None) -> TemplateMeta:
    """Register a copy of template ``key`` without the rows of the ``drop_rows`` tables.

    The schema is kept whole; the emptied tables also lose their ``AUTO_INCREMENT=N``
    so new rows start again at 1. The original template is left untouched.
    """
    meta, text = _sql_of(key)
    new_key = _slugify(new_key or f"{key}-clean")
    if new_key == key:
        raise ValueError("The clean template needs a key of its own.")
    drop = {t.lower() for t in drop_rows}
    dest = templates_dir() / f"{new_key}.sql"
    tmp = dest.with_name(dest.name + ".part")
    total, done, removed = len(text), 0, 0
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"-- Clean copy of template '{key}' made by XUI DB Tool: rows removed "
                 f"from {', '.join(sorted(drop)) or 'no table'}.\n")
        for n, stmt in enumerate(_iter_statements(text), 1):
            done += len(stmt)
            s = stmt.strip()
            m = _INSERT_RE.match(s)
            if m and m.group(1).lower() in drop:
                removed += 1
                continue
            m = _CREATE_RE.match(s)
            if m and m.group(1).lower() in drop:
                s = _AUTO_INC.sub("", s)
            if s:
                fh.write(s + ";\n")
            if progress and n % 500 == 0:
                progress("cleaning", done, total)
    tmp.replace(dest)
    if progress:
        progress("cleaning", total, total)
    log.info("Cleaned template %s -> %s (%d INSERT statements removed)", key, new_key, removed)
    return register_template(dest, name=name or f"{meta.name} (clean)", panel_key=meta.panel_key,
                             version=meta.version, key=new_key)


# Derived, name-only signatures shipped with the package (no SQL redistributed).
# Used for detection on a fresh clone where no local templates are registered.
PACKAGED_SIGNATURES = Path(__file__).resolve().parent.parent / "data" / "panel_signatures.json"


def load_packaged_signatures() -> list[TemplateMeta]:
    if not PACKAGED_SIGNATURES.is_file():
        return []
    try:
        data = json.loads(PACKAGED_SIGNATURES.read_text(encoding="utf-8"))
        return [TemplateMeta(**t) for t in data.get("templates", [])]
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read packaged signatures: %s", exc)
        return []


def export_signatures(out_path: str | Path | None = None) -> Path:
    """Write name-only signatures (no SQL) for the repo from the local manifest."""
    out = Path(out_path) if out_path else PACKAGED_SIGNATURES
    out.parent.mkdir(parents=True, exist_ok=True)
    sigs = [
        {"key": m.key, "name": m.name, "panel_key": m.panel_key,
         "version": m.version, "table_count": m.table_count, "tables": m.tables}
        for m in load_manifest()
    ]
    out.write_text(json.dumps({"templates": sigs}, indent=2), encoding="utf-8")
    log.info("Exported %d signature(s) to %s", len(sigs), out)
    return out


def _all_signatures() -> list[TemplateMeta]:
    """Local manifest if present, else the packaged name-only signatures."""
    return load_manifest() or load_packaged_signatures()


def match_template(tables: list[str]) -> tuple[TemplateMeta | None, float]:
    """Return the template whose table set best matches ``tables`` (Jaccard)."""
    present = {t.lower() for t in tables}
    best: tuple[TemplateMeta | None, float] = (None, 0.0)
    for meta in _all_signatures():
        tmpl = {t.lower() for t in meta.tables}
        if not tmpl:
            continue
        inter = len(present & tmpl)
        union = len(present | tmpl)
        score = inter / union if union else 0.0  # Jaccard similarity
        if score > best[1]:
            best = (meta, round(score, 3))
    return best


def create_from_template(
    target_config,
    key: str,
    db_name: str,
    *,
    drop_existing: bool = False,
    allow_drop_panel: bool = False,
    progress=None,
):
    """Create ``db_name`` on the target server and load the template schema in.

    ``target_config`` is a :class:`ConnectionConfig`; its ``database`` is ignored
    (the new one is created and selected). Dropping a database that holds panel
    data needs ``allow_drop_panel`` (see :meth:`MySQLSource.create_database`).
    """
    from .mysql_source import MySQLSource  # local import keeps core import-light
    from .xui_migrate import _DB_DIRECTIVE, _LOCKS, SESSION_PREAMBLE

    meta, sql_text = _sql_of(key)
    cfg = target_config
    cfg.database = ""  # connect without selecting a schema
    # The template's own CREATE DATABASE / USE would send the tables elsewhere.
    statements = [s for s in (st.strip() for st in _iter_statements(sql_text))
                  if s and not _DB_DIRECTIVE.match(s) and not _LOCKS.match(s)]

    with MySQLSource(cfg) as target:
        target.create_database(db_name, drop_existing=drop_existing,
                               allow_drop_panel=allow_drop_panel)
        # Relaxed sql_mode as mysqldump expects: zero-date defaults load on MySQL 8 too.
        count = target.execute_script([*SESSION_PREAMBLE, *statements],
                                      progress=progress) - len(SESSION_PREAMBLE)
    log.info("Created %s from template %s (%d statements)", db_name, key, count)
    return {"database": db_name, "template": key, "statements": count}
