# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Data viewer: browse a table's rows with search, sorting and paging."""

from __future__ import annotations

import json
from collections.abc import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QAction, QFontDatabase, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..core.exporter import SECRET_MASK, is_secret_column
from ..core.source import DataSource, Page, cell_text
from ..i18n import _

PAGE_SIZES = (50, 100, 500, 1000)
CELL_CHARS = 200        # longer values are cut in the grid (full text in the details)
TOOLTIP_CHARS = 2000
_ROOT = QModelIndex()

# Runs ``job()`` off the GUI thread; calls ``on_done(result)`` or ``on_error(text)``.
# Returns False when another operation is still running.
RunJob = Callable[[Callable[[], object], Callable[[object], None], Callable[[str], None]], bool]


def full_text(value: object, masked: bool) -> str:
    """The whole value as text: NULL, the mask, pretty JSON or plain text."""
    if value is None:
        return "NULL"
    if masked and value != "":
        return SECRET_MASK
    text = cell_text(value)
    if text[:1] in "[{":
        try:
            parsed = json.loads(text)
        except ValueError:
            return text
        if isinstance(parsed, (list, dict)) and parsed:
            return json.dumps(parsed, ensure_ascii=False, indent=2)
    return text


def grid_text(value: object, masked: bool) -> str:
    if value is None:
        return "NULL"
    if masked and value != "":
        return SECRET_MASK
    text = cell_text(value).replace("\r\n", "\n").replace("\n", " ⏎ ")
    return text if len(text) <= CELL_CHARS else text[:CELL_CHARS] + "…"


class RowsModel(QAbstractTableModel):
    """One page of rows; NULLs in grey italics, numbers right-aligned."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.page = Page((), (), 0)
        self.masked: list[bool] = []

    def set_page(self, page: Page, show_secrets: bool) -> None:
        self.beginResetModel()
        self.page = page
        self.masked = [not show_secrets and is_secret_column(c) for c in page.columns]
        self.endResetModel()

    def set_show_secrets(self, show: bool) -> None:
        self.set_page(self.page, show)

    def rowCount(self, parent=_ROOT) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self.page.rows)

    def columnCount(self, parent=_ROOT) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self.page.columns)

    def value(self, row: int, col: int) -> object:
        return self.page.rows[row][col]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        value, masked = self.value(index.row(), index.column()), self.masked[index.column()]
        if role == Qt.DisplayRole:
            return grid_text(value, masked)
        if role == Qt.ToolTipRole:
            text = full_text(value, masked)
            return text if len(text) <= TOOLTIP_CHARS else text[:TOOLTIP_CHARS] + "…"
        if role == Qt.TextAlignmentRole and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole and value is None:
            return QGuiApplication.palette().placeholderText()
        if role == Qt.FontRole and value is None:
            font = QGuiApplication.font()
            font.setItalic(True)
            return font
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802 - Qt API
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.page.columns[section]
        return str(self.page.offset + section + 1)


class RowDialog(QDialog):
    """Every column of one row, full values (JSON pretty-printed)."""

    def __init__(self, parent: QWidget, title: str, columns, values, masked):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 560)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        width = max((len(c) for c in columns), default=0)
        lines = []
        for col, value, hide in zip(columns, values, masked, strict=True):
            body = full_text(value, hide).replace("\n", "\n" + " " * (width + 3))
            lines.append(f"{col.ljust(width)} : {body}")
        text.setPlainText("\n".join(lines))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        v = QVBoxLayout(self)
        v.addWidget(text)
        v.addWidget(buttons)


class DataViewer(QDialog):
    """A non-modal window browsing one table of the connected source."""

    def __init__(self, parent: QWidget, source: DataSource, table: str, run_job: RunJob):
        super().__init__(parent, Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.source, self.table, self.run_job = source, table, run_job
        self.offset, self.order_by, self.descending = 0, None, False
        self.setWindowTitle(f"{table} — {source.label}")
        self.resize(1100, 640)

        self.search = QLineEdit()
        self.search.setPlaceholderText(_("Text to find…"))
        self.search.returnPressed.connect(self._new_search)
        self.column = QComboBox()
        self.column.addItem(_("All columns"), None)
        for col in source.table_info(table).columns:
            self.column.addItem(col.name, col.name)
        self.column.currentIndexChanged.connect(lambda _i: self._new_search())
        find_btn = QPushButton(_("Search"))
        find_btn.setProperty("role", "primary")
        find_btn.clicked.connect(self._new_search)
        clear_btn = QPushButton(_("Clear"))
        clear_btn.clicked.connect(self._clear_search)
        top = QHBoxLayout()
        top.addWidget(QLabel(_("Search:")))
        top.addWidget(self.search, 1)
        top.addWidget(self.column)
        top.addWidget(find_btn)
        top.addWidget(clear_btn)

        self.model = RowsModel(self)
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setAlternatingRowColors(True)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setWordWrap(False)
        self.view.horizontalHeader().setSectionsClickable(True)
        self.view.horizontalHeader().setSortIndicatorShown(True)
        self.view.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        self.view.horizontalHeader().sectionClicked.connect(self._sort_by)
        self.view.doubleClicked.connect(lambda idx: self._show_row(idx.row()))
        copy = QAction(_("Copy"), self.view)
        copy.setShortcut(QKeySequence.Copy)
        copy.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        copy.triggered.connect(self._copy)
        self.view.addAction(copy)

        self.status = QLabel()
        self.first_btn, self.prev_btn = QPushButton("«"), QPushButton("‹")
        self.next_btn, self.last_btn = QPushButton("›"), QPushButton("»")
        for btn, tip, step in ((self.first_btn, _("First page"), "first"),
                               (self.prev_btn, _("Previous page"), "prev"),
                               (self.next_btn, _("Next page"), "next"),
                               (self.last_btn, _("Last page"), "last")):
            btn.setToolTip(tip)
            btn.setFixedWidth(40)
            btn.clicked.connect(lambda _c=False, s=step: self._go(s))
        self.page_size = QComboBox()
        for n in PAGE_SIZES:
            self.page_size.addItem(_("{count} per page", count=n), n)
        self.page_size.setCurrentIndex(1)
        self.page_size.currentIndexChanged.connect(lambda _i: self._new_search())
        self.secrets = QCheckBox(_("Show passwords/keys"))
        self.secrets.toggled.connect(self.model.set_show_secrets)
        details = QPushButton(_("Row details…"))
        details.clicked.connect(self._show_current_row)
        bottom = QHBoxLayout()
        for w in (self.first_btn, self.prev_btn, self.status, self.next_btn, self.last_btn):
            bottom.addWidget(w)
        bottom.addWidget(self.page_size)
        bottom.addStretch(1)
        bottom.addWidget(self.secrets)
        bottom.addWidget(details)

        v = QVBoxLayout(self)
        v.addLayout(top)
        v.addWidget(self.view, 1)
        v.addLayout(bottom)
        self.refresh()

    # ------------------------------------------------------------- loading
    @property
    def limit(self) -> int:
        return self.page_size.currentData()

    def refresh(self) -> None:
        src, table = self.source, self.table
        kwargs = dict(offset=self.offset, limit=self.limit, search=self.search.text().strip(),
                      column=self.column.currentData(), order_by=self.order_by,
                      descending=self.descending)

        def job():
            return src.fetch_page(table, **kwargs)

        if self.run_job(job, self._loaded, self._failed):
            self.status.setText(_("Loading…"))
            self._set_nav(False)
        else:
            self.status.setText(_("Busy — another operation is running; try again."))

    def _loaded(self, page: Page) -> None:
        self.model.set_page(page, self.secrets.isChecked())
        self.view.resizeColumnsToContents()
        header = self.view.horizontalHeader()
        for col in range(self.model.columnCount()):
            header.resizeSection(col, min(header.sectionSize(col), 320))
        if self.order_by in page.columns:
            header.setSortIndicator(list(page.columns).index(self.order_by),
                                    Qt.DescendingOrder if self.descending else Qt.AscendingOrder)
        if page.rows:
            text = _("Rows {first}–{last} of {total}", first=page.offset + 1,
                     last=page.offset + len(page.rows), total=page.total)
        else:
            text = _("No rows ({total} in total)", total=page.total)
        self.status.setText(text)
        self._set_nav(True)

    def _failed(self, error: str) -> None:
        self.status.setText(_("ERROR: {error}", error=error))
        self._set_nav(True)

    def _set_nav(self, enabled: bool) -> None:
        total, off = self.model.page.total, self.offset
        self.first_btn.setEnabled(enabled and off > 0)
        self.prev_btn.setEnabled(enabled and off > 0)
        self.next_btn.setEnabled(enabled and off + self.limit < total)
        self.last_btn.setEnabled(enabled and off + self.limit < total)

    # ------------------------------------------------------------- actions
    def _new_search(self) -> None:
        self.offset = 0
        self.refresh()

    def _clear_search(self) -> None:
        self.search.clear()
        self.column.blockSignals(True)  # one search, not one per changed control
        self.column.setCurrentIndex(0)
        self.column.blockSignals(False)
        self._new_search()

    def _go(self, step: str) -> None:
        total, limit = self.model.page.total, self.limit
        last = max((total - 1) // limit * limit, 0)
        self.offset = {"first": 0, "prev": max(self.offset - limit, 0),
                       "next": min(self.offset + limit, last), "last": last}[step]
        self.refresh()

    def _sort_by(self, section: int) -> None:
        col = self.model.page.columns[section]
        self.descending = not self.descending if col == self.order_by else False
        self.order_by = col
        self._new_search()

    def _show_current_row(self) -> None:
        index = self.view.currentIndex()
        if index.isValid():
            self._show_row(index.row())

    def _show_row(self, row: int) -> None:
        page = self.model.page
        title = _("Row {number} of {table}", number=page.offset + row + 1, table=self.table)
        RowDialog(self, title, page.columns, page.rows[row], self.model.masked).exec()

    def _copy(self) -> None:
        indexes = sorted(self.view.selectedIndexes(), key=lambda i: (i.row(), i.column()))
        if not indexes:
            return
        lines: dict[int, list[str]] = {}
        for idx in indexes:
            value = self.model.value(idx.row(), idx.column())
            if value is None:
                text = ""
            elif self.model.masked[idx.column()] and value != "":
                text = SECRET_MASK
            else:  # raw value, one line per row, tabs between cells
                text = " ".join(cell_text(value).replace("\t", " ").splitlines())
            lines.setdefault(idx.row(), []).append(text)
        QGuiApplication.clipboard().setText("\n".join("\t".join(c) for c in lines.values()))
