# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Update checks and installs against a faked GitHub (no network)."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from xdbtool.core import updater
from xdbtool.core.updater import UpdateError


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def release_json(tag="v0.2.0", assets=()):
    return {
        "tag_name": tag, "name": f"XUI DB Tool {tag}", "body": "## Changes\n- things",
        "html_url": f"https://github.com/{updater.REPO}/releases/tag/{tag}",
        "zipball_url": f"https://api.github.com/repos/{updater.REPO}/zipball/{tag}",
        "prerelease": False, "draft": False, "published_at": "2026-10-20T10:00:00Z",
        "assets": [{"name": n, "url": f"https://api.github.com/assets/{i}",
                    "browser_download_url": f"https://github.com/dl/{n}", "size": 1}
                   for i, n in enumerate(assets)],
    }


@pytest.fixture
def github(monkeypatch):
    """Route updater.urlopen to an in-memory {url: bytes | HTTPError} map."""
    routes: dict[str, object] = {}
    seen: list[Request] = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        body = routes[request.full_url]
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body if isinstance(body, bytes) else json.dumps(body).encode())

    monkeypatch.setattr(updater, "urlopen", fake_urlopen)
    return routes, seen


LATEST = f"{updater.API}/repos/{updater.REPO}/releases/latest"


@pytest.mark.parametrize("candidate,current,newer", [
    ("v0.2.0", "0.1.0", True), ("0.1.1", "0.1.0", True), ("v0.1.0", "0.1.0", False),
    ("v0.1.0", "0.2.0", False), ("1.0", "0.9.9", True), ("v1.0.0rc1", "1.0.0", False),
    ("v1.0.0", "1.0.0rc1", True), ("v1.0.0-beta.2", "1.0.0-beta.1", True),
    ("nightly", "0.1.0", False),
])
def test_version_comparison(candidate, current, newer):
    assert updater.is_newer(candidate, current) is newer


def test_check_reports_newer_release_and_sends_token(github):
    routes, seen = github
    routes[LATEST] = release_json("v9.0.0", ["XUI-DB-Tool-9.0.0-windows.zip", "SHA256SUMS.txt"])
    newer, rel = updater.check("tok123", current="0.1.0")
    assert newer and rel.version == "9.0.0" and rel.notes.startswith("## Changes")
    assert rel.windows_asset().name == "XUI-DB-Tool-9.0.0-windows.zip"
    assert seen[0].get_header("Authorization") == "Bearer tok123"


def test_no_token_means_no_authorization_header(github):
    routes, seen = github
    routes[LATEST] = release_json("v0.1.0")
    assert updater.check("", current="0.1.0")[0] is False
    assert seen[0].get_header("Authorization") is None


def test_missing_release_or_private_repo_is_explained(github):
    routes, _seen = github
    routes[LATEST] = HTTPError(LATEST, 404, "Not Found", {}, None)
    with pytest.raises(UpdateError, match="private"):
        updater.check("")


def test_token_is_not_forwarded_to_another_host():
    handler = updater._StripAuthOnRedirect()
    req = Request("https://api.github.com/repos/x/y/releases/assets/1",
                  headers={"Authorization": "Bearer t", "Accept": "application/octet-stream"})
    away = handler.redirect_request(req, None, 302, "Found", {},
                                    "https://objects.githubusercontent.com/signed?x=1")
    assert away.get_header("Authorization") is None
    same = handler.redirect_request(req, None, 302, "Found", {},
                                    "https://api.github.com/other")
    assert same.get_header("Authorization") == "Bearer t"


def test_checksums_parse_and_verify(tmp_path):
    f = tmp_path / "pkg.zip"
    f.write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    sums = updater.parse_checksums(f"{digest}  pkg.zip\n{'0' * 64} *other.zip\ngarbage\n")
    assert sums == {"pkg.zip": digest, "other.zip": "0" * 64}
    updater.verify(f, "pkg.zip", sums)
    with pytest.raises(UpdateError, match="mismatch"):
        updater.verify(f, "pkg.zip", {"pkg.zip": "1" * 64})
    with pytest.raises(UpdateError, match="not listed"):
        updater.verify(f, "pkg.zip", {})


def test_git_checkout_is_never_changed(monkeypatch):
    monkeypatch.setattr(updater, "install_kind", lambda: "git")
    with pytest.raises(UpdateError, match="git pull"):
        updater.apply_update(updater._release(release_json()))


def test_pip_install_uses_the_downloaded_source(github, monkeypatch):
    routes, _seen = github
    rel = updater._release(release_json("v0.2.0"))
    routes[rel.zipball_url] = b"PK-source"
    ran = {}

    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(updater, "install_kind", lambda: "pip")
    monkeypatch.setattr(updater.subprocess, "run",
                        lambda cmd, **kw: ran.setdefault("cmd", cmd) and Done())
    assert updater.apply_update(rel) == "restart-manually"
    cmd = ran["cmd"]
    assert cmd[:5] == [sys.executable, "-m", "pip", "install", "--upgrade"]
    assert cmd[5].endswith("v0.2.0.zip")


def _windows_release(github, tamper=False):
    routes, _seen = github
    name = "XUI-DB-Tool-0.2.0-windows.zip"
    rel = updater._release(release_json("v0.2.0", [name, "SHA256SUMS.txt"]))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("XUI-DB-Tool/XUI-DB-Tool.exe", b"new exe")
        zf.writestr("XUI-DB-Tool/_internal/data.txt", b"x")
    payload = buf.getvalue()
    digest = hashlib.sha256(b"tampered" if tamper else payload).hexdigest()
    routes[rel.asset(name).api_url] = payload
    routes[rel.asset("SHA256SUMS.txt").api_url] = f"{digest}  {name}\n".encode()
    return rel


def test_windows_update_is_verified_staged_and_handed_to_a_script(github, monkeypatch):
    rel = _windows_release(github)
    launched = {}
    monkeypatch.setattr(updater, "install_kind", lambda: "frozen")
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda args, **kw: launched.setdefault("args", args))
    assert updater.apply_update(rel, "tok") == "restart"
    script = updater.Path(launched["args"][2])
    text = script.read_text(encoding="utf-8")
    staged = script.parent / "new" / "XUI-DB-Tool"
    assert (staged / "XUI-DB-Tool.exe").read_bytes() == b"new exe"
    assert f'robocopy.exe" "{staged}"' in text and str(updater.os.getpid()) in text
    # System tools by full path, so a foreign PATH (Git Bash) cannot skip the wait.
    for tool in ("tasklist.exe", "find.exe", "timeout.exe", "robocopy.exe"):
        assert f"%SystemRoot%\\System32\\{tool}" in text


def test_windows_update_refuses_a_bad_checksum(github, monkeypatch):
    rel = _windows_release(github, tamper=True)
    monkeypatch.setattr(updater, "install_kind", lambda: "frozen")
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("must not launch the updater"))
    with pytest.raises(UpdateError, match="mismatch"):
        updater.apply_update(rel)


def test_cli_update_check(github, capsys):
    from xdbtool.__main__ import main

    routes, _seen = github
    routes[LATEST] = release_json("v99.0.0")
    assert main(["update", "--check"]) == 0
    out = capsys.readouterr().out
    assert "Latest: 99.0.0" in out and "update is available" in out
