# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""PyInstaller entry point (an absolute import, so the package's relative imports work)."""

from xdbtool.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
