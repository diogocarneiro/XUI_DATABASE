"""Live MySQL/MariaDB data source backed by PyMySQL."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

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

    def masked(self) -> dict[str, object]:
        """Dict suitable for logging/saving — password redacted."""
        d = self.__dict__.copy()
        d["password"] = "***" if self.password else ""
        return d


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
        self.label = f"{config.user}@{config.host}:{config.port}/{config.database or '?'}"

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        if self._conn is not None:
            return
        try:
            self._conn = pymysql.connect(
                host=self.config.host,
                port=self.config.port,
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
            raise SourceError(f"Could not connect: {exc}") from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

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
    def create_database(self, name: str, charset: str = "utf8mb4", drop_existing: bool = False):
        """Create (optionally dropping) a database and select it as current."""
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
        self.label = f"{self.config.user}@{self.config.host}:{self.config.port}/{name}"

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
