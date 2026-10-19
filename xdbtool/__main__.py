# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Entry point: launch the GUI, or run headless export/migrate/maintenance.

    xui-db-tool                 # launch GUI
    xui-db-tool gui             # launch GUI explicitly
    xui-db-tool info   <args>   # detect panel + list tables
    xui-db-tool export <args>   # export tables to json/csv/sql
    xui-db-tool maint  <args>   # check/repair/analyze/optimize (live MySQL)
    xui-db-tool migrate <args>  # migrate between databases
    xui-db-tool xui-migrate <dump> <args>  # stage an old panel DB as `xui_migrate`

Run ``xui-db-tool <command> -h`` for each command's options.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core.config import config_dir
from .core.logger import setup_logging


def _add_source_args(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dump", help="Path to a mysqldump .sql file")
    g.add_argument("--host", help="MySQL host (live connection)")
    p.add_argument("--port", type=int, default=3306)
    p.add_argument("--user", default="root")
    p.add_argument("--password", default="")
    p.add_argument("--database", default="")


def _open_source(args):
    from .core.dump_source import SqlDumpSource
    from .core.mysql_source import ConnectionConfig, MySQLSource

    if getattr(args, "dump", None):
        return SqlDumpSource(args.dump)
    cfg = ConnectionConfig(
        host=args.host, port=args.port, user=args.user,
        password=args.password, database=args.database,
    )
    return MySQLSource(cfg)


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
    from .core.mysql_source import ConnectionConfig
    from .core.templates import create_from_template

    cfg = ConnectionConfig(
        host=args.target_host, port=args.target_port, user=args.target_user,
        password=args.target_password,
    )
    result = create_from_template(
        cfg, args.template, args.target_database,
        drop_existing=args.drop, progress=_cli_progress,
    )
    print(file=sys.stderr)
    print(f"Created database '{result['database']}' from template "
          f"'{result['template']}' ({result['statements']} statements).")
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


def cmd_migrate(args) -> int:
    from .core.migrator import MigrationOptions, build_plan, execute_plan
    from .core.mysql_source import ConnectionConfig, MySQLSource

    target = MySQLSource(ConnectionConfig(
        host=args.target_host, port=args.target_port, user=args.target_user,
        password=args.target_password, database=args.target_database,
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
    from .core.mysql_source import ConnectionConfig
    from .core.restore import restore_dump

    cfg = ConnectionConfig(
        host=args.target_host, port=args.target_port, user=args.target_user,
        password=args.target_password,
    )
    result = restore_dump(
        cfg, args.dump_file, database=args.target_database,
        create=args.create, drop_existing=args.drop, progress=_cli_progress,
    )
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xui-db-tool", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
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
    pg.set_defaults(func=cmd_migrate)

    pt = sub.add_parser("templates", help="List or register base schema templates")
    pt.add_argument("--register", metavar="FILE.sql", help="Register a base schema dump")
    pt.add_argument("--panel", help="Panel key (e.g. xui_one, xtream_ui)")
    pt.add_argument("--name", help="Display name")
    pt.add_argument("--version", help="Panel version, e.g. 1.5.13")
    pt.add_argument("--key", help="Explicit template key (default: panel-version)")
    pt.add_argument("--export-signatures", action="store_true",
                    help="Write name-only signatures to the package for the repo")
    pt.set_defaults(func=cmd_templates)

    pd = sub.add_parser("init-db", help="Create a fresh panel DB from a template")
    pd.add_argument("--template", required=True, help="Template key (see `templates`)")
    pd.add_argument("--target-host", required=True)
    pd.add_argument("--target-port", type=int, default=3306)
    pd.add_argument("--target-user", default="root")
    pd.add_argument("--target-password", default="")
    pd.add_argument("--target-database", required=True, help="New database name to create")
    pd.add_argument("--drop", action="store_true", help="DROP the database first if it exists")
    pd.set_defaults(func=cmd_init_db)

    pr = sub.add_parser("restore", help="Inject a .sql dump directly into a server")
    pr.add_argument("dump_file", help="Path to the .sql dump to execute")
    pr.add_argument("--target-host", required=True)
    pr.add_argument("--target-port", type=int, default=3306)
    pr.add_argument("--target-user", default="root")
    pr.add_argument("--target-password", default="")
    pr.add_argument("--target-database", help="Schema to load into (created with --create)")
    pr.add_argument("--create", action="store_true", help="CREATE the target database first")
    pr.add_argument("--drop", action="store_true", help="DROP the database first if it exists")
    pr.set_defaults(func=cmd_restore)

    px = sub.add_parser(
        "xui-migrate",
        help="Stage an Xtream UI / ZapX / StreamCreed / XC v2 dump as `xui_migrate`",
        description="Load an old panel dump into the `xui_migrate` database on the XUI.one "
                    "server, ready for XUI's own 'Database Migration' page.",
    )
    px.add_argument("dump_file", help="Old panel dump (.sql or .sql.gz)")
    px.add_argument("--ssh-host",
                    help="XUI server SSH host (recommended: runs mysql on the server)")
    px.add_argument("--ssh-port", type=int, default=22)
    px.add_argument("--ssh-user", default="root")
    px.add_argument("--ssh-password", default="", help="SSH password (or use --ssh-key/agent)")
    px.add_argument("--ssh-key", help="Private key file for SSH")
    px.add_argument("--mysql-user", default="",
                    help="With --ssh-host: MySQL user on the server (blank = root via socket)")
    px.add_argument("--mysql-password", default="")
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
    return p


def main(argv: list[str] | None = None) -> int:
    setup_logging(log_dir=config_dir() / "logs")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "gui"):
        from .gui.app import run
        return run()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
