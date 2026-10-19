# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Run commands and upload files on a panel server over SSH (Paramiko).

Panel MySQL servers usually accept connections only from the machine itself, so
the tool can instead upload a script over SFTP and run the server's own ``mysql``
client. MySQL credentials, when needed, are passed in a temporary ``chmod 600``
option file — never on the command line, where other users could see them.
"""

from __future__ import annotations

import secrets
import select
import shlex
import socket
import threading
from dataclasses import dataclass
from pathlib import Path

from .logger import get_logger

log = get_logger(__name__)

try:
    import paramiko
except ImportError as exc:  # pragma: no cover - exercised only without the dep
    paramiko = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


class SshError(Exception):
    """SSH connection or remote command failure."""


@dataclass
class SshConfig:
    host: str
    port: int = 22
    user: str = "root"
    password: str = ""
    key_file: str = ""
    timeout: int = 15
    # For SshTunnel: "" forwards to MySQL's TCP port; "auto" or a path connects to
    # MySQL's unix socket on the server instead — like typing `mysql -uroot` there,
    # so root accounts that use unix_socket authentication (no password) work.
    mysql_socket: str = ""


SOCKET_CANDIDATES = (
    "/run/mysqld/mysqld.sock", "/var/run/mysqld/mysqld.sock", "/var/lib/mysql/mysql.sock",
    "/tmp/mysql.sock",
)

# Relays stdin/stdout of an SSH exec channel to MySQL's unix socket. Python 3 is on
# every supported server; socat / nc -U are fallbacks.
_PY_BRIDGE = (
    "import os,socket,sys,threading\n"
    "s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1])\n"
    "def up():\n"
    " while True:\n"
    "  d=os.read(0,65536)\n"
    "  if not d:\n"
    "   s.shutdown(socket.SHUT_WR);return\n"
    "  s.sendall(d)\n"
    "threading.Thread(target=up,daemon=True).start()\n"
    "while True:\n"
    " d=s.recv(65536)\n"
    " if not d:break\n"
    " os.write(1,d)\n"
)


def socket_bridge_command(path: str) -> str:
    p = shlex.quote(path)
    return (f"if command -v python3 >/dev/null 2>&1; then exec python3 -c "
            f"{shlex.quote(_PY_BRIDGE)} {p}; "
            f"elif command -v socat >/dev/null 2>&1; then exec socat - UNIX-CONNECT:{p}; "
            f"else exec nc -U {p}; fi")


def _mysql_cnf(user: str, password: str) -> str:
    def esc(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return f"[client]\nuser={esc(user)}\npassword={esc(password)}\n"


def connect_client(c: SshConfig):
    """Open an authenticated Paramiko client for ``c`` (raises :class:`SshError`)."""
    if paramiko is None:  # pragma: no cover
        raise SshError("Paramiko is not installed. Run: pip install -r requirements.txt"
                       ) from _IMPORT_ERROR
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    # Unknown host keys are accepted but logged, like a first `ssh` login.
    client.set_missing_host_key_policy(paramiko.WarningPolicy())
    try:
        client.connect(
            c.host, port=c.port, username=c.user, password=c.password or None,
            key_filename=c.key_file or None, timeout=c.timeout,
            allow_agent=not c.password, look_for_keys=not (c.password or c.key_file),
        )
    except Exception as exc:  # noqa: BLE001 - normalise to SshError
        client.close()
        raise SshError(f"SSH connection to {c.user}@{c.host}:{c.port} failed: {exc}") from exc
    log.info("SSH connected to %s@%s:%d", c.user, c.host, c.port)
    return client


class SshTunnel:
    """Forward a local port to ``remote_host:remote_port`` as seen from the SSH server.

    Lets a normal MySQL client reach a server whose MySQL only accepts local
    connections: to MariaDB the connection comes from ``127.0.0.1``, exactly like
    the panel's own. With ``config.mysql_socket`` set, each connection is relayed
    to MySQL's unix socket by a small bridge run on the server as the SSH user, so
    MariaDB sees a local socket login (unix_socket auth: ``root`` with no
    password). Use as a context manager or call :meth:`start` / :meth:`stop`.
    """

    def __init__(self, config: SshConfig, remote_host: str = "127.0.0.1",
                 remote_port: int = 3306):
        self.config = config
        self.remote = (remote_host, remote_port)
        self.local_port = 0
        self._client = None
        self._server: socket.socket | None = None
        self._stop = threading.Event()
        self.socket_path = ""

    def __enter__(self) -> SshTunnel:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _run(self, command: str) -> tuple[int, str]:
        _, out, _ = self._client.exec_command(command, timeout=self.config.timeout)
        text = out.read().decode("utf-8", "replace").strip()
        return out.channel.recv_exit_status(), text

    def _find_socket(self) -> str:
        wanted = self.config.mysql_socket
        if wanted and wanted != "auto":
            candidates = [wanted]
        else:
            # The server's own configuration first, then the usual locations.
            _, conf = self._run("my_print_defaults mysqld client 2>/dev/null | "
                                "sed -n 's/^--socket=//p'")
            candidates = [*conf.splitlines(), *SOCKET_CANDIDATES]
        for path in dict.fromkeys(p for p in candidates if p):
            code, _ = self._run(f"test -S {shlex.quote(path)}")
            if code == 0:
                return path
        raise SshError("MySQL socket not found on the server (tried: "
                       + ", ".join(dict.fromkeys(candidates)) + ").")

    def start(self) -> int:
        self._client = connect_client(self.config)
        if self.config.mysql_socket:
            self.socket_path = self._find_socket()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))      # local only, random free port
        self._server.listen(8)
        self._server.settimeout(0.5)
        self.local_port = self._server.getsockname()[1]
        threading.Thread(target=self._accept_loop, name="ssh-tunnel", daemon=True).start()
        target = self.socket_path or f"{self.remote[0]}:{self.remote[1]}"
        log.info("SSH tunnel 127.0.0.1:%d -> %s via %s", self.local_port, target,
                 self.config.host)
        return self.local_port

    def _open_channel(self, peer):
        transport = self._client.get_transport()
        if not self.socket_path:
            return transport.open_channel("direct-tcpip", self.remote, peer)
        chan = transport.open_session()
        chan.exec_command(socket_bridge_command(self.socket_path))
        return chan

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._client is not None:
            self._client.close()
            self._client = None

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, peer = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return  # server socket closed by stop()
            try:
                chan = self._open_channel(peer)
            except Exception as exc:  # noqa: BLE001
                log.warning("SSH tunnel could not open a channel: %s", exc)
                sock.close()
                continue
            threading.Thread(target=self._pump, args=(sock, chan), daemon=True).start()

    def _pump(self, sock: socket.socket, chan) -> None:
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([sock, chan], [], [], 0.5)
                for src, dst in ((sock, chan), (chan, sock)):
                    if src in ready:
                        data = src.recv(65536)
                        if not data:
                            return
                        dst.sendall(data)
        except OSError:
            pass
        finally:
            chan.close()
            sock.close()


class SshSession:
    """A connected SSH client with helpers for remote ``mysql`` execution."""

    def __init__(self, config: SshConfig):
        if paramiko is None:  # pragma: no cover
            raise SshError("Paramiko is not installed. Run: pip install -r requirements.txt"
                           ) from _IMPORT_ERROR
        self.config = config
        self._client = None
        self._tmp: list[str] = []
        self._cnf: str | None = None

    def __enter__(self) -> SshSession:
        self._client = connect_client(self.config)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._tmp:
                self.run("rm -f " + " ".join(shlex.quote(p) for p in self._tmp), check=False)
        finally:
            self._client.close()
            self._client = None

    def temp_path(self, suffix: str) -> str:
        path = f"/tmp/xdbtool-{secrets.token_hex(6)}{suffix}"
        self._tmp.append(path)
        return path

    def run(self, command: str, *, check: bool = True) -> tuple[int, str, str]:
        _, stdout, stderr = self._client.exec_command(command)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        if check and code != 0:
            raise SshError(f"Remote command failed (exit {code}): {err.strip() or out.strip()}")
        return code, out, err

    def upload(self, local: str | Path, remote: str, progress=None, mode: int = 0o600) -> None:
        sftp = self._client.open_sftp()
        try:
            cb = (lambda done, total: progress("uploading", done, total)) if progress else None
            sftp.put(str(local), remote, callback=cb)
            sftp.chmod(remote, mode)
        finally:
            sftp.close()

    def write_text(self, remote: str, text: str, mode: int = 0o600) -> None:
        sftp = self._client.open_sftp()
        try:
            with sftp.open(remote, "w") as fh:
                fh.chmod(mode)
                fh.write(text)
        finally:
            sftp.close()

    def set_mysql_credentials(self, user: str = "", password: str = "") -> None:
        """Use these MySQL credentials for :meth:`mysql`; blank = server default (root socket)."""
        if not user:
            self._cnf = None
            return
        self._cnf = self.temp_path(".cnf")
        self.write_text(self._cnf, _mysql_cnf(user, password))

    def mysql_cmd(self, *args: str, program: str = "mysql") -> str:
        parts = [program]
        if self._cnf:
            parts.append(f"--defaults-extra-file={shlex.quote(self._cnf)}")
        return " ".join(parts + [shlex.quote(a) for a in args])

    def mysql_query(self, sql: str) -> list[list[str]]:
        """Run ``sql`` with the remote client and return tab-separated rows."""
        _, out, _ = self.run(self.mysql_cmd("-N", "-B", "-e", sql))
        return [line.split("\t") for line in out.splitlines() if line]
