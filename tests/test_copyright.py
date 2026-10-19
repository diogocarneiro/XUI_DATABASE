# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Every Python source file must carry the project's copyright header."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HEADER = (
    "# SPDX-License-Identifier: MIT\n"
    "# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>\n"
)
FILES = sorted(p for d in ("xdbtool", "tests") for p in (ROOT / d).rglob("*.py"))


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_copyright_header(path):
    assert path.read_text(encoding="utf-8").startswith(HEADER), (
        f"{path.relative_to(ROOT)} is missing the copyright header"
    )
