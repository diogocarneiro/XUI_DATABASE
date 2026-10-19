# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Reusable "Connect via: Direct / SSH tunnel" rows for any MySQL connection form."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from ..core.ssh_remote import SshConfig


class SshFields:
    """Adds a connection-mode combo and an SSH row to a ``QFormLayout``.

    Modes: direct MySQL; SSH + the server's MySQL socket (logs in like `mysql -uroot`
    typed on the server — works for root with unix_socket auth, no password); and
    SSH tunnel to MySQL's TCP port on 127.0.0.1. In the SSH modes the address the
    user typed as MySQL host is the server itself, so it moves to the SSH host and
    MySQL becomes 127.0.0.1; with the socket the MySQL host/port rows are hidden.
    """

    MODES = (
        ("direct", "Direct MySQL connection"),
        ("socket", "SSH + server MySQL socket (like `mysql -uroot` on the server)"),
        ("ssh", "SSH tunnel to MySQL TCP 127.0.0.1 (password users)"),
    )

    def __init__(self, form: QFormLayout, host_edit: QLineEdit, host_label: str = "Host:",
                 port_edit: QWidget | None = None):
        self.form = form
        self.host_edit = host_edit
        self.port_edit = port_edit
        self.host_label = host_label
        self.via = QComboBox()
        for key, label in self.MODES:
            self.via.addItem(label, key)
        self.host = QLineEdit()
        self.host.setPlaceholderText("server IP, e.g. 192.168.1.106")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.user = QLineEdit("root")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("blank = SSH key / agent")
        self.key = QLineEdit()
        self.key.setPlaceholderText("optional private key file")

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        for label, widget, stretch in (("Host:", self.host, 3), ("Port:", self.port, 0),
                                       ("User:", self.user, 1), ("Password:", self.password, 2),
                                       ("Key:", self.key, 2)):
            row.addWidget(QLabel(label))
            row.addWidget(widget, stretch)
        self.row_widget = QWidget()
        self.row_widget.setLayout(row)

    def add_rows(self) -> None:
        """Append the two rows to the form (call before adding the host row)."""
        self.form.addRow("Connect via:", self.via)
        self.form.addRow("SSH:", self.row_widget)
        self.via.currentIndexChanged.connect(self._on_mode)
        self._on_mode(0)

    @property
    def mode(self) -> str:
        return self.via.currentData()

    @property
    def enabled(self) -> bool:
        return self.mode != "direct"

    def set_mode(self, mode: str) -> None:
        idx = self.via.findData(mode)
        self.via.setCurrentIndex(max(idx, 0))

    def _on_mode(self, _idx: int) -> None:
        ssh, sock = self.enabled, self.mode == "socket"
        self.form.setRowVisible(self.row_widget, ssh)
        for w in (self.host_edit, self.port_edit):
            if w is not None and self.form.indexOf(w) >= 0:
                self.form.setRowVisible(w, not sock)
        label = self.form.labelForField(self.host_edit)
        if label is not None:
            label.setText("MySQL host (on server):" if ssh else self.host_label)
        typed = self.host_edit.text().strip()
        if ssh and typed not in ("", "127.0.0.1", "localhost"):
            if not self.host.text().strip():
                self.host.setText(typed)
            if typed == self.host.text().strip():
                self.host_edit.setText("127.0.0.1")

    def config(self) -> SshConfig | None:
        """The SSH settings, or ``None`` in direct mode. Raises if the host is empty."""
        if not self.enabled:
            return None
        cfg = SshConfig(host=self.host.text().strip(), port=self.port.value(),
                        user=self.user.text().strip() or "root",
                        password=self.password.text(), key_file=self.key.text().strip(),
                        mysql_socket="auto" if self.mode == "socket" else "")
        if not cfg.host:
            raise ValueError("Enter the SSH host.")
        return cfg
