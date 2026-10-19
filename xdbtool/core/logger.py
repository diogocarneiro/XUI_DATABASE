# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Lightweight logging setup shared by CLI, GUI and core modules."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False


def setup_logging(level: int = logging.INFO, log_dir: Path | None = None) -> logging.Logger:
    """Configure the root ``xdbtool`` logger once. Safe to call repeatedly."""
    global _CONFIGURED
    logger = logging.getLogger("xdbtool")
    if _CONFIGURED:
        return logger

    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "xdbtool.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    _CONFIGURED = True
    return logger


def get_logger(name: str = "xdbtool") -> logging.Logger:
    return logging.getLogger(name)
