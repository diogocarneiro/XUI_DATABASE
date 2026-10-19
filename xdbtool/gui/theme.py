# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Light / dark / automatic themes: colour tokens, a QSS stylesheet and its icons.

``apply_theme(app, mode)`` sets the Fusion style, a matching ``QPalette`` and the
stylesheet. In ``"auto"`` mode it follows the operating system's colour scheme and
re-applies itself when the system switches (Qt 6.5+ ``colorSchemeChanged``).

Everything except :func:`apply_theme` is pure Python, so tokens and the generated
stylesheet are testable without a display.
"""

from __future__ import annotations

from pathlib import Path

MODES = ("auto", "light", "dark")
DEFAULT_MODE = "auto"

TOKENS: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#f3f5f9", "surface": "#ffffff", "surface_alt": "#eef1f6",
        "border": "#d5dbe5", "border_strong": "#b9c2d0",
        "text": "#1b2230", "muted": "#5d6878",
        "accent": "#2563eb", "accent_hover": "#1d4ed8", "accent_text": "#ffffff",
        "accent_soft": "#dbe7fe",
        "danger": "#dc2626", "danger_hover": "#b91c1c",
        "success": "#15803d", "warning": "#b45309",
        "input": "#ffffff", "log_bg": "#f8fafc", "scroll": "#c3cad6",
    },
    "dark": {
        "bg": "#0e131a", "surface": "#161d27", "surface_alt": "#1c2531",
        "border": "#283344", "border_strong": "#3a475b",
        "text": "#e5eaf1", "muted": "#94a1b3",
        "accent": "#3b82f6", "accent_hover": "#60a5fa", "accent_text": "#ffffff",
        "accent_soft": "#1d3557",
        "danger": "#ef4444", "danger_hover": "#f87171",
        "success": "#22c55e", "warning": "#f59e0b",
        "input": "#10161f", "log_bg": "#0a0f15", "scroll": "#334155",
    },
}

_ICONS = {
    "chevron-down": '<path d="M4 6l4 4 4-4" fill="none" stroke="{c}" stroke-width="1.8" '
                    'stroke-linecap="round" stroke-linejoin="round"/>',
    "chevron-up": '<path d="M4 10l4-4 4 4" fill="none" stroke="{c}" stroke-width="1.8" '
                  'stroke-linecap="round" stroke-linejoin="round"/>',
    "check": '<path d="M3.5 8.5l3 3 6-7" fill="none" stroke="{c}" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round"/>',
}


def resolve(mode: str, system_dark: bool) -> str:
    """The concrete scheme (``"light"``/``"dark"``) for a mode and the OS setting."""
    if mode == "light" or mode == "dark":
        return mode
    return "dark" if system_dark else "light"


def write_icons(scheme: str, folder: Path) -> dict[str, str]:
    """Write the theme's SVG icons; returns QSS-ready (forward-slash) paths by name."""
    folder.mkdir(parents=True, exist_ok=True)
    t = TOKENS[scheme]
    colours = {"chevron-down": t["muted"], "chevron-up": t["muted"], "check": t["accent_text"]}
    paths = {}
    for name, body in _ICONS.items():
        path = folder / f"{name}-{scheme}.svg"
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
               f'viewBox="0 0 16 16">{body.format(c=colours[name])}</svg>')
        if not path.is_file() or path.read_text(encoding="utf-8") != svg:
            path.write_text(svg, encoding="utf-8")
        paths[name] = path.as_posix()
    return paths


def stylesheet(scheme: str, icons: dict[str, str] | None = None) -> str:
    """The application stylesheet for ``scheme`` (tokens substituted)."""
    t = dict(TOKENS[scheme])
    icons = icons or {}
    t.update(down=icons.get("chevron-down", ""), up=icons.get("chevron-up", ""),
             check=icons.get("check", ""))
    return _QSS.format(**t)


# Doubled braces are literal QSS braces; {name} are tokens.
_QSS = """
* {{ outline: none; }}
QWidget {{ color: {text}; font-size: 10pt; }}
QMainWindow, QDialog, QMessageBox {{ background: {bg}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QLabel {{ background: transparent; }}
QSplitter::handle {{ background: transparent; }}

QLabel#appTitle {{ font-size: 15pt; font-weight: 600; }}
QLabel#appSubtitle, QLabel[role="muted"] {{ color: {muted}; }}
QLabel#status {{ color: {muted}; padding: 2px 8px; border-radius: 10px;
                 background: {surface_alt}; }}
QLabel#status[state="ok"] {{ color: {success}; }}
QLabel#status[state="error"] {{ color: {danger}; }}

/* Card with its title inside, as a header row (not straddling the border). */
QGroupBox {{
    background: {surface}; border: 1px solid {border}; border-radius: 10px;
    margin-top: 0; padding: 40px 14px 12px 14px; font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: padding; subcontrol-position: top left;
    left: 16px; top: 13px; padding: 0; color: {text}; font-size: 10.5pt;
}}
QTextBrowser {{ background: {surface}; border: 1px solid {border}; border-radius: 8px;
    padding: 6px 10px; }}

QTabWidget::pane {{
    background: {surface}; border: 1px solid {border}; border-radius: 10px; top: -1px;
}}
QTabBar::tab {{
    background: transparent; color: {muted}; padding: 7px 10px; margin-right: 1px;
    border: none; border-bottom: 2px solid transparent;
}}
QTabBar::tab:hover {{ color: {text}; }}
QTabBar::tab:selected {{ color: {accent}; border-bottom: 2px solid {accent}; font-weight: 600; }}

QPushButton {{
    background: {surface_alt}; color: {text}; border: 1px solid {border};
    border-radius: 7px; padding: 6px 14px; min-height: 18px;
}}
QPushButton:hover {{ border-color: {border_strong}; background: {bg}; }}
QPushButton:pressed {{ background: {border}; }}
QPushButton:disabled {{ color: {muted}; background: {surface_alt}; border-color: {border}; }}
QPushButton[role="primary"] {{
    background: {accent}; color: {accent_text}; border: 1px solid {accent}; font-weight: 600;
}}
QPushButton[role="primary"]:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
QPushButton[role="danger"] {{
    background: transparent; color: {danger}; border: 1px solid {danger}; font-weight: 600;
}}
QPushButton[role="danger"]:hover {{ background: {danger}; color: {accent_text}; }}

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {input}; color: {text}; border: 1px solid {border}; border-radius: 7px;
    padding: 5px 8px; selection-background-color: {accent}; selection-color: {accent_text};
}}
QLineEdit, QSpinBox, QComboBox {{ min-height: 20px; max-height: 32px; }}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{ border-color: {border_strong}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {accent};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: {muted};
    background: {surface_alt}; }}
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url("{down}"); width: 14px; height: 14px; }}
QComboBox QAbstractItemView {{
    background: {surface}; color: {text}; border: 1px solid {border}; border-radius: 6px;
    selection-background-color: {accent_soft}; selection-color: {text}; padding: 4px;
}}
QSpinBox {{ padding-right: 22px; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 20px; background: transparent; }}
QSpinBox::up-arrow {{ image: url("{up}"); width: 12px; height: 12px; }}
QSpinBox::down-arrow {{ image: url("{down}"); width: 12px; height: 12px; }}

QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid {border_strong}; background: {input};
}}
QCheckBox::indicator:hover {{ border-color: {accent}; }}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent};
    image: url("{check}"); }}
QCheckBox:disabled {{ color: {muted}; }}

QTableWidget, QTableView {{
    background: {surface}; alternate-background-color: {surface_alt}; color: {text};
    border: 1px solid {border}; border-radius: 8px; gridline-color: transparent;
    selection-background-color: {accent_soft}; selection-color: {text};
}}
QTableView::item {{ padding: 4px 6px; border: none; }}
QHeaderView::section {{
    background: {surface_alt}; color: {muted}; border: none;
    border-bottom: 1px solid {border}; padding: 6px 8px; font-weight: 600;
}}
QTableCornerButton::section {{ background: {surface_alt}; border: none; }}

QPlainTextEdit#log {{
    background: {log_bg}; border-radius: 8px; padding: 8px;
    font-family: "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace; font-size: 9pt;
}}

QProgressBar {{
    background: {surface_alt}; border: none; border-radius: 6px; height: 12px;
    text-align: center; color: {muted}; font-size: 8pt;
}}
QProgressBar::chunk {{ background: {accent}; border-radius: 6px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {scroll}; border-radius: 4px; min-height: 24px;
    min-width: 24px; }}
QScrollBar::handle:hover {{ background: {muted}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none; border: none; width: 0; height: 0; }}

QMenuBar {{ background: {bg}; padding: 2px 4px; }}
QMenuBar::item {{ padding: 5px 10px; border-radius: 6px; background: transparent; }}
QMenuBar::item:selected {{ background: {surface_alt}; }}
QMenu {{ background: {surface}; border: 1px solid {border}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 22px; border-radius: 5px; }}
QMenu::item:selected {{ background: {accent_soft}; color: {text}; }}
QMenu::separator {{ height: 1px; background: {border}; margin: 4px 8px; }}

QToolTip {{ background: {surface}; color: {text}; border: 1px solid {border};
    border-radius: 6px; padding: 5px 8px; }}
"""


# -- Qt side ------------------------------------------------------------------
_state: dict[str, object] = {"mode": DEFAULT_MODE, "connected": False}


def _palette(scheme: str):
    from PySide6.QtGui import QColor, QPalette

    t = TOKENS[scheme]
    p = QPalette()
    roles = {
        QPalette.Window: t["bg"], QPalette.WindowText: t["text"], QPalette.Base: t["input"],
        QPalette.AlternateBase: t["surface_alt"], QPalette.Text: t["text"],
        QPalette.Button: t["surface_alt"], QPalette.ButtonText: t["text"],
        QPalette.ToolTipBase: t["surface"], QPalette.ToolTipText: t["text"],
        QPalette.PlaceholderText: t["muted"], QPalette.Highlight: t["accent"],
        QPalette.HighlightedText: t["accent_text"], QPalette.Link: t["accent"],
        QPalette.BrightText: t["danger"],
    }
    for role, colour in roles.items():
        p.setColor(role, QColor(colour))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(t["muted"]))
    return p


def system_is_dark(app) -> bool:
    from PySide6.QtCore import Qt

    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:  # Qt < 6.5: fall back to the palette's window lightness
        return app.palette().window().color().lightness() < 128


def apply_theme(app, mode: str = DEFAULT_MODE, icon_dir: Path | None = None) -> str:
    """Style ``app`` for ``mode`` ("auto"/"light"/"dark"); returns the scheme used."""
    from ..core.config import config_dir

    mode = mode if mode in MODES else DEFAULT_MODE
    _state["mode"] = mode
    scheme = resolve(mode, system_is_dark(app))
    icons = write_icons(scheme, icon_dir or config_dir() / "theme")
    app.setStyle("Fusion")
    app.setPalette(_palette(scheme))
    app.setStyleSheet(stylesheet(scheme, icons))
    if not _state["connected"]:
        hints = app.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            # Re-style when the OS switches light/dark, but only in automatic mode.
            hints.colorSchemeChanged.connect(
                lambda _s: _state["mode"] == "auto" and apply_theme(app, "auto", icon_dir))
            _state["connected"] = True
    return scheme


def current_mode() -> str:
    return str(_state["mode"])
