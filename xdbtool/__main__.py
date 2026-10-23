# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Entry point: launch the GUI, or run headless export/migrate/maintenance.

    xui-db-tool                 # launch GUI
    xui-db-tool gui             # launch GUI explicitly
    xui-db-tool info   <args>   # detect panel + list tables
    xui-db-tool export <args>   # export tables to json/csv/sql
    xui-db-tool maint  <args>   # check/repair/analyze/optimize (live MySQL)
    xui-db-tool check  <args>   # integrity check: broken links between tables
    xui-db-tool renumber <args> # renumber stream ids to 1..N (every reference too)
    xui-db-tool view <table> <args>  # show rows: search, sort, paging
    xui-db-tool edit <table> <args>  # change/insert/delete/import rows (live, undo script)
    xui-db-tool migrate <args>  # migrate between databases
    xui-db-tool rebuild <args>  # new DB from a template + chosen tables of a dump
    xui-db-tool xui-migrate <dump> <args>  # stage an old panel DB as `xui_migrate`

Run ``xui-db-tool <command> -h`` for each command's options.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core.config import config_dir
from .core.logger import setup_logging
from .i18n import LANGUAGES, SOURCE_LANGUAGE, set_language


def _add_source_args(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dump", help="Path to a mysqldump .sql file")
    g.add_argument("--host", help="MySQL host (live connection)")
    p.add_argument("--port", type=int, default=3306)
    p.add_argument("--user", default="root")
    p.add_argument("--password", default="")
    p.add_argument("--database", default="")
    _add_ssh_args(p, mysql_creds=False)


def _open_source(args):
    from .core.dump_source import SqlDumpSource
    from .core.mysql_source import ConnectionConfig, MySQLSource

    if getattr(args, "dump", None):
        return SqlDumpSource(args.dump)
    cfg = ConnectionConfig(
        host=args.host, port=args.port, user=args.user,
        password=args.password, database=args.database, ssh=_ssh_from_args(args),
    )
    return MySQLSource(cfg)


def _ssh_from_args(args, prefix: str = ""):
    """SshConfig from ``--{prefix}ssh-*`` options, or None when no SSH host is given."""
    host = getattr(args, f"{prefix}ssh_host", None)
    if not host:
        return None
    from .core.ssh_remote import SshConfig

    return SshConfig(host=host, port=getattr(args, f"{prefix}ssh_port"),
                     user=getattr(args, f"{prefix}ssh_user"),
                     password=getattr(args, f"{prefix}ssh_password"),
                     key_file=getattr(args, f"{prefix}ssh_key") or "",
                     mysql_socket=getattr(args, f"{prefix}ssh_mysql_socket", "") or "")


def _cli_progress(label: str, done: int, total: int) -> None:
    pct = f"{done * 100 // total}%" if total else "?"
    print(f"\r  {label}: {done}/{total} ({pct})", end="", file=sys.stderr, flush=True)


def cmd_info(args) -> int:
    from .core import profiles
    from .core.templates import match_template

    with _open_source(args) as src:
        tables = src.list_tables()
        det = profiles.detect_panel(tables)
        print(f"Source : {src.label}")
        print(f"Panel  : {det.profile.name}  (confidence {det.confidence:.0%})")
        meta, score = match_template(tables)
        if meta:
            print(f"Closest template : {meta.name}  (similarity {score:.0%})")
        print(f"Tables : {len(tables)}")
        for cat, ts in profiles.categorise_tables(tables).items():
            print(f"  [{cat}] {', '.join(ts)}")
    return 0


def cmd_templates(args) -> int:
    from .core.templates import export_signatures, list_templates, register_template, templates_dir

    if args.export_signatures:
        out = export_signatures()
        print(f"Exported signatures → {out}")
        return 0
    if args.register:
        meta = register_template(
            args.register, name=args.name or args.panel, panel_key=args.panel,
            version=args.version or "", key=args.key,
        )
        print(f"Registered '{meta.key}': {meta.table_count} tables, "
              f"{len(meta.seed_tables)} with seed data.")
        return 0
    if args.rows or args.clean:
        from .core.templates import clean_template, seed_tables

        try:
            seeds = seed_tables(args.rows or args.clean)
        except (ValueError, FileNotFoundError) as exc:
            print(exc, file=sys.stderr)
            return 2
        keep = {t.lower() for t in args.keep_rows or ()}
        extra = {t.lower() for t in args.drop_rows or ()}
        drop = [s.name for s in seeds
                if (s.suggested or s.name.lower() in extra) and s.name.lower() not in keep]
        print(f"Tables with rows in '{args.rows or args.clean}' (X = rows removed by --clean):")
        for s in seeds:
            mark = "X" if s.name in drop else " "
            print(f"  {mark} {s.name:26} {s.size / 1e6:9.2f} MB")
        if args.rows:
            return 0
        meta = clean_template(args.clean, drop, new_key=args.key, name=args.name,
                              progress=_cli_progress)
        print(file=sys.stderr)
        print(f"Registered clean template '{meta.key}' "
              f"({meta.sql_path().stat().st_size / 1e6:.2f} MB, rows kept in "
              f"{', '.join(meta.seed_tables) or 'no table'}).")
        return 0
    rows = list_templates()
    print(f"Templates dir: {templates_dir()}")
    if not rows:
        print("No templates registered. Use: templates --register <file.sql> --panel <key>")
        return 0
    for m in rows:
        present = "ok" if m.sql_path().is_file() else "MISSING .sql"
        print(f"  {m.key:22} {m.name:22} {m.table_count:>3} tables  [{present}]")
    return 0


def cmd_init_db(args) -> int:
    from .core.mysql_source import ConnectionConfig, DropRefused
    from .core.templates import create_from_template

    cfg = ConnectionConfig(
        host=args.target_host, port=args.target_port, user=args.target_user,
        password=args.target_password, ssh=_ssh_from_args(args),
    )
    try:
        result = create_from_template(
            cfg, args.template, args.target_database, drop_existing=args.drop,
            allow_drop_panel=args.allow_drop_panel, progress=_cli_progress,
        )
    except DropRefused as exc:
        print(f"\n{exc}\n(add --allow-drop-panel to really destroy it)", file=sys.stderr)
        return 2
    print(file=sys.stderr)
    print(f"Created database '{result['database']}' from template "
          f"'{result['template']}' ({result['statements']} statements).")
    return 0


def cmd_rebuild(args) -> int:
    from .core import rebuild
    from .core.mysql_source import ConnectionConfig, DropRefused
    from .core.source import SourceError

    if not args.plan_only and not (args.sql_out or args.target_host):
        print("--target-host or --sql-out is required unless --plan-only is used.",
              file=sys.stderr)
        return 2
    live = args.target_host and not (args.sql_out or args.plan_only)
    if live and not args.target_database:
        print("--target-database (the new database's name) is required.", file=sys.stderr)
        return 2
    try:
        with _open_source(args) as src:
            key = args.template
            if not key:
                meta, score = rebuild.suggest_template(src)
                if meta is None:
                    print("No registered template with its .sql on disk; register one with "
                          "`templates --register`.", file=sys.stderr)
                    return 2
                key = meta.key
                print(f"Template: {key} (closest match, {score:.0%}; choose with --template)")
            exclude = {t.lower() for t in args.exclude or ()}
            tables = [t for t in (args.tables or src.list_tables()) if t.lower() not in exclude]
            plan = rebuild.build_rebuild(src, key, tables,
                                         template_rows=args.template_rows)
            print(f"Source : {src.label}")
            for line in plan.summary_lines():
                print(f"  {line}")
            if args.plan_only:
                return 0
            if args.sql_out:
                db = args.target_database or ""
                out = rebuild.write_rebuild_script(src, plan, args.sql_out, database=db,
                                                   drop_existing=args.drop,
                                                   progress=_cli_progress)
                print(file=sys.stderr)
                if db:
                    print(f"Wrote {out}. On the server run:  {rebuild.run_command(out)}")
                else:
                    print(f"Wrote {out} (portable: no database name inside). Import it into an "
                          f"empty database:  {rebuild.run_command(out, 'DATABASE')}")
                return 0
            cfg = ConnectionConfig(
                host=args.target_host, port=args.target_port, user=args.target_user,
                password=args.target_password, ssh=_ssh_from_args(args, "target_"),
            )
            result = rebuild.rebuild_live(
                cfg, src, plan, args.target_database, drop_existing=args.drop,
                allow_drop_panel=args.allow_drop_panel, progress=_cli_progress,
                message=lambda m: print(f"\n{m}", file=sys.stderr))
    except DropRefused as exc:
        print(f"\n{exc}\n(add --allow-drop-panel to really destroy it)", file=sys.stderr)
        return 2
    except (rebuild.RebuildError, SourceError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    print(file=sys.stderr)
    rows = sum(result["written"].values())
    print(f"Created '{result['database']}' from template '{result['template']}' and copied "
          f"{rows} rows across {len(result['written'])} table(s).")
    return 0


def cmd_export(args) -> int:
    from .core.exporter import ExportOptions, export_tables

    with _open_source(args) as src:
        tables = args.tables or src.list_tables()
        opts = ExportOptions(fmt=args.format, out_dir=Path(args.out))
        paths = export_tables(src, tables, opts, _cli_progress)
        print(file=sys.stderr)
        print(f"Wrote {len(paths)} file(s) to {args.out}")
    return 0


def cmd_maint(args) -> int:
    from .core.repair import check_and_repair, run_maintenance

    with _open_source(args) as src:
        tables = args.tables or src.list_tables()
        if args.operation == "auto":
            results = check_and_repair(src, tables, _cli_progress)
        else:
            results = run_maintenance(src, tables, args.operation, _cli_progress)
        print(file=sys.stderr)
        for r in results:
            print(f"[{r.operation}] {r.table}: {r.status} — {r.message}")
    return 0


def cmd_check(args) -> int:
    from .core.integrity import apply_fixes, check_integrity, fix_statements, write_fix_script

    with _open_source(args) as src:
        report = check_integrity(src, _cli_progress)
        print(file=sys.stderr)
        for line in report.summary_lines():
            print(line)
        if args.verbose and report.skipped:
            print("Not checked (tables/columns absent): " + ", ".join(report.skipped))
        if args.fix_sql:
            path = write_fix_script(fix_statements(report), args.fix_sql, src.label)
            print(f"Fix script: {path}")
        if args.fix and report.fixable:
            result = apply_fixes(src, report, backup_dir=args.backup_dir,
                                 progress=_cli_progress, message=print)
            print(file=sys.stderr)
            print(f"Backup: {result['backup']}")
            print(f"Applied {result['statements']} statement(s), "
                  f"{result['changed']} row(s) changed. Run the check again: fixes can "
                  "uncover further broken links (e.g. events of a deleted device).")
            return 0
    return 1 if report.issues else 0


def cmd_renumber(args) -> int:
    from .core.renumber import apply_renumber, build_renumber, files_script, write_renumber_script

    with _open_source(args) as src:
        plan = build_renumber(src, start=args.start, order="type" if args.live_first else "id",
                              progress=_cli_progress)
        print(file=sys.stderr)
        for line in plan.summary_lines():
            print(line)
        if plan.problems:
            return 2
        if not plan.mapping:
            return 0
        if args.sql_out:
            print(f"SQL script: {write_renumber_script(plan, args.sql_out, src.label)}")
        if args.files_script:
            Path(args.files_script).write_text(files_script(plan), encoding="utf-8",
                                               newline="\n")
            print(f"File-rename script (run it on the server): {args.files_script}")
        if args.apply:
            result = apply_renumber(src, plan, backup_dir=args.backup_dir,
                                    progress=_cli_progress, message=print)
            print(file=sys.stderr)
            print(f"Backup: {result['backup']}")
            print(f"Renumbered {len(plan.mapping)} stream(s): {result['statements']} "
                  f"statement(s), {result['changed']} row(s) changed.")
    return 0


CLI_MASK = "***"  # like export redaction; plain ASCII for any console


def _cell(value, width: int, secret: bool) -> str:
    from .core.source import cell_text

    if value is None:
        return "NULL"
    if secret and value != "":
        return CLI_MASK
    text = " ".join(cell_text(value).split())
    return text if len(text) <= width else text[:max(width - 1, 1)] + "…"


def cmd_view(args) -> int:
    import json

    from .core.exporter import _json_default, is_secret_column

    columns = [c.strip() for c in args.columns.split(",") if c.strip()] if args.columns else None
    with _open_source(args) as src:
        page = src.fetch_page(args.table, offset=args.offset, limit=args.limit,
                              search=args.search or "", column=args.column, columns=columns,
                              order_by=args.order, descending=args.desc)
    secret = [not args.show_secrets and is_secret_column(c) for c in page.columns]
    if args.json:
        rows = [{c: (CLI_MASK if hide and v not in (None, "") else v)
                 for c, v, hide in zip(page.columns, row, secret, strict=True)}
                for row in page.rows]
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default))
        return 0
    cells = [[_cell(v, args.width, hide) for v, hide in zip(row, secret, strict=True)]
             for row in page.rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(page.columns)]
    print("  ".join(c.ljust(w) for c, w in zip(page.columns, widths, strict=True)).rstrip())
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)).rstrip())
    first = page.offset + 1 if page.rows else 0
    print(f"\nRows {first}-{page.offset + len(page.rows)} of {page.total}"
          + (f" matching {args.search!r}" if args.search else ""))
    return 0


def _assignments(items, info, what: str) -> dict:
    from .core.editor import coerce

    cols = {c.name: c for c in info.columns}
    out = {}
    for item in items or []:
        name, sep, text = item.partition("=")
        if not sep or name not in cols:
            raise SystemExit(f"{what} {item!r}: use COLUMN=VALUE with a column of the table")
        try:
            out[name] = coerce(text, cols[name])
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    return out


def cmd_edit(args) -> int:
    from .core import editor
    from .core.mysql_source import MySQLSource

    with _open_source(args) as src:
        if not isinstance(src, MySQLSource):
            print("edit needs a live MySQL/MariaDB connection (--host).", file=sys.stderr)
            return 2
        info = src.table_info(args.table)
        reason = editor.editable_reason(src, info)
        if reason:
            print(reason, file=sys.stderr)
            return 2
        values = _assignments(args.set, info, "--set")
        for name in args.null or []:
            values[name] = editor.coerce(None, {c.name: c for c in info.columns}[name])
        if args.import_file:
            changes, unknown = editor.import_changes(editor.read_rows_file(args.import_file),
                                                     info)
            if unknown:
                print(f"Columns not in `{args.table}`, ignored: {', '.join(unknown)}")
        elif args.insert:
            changes = [editor.Change(editor.INSERT, values)]
        else:
            key = _assignments(args.key, info, "--key")
            if set(key) != set(editor.key_columns(info)):
                print(f"--key must give exactly the primary key: "
                      f"{', '.join(editor.key_columns(info))}", file=sys.stderr)
                return 2
            row = editor.fetch_row(src, args.table, key)
            if row is None:
                print("No row has that key.", file=sys.stderr)
                return 1
            if args.delete:
                changes = [editor.Change(editor.DELETE, key=key, old=row)]
            else:
                values = {c: v for c, v in values.items() if v != row[c]}
                if not values:
                    print("Nothing to change: the row already holds these values.")
                    return 0
                cols = {c.name: c for c in info.columns}
                for c, v in values.items():
                    error, warning = editor.check_value(cols[c], row[c], v)
                    if error or (warning and not args.force):
                        print((error or warning + " (use --force)"), file=sys.stderr)
                        return 2
                changes = [editor.Change(editor.UPDATE, values, key,
                                         {c: row[c] for c in values})]
        for change in changes:
            print(editor.change_sql(args.table, change) + ";")
        if args.dry_run:
            return 0
        result = editor.apply_changes(src, args.table, changes, backup_dir=args.backup_dir)
        counts = result["counts"]
        print(f"Saved: {counts['update']} changed, {counts['insert']} added, "
              f"{counts['delete']} deleted. Undo script: {result['undo']}")
    return 0


def cmd_migrate(args) -> int:
    from .core.migrator import MigrationOptions, build_plan, execute_plan
    from .core.mysql_source import ConnectionConfig, MySQLSource

    target = MySQLSource(ConnectionConfig(
        host=args.target_host, port=args.target_port, user=args.target_user,
        password=args.target_password, database=args.target_database,
        ssh=_ssh_from_args(args, "target_"),
    ))
    with _open_source(args) as src, target:
        plan = build_plan(src, target, args.tables or None)
        print("Migration plan:")
        for m in plan.mappings:
            print(f"  {m.summary}")
        if plan.missing_in_target:
            print(f"  (missing in target: {', '.join(plan.missing_in_target)})")
        if args.plan_only:
            return 0
        opts = MigrationOptions(
            truncate_target=args.truncate,
            ignore_duplicates=not args.no_ignore,
            dry_run_sql_path=args.sql_out,
        )
        written = execute_plan(src, target, plan, opts, _cli_progress)
        print(file=sys.stderr)
        total = sum(written.values())
        dest = args.sql_out or target.label
        print(f"Transferred {total} rows across {len(written)} table(s) → {dest}")
    return 0


def cmd_restore(args) -> int:
    from .core.mysql_source import ConnectionConfig, DropRefused
    from .core.restore import restore_dump

    cfg = ConnectionConfig(
        host=args.target_host, port=args.target_port, user=args.target_user,
        password=args.target_password, ssh=_ssh_from_args(args),
    )
    try:
        result = restore_dump(
            cfg, args.dump_file, database=args.target_database, create=args.create,
            drop_existing=args.drop, allow_drop_panel=args.allow_drop_panel,
            progress=_cli_progress,
        )
    except DropRefused as exc:
        print(f"\n{exc}\n(add --allow-drop-panel to really destroy it)", file=sys.stderr)
        return 2
    print(file=sys.stderr)
    print(f"Restored '{result['source']}' into '{result['database']}' "
          f"({result['statements']} statements).")
    return 0


def cmd_xui_migrate(args) -> int:
    from .core.mysql_source import ConnectionConfig
    from .core.xui_migrate import XuiMigrateError, load_dump, stage_live, write_script

    try:
        prepared = load_dump(args.dump_file, skip_logs=not args.keep_logs, force=args.force)
    except XuiMigrateError as exc:
        print(f"Cannot stage this dump: {exc}", file=sys.stderr)
        return 2
    print(f"Dump : {Path(args.dump_file).name}")
    for line in prepared.summary_lines():
        print(f"  {line}")
    if args.check_only:
        return 0
    if args.sql_out:
        out = write_script(prepared, args.sql_out, database=args.database,
                           drop_existing=args.drop, grant_to=args.grant)
        print(f"Wrote {out}. On the XUI server run:  mysql -u root -p < {out.name}")
        return 0
    if not (args.target_host or args.ssh_host):
        print("--ssh-host or --target-host is required unless --sql-out or --check-only "
              "is used.", file=sys.stderr)
        return 2

    from .core.source import SourceError
    from .core.ssh_remote import SshConfig, SshError
    from .core.xui_migrate import stage_via_ssh

    try:
        if args.ssh_host:
            ssh = SshConfig(host=args.ssh_host, port=args.ssh_port, user=args.ssh_user,
                            password=args.ssh_password, key_file=args.ssh_key or "")
            result = stage_via_ssh(
                ssh, prepared, mysql_user=args.mysql_user, mysql_password=args.mysql_password,
                database=args.database, drop_existing=args.drop, grant_to=args.grant,
                progress=_cli_progress, message=lambda m: print(f"\n{m}", file=sys.stderr),
            )
        else:
            cfg = ConnectionConfig(
                host=args.target_host, port=args.target_port, user=args.target_user,
                password=args.target_password,
            )
            result = stage_live(cfg, prepared, database=args.database,
                                drop_existing=args.drop, grant_to=args.grant,
                                progress=_cli_progress)
    except (XuiMigrateError, SshError, SourceError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    print(file=sys.stderr)
    print(f"Loaded {result['tables']} tables into `{result['database']}` "
          f"({result['statements']} statements).")
    for table, n in result["row_counts"].items():
        print(f"  {table:18} {n:>8} rows")
    if not result["xui_server"]:
        print("Note: no `xui` database on this server — is it the XUI.one host?")
    print("Next: open the XUI panel → Database Migration → 'Migrate Database'.")
    return 0


def cmd_xui_import(args) -> int:
    from .core.dump_source import SqlDumpSource
    from .core.source import SourceError
    from .core.ssh_remote import SshConfig, SshError
    from .core.xui_import import (
        RescueAccess,
        XuiImportError,
        build_import,
        import_via_ssh,
        report_lines,
        write_import_script,
    )

    chosen = [t for t, on in (("access_codes", args.import_access_codes),
                              ("settings", args.import_settings),
                              ("servers", args.import_servers)) if on]
    try:
        if args.sql_out:
            from .core.templates import list_templates

            meta = next((m for m in list_templates() if m.key == args.template), None)
            if meta is None:
                print("--sql-out needs --template <key> (see `templates`) as the target schema.",
                      file=sys.stderr)
                return 2
            with SqlDumpSource(meta.sql_path()) as tmpl:
                schema = {t: [c.name for c in tmpl.table_info(t).columns]
                          for t in tmpl.list_tables()}
            with SqlDumpSource(args.dump_file) as src:
                imp = build_import(src, schema, import_tables=chosen, content=args.content,
                                   include_logs=args.include_logs, server_mode=args.servers)
                print("\n".join(imp.summary_lines()))
                rescue = None if args.no_rescue else RescueAccess.generate()
                out = write_import_script(src, imp, args.sql_out, database=args.database,
                                          rescue=rescue, target_schema=schema)
            run = (f"gunzip -c {out.name} | mysql -u root -p" if out.suffix == ".gz"
                   else f"mysql -u root -p < {out.name}")
            print(f"Wrote {out}. Back up `{args.database}` first, then on the server run:  {run}")
            if rescue:
                print(f"Rescue access after running it:  /{rescue.code}/  user "
                      f"{rescue.username}  password {rescue.password}")
            return 0
        if not args.ssh_host:
            print("--ssh-host is required (or use --sql-out with --template).", file=sys.stderr)
            return 2
        ssh = SshConfig(host=args.ssh_host, port=args.ssh_port, user=args.ssh_user,
                        password=args.ssh_password, key_file=args.ssh_key or "")
        result = import_via_ssh(
            ssh, args.dump_file, mysql_user=args.mysql_user, mysql_password=args.mysql_password,
            database=args.database, import_tables=chosen, content=args.content,
            include_logs=args.include_logs, server_mode=args.servers,
            backup=not args.no_backup, create_rescue=not args.no_rescue,
            refresh_codes=not args.no_refresh_codes, plan_only=args.plan_only,
            progress=_cli_progress, message=print,
        )
    except (XuiImportError, SshError, SourceError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    if args.plan_only:
        return 0
    print(file=sys.stderr)
    print("\n".join(report_lines(result)))
    print("Done. Restart XUI on the server if lines/streams do not show up straight away.")
    return 0


def cmd_update(args) -> int:
    from . import __version__
    from .core import updater
    from .core.config import load_settings

    token = args.token or updater.token_from_env(load_settings())
    try:
        newer, release = updater.check(token, include_prereleases=args.pre)
    except updater.UpdateError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"Installed: {__version__}   Latest: {release.version} ({release.html_url})")
    if not newer:
        print("Up to date.")
        return 0
    if args.check:
        print("An update is available; run `update` without --check to install it.")
        return 0
    try:
        result = updater.apply_update(release, token, progress=_cli_progress,
                                      message=lambda m: print(m, file=sys.stderr))
    except updater.UpdateError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    print(file=sys.stderr)
    print("Update staged: the app restarts when this process exits." if result == "restart"
          else f"Installed {release.version}. Restart the app to use it.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xui-db-tool", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lang", choices=list(LANGUAGES),
                   help="Interface language (GUI default: saved choice or pt; CLI default: en)")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("gui", help="Launch the graphical interface")

    pi = sub.add_parser("info", help="Detect panel and list tables")
    _add_source_args(pi)
    pi.set_defaults(func=cmd_info)

    pe = sub.add_parser("export", help="Export tables")
    _add_source_args(pe)
    pe.add_argument("--format", choices=["json", "csv", "sql"], default="json")
    pe.add_argument("--out", default="exports")
    pe.add_argument("--tables", nargs="*", help="Specific tables (default: all)")
    pe.set_defaults(func=cmd_export)

    pm = sub.add_parser("maint", help="Check/repair/analyze/optimize (live MySQL)")
    _add_source_args(pm)
    pm.add_argument("--operation", choices=["check", "repair", "analyze", "optimize", "auto"],
                    default="check")
    pm.add_argument("--tables", nargs="*")
    pm.set_defaults(func=cmd_maint)

    pc = sub.add_parser(
        "check", help="Integrity check: broken links between panel tables (exit 1 if any)")
    _add_source_args(pc)
    pc.add_argument("--fix-sql", metavar="FILE",
                    help="Write the SQL that fixes every fixable problem to FILE")
    pc.add_argument("--fix", action="store_true",
                    help="Live MySQL: back up the affected tables, then apply the fixes")
    pc.add_argument("--backup-dir", default="backups",
                    help="Where --fix saves the backup (default: ./backups)")
    pc.add_argument("-v", "--verbose", action="store_true",
                    help="Also list the rules not checked")
    pc.set_defaults(func=cmd_check)

    pn = sub.add_parser(
        "renumber", help="Renumber stream ids to 1..N, updating every reference")
    _add_source_args(pn)
    pn.add_argument("--start", type=int, default=1, help="First new id (default: 1)")
    pn.add_argument("--live-first", action="store_true",
                    help="Number live channels first, then radio, movies and episodes "
                         "(default: keep the current id order)")
    pn.add_argument("--sql-out", metavar="FILE", help="Write the renumbering SQL to FILE")
    pn.add_argument("--files-script", metavar="FILE",
                    help="Write a bash script that renames movie/episode files and TV "
                         "archive folders on the server")
    pn.add_argument("--apply", action="store_true",
                    help="Live MySQL: back up the affected tables, then renumber")
    pn.add_argument("--backup-dir", default="backups",
                    help="Where --apply saves the backup (default: ./backups)")
    pn.set_defaults(func=cmd_renumber)

    pv = sub.add_parser("view", help="Show rows of a table (search, sort, paging)")
    _add_source_args(pv)
    pv.add_argument("table")
    pv.add_argument("--search", help="Keep rows containing this text (any column)")
    pv.add_argument("--column", help="Search only this column")
    pv.add_argument("--columns", help="Comma-separated columns to show (default: all)")
    pv.add_argument("--order", metavar="COLUMN", help="Sort by this column")
    pv.add_argument("--desc", action="store_true", help="Sort descending")
    pv.add_argument("--limit", type=int, default=20)
    pv.add_argument("--offset", type=int, default=0)
    pv.add_argument("--width", type=int, default=40, help="Maximum column width")
    pv.add_argument("--json", action="store_true", help="Print the rows as JSON")
    pv.add_argument("--show-secrets", action="store_true",
                    help="Show password/token/key columns (masked by default)")
    pv.set_defaults(func=cmd_view)

    pe2 = sub.add_parser(
        "edit", help="Change, insert, delete or import rows (live MySQL; writes an undo script)")
    _add_source_args(pe2)
    pe2.add_argument("table")
    pe2.add_argument("--key", action="append", metavar="COLUMN=VALUE",
                     help="Primary key of the row to change/delete (repeat for each column)")
    pe2.add_argument("--set", action="append", metavar="COLUMN=VALUE",
                     help="New value (repeatable)")
    pe2.add_argument("--null", action="append", metavar="COLUMN", help="Set a column to NULL")
    what = pe2.add_mutually_exclusive_group()
    what.add_argument("--delete", action="store_true", help="Delete the row given by --key")
    what.add_argument("--insert", action="store_true", help="Insert a new row from --set/--null")
    what.add_argument("--import", dest="import_file", metavar="FILE",
                      help="Insert the rows of a CSV (header line) or JSON file")
    pe2.add_argument("--force", action="store_true",
                     help="Also overwrite a password hash with plain text")
    pe2.add_argument("--dry-run", action="store_true", help="Only print the SQL")
    pe2.add_argument("--backup-dir", default="backups",
                     help="Where the undo script is written (default: ./backups)")
    pe2.set_defaults(func=cmd_edit)

    pg = sub.add_parser("migrate", help="Migrate rows into a target MySQL database")
    _add_source_args(pg)
    pg.add_argument("--target-host", required=True)
    pg.add_argument("--target-port", type=int, default=3306)
    pg.add_argument("--target-user", default="root")
    pg.add_argument("--target-password", default="")
    pg.add_argument("--target-database", required=True)
    pg.add_argument("--tables", nargs="*")
    pg.add_argument("--truncate", action="store_true", help="Empty target tables first")
    pg.add_argument("--no-ignore", action="store_true", help="Do not use INSERT IGNORE")
    pg.add_argument("--sql-out", help="Write migration SQL to this file instead of executing")
    pg.add_argument("--plan-only", action="store_true", help="Show the plan and stop")
    _add_ssh_args(pg, mysql_creds=False, prefix="target-")
    pg.set_defaults(func=cmd_migrate)

    pt = sub.add_parser("templates", help="List or register base schema templates")
    pt.add_argument("--register", metavar="FILE.sql", help="Register a base schema dump")
    pt.add_argument("--panel", help="Panel key (e.g. xui_one, xtream_ui)")
    pt.add_argument("--name", help="Display name")
    pt.add_argument("--version", help="Panel version, e.g. 1.5.13")
    pt.add_argument("--key", help="Explicit template key (default: panel-version)")
    pt.add_argument("--export-signatures", action="store_true",
                    help="Write name-only signatures to the package for the repo")
    pt.add_argument("--rows", metavar="KEY",
                    help="List the template's tables that have rows, with their size")
    pt.add_argument("--clean", metavar="KEY",
                    help="Register a copy without a panel's data (streams, lines, accounts, "
                         "logs…): same schema, config rows only; --key/--name name the copy "
                         "(default: KEY-clean)")
    pt.add_argument("--keep-rows", nargs="*", metavar="TABLE",
                    help="With --clean: keep the rows of these tables")
    pt.add_argument("--drop-rows", nargs="*", metavar="TABLE",
                    help="With --clean: also remove the rows of these tables")
    pt.set_defaults(func=cmd_templates)

    pd = sub.add_parser("init-db", help="Create a fresh panel DB from a template")
    pd.add_argument("--template", required=True, help="Template key (see `templates`)")
    pd.add_argument("--target-host", required=True)
    pd.add_argument("--target-port", type=int, default=3306)
    pd.add_argument("--target-user", default="root")
    pd.add_argument("--target-password", default="")
    pd.add_argument("--target-database", required=True, help="New database name to create")
    pd.add_argument("--drop", action="store_true", help="DROP the database first if it exists")
    pd.add_argument("--allow-drop-panel", action="store_true",
                    help="Allow --drop even when the database holds panel data (destroys it!)")
    _add_ssh_args(pd, mysql_creds=False)
    pd.set_defaults(func=cmd_init_db)

    pb = sub.add_parser(
        "rebuild", help="Create a new DB from a template and copy chosen tables of a dump",
        description="Creates the new database from a registered template (clean schema of "
                    "the panel version) and copies the rows of the chosen source tables, "
                    "matching columns by name. Tables taken from the source replace the "
                    "template's default rows; the others keep them.")
    _add_source_args(pb)
    pb.add_argument("--template", help="Template key (default: the closest registered one)")
    pb.add_argument("--tables", nargs="*", help="Tables to copy (default: all)")
    pb.add_argument("--exclude", nargs="*", help="Tables not to copy (e.g. log tables)")
    pb.add_argument("--template-rows", choices=["missing", "all", "none"], default="missing",
                    help="Which tables keep the template's own rows when not filled from "
                         "the source: missing = only tables the source does not have "
                         "(default; never panel data/logs), all = every one, "
                         "none = schema only")
    pb.add_argument("--plan-only", action="store_true", help="Show the plan and stop")
    pb.add_argument("--sql-out", metavar="FILE",
                    help="Save the rebuilt database as a local .sql/.sql.gz file instead; "
                         "with --target-database the file creates that database, without "
                         "it the file is portable (import into any empty database)")
    pb.add_argument("--target-host", help="MySQL server where the new database is created")
    pb.add_argument("--target-port", type=int, default=3306)
    pb.add_argument("--target-user", default="root")
    pb.add_argument("--target-password", default="")
    pb.add_argument("--target-database", help="Name of the new database")
    pb.add_argument("--drop", action="store_true", help="Replace the database if it exists")
    pb.add_argument("--allow-drop-panel", action="store_true",
                    help="Allow --drop even when the database holds panel data (destroys it!)")
    _add_ssh_args(pb, mysql_creds=False, prefix="target-")
    pb.set_defaults(func=cmd_rebuild)

    pr = sub.add_parser("restore", help="Inject a .sql dump directly into a server")
    pr.add_argument("dump_file", help="Path to the .sql dump to execute")
    pr.add_argument("--target-host", required=True)
    pr.add_argument("--target-port", type=int, default=3306)
    pr.add_argument("--target-user", default="root")
    pr.add_argument("--target-password", default="")
    pr.add_argument("--target-database", help="Schema to load into (created with --create)")
    pr.add_argument("--create", action="store_true", help="CREATE the target database first")
    pr.add_argument("--drop", action="store_true", help="DROP the database first if it exists")
    pr.add_argument("--allow-drop-panel", action="store_true",
                    help="Allow --drop even when the database holds panel data (destroys it!)")
    _add_ssh_args(pr, mysql_creds=False)
    pr.set_defaults(func=cmd_restore)

    px = sub.add_parser(
        "xui-migrate",
        help="Stage an Xtream UI / ZapX / StreamCreed / XC v2 dump as `xui_migrate`",
        description="Load an old panel dump into the `xui_migrate` database on the XUI.one "
                    "server, ready for XUI's own 'Database Migration' page.",
    )
    px.add_argument("dump_file", help="Old panel dump (.sql or .sql.gz)")
    _add_ssh_args(px)
    px.add_argument("--target-host", help="Direct MySQL host (only if it accepts remote logins)")
    px.add_argument("--target-port", type=int, default=3306)
    px.add_argument("--target-user", default="root")
    px.add_argument("--target-password", default="")
    px.add_argument("--database", default="xui_migrate", help="Schema name (default: xui_migrate)")
    px.add_argument("--drop", action="store_true", help="Replace an existing xui_migrate database")
    px.add_argument("--keep-logs", action="store_true",
                    help="Also load log table rows (XUI ignores them; skipped by default)")
    px.add_argument("--grant", metavar="USER@HOST",
                    help="GRANT ALL on xui_migrate.* to XUI's MySQL user")
    px.add_argument("--force", action="store_true",
                    help="Load even if the dump does not look like an XC v2-family DB")
    px.add_argument("--sql-out", help="Write a script to run on the server instead of loading")
    px.add_argument("--check-only", action="store_true", help="Analyse the dump and stop")
    px.set_defaults(func=cmd_xui_migrate)

    pxi = sub.add_parser(
        "xui-import",
        help="Import an XUI.one backup into another XUI.one server's `xui` database",
        description="XUI-to-XUI import over SSH: copies the tables/columns both sides share, "
                    "keeps the target's system tables, backs up `xui` first.",
    )
    pxi.add_argument("dump_file", help="XUI.one backup (.sql or .sql.gz)")
    _add_ssh_args(pxi)
    pxi.add_argument("--database", default="xui", help="Target XUI database (default: xui)")
    pxi.add_argument("--servers", choices=["main", "import"], default="main",
                     help="main: move all streams to the target's main server (default); "
                          "import: add the backup's load balancers as extra servers")
    pxi.add_argument(
        "--content", type=_content_list, metavar="KINDS",
        help="Comma-separated content to import (default: all): live, movies, series, radio, "
             "bouquets, lines, resellers, epg, security, other. Content not listed is left "
             "as it is on the target; e.g. --content movies replaces only the movies.",
    )
    pxi.add_argument("--import-access-codes", action="store_true",
                     help="Replace access codes with the backup's (old admin/reseller URLs)")
    pxi.add_argument("--import-settings", action="store_true",
                     help="Replace panel settings with the backup's")
    pxi.add_argument("--import-servers", action="store_true",
                     help="Replace the server list with the backup's (old IPs; ignores --servers)")
    pxi.add_argument("--no-rescue", action="store_true",
                     help="Do not create the rescue admin access code + user")
    pxi.add_argument("--no-refresh-codes", action="store_true",
                     help="Do not run XUI's `tools access` after the import")
    pxi.add_argument("--include-logs", action="store_true", help="Also import log/stat tables")
    pxi.add_argument("--no-backup", action="store_true",
                     help="Skip the mysqldump of the current `xui` on the server")
    pxi.add_argument("--plan-only", action="store_true", help="Show what would be imported")
    pxi.add_argument("--sql-out", help="Offline: write the import SQL (needs --template)")
    pxi.add_argument("--template", help="Template key used as target schema with --sql-out")
    pxi.set_defaults(func=cmd_xui_import)

    pu = sub.add_parser("update", help="Check GitHub Releases for a new version and install it")
    pu.add_argument("--check", action="store_true", help="Only report whether one exists")
    pu.add_argument("--pre", action="store_true", help="Include pre-releases")
    pu.add_argument("--token", default="",
                    help="Optional GitHub token (only raises the API rate limit); "
                         "default: $XDBTOOL_GITHUB_TOKEN")
    pu.set_defaults(func=cmd_update)
    return p


def _content_list(value: str) -> list[str]:
    from .core.xui_import import CONTENT_GROUPS

    kinds = [v.strip().lower() for v in value.split(",") if v.strip()]
    bad = [k for k in kinds if k not in CONTENT_GROUPS]
    if bad or not kinds:
        raise argparse.ArgumentTypeError(
            f"unknown content {bad}; choose from {', '.join(CONTENT_GROUPS)}")
    return kinds


def _add_ssh_args(p: argparse.ArgumentParser, mysql_creds: bool = True,
                  prefix: str = "") -> None:
    help_host = ("XUI server SSH host (runs mysql on the server)" if mysql_creds else
                 "Reach MySQL through an SSH tunnel to this host; the MySQL host option is "
                 "then the address on that server (usually 127.0.0.1)")
    p.add_argument(f"--{prefix}ssh-host", help=help_host)
    p.add_argument(f"--{prefix}ssh-port", type=int, default=22)
    p.add_argument(f"--{prefix}ssh-user", default="root")
    p.add_argument(f"--{prefix}ssh-password", default="",
                   help="SSH password (or use a key/agent)")
    p.add_argument(f"--{prefix}ssh-key", help="Private key file for SSH")
    if not mysql_creds:
        p.add_argument(f"--{prefix}ssh-mysql-socket", nargs="?", const="auto", default="",
                       metavar="PATH",
                       help="Log in through the server's MySQL socket, like `mysql -uroot` "
                            "there (root without password); PATH defaults to auto-detect")
        return
    p.add_argument("--mysql-user", default="",
                   help="With --ssh-host: MySQL user on the server (blank = root via socket)")
    p.add_argument("--mysql-password", default="")


def main(argv: list[str] | None = None) -> int:
    setup_logging(log_dir=config_dir() / "logs")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "gui"):
        import importlib.util

        if importlib.util.find_spec("PySide6") is None:  # CLI-only build or install
            parser.print_help()
            if args.command == "gui":
                print("\nThis build has no graphical interface (PySide6 is not installed); "
                      "use the commands above.", file=sys.stderr)
                return 2
            return 0
        from .gui.app import run
        return run(language=args.lang)
    set_language(args.lang or SOURCE_LANGUAGE)  # CLI output stays English unless asked
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
