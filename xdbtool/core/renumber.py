# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Renumber stream ids: compact ``streams.id`` to 1..N and update every reference.

After old channels are deleted the remaining streams keep their high ids (e.g. the
first channel is 300). This module gives them consecutive ids again, keeping their
order, and rewrites every place that stores a stream id: child tables
(``streams_servers``, episodes, options…), the JSON lists of bouquets and MAG
favourites, and every other ``stream_id`` column (activity logs, queue…).

Stream ids are part of every playback URL (``…/live/user/pass/300.ts``) and of the
file names of movies/episodes on disk, so the plan also lists what must happen on
the server afterwards (restart, client playlists, file renames — see
:func:`files_script`).
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..i18n import N_, _
from .exporter import ExportOptions, _sql_literal, export_tables
from .integrity import FAMILIES, _item_key, _key, _Schema, detect_family
from .logger import get_logger
from .mysql_source import MySQLSource, _quote_ident
from .source import DataSource, SourceError

log = get_logger(__name__)

ProgressFn = Callable[[str, int, int], None]

TABLE = "streams"
# Added to ids during the update so no intermediate value can clash with a real id
# (a unique key such as streams.id or (stream_id, server_id) is checked row by row).
OFFSET = 1_000_000_000
_MARK = "#xdb#"   # JSON values are moved through a marked value for the same reason
_RUNS_PER_STATEMENT = 200

# Orphan policies: what happens to a reference to a stream that no longer exists
# whose value is one of the *new* ids (it would silently point at another stream).
REFUSE = "refuse"   # stop: fix it with the integrity check first
CLEAR = "clear"     # set it to NULL / 0 (logs, runtime state)
PRUNE = "prune"     # drop it from the JSON list (MAG favourites)

# `stream_id` columns that do not hold this panel's stream ids.
FOREIGN_STREAM_IDS = frozenset({"providers_streams"})   # the provider's own ids
# Further references that the integrity rules do not cover.
EXTRA_SCALARS = (("mag_devices", "last_itv_id"),)
EXTRA_LISTS = (("mag_devices", "fav_channels"),)

TYPE_LABELS = {1: N_("live"), 2: N_("movie"), 3: N_("created live"), 4: N_("radio"),
               5: N_("episode")}
# "type" order: live channels first, then radio, movies and episodes.
TYPE_ORDER = {1: 0, 3: 1, 4: 2, 2: 3, 5: 4}

# Where the panels keep files named by stream id (movies/episodes, TV archive).
FILE_DIRS = {
    "xui_one": ("/home/xui/content/vod", "/home/xui/content/archive"),
    "xtream": ("/home/xtreamcodes/iptv_xtream_codes/movies",
               "/home/xtreamcodes/iptv_xtream_codes/tv_archive"),
}


class RenumberError(Exception):
    """The renumbering cannot be planned or applied safely."""


@dataclass
class RefPlan:
    table: str
    column: str
    json: bool
    policy: str
    nullable: bool = True
    rows: int = 0                                     # rows whose ids change
    orphans: Counter = field(default_factory=Counter)  # clashing missing id -> rows
    invalid: int = 0                                  # JSON values that are not id lists

    @property
    def where(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass
class RenumberPlan:
    family: str
    start: int
    order: str
    total: int = 0
    mapping: dict[int, int] = field(default_factory=dict)   # changed ids only
    old_range: tuple[int, int] = (0, 0)
    new_range: tuple[int, int] = (0, 0)
    changed_by_type: Counter = field(default_factory=Counter)
    refs: list[RefPlan] = field(default_factory=list)
    json_updates: dict[tuple[str, str], dict] = field(default_factory=dict)

    @property
    def problems(self) -> list[str]:
        out = []
        for ref in self.refs:
            if ref.invalid:
                out.append(_("{where}: {rows} value(s) are not valid id lists — fix them "
                             "first (see Integrity).", where=ref.where, rows=ref.invalid))
            if ref.orphans and ref.policy == REFUSE:
                ids = ", ".join(str(i) for i in sorted(ref.orphans)[:10])
                out.append(_("{where}: {rows} row(s) point to deleted streams whose ids "
                             "({ids}) will be reused — run the Integrity check and apply its "
                             "fixes first.", where=ref.where, rows=sum(ref.orphans.values()),
                             ids=ids))
        return out

    @property
    def tables(self) -> list[str]:
        """Tables the renumbering changes."""
        out = [TABLE] if self.mapping else []
        return out + sorted({r.table for r in self.refs if r.rows or r.orphans} - {TABLE})

    def summary_lines(self) -> list[str]:
        lines = [_("Panel family: {family}", family=FAMILIES[self.family][0]),
                 _("Streams: {count}", count=self.total)]
        if not self.mapping:
            lines.append(_("The stream ids are already consecutive from {start}; nothing to "
                           "renumber.", start=self.start))
            return lines
        kinds = ", ".join(f"{_(TYPE_LABELS.get(t, str(t)))}: {n}"
                          for t, n in sorted(self.changed_by_type.items(), key=str))
        lines += [
            _("Ids {old_first}–{old_last} become {new_first}–{new_last}.",
              old_first=self.old_range[0], old_last=self.old_range[1],
              new_first=self.new_range[0], new_last=self.new_range[1]),
            _("Streams that change id: {count} ({kinds})", count=len(self.mapping),
              kinds=kinds),
            _("References updated:"),
        ]
        for ref in self.refs:
            if not (ref.rows or ref.orphans):
                continue
            text = "  " + _("{where}: {rows} row(s)", where=ref.where, rows=ref.rows)
            if ref.orphans and ref.policy != REFUSE:
                what = _("cleared") if ref.policy == CLEAR else _("removed")
                text += "; " + _("{rows} reference(s) to deleted streams {what}",
                                 rows=sum(ref.orphans.values()), what=what)
            lines.append(text)
        problems = self.problems
        if problems:
            lines.append(_("Cannot renumber yet:"))
            lines += ["  " + p for p in problems]
        lines += [_("After renumbering:")] + ["  " + n for n in after_notes()]
        return lines


def after_notes() -> list[str]:
    return [
        _("Restart XUI (or every stream) so running streams and caches use the new ids."),
        _("Playback URLs change (…/live/user/pass/ID.ts): clients must reload their M3U "
          "playlist; Xtream-API apps refresh on their next update."),
        _("Movies/episodes stored on the server as ID.ext and TV archive folders must be "
          "renamed: save and run the file-rename script on the server."),
    ]


# ------------------------------------------------------------------ helpers
def remap_json(raw, mapping: dict, drop: frozenset = frozenset()):
    """``raw`` with every stream id in ``mapping`` replaced (and ``drop`` ids removed).

    Handles a JSON list (``[1,"2"]``), a JSON object of lists (favourites per kind)
    and a single id. Returns ``raw`` itself when nothing changes; raises
    ``ValueError`` when ``raw`` is not one of those.
    """
    if raw is None:
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    s = str(raw).strip()
    if s in ("", "0", "null"):
        return raw
    if s.lstrip("-").isdigit():
        k = int(s)
        if k not in mapping:
            return raw
        return str(mapping[k]) if isinstance(raw, str) else mapping[k]
    value = json.loads(s)  # ValueError for junk

    def item(v):
        k = _item_key(v)
        if k is None:
            raise ValueError(f"not an id: {v!r}")
        if k not in mapping:
            return v
        return str(mapping[k]) if isinstance(v, str) else mapping[k]

    def walk_list(items):
        return [item(v) for v in items if _item_key(v) not in drop]

    if isinstance(value, list):
        new = walk_list(value)
    elif isinstance(value, dict):
        new = {k: walk_list(v) if isinstance(v, list) else v for k, v in value.items()}
    elif isinstance(value, int) and not isinstance(value, bool):
        new = mapping.get(value, value)
    else:
        raise ValueError(f"not an id list: {s[:40]!r}")
    return raw if new == value else json.dumps(new, separators=(",", ":"))


def _ids_in(raw) -> list:
    """Every id inside a JSON id value (list, object of lists or single id)."""
    if raw is None:
        return []
    s = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    s = s.strip()
    if s in ("", "0", "null"):
        return []
    if s.lstrip("-").isdigit():
        return [int(s)]
    value = json.loads(s)
    if isinstance(value, dict):
        value = [v for vs in value.values() if isinstance(vs, list) for v in vs]
    elif not isinstance(value, list):
        value = [value]
    keys = [_item_key(v) for v in value]
    if any(k is None for k in keys):
        raise ValueError("not an id list")
    return keys


def _runs(mapping: dict[int, int]) -> list[tuple[int, int, int]]:
    """``(first, last, delta)`` runs of consecutive old ids that move by the same delta."""
    runs: list[list[int]] = []
    for old in sorted(mapping):
        delta = mapping[old] - old
        if runs and runs[-1][1] == old - 1 and runs[-1][2] == delta:
            runs[-1][1] = old
        else:
            runs.append([old, old, delta])
    return [tuple(r) for r in runs]


def _shift_statements(table: str, column: str, runs) -> list[str]:
    """Two-phase update of a scalar id column: old → new + OFFSET, then − OFFSET."""
    t, c = _quote_ident(table), _quote_ident(column)
    out = []
    for i in range(0, len(runs), _RUNS_PER_STATEMENT):
        chunk = runs[i:i + _RUNS_PER_STATEMENT]
        conds = [f"{c} = {lo}" if lo == hi else f"{c} BETWEEN {lo} AND {hi}"
                 for lo, hi, _d in chunk]
        cases = " ".join(f"WHEN {cond} THEN {c} + {d + OFFSET}"
                         for cond, (_lo, _hi, d) in zip(conds, chunk, strict=True))
        out.append(f"UPDATE {t} SET {c} = CASE {cases} END WHERE {' OR '.join(conds)}")
    out.append(f"UPDATE {t} SET {c} = {c} - {OFFSET} WHERE {c} >= {OFFSET}")
    return out


# --------------------------------------------------------------------- plan
def _references(schema: _Schema, family: str) -> list[RefPlan]:
    links = FAMILIES[family][1]
    refs: list[RefPlan] = []
    seen: set[tuple[str, str]] = set()

    def add(table: str, column: str, as_json: bool, policy: str) -> None:
        real, col = schema.table(table), schema.column(table, column)
        if not real or not col or (real.lower(), col.lower()) in seen:
            return
        seen.add((real.lower(), col.lower()))
        nullable = schema.columns(real)[col.lower()].nullable
        refs.append(RefPlan(real, col, as_json, policy, nullable))

    for lk in links:
        if lk.target == TABLE and lk.target_column == "id":
            add(lk.table, lk.column, lk.many, REFUSE)
    for table, column in EXTRA_LISTS:
        add(table, column, True, PRUNE)
    for table, column in EXTRA_SCALARS:
        add(table, column, False, CLEAR)
    for low, real in sorted(schema.names.items()):
        if low == TABLE or low in FOREIGN_STREAM_IDS:
            continue
        if schema.column(real, "stream_id"):
            add(real, "stream_id", False, CLEAR)
    return refs


def build_renumber(source: DataSource, *, start: int = 1, order: str = "id",
                   progress: ProgressFn | None = None) -> RenumberPlan:
    """Plan new consecutive stream ids from ``start`` (``order``: "id" or "type")."""
    if order not in ("id", "type"):
        raise ValueError("order must be 'id' or 'type'")
    if start < 1:
        raise ValueError("start must be 1 or more")
    schema = _Schema(source)
    family = detect_family(schema.names)
    if not schema.has(TABLE, "id"):
        raise RenumberError(_("This database has no `streams` table with an `id` column."))
    plan = RenumberPlan(family=family, start=start, order=order)
    has_type = schema.has(TABLE, "type")
    cols = [schema.column(TABLE, "id")] + ([schema.column(TABLE, "type")] if has_type else [])
    rows = []
    for row in source.iter_columns(schema.table(TABLE), cols):
        k = _item_key(row[0])
        if k is None:
            raise RenumberError(_("streams.id holds a value that is not a number: {value}",
                                  value=row[0]))
        rows.append((k, _item_key(row[1]) if has_type else None))
    plan.total = len(rows)
    if not rows:
        return plan
    if order == "type":
        rows.sort(key=lambda r: (TYPE_ORDER.get(r[1], 9), r[0]))
    else:
        rows.sort()
    old_ids = [r[0] for r in rows]
    if max(old_ids) >= OFFSET or start + len(rows) >= OFFSET:
        raise RenumberError(_("Stream ids are too large to renumber safely."))
    new_of = {old: start + i for i, old in enumerate(old_ids)}
    plan.mapping = {o: n for o, n in new_of.items() if o != n}
    plan.old_range = (min(old_ids), max(old_ids))
    plan.new_range = (start, start + len(rows) - 1)
    type_of = dict(rows)
    plan.changed_by_type = Counter(type_of[o] for o in plan.mapping)
    if not plan.mapping:
        return plan

    existing = set(old_ids)
    new_ids = set(new_of.values())
    plan.refs = _references(schema, family)
    for n, ref in enumerate(plan.refs, 1):
        values = Counter(v for (v,) in source.iter_columns(ref.table, [ref.column]))
        if ref.json:
            updates = {}
            for raw, count in values.items():
                try:
                    ids = _ids_in(raw)
                except ValueError:
                    ref.invalid += count
                    continue
                clash = {i for i in ids if i not in existing and i in new_ids}
                for i in clash:
                    ref.orphans[i] += count
                drop = frozenset(clash) if ref.policy == PRUNE else frozenset()
                new = remap_json(raw, plan.mapping, drop)
                if new != raw:
                    updates[raw] = new
                    ref.rows += count
            plan.json_updates[(ref.table, ref.column)] = updates
        else:
            for raw, count in values.items():
                k = _key(raw)
                if k is None:
                    continue
                if not isinstance(k, int) or k >= OFFSET:
                    raise RenumberError(_("{where} holds a value that cannot be renumbered "
                                          "safely: {value}", where=ref.where, value=raw))
                if k in plan.mapping:
                    ref.rows += count
                elif k not in existing and k in new_ids:
                    ref.orphans[k] += count
        if progress:
            progress(ref.table, n, len(plan.refs))
    log.info("Renumber plan (%s): %d of %d stream(s) change id", family, len(plan.mapping),
             plan.total)
    return plan


# ---------------------------------------------------------------------- SQL
def renumber_statements(plan: RenumberPlan) -> list[str]:
    """The SQL that applies ``plan`` (refuses while :attr:`RenumberPlan.problems` exist)."""
    if plan.problems:
        raise RenumberError("\n".join(plan.problems))
    if not plan.mapping:
        return []
    stmts: list[str] = []
    # 1. Neutralise references to deleted streams whose ids are about to be reused.
    for ref in plan.refs:
        if ref.orphans and not ref.json and ref.policy == CLEAR:
            empty = "NULL" if ref.nullable else "0"
            ids = ", ".join(str(i) for i in sorted(ref.orphans))
            stmts.append(f"UPDATE {_quote_ident(ref.table)} SET {_quote_ident(ref.column)} = "
                         f"{empty} WHERE {_quote_ident(ref.column)} IN ({ids})")
    # 2. JSON id lists: every distinct value once, through a marked value.
    for (table, column), updates in plan.json_updates.items():
        t, c = _quote_ident(table), _quote_ident(column)
        for raw, new in updates.items():
            stmts.append(f"UPDATE {t} SET {c} = {_sql_literal(_MARK + str(new))} "
                         f"WHERE {c} = {_sql_literal(raw)}")
        for new in dict.fromkeys(str(v) for v in updates.values()):
            stmts.append(f"UPDATE {t} SET {c} = {_sql_literal(new)} "
                         f"WHERE {c} = {_sql_literal(_MARK + new)}")
    # 3. Scalar ids, the streams themselves included.
    runs = _runs(plan.mapping)
    stmts += _shift_statements(TABLE, "id", runs)
    for ref in plan.refs:
        if not ref.json and ref.rows:
            stmts += _shift_statements(ref.table, ref.column, runs)
    return stmts


def auto_increment_sql(plan: RenumberPlan) -> str:
    # ALTER TABLE commits implicitly, so it runs after the transaction.
    return f"ALTER TABLE {_quote_ident(TABLE)} AUTO_INCREMENT = {plan.new_range[1] + 1}"


def write_renumber_script(plan: RenumberPlan, path: str | Path, label: str = "") -> Path:
    stmts = renumber_statements(plan)
    path = Path(path)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("-- Generated by XUI DB Tool: renumber stream ids"
                 + (f" of {label}" if label else "") + ".\n"
                 "-- Back up the database and stop the streams before running it.\n")
        for note in after_notes():
            fh.write(f"-- {note}\n")
        fh.write("SET FOREIGN_KEY_CHECKS = 0;\nSTART TRANSACTION;\n")
        for stmt in stmts:
            fh.write(stmt + ";\n")
        fh.write(f"COMMIT;\n{auto_increment_sql(plan)};\nSET FOREIGN_KEY_CHECKS = 1;\n")
    log.info("Wrote renumber script %s (%d statements)", path, len(stmts))
    return path


def files_script(plan: RenumberPlan, dirs: tuple[str, ...] | None = None) -> str:
    """A bash script renaming files/folders named by stream id (two passes, no clashes)."""
    dirs = dirs or FILE_DIRS[plan.family]
    pairs = "\n".join(f"{o} {n}" for o, n in sorted(plan.mapping.items()))
    dir_list = " ".join(f'"{d}"' for d in dirs)
    return f"""#!/bin/bash
# Generated by XUI DB Tool: rename movie/episode files and TV archive folders after
# renumbering the stream ids. Run it on the panel server as root, once, after the
# database was renumbered. Files that do not exist are skipped.
set -u
DIRS=({dir_list})
read -r -d '' MAP <<'EOF'
{pairs}
EOF
moved=0
for pass in 1 2; do
  while read -r old new; do
    [ -n "$old" ] || continue
    for dir in "${{DIRS[@]}}"; do
      [ -d "$dir" ] || continue
      if [ "$pass" = 1 ]; then from="$old"; to="xdbtmp_$new"; else from="xdbtmp_$new"; to="$new"; fi
      for f in "$dir/$from" "$dir/$from".*; do
        [ -e "$f" ] || continue
        mv -n "$f" "$dir/$to${{f#"$dir/$from"}}" && moved=$((moved + 1))
      done
    done
  done <<< "$MAP"
done
echo "Renamed $((moved / 2)) file(s)/folder(s)."
"""


def apply_renumber(
    source: DataSource,
    plan: RenumberPlan,
    *,
    backup_dir: str | Path,
    progress: ProgressFn | None = None,
    message: Callable[[str], None] | None = None,
) -> dict:
    """Back up every affected table as SQL, then renumber in one transaction."""
    if not isinstance(source, MySQLSource):
        raise SourceError(_("Renumbering can only be applied to a live MySQL/MariaDB "
                            "connection; save the SQL script instead."))
    say = message or (lambda _m: None)
    stmts = renumber_statements(plan)
    if not stmts:
        return {"backup": None, "statements": 0, "changed": 0}
    folder = Path(backup_dir) / f"renumber-{time.strftime('%Y%m%d-%H%M%S')}"
    say(_("Backing up {tables} to {folder}…", tables=", ".join(plan.tables), folder=folder))
    export_tables(source, plan.tables, ExportOptions(fmt="sql", out_dir=folder), progress)
    say(_("Renumbering {count} stream(s)…", count=len(plan.mapping)))
    changed = source.run_transaction(stmts, progress)
    source.execute(auto_increment_sql(plan))
    log.info("Renumbered %d stream id(s); %d row(s) changed", len(plan.mapping), changed)
    return {"backup": folder, "statements": len(stmts), "changed": changed}
