# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Data viewer: browse a table's rows (search, sort, paging) and, live, edit them."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFontDatabase, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..core import editor
from ..core.exporter import SECRET_MASK, is_secret_column
from ..core.source import DataSource, Page, TableInfo, cell_text
from ..i18n import _

PAGE_SIZES = (50, 100, 500, 1000)
CELL_CHARS = 200        # longer values are cut in the grid (full text in the details)
TOOLTIP_CHARS = 2000
_ROOT = QModelIndex()
# Translucent, so they read on the light and the dark theme alike.
EDITED = QColor(255, 196, 0, 70)
ADDED = QColor(40, 180, 90, 60)
DELETED = QColor(230, 60, 60, 60)

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
    """One page of rows plus the pending edits, new rows and deletions."""

    pending_changed = Signal(int)

    def __init__(self, info: TableInfo, parent=None):
        super().__init__(parent)
        self.info = info
        self.cols = {c.name: c for c in info.columns}
        self.keys = editor.key_columns(info)
        self.auto = editor.auto_increment(info)
        self.edit_mode = False
        self.show_secrets = False
        # Callbacks set by the viewer: show an error / ask to confirm a warning.
        self.on_error: Callable[[str], None] = lambda _m: None
        self.on_confirm: Callable[[str], bool] = lambda _m: True
        self.page = Page((), (), 0)
        self.masked: list[bool] = []
        self._reset_rows()

    # ------------------------------------------------------------ state
    def _reset_rows(self) -> None:
        self.origin = [tuple(r) for r in self.page.rows]
        self.rows = [list(r) for r in self.page.rows]
        self.changed: set[tuple[int, int]] = set()
        self.deleted: set[int] = set()

    def set_page(self, page: Page, show_secrets: bool) -> None:
        self.beginResetModel()
        self.page = page
        self.show_secrets = show_secrets
        self.masked = [not show_secrets and is_secret_column(c) for c in page.columns]
        self._reset_rows()
        self.endResetModel()
        self.pending_changed.emit(0)

    def set_show_secrets(self, show: bool) -> None:
        self.beginResetModel()
        self.show_secrets = show
        self.masked = [not show and is_secret_column(c) for c in self.page.columns]
        self.endResetModel()

    def discard(self) -> None:
        self.set_page(self.page, self.show_secrets)

    def is_added(self, row: int) -> bool:
        return row >= len(self.origin)

    def pending(self) -> int:
        edited = {r for r, _c in self.changed if not self.is_added(r) and r not in self.deleted}
        return len(edited) + len(self.deleted) + (len(self.rows) - len(self.origin))

    def value(self, row: int, col: int) -> object:
        return self.rows[row][col]

    def column_info(self, col: int):
        return self.cols[self.page.columns[col]]

    def cell_editable(self, row: int, col: int) -> bool:
        name = self.page.columns[col]
        if not self.edit_mode or row in self.deleted or self.masked[col]:
            return False
        return name != self.auto if self.is_added(row) else name not in self.keys

    # --------------------------------------------------------- Qt model
    def rowCount(self, parent=_ROOT) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=_ROOT) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self.page.columns)

    def flags(self, index):
        base = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if index.isValid() and self.cell_editable(index.row(), index.column()):
            base |= Qt.ItemIsEditable
        return base

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        value, masked = self.rows[r][c], self.masked[c]
        unset = self.is_added(r) and (r, c) not in self.changed  # the server fills it in
        if role == Qt.DisplayRole:
            if unset:
                return _("(auto)") if self.page.columns[c] == self.auto else _("(default)")
            return grid_text(value, masked)
        if role == Qt.ForegroundRole and unset:
            return QGuiApplication.palette().placeholderText()
        if role == Qt.EditRole:
            return cell_text(value)
        if role == Qt.ToolTipRole:
            text = full_text(value, masked)
            return text if len(text) <= TOOLTIP_CHARS else text[:TOOLTIP_CHARS] + "…"
        if role == Qt.TextAlignmentRole and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.BackgroundRole:
            if r in self.deleted:
                return DELETED
            if self.is_added(r):
                return ADDED
            if (r, c) in self.changed:
                return EDITED
            return None
        if role == Qt.ForegroundRole and value is None:
            return QGuiApplication.palette().placeholderText()
        if role == Qt.FontRole and (value is None or r in self.deleted):
            font = QGuiApplication.font()
            font.setItalic(value is None)
            font.setStrikeOut(r in self.deleted)
            return font
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802 - Qt API
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.page.columns[section]
        return "+" if self.is_added(section) else str(self.page.offset + section + 1)

    def setData(self, index, text, role=Qt.EditRole):  # noqa: N802 - Qt API
        if role != Qt.EditRole or not index.isValid():
            return False
        r, c = index.row(), index.column()
        col = self.column_info(c)
        try:
            new = editor.coerce(text, col)
        except ValueError as exc:
            self.on_error(str(exc))
            return False
        return self._store(r, c, new, check=True)

    def _store(self, r: int, c: int, new: object, check: bool) -> bool:
        old = None if self.is_added(r) else self.origin[r][c]
        if check:
            error, warning = editor.check_value(self.column_info(c), old, new)
            if error:
                self.on_error(error)
                return False
            if warning and new != self.rows[r][c] and not self.on_confirm(warning):
                return False
        self.rows[r][c] = new
        if self.is_added(r) or new != old:
            self.changed.add((r, c))
        else:
            self.changed.discard((r, c))
        index = self.index(r, c)
        self.dataChanged.emit(index, index)
        self.pending_changed.emit(self.pending())
        return True

    # ---------------------------------------------------------- editing
    def set_null(self, indexes) -> list[str]:
        """Set the editable, nullable cells to NULL; returns columns that refuse NULL."""
        refused = []
        for idx in indexes:
            if not self.cell_editable(idx.row(), idx.column()):
                continue
            col = self.column_info(idx.column())
            if col.nullable:
                self._store(idx.row(), idx.column(), None, check=False)
            elif col.name not in refused:
                refused.append(col.name)
        return refused

    def add_row(self, values: dict | None = None) -> int:
        row = len(self.rows)
        self.beginInsertRows(_ROOT, row, row)
        self.rows.append([None] * len(self.page.columns))
        self.endInsertRows()
        for name, value in (values or {}).items():
            if name in self.page.columns and name != self.auto:
                c = list(self.page.columns).index(name)
                self.rows[row][c] = value
                self.changed.add((row, c))
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.page.columns) - 1))
        self.pending_changed.emit(self.pending())
        return row

    def row_values(self, row: int) -> dict:
        return dict(zip(self.page.columns, self.rows[row], strict=True))

    def toggle_delete(self, rows) -> None:
        """Mark loaded rows for deletion (or restore them); drop new rows outright."""
        for r in sorted(set(rows), reverse=True):
            if self.is_added(r):
                self.beginRemoveRows(_ROOT, r, r)
                del self.rows[r]
                self.changed = {(rr - (rr > r), cc) for rr, cc in self.changed if rr != r}
                self.endRemoveRows()
            else:
                self.deleted.symmetric_difference_update({r})
                self.dataChanged.emit(self.index(r, 0),
                                      self.index(r, len(self.page.columns) - 1))
        self.pending_changed.emit(self.pending())

    def changes(self) -> list[editor.Change]:
        cols = list(self.page.columns)
        out = []
        for r, origin in enumerate(self.origin):
            key = {k: origin[cols.index(k)] for k in self.keys}
            if r in self.deleted:
                out.append(editor.Change(editor.DELETE, key=key,
                                         old=dict(zip(cols, origin, strict=True))))
                continue
            edited = sorted(c for rr, c in self.changed if rr == r)
            if edited:
                out.append(editor.Change(
                    editor.UPDATE, {cols[c]: self.rows[r][c] for c in edited}, key,
                    {cols[c]: origin[c] for c in edited}))
        for r in range(len(self.origin), len(self.rows)):
            out.append(editor.Change(editor.INSERT, {
                cols[c]: self.rows[r][c] for c in sorted(c for rr, c in self.changed if rr == r)}))
        return out


class HighlightDelegate(QStyledItemDelegate):
    """Paints the model's edit colours: the theme's item style hides BackgroundRole."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        color = index.data(Qt.BackgroundRole)
        if isinstance(color, QColor):
            painter.fillRect(option.rect, color)  # translucent overlay keeps text readable


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


class SaveDialog(QDialog):
    """What will be saved, with the SQL, before anything is written."""

    def __init__(self, parent: QWidget, table: str, changes):
        super().__init__(parent)
        self.setWindowTitle(_("Save changes"))
        self.resize(760, 480)
        counts = editor.summary(changes)
        head = QLabel(_("Save to `{table}`: {updated} row(s) changed, {added} added, {deleted} "
                        "deleted. Everything runs in one transaction and an undo script is "
                        "written to the export folder.", table=table,
                        updated=counts[editor.UPDATE], added=counts[editor.INSERT],
                        deleted=counts[editor.DELETE]))
        head.setWordWrap(True)
        sql = QPlainTextEdit()
        sql.setReadOnly(True)
        sql.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        sql.setPlainText(";\n".join(editor.change_sql(table, c) for c in changes) + ";")
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v = QVBoxLayout(self)
        v.addWidget(head)
        v.addWidget(sql, 1)
        v.addWidget(buttons)


class DataViewer(QDialog):
    """A non-modal window browsing (and, on a live server, editing) one table."""

    def __init__(self, parent: QWidget, source: DataSource, table: str, run_job: RunJob,
                 backup_dir: Callable[[], Path] = lambda: Path("backups")):
        super().__init__(parent, Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.source, self.table, self.run_job = source, table, run_job
        self.backup_dir = backup_dir
        self.offset, self.order_by, self.descending = 0, None, False
        self.info = source.table_info(table)
        self.setWindowTitle(f"{table} — {source.label}")
        self.resize(1100, 680)

        self.search = QLineEdit()
        self.search.setPlaceholderText(_("Text to find…"))
        self.search.returnPressed.connect(self._new_search)
        self.column = QComboBox()
        self.column.addItem(_("All columns"), None)
        for col in self.info.columns:
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

        self.model = RowsModel(self.info, self)
        self.model.on_error = lambda msg: QMessageBox.warning(self, _("Invalid value"), msg)
        self.model.on_confirm = lambda msg: QMessageBox.question(
            self, _("Confirm"), msg) == QMessageBox.Yes
        self.model.pending_changed.connect(self._pending_changed)
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(HighlightDelegate(self.view))
        self.view.setAlternatingRowColors(True)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setWordWrap(False)
        self.view.horizontalHeader().setSectionsClickable(True)
        self.view.horizontalHeader().setSortIndicatorShown(True)
        self.view.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        self.view.horizontalHeader().sectionClicked.connect(self._sort_by)
        self.view.doubleClicked.connect(self._double_clicked)
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
        self.edit_mode = QCheckBox(_("Edit mode"))
        self.edit_mode.toggled.connect(self._toggle_edit)
        bottom = QHBoxLayout()
        for w in (self.first_btn, self.prev_btn, self.status, self.next_btn, self.last_btn):
            bottom.addWidget(w)
        bottom.addWidget(self.page_size)
        bottom.addStretch(1)
        bottom.addWidget(self.secrets)
        bottom.addWidget(details)
        bottom.addWidget(self.edit_mode)

        v = QVBoxLayout(self)
        v.addLayout(top)
        v.addWidget(self.view, 1)
        v.addWidget(self._build_edit_bar())
        v.addLayout(bottom)
        reason = editor.editable_reason(source, self.info)
        self.edit_mode.setEnabled(not reason)
        self.edit_mode.setToolTip(reason or _("Change, add and delete rows of this table"))
        self.refresh()

    def _build_edit_bar(self) -> QWidget:
        self.edit_bar = QWidget()
        h = QHBoxLayout(self.edit_bar)
        h.setContentsMargins(0, 0, 0, 0)
        buttons = (
            (_("Add row"), self._add_row, _("Append an empty row; fill in its cells")),
            (_("Duplicate"), self._duplicate, _("Copy the selected rows as new rows")),
            (_("Delete / restore"), self._delete, _("Mark the selected rows for deletion")),
            (_("Set NULL"), self._set_null, _("Set the selected cells to NULL")),
            (_("Import CSV/JSON…"), self._import, _("Add rows from a CSV or JSON file")),
        )
        for label, slot, tip in buttons:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            h.addWidget(b)
        h.addStretch(1)
        self.pending_label = QLabel()
        discard = QPushButton(_("Discard"))
        discard.clicked.connect(self._discard)
        self.save_btn = QPushButton(_("Save changes…"))
        self.save_btn.setProperty("role", "danger")
        self.save_btn.clicked.connect(self._save)
        for w in (self.pending_label, discard, self.save_btn):
            h.addWidget(w)
        self.edit_bar.setVisible(False)
        return self.edit_bar

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

        self.status.setText(_("Loading…"))
        self._set_nav(False)
        if not self.run_job(job, self._loaded, self._failed):
            self.status.setText(_("Busy — another operation is running; try again."))
            self._set_nav(True)

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

    # ---------------------------------------------------------- navigation
    def _leave_ok(self) -> bool:
        """Pending changes must be saved or discarded before the page is reloaded."""
        count = self.model.pending()
        if not count:
            return True
        box = QMessageBox(QMessageBox.Question, _("Unsaved changes"), _(
            "{count} unsaved change(s) on this page. Discard them?", count=count),
            QMessageBox.Discard | QMessageBox.Cancel, self)
        if box.exec() == QMessageBox.Discard:
            self.model.discard()
            return True
        return False

    def _new_search(self) -> None:
        if not self._leave_ok():
            return
        self.offset = 0
        self.refresh()

    def _clear_search(self) -> None:
        if not self._leave_ok():
            return
        self.search.clear()
        self.column.blockSignals(True)  # one search, not one per changed control
        self.column.setCurrentIndex(0)
        self.column.blockSignals(False)
        self._new_search()

    def _go(self, step: str) -> None:
        if not self._leave_ok():
            return
        total, limit = self.model.page.total, self.limit
        last = max((total - 1) // limit * limit, 0)
        self.offset = {"first": 0, "prev": max(self.offset - limit, 0),
                       "next": min(self.offset + limit, last), "last": last}[step]
        self.refresh()

    def _sort_by(self, section: int) -> None:
        if not self._leave_ok():
            return
        col = self.model.page.columns[section]
        self.descending = not self.descending if col == self.order_by else False
        self.order_by = col
        self.offset = 0
        self.refresh()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._leave_ok():
            event.accept()
        else:
            event.ignore()

    # ------------------------------------------------------------- viewing
    def _double_clicked(self, index) -> None:
        if not self.model.flags(index) & Qt.ItemIsEditable:
            self._show_row(index.row())

    def _show_current_row(self) -> None:
        index = self.view.currentIndex()
        if index.isValid():
            self._show_row(index.row())

    def _show_row(self, row: int) -> None:
        page = self.model.page
        number = "+" if self.model.is_added(row) else page.offset + row + 1
        title = _("Row {number} of {table}", number=number, table=self.table)
        RowDialog(self, title, page.columns, self.model.rows[row], self.model.masked).exec()

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

    # ------------------------------------------------------------- editing
    def _toggle_edit(self, on: bool) -> None:
        if not on and not self._leave_ok():
            self.edit_mode.blockSignals(True)
            self.edit_mode.setChecked(True)
            self.edit_mode.blockSignals(False)
            return
        self.model.edit_mode = on
        self.view.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.AnyKeyPressed if on else QAbstractItemView.NoEditTriggers)
        self.edit_bar.setVisible(on)
        self.model.layoutChanged.emit()  # refresh the editable flags

    def _pending_changed(self, count: int) -> None:
        self.pending_label.setText(_("{count} unsaved change(s)", count=count) if count else "")
        self.save_btn.setEnabled(bool(count))

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.view.selectedIndexes()})

    def _add_row(self) -> None:
        row = self.model.add_row()
        self.view.scrollToBottom()
        self.view.setCurrentIndex(self.model.index(row, 0))

    def _duplicate(self) -> None:
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, _("Duplicate"), _("Select the rows to copy first."))
            return
        for r in rows:
            self.model.add_row(self.model.row_values(r))
        self.view.scrollToBottom()

    def _delete(self) -> None:
        rows = self._selected_rows()
        if rows:
            self.model.toggle_delete(rows)

    def _set_null(self) -> None:
        refused = self.model.set_null(self.view.selectedIndexes())
        if refused:
            QMessageBox.information(self, _("Set NULL"), _(
                "These columns cannot be NULL: {columns}", columns=", ".join(refused)))

    def _import(self) -> None:
        path, _sel = QFileDialog.getOpenFileName(
            self, _("Import rows"), "", _("CSV or JSON (*.csv *.json);;*.*"))
        if not path:
            return
        try:
            changes, unknown = editor.import_changes(editor.read_rows_file(path), self.info)
        except (OSError, ValueError, editor.EditError) as exc:
            QMessageBox.warning(self, _("Import rows"), str(exc))
            return
        for change in changes:
            self.model.add_row(change.values)
        self.view.scrollToBottom()
        text = _("{count} row(s) added from {name}; review them, then Save changes.",
                 count=len(changes), name=Path(path).name)
        if unknown:
            text += "\n\n" + _("Columns not in `{table}`, ignored: {columns}", table=self.table,
                               columns=", ".join(unknown))
        QMessageBox.information(self, _("Import rows"), text)

    def _discard(self) -> None:
        if self.model.pending() and QMessageBox.question(
                self, _("Discard"), _("Discard {count} unsaved change(s)?",
                                      count=self.model.pending())) == QMessageBox.Yes:
            self.model.discard()

    def _save(self) -> None:
        changes = self.model.changes()
        if not changes:
            return
        if SaveDialog(self, self.table, changes).exec() != QDialog.Accepted:
            return
        src, table, folder = self.source, self.table, self.backup_dir()

        def job():
            return editor.apply_changes(src, table, changes, backup_dir=folder)

        def done(result):
            counts = result["counts"]
            QMessageBox.information(self, _("Saved"), _(
                "Saved: {updated} changed, {added} added, {deleted} deleted.\n\nUndo script: "
                "{path}", updated=counts[editor.UPDATE], added=counts[editor.INSERT],
                deleted=counts[editor.DELETE], path=result["undo"]))
            self.model.discard()
            self.refresh()

        def failed(error: str) -> None:
            self.status.setText(_("Nothing was saved."))
            QMessageBox.critical(self, _("Save changes"), error)

        self.status.setText(_("Saving…"))
        if not self.run_job(job, done, failed):
            self.status.setText(_("Busy — another operation is running; try again."))
