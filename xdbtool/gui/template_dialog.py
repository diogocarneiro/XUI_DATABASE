# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Dialog that picks which rows a clean copy of a template leaves out."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core.rebuild import format_size
from ..core.templates import SeedTable, TemplateMeta
from ..i18n import _


class CleanTemplateDialog(QDialog):
    """Lists the template's tables with rows; ticked ones lose their rows in the copy."""

    def __init__(self, parent, meta: TemplateMeta, seeds: list[SeedTable]):
        super().__init__(parent)
        self.setWindowTitle(_("Clean template"))
        self.resize(620, 620)
        self.seeds = seeds
        layout = QVBoxLayout(self)
        intro = QLabel(_(
            "Makes a copy of <b>{name}</b> with the same schema but without the rows of the "
            "ticked tables. Tables holding a panel's data — streams, lines, accounts, logs, "
            "server addresses, block lists — are ticked already; configuration tables "
            "(settings, groups, stream types…) keep their rows. The original template is not "
            "changed.", name=meta.name))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(len(seeds), 2)
        self.table.setHorizontalHeaderLabels([_("Remove rows of"), _("Size")])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        for r, seed in enumerate(seeds):
            item = QTableWidgetItem(seed.name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if seed.suggested else Qt.Unchecked)
            self.table.setItem(r, 0, item)
            size = QTableWidgetItem(format_size(seed.size))
            size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(r, 1, size)
        self.table.itemChanged.connect(self._update_total)
        layout.addWidget(self.table, 1)
        self.total = QLabel()
        layout.addWidget(self.total)

        form = QFormLayout()
        self.key = QLineEdit(f"{meta.key}-clean")
        self.name = QLineEdit(_("{name} (clean)", name=meta.name))
        form.addRow(_("New template key:"), self.key)
        form.addRow(_("Display name:"), self.name)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(_("Create clean template"))
        buttons.button(QDialogButtonBox.Cancel).setText(_("Cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_total()

    def dropped(self) -> list[str]:
        return [self.table.item(r, 0).text() for r in range(self.table.rowCount())
                if self.table.item(r, 0).checkState() == Qt.Checked]

    def _update_total(self, *_args) -> None:
        gone = set(self.dropped())
        removed = sum(s.size for s in self.seeds if s.name in gone)
        kept = sum(s.size for s in self.seeds) - removed
        self.total.setText(_("Rows removed: {removed} · rows kept: {kept}",
                             removed=format_size(removed), kept=format_size(kept)))
