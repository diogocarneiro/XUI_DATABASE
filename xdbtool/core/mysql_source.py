# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Live MySQL/MariaDB data source backed by PyMySQL."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ..i18n import N_, _
from .logger import get_logger
from .source import ColumnInfo, DataSource, SourceError, TableInfo

log = get_logger(__name__)

try:  # PyMySQL is a runtime dependency; keep import error friendly.
    import pymysql
    import pymysql.cursors
except ImportError as exc:  # pragma: no cover - exercised only without the dep
    pymysql = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@dataclass
class ConnectionConfig:
    """Connection parameters for a MySQL/MariaDB server."""

    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = ""
    charset: str = "utf8mb4"
    connect_timeout: int = 10
    # Optional SSH tunnel (an ssh_remote.SshConfig). When set, ``host``/``port`` are
    # the MySQL address *as seen from the SSH server* (usually 127.0.0.1:3306).
    ssh: object | None = None

    def masked(self) -> dict[str, object]:
        """Dict suitable for logging/saving — passwords redacted."""
        d = self.__dict__.copy()
        d["password"] = "***" if self.password else ""
        if self.ssh is not None:
            d["ssh"] = f"{self.ssh.user}@{self.ssh.host}:{self.ssh.port}"
        return d


# Extra explanations appended to connection errors, by MySQL error code.
_CONNECT_HINTS = {
    1130: N_("The server only accepts this MySQL user from its own machine (normal on "
             "XUI.one). Use the SSH connection mode, an SSH tunnel "
             "(ssh -L 3307:127.0.0.1:3306 root@server), or run a generated script on the "
             "server."),
    1045: N_("Wrong user/password, or this user is not allowed from your IP address."),
}
_SSH_HINTS = {
    1045: N_("Over an SSH TCP tunnel the login comes from 127.0.0.1. If `mysql -uroot` works "
             "on the server without a password, choose 'SSH + server MySQL socket' instead "
             "(root uses unix_socket auth, which never works over TCP). Otherwise use the "
             "panel's MySQL user (XUI: /home/xui/config/config.ini)."),
    1130: N_("This MySQL user is not allowed from 127.0.0.1 on the server."),
}
_SOCKET_HINTS = {
    1045: N_("Through the server socket MariaDB authenticates as the SSH user's account: "
             "with SSH user root, leave the MySQL password blank and use user root "
             "(like `mysql -uroot`), or give the password of a localhost MySQL user."),
}


PANEL_DATA_TABLES = ("lines", "users", "reg_users", "streams", "bouquets")


class DropRefused(SourceError):
    """Refused to DROP a database that contains panel data."""

    def __init__(self, database: str, rows: dict[str, int]):
        self.database = database
        self.rows = rows
        detail = ", ".join(f"{t}: {n}" for t, n in rows.items())
        super().__init__(_(
            "Refusing to DROP `{db}`: it holds panel data ({detail}). Use a new database "
            "name, or confirm explicitly that this data may be destroyed.",
            db=database, detail=detail))


def _quote_ident(name: str) -> str:
    """Safely quote a MySQL identifier (table/column) by escaping backticks."""
    if "\x00" in name:
        raise SourceError(f"Invalid identifier: {name!r}")
    return "`" + name.replace("`", "``") + "`"


class MySQLSource(DataSource):
    """Read-and-maintain access to a live panel database."""

    supports_repair = True

    def __init__(self, config: ConnectionConfig):
        if pymysql is None:  # pragma: no cover
            raise SourceError(
                "PyMySQL is not installed. Run: pip install -r requirements.txt"
            ) from _IMPORT_ERROR
        self.config = config
        self._conn = None
        self._tunnel = None
        self.label = self._make_label(config.database)

    def _make_label(self, database: str) -> str:
        c = self.config
        where = f"{c.host}:{c.port}" if c.ssh is None else f"{c.ssh.host} (SSH)"
        return f"{c.user}@{where}/{database or '?'}"

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        if self._conn is not None:
            return
        host, port = self.config.host, self.config.port
        if self.config.ssh is not None:
            from .ssh_remote import SshError, SshTunnel

            # Through SSH the server reaches its own MySQL locally; its LAN address
            # would make MariaDB see a remote login from that IP.
            if not host or host == self.config.ssh.host:
                host = "127.0.0.1"
            tunnel = SshTunnel(self.config.ssh, host, port)
            try:
                host, port = "127.0.0.1", tunnel.start()
            except SshError as exc:
                tunnel.stop()
                raise SourceError(str(exc)) from exc
            self._tunnel = tunnel
        try:
            self._conn = pymysql.connect(
                host=host,
                port=port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database or None,
                charset=self.config.charset,
                connect_timeout=self.config.connect_timeout,
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
            )
            log.info("Connected to %s", self.label)
        except Exception as exc:  # noqa: BLE001 - normalise to SourceError
            self._close_tunnel()
            code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
            if self.config.ssh is None:
                hint = _CONNECT_HINTS.get(code, "")
            elif getattr(self.config.ssh, "mysql_socket", ""):
                hint = _SOCKET_HINTS.get(code, "")
            else:
                hint = _SSH_HINTS.get(code, "")
            hint = "\n\n" + _(hint) if hint else ""
            raise SourceError(_("Could not connect: {error}", error=exc) + hint) from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
        self._close_tunnel()

    def _close_tunnel(self) -> None:
        if self._tunnel is not None:
            self._tunnel.stop()
            self._tunnel = None

    def _cursor(self):
        if self._conn is None:
            raise SourceError("Not connected. Call open() first.")
        return self._conn.cursor()

    # -- helpers -----------------------------------------------------------
    def _db(self) -> str:
        if not self.config.database:
            raise SourceError("No database selected.")
        return self.config.database

    def list_databases(self) -> list[str]:
        """List schemas visible to this user (excluding system schemas)."""
        system = {"information_schema", "performance_schema", "mysql", "sys"}
        with self._cursor() as cur:
            cur.execute("SHOW DATABASES")
            return [r["Database"] for r in cur.fetchall() if r["Database"] not in system]

    # -- introspection -----------------------------------------------------
    def list_tables(self) -> list[str]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME AS n FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
                "ORDER BY TABLE_NAME",
                (self._db(),),
            )
            return [r["n"] for r in cur.fetchall()]

    def table_info(self, table: str) -> TableInfo:
        with self._cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY, "
                "COLUMN_DEFAULT, EXTRA FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
                (self._db(), table),
            )
            cols = [
                ColumnInfo(
                    name=r["COLUMN_NAME"],
                    data_type=r["DATA_TYPE"],
                    nullable=(r["IS_NULLABLE"] == "YES"),
                    key=r["COLUMN_KEY"] or "",
                    default=r["COLUMN_DEFAULT"],
                    extra=r["EXTRA"] or "",
                )
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT ENGINE, TABLE_COLLATION, TABLE_ROWS FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (self._db(), table),
            )
            meta = cur.fetchone() or {}
        return TableInfo(
            name=table,
            columns=tuple(cols),
            row_count=meta.get("TABLE_ROWS"),
            engine=meta.get("ENGINE"),
            collation=meta.get("TABLE_COLLATION"),
        )

    def row_count(self, table: str) -> int:
        with self._cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS c FROM {_quote_ident(table)}")
            return int(cur.fetchone()["c"])

    # -- data --------------------------------------------------------------
    def iter_rows(self, table: str, batch_size: int = 1000) -> Iterator[dict]:
        if self._conn is None:
            raise SourceError("Not connected.")
        # SSCursor streams server-side so huge tables don't exhaust memory.
        cur = self._conn.cursor(pymysql.cursors.SSDictCursor)
        try:
            cur.execute(f"SELECT * FROM {_quote_ident(table)}")
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                yield from rows
        finally:
            cur.close()

    # -- maintenance -------------------------------------------------------
    def check_table(self, table: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(f"CHECK TABLE {_quote_ident(table)}")
            return list(cur.fetchall())

    def repair_table(self, table: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(f"REPAIR TABLE {_quote_ident(table)}")
            return list(cur.fetchall())

    def analyze_table(self, table: str) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(f"ANALYZE TABLE {_quote_ident(table)}")
            return list(cur.fetchall())

    # -- schema provisioning ----------------------------------------------
    def panel_rows(self, name: str) -> dict[str, int]:
        """Row counts of panel data tables in database ``name`` ({} if absent/empty)."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME AS n FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN %s",
                (name, PANEL_DATA_TABLES),
            )
            tables = [r["n"] for r in cur.fetchall()]
            counts = {}
            for t in tables:
                cur.execute(f"SELECT COUNT(*) AS c FROM {_quote_ident(name)}.{_quote_ident(t)}")
                counts[t] = int(cur.fetchone()["c"])
        return {t: n for t, n in counts.items() if n}

    def create_database(self, name: str, charset: str = "utf8mb4", drop_existing: bool = False,
                        allow_drop_panel: bool = False):
        """Create (optionally dropping) a database and select it as current.

        Dropping a database that holds panel data (lines, users, streams…) raises
        :class:`DropRefused` unless ``allow_drop_panel`` — so a template/restore
        aimed at the live panel database by mistake cannot wipe it.
        """
        if drop_existing and not allow_drop_panel:
            rows = self.panel_rows(name)
            if rows:
                raise DropRefused(name, rows)
        with self._cursor() as cur:
            if drop_existing:
                cur.execute(f"DROP DATABASE IF EXISTS {_quote_ident(name)}")
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS {_quote_ident(name)} "
                f"CHARACTER SET {charset}"
            )
        self.select_database(name)

    def select_database(self, name: str) -> None:
        with self._cursor() as cur:
            cur.execute(f"USE {_quote_ident(name)}")
        self.config.database = name
        self.label = self._make_label(name)

    def execute(self, sql: str, args=None) -> list[dict]:
        """Run one statement (with PyMySQL-escaped ``args``) and return its rows."""
        with self._cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())

    def execute_script(self, statements, progress=None) -> int:
        """Execute an iterable of SQL statements in order. Returns count run."""
        if self._conn is None:
            raise SourceError("Not connected.")
        stmts = list(statements)
        total = len(stmts)
        done = 0
        with self._cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for stmt in stmts:
                s = stmt.strip()
                if not s:
                    continue
                try:
                    cur.execute(s)
                except Exception as exc:  # noqa: BLE001
                    raise SourceError(f"Statement failed: {exc}\n--- SQL ---\n{s[:300]}") from exc
                done += 1
                if progress and done % 20 == 0:
                    progress("executing", done, total)
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        if progress:
            progress("executing", done, total)
        return done

    # -- info --------------------------------------------------------------
    def server_info(self) -> dict[str, str]:
        with self._cursor() as cur:
            cur.execute("SELECT VERSION() AS v")
            version = cur.fetchone()["v"]
        return {"label": self.label, "server_version": version, "database": self._db()}
