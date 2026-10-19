# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Keep the test run away from the user's real config dir, profiles and logs."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolated_config_dir(tmp_path_factory):
    saved = {k: os.environ.get(k) for k in ("APPDATA", "XDG_CONFIG_HOME")}
    cfg = str(tmp_path_factory.mktemp("config"))
    os.environ["APPDATA"] = os.environ["XDG_CONFIG_HOME"] = cfg
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
