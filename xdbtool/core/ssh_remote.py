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
import shlex
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


def _mysql_cnf(user: str, password: str) -> str:
    def esc(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return f"[client]\nuser={esc(user)}\npassword={esc(password)}\n"


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
        c = self.config
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
            raise SshError(f"SSH connection to {c.user}@{c.host}:{c.port} failed: {exc}") from exc
        self._client = client
        log.info("SSH connected to %s@%s:%d", c.user, c.host, c.port)
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

    def mysql_cmd(self, *args: str) -> str:
        parts = ["mysql"]
        if self._cnf:
            parts.append(f"--defaults-extra-file={shlex.quote(self._cnf)}")
        return " ".join(parts + [shlex.quote(a) for a in args])

    def mysql_query(self, sql: str) -> list[list[str]]:
        """Run ``sql`` with the remote client and return tab-separated rows."""
        _, out, _ = self.run(self.mysql_cmd("-N", "-B", "-e", sql))
        return [line.split("\t") for line in out.splitlines() if line]
