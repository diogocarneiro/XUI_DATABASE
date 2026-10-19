# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Check GitHub Releases for a newer version and install it.

How an update is applied depends on how the tool runs (:func:`install_kind`):

* ``frozen`` — the Windows build (PyInstaller): the release's
  ``XUI-DB-Tool-<version>-windows.zip`` is downloaded, **verified against the
  release's SHA256SUMS.txt**, unpacked, and a small script swaps the files in
  once the app has exited, then restarts it.
* ``pip`` — installed as a package: the release's source archive is downloaded
  and installed with ``pip install --upgrade``; restart afterwards.
* ``git`` — a development checkout: nothing is changed automatically; use
  ``git pull``.

The repository is public, so no credentials are needed. A GitHub token
(``XDBTOOL_GITHUB_TOKEN``, the ``github_token`` setting or CLI ``--token``) is
optional and only raises the API rate limit (60 requests/hour per IP without one).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .. import __version__
from ..i18n import _
from .logger import get_logger

log = get_logger(__name__)

REPO = "diogocarneiro/XUI_DATABASE"
API = "https://api.github.com"
APP_NAME = "XUI-DB-Tool"
CHECKSUMS = "SHA256SUMS.txt"
TOKEN_ENV = "XDBTOOL_GITHUB_TOKEN"

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-.]?(a|b|rc|alpha|beta)\.?(\d*))?$",
                         re.I)


class UpdateError(Exception):
    """Checking for or applying an update failed."""


# -- versions -------------------------------------------------------------------
def parse_version(text: str) -> tuple:
    """``"v1.2.3"`` → ``(1, 2, 3, 3, 0)``; pre-releases sort before the final release."""
    m = _VERSION_RE.match(text.strip())
    if not m:
        raise ValueError(f"Not a version: {text!r}")
    major, minor, patch, pre, pre_n = m.groups()
    rank = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2}.get((pre or "").lower(), 3)
    return int(major), int(minor), int(patch or 0), rank, int(pre_n or 0)


def is_newer(candidate: str, current: str = __version__) -> bool:
    try:
        return parse_version(candidate) > parse_version(current)
    except ValueError:
        return False


# -- GitHub API -----------------------------------------------------------------
@dataclass
class Asset:
    name: str
    api_url: str
    download_url: str
    size: int = 0


@dataclass
class Release:
    tag: str
    name: str
    notes: str
    html_url: str
    zipball_url: str
    prerelease: bool = False
    published_at: str = ""
    assets: list[Asset] = field(default_factory=list)

    @property
    def version(self) -> str:
        return self.tag.lstrip("vV")

    def asset(self, name: str) -> Asset | None:
        return next((a for a in self.assets if a.name == name), None)

    def windows_asset(self) -> Asset | None:
        return self.asset(f"{APP_NAME}-{self.version}-windows.zip")


def token_from_env(settings: dict | None = None) -> str:
    """Optional token from the environment. A ``github_token`` left in the settings by
    older versions (private-repository era) is deliberately ignored."""
    return os.environ.get(TOKEN_ENV, "").strip()


class _StripAuthOnRedirect(HTTPRedirectHandler):
    """Drop the token when GitHub redirects to another host (signed asset URLs):
    the token must not leave GitHub, and the storage host rejects two auth schemes."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(newurl).hostname != urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


_opener = build_opener(_StripAuthOnRedirect)


def urlopen(request, timeout: float):
    return _opener.open(request, timeout=timeout)


def _request(url: str, token: str = "", accept: str = "application/vnd.github+json"):
    headers = {"Accept": accept, "User-Agent": f"{APP_NAME}/{__version__}",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return Request(url, headers=headers)


def _get_json(url: str, token: str, timeout: float):
    try:
        with urlopen(_request(url, token), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 401 and token:
            # The repository is public: a bad or expired token must not block updates.
            log.warning("GitHub rejected the token; retrying anonymously")
            return _get_json(url, "", timeout)
        if exc.code == 404:
            raise UpdateError(_("No release has been published yet.")) from exc
        if exc.code in (401, 403, 429):
            raise UpdateError(_("GitHub refused the request ({code}), probably the hourly "
                                "limit; try again later.", code=exc.code)) from exc
        raise UpdateError(_("GitHub error {code}.", code=exc.code)) from exc
    except URLError as exc:
        raise UpdateError(_("Cannot reach GitHub: {error}", error=exc.reason)) from exc


def _release(data: dict) -> Release:
    return Release(
        tag=data["tag_name"], name=data.get("name") or data["tag_name"],
        notes=data.get("body") or "", html_url=data.get("html_url", ""),
        zipball_url=data.get("zipball_url", ""), prerelease=bool(data.get("prerelease")),
        published_at=data.get("published_at") or "",
        assets=[Asset(a["name"], a["url"], a.get("browser_download_url", ""), a.get("size", 0))
                for a in data.get("assets", [])],
    )


def latest_release(token: str = "", include_prereleases: bool = False,
                   timeout: float = 10.0, repo: str = REPO) -> Release:
    if include_prereleases:
        data = _get_json(f"{API}/repos/{repo}/releases?per_page=10", token, timeout)
        published = [d for d in data if not d.get("draft")]
        if not published:
            raise UpdateError(_("No release has been published yet."))
        return _release(max(published, key=lambda d: _sort_key(d["tag_name"])))
    return _release(_get_json(f"{API}/repos/{repo}/releases/latest", token, timeout))


def _sort_key(tag: str) -> tuple:
    try:
        return parse_version(tag)
    except ValueError:
        return (0,)


def check(token: str = "", current: str = __version__, **kw) -> tuple[bool, Release]:
    """Return ``(update_available, latest_release)``."""
    rel = latest_release(token, **kw)
    return is_newer(rel.version, current), rel


# -- download & verify ------------------------------------------------------------
def download(url: str, dest: Path, token: str = "", progress=None,
             accept: str = "application/octet-stream", timeout: float = 60.0) -> Path:
    """Stream ``url`` to ``dest`` (API asset URLs need ``accept=octet-stream``)."""
    try:
        with urlopen(_request(url, token, accept), timeout=timeout) as resp, \
                dest.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(256 * 1024):
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress("download", done, total)
    except (HTTPError, URLError) as exc:
        raise UpdateError(_("Download failed: {error}", error=exc)) from exc
    return dest


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_checksums(text: str) -> dict[str, str]:
    """``sha256sum`` output (``<hex>  <name>``, optional ``*`` before the name)."""
    out = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            out[parts[1].lstrip("*").strip()] = parts[0].lower()
    return out


def verify(path: Path, name: str, checksums: dict[str, str]) -> None:
    expected = checksums.get(name)
    if not expected:
        raise UpdateError(_("{file} is not listed in {sums}; refusing to install it.",
                            file=name, sums=CHECKSUMS))
    actual = sha256(path)
    if actual != expected:
        raise UpdateError(_("Checksum mismatch for {file}; the download was refused.",
                            file=name))


# -- install ----------------------------------------------------------------------
def install_kind() -> str:
    if getattr(sys, "frozen", False):
        return "frozen"
    root = Path(__file__).resolve().parents[2]
    if (root / ".git").exists():
        return "git"
    return "pip"


def updater_script(pid: int, staging: Path, target: Path, exe: Path, cleanup: Path) -> str:
    """Windows batch file: wait for the app to exit, copy the new files, restart.

    System tools are called by full path: an inherited PATH (e.g. Git Bash) can put
    GNU ``find``/``timeout`` first, which would silently skip the wait.
    """
    sys32 = r"%SystemRoot%\System32"
    return (
        "@echo off\r\n"
        ":wait\r\n"
        f'"{sys32}\\tasklist.exe" /FI "PID eq {pid}" /NH 2>nul | '
        f'"{sys32}\\find.exe" "{pid}" >nul && '
        f'("{sys32}\\timeout.exe" /t 1 /nobreak >nul & goto wait)\r\n'
        f'"{sys32}\\robocopy.exe" "{staging}" "{target}" /E /R:5 /W:1 '
        "/NFL /NDL /NJH /NJS /NP >nul\r\n"
        "if %ERRORLEVEL% GEQ 8 (echo Update failed & pause & exit /b 1)\r\n"
        f'start "" "{exe}"\r\n'
        f'rd /s /q "{cleanup}"\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
    )


def _asset_url(asset: Asset, token: str) -> str:
    """Public download link (no API quota) unless a token is set; then the API URL."""
    return asset.api_url if token or not asset.download_url else asset.download_url


def apply_update(release: Release, token: str = "", progress=None, message=None) -> str:
    """Install ``release``. Returns ``"restart"`` (exit now, the script restarts the
    app), ``"restart-manually"`` (pip install done) or raises :class:`UpdateError`."""
    say = message or (lambda _m: None)
    kind = install_kind()
    if kind == "git":
        raise UpdateError(_("This copy runs from a git checkout; update it with "
                            "`git pull` instead."))
    work = Path(tempfile.mkdtemp(prefix="xdbtool-update-"))
    if kind == "pip":
        say(_("Downloading the source of {tag}…", tag=release.tag))
        archive = download(release.zipball_url, work / f"{release.tag}.zip", token, progress,
                           accept="application/vnd.github+json")
        say(_("Installing with pip…"))
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", str(archive)]
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode != 0:
            raise UpdateError(_("pip failed: {error}",
                                error=(done.stderr or done.stdout).strip()[-600:]))
        return "restart-manually"

    if sys.platform != "win32":
        raise UpdateError(_("Automatic update of the packaged app is only available on "
                            "Windows."))
    asset, sums = release.windows_asset(), release.asset(CHECKSUMS)
    if asset is None or sums is None:
        raise UpdateError(_("Release {tag} has no Windows package with checksums.",
                            tag=release.tag))
    say(_("Downloading {file}…", file=asset.name))
    zipped = download(_asset_url(asset, token), work / asset.name, token, progress)
    sums_file = download(_asset_url(sums, token), work / CHECKSUMS, token)
    verify(zipped, asset.name, parse_checksums(sums_file.read_text(encoding="utf-8")))
    say(_("Checksum verified."))
    staging = work / "new"
    with zipfile.ZipFile(zipped) as zf:
        zf.extractall(staging)
    inner = staging / APP_NAME  # the zip holds one top-level folder
    source = inner if inner.is_dir() else staging
    exe = Path(sys.executable)
    script = work / "apply-update.cmd"
    script.write_text(updater_script(os.getpid(), source, exe.parent, exe, work),
                      encoding="utf-8")
    subprocess.Popen(["cmd", "/c", str(script)],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     close_fds=True)
    log.info("Update to %s staged in %s", release.tag, work)
    return "restart"
