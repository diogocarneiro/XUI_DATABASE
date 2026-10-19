# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Persist connection profiles and app settings in the user's config directory.

Passwords are stored only if the user opts in; by default they are left blank and
asked for at connect time. Nothing here is written inside the project tree.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .logger import get_logger

log = get_logger(__name__)

APP_DIR_NAME = "xui-db-tool"


def config_dir() -> Path:
    """Return a per-user config directory, created on first use."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / APP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class ConnectionProfile:
    name: str
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    database: str = ""
    # Stored only when the user explicitly chooses to save it.
    password: str = ""
    save_password: bool = False
    # Optional SSH tunnel to reach a MySQL that only accepts local connections.
    use_ssh: bool = False
    ssh_socket: bool = False  # SSH via the server's MySQL socket
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_key: str = ""
    ssh_password: str = ""

    def to_stored(self) -> dict:
        d = asdict(self)
        if not self.save_password:
            d["password"] = ""
            d["ssh_password"] = ""
        return d


@dataclass
class Store:
    profiles: list[ConnectionProfile] = field(default_factory=list)

    def upsert(self, profile: ConnectionProfile) -> None:
        for i, p in enumerate(self.profiles):
            if p.name == profile.name:
                self.profiles[i] = profile
                return
        self.profiles.append(profile)

    def remove(self, name: str) -> None:
        self.profiles = [p for p in self.profiles if p.name != name]


def _profiles_path() -> Path:
    return config_dir() / "connections.json"


def load_store() -> Store:
    path = _profiles_path()
    if not path.is_file():
        return Store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(ConnectionProfile)}
        profiles = [ConnectionProfile(**{k: v for k, v in p.items() if k in known})
                    for p in data.get("profiles", [])]
        return Store(profiles=profiles)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read connection store: %s", exc)
        return Store()


def save_store(store: Store) -> None:
    path = _profiles_path()
    data = {"profiles": [p.to_stored() for p in store.profiles]}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("Saved %d connection profile(s)", len(store.profiles))
