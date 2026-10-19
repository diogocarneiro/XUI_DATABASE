# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Main application window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from .. import about as about_info
from ..core import profiles, templates, updater, xui_import, xui_migrate
from ..core.config import (
    ConnectionProfile,
    load_settings,
    load_store,
    save_settings,
    save_store,
)
from ..core.dump_source import SqlDumpSource
from ..core.exporter import ExportOptions, export_tables
from ..core.migrator import MigrationOptions, build_plan, execute_plan
from ..core.mysql_source import ConnectionConfig, DropRefused, MySQLSource
from ..core.repair import check_and_repair, run_maintenance
from ..core.restore import restore_dump
from ..core.source import DataSource
from ..core.ssh_remote import SshConfig
from ..core.templates import create_from_template, register_template
from ..i18n import LANGUAGES, _, get_language, set_language
from . import theme
from .help_dialog import AboutDialog, HelpDialog
from .ssh_fields import SshFields
from .workers import JobRunner

REDACT_HINTS = frozenset({"password", "pass", "pwd", "secret", "token", "api_key", "apikey"})
SQL_FILTER = "SQL (*.sql);;*.*"
_startup_check_done = False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("XUI DB Tool")
        self.resize(1040, 720)
        self.source: DataSource | None = None
        self.runner = JobRunner(self)
        self.update_runner = JobRunner(self)  # update checks never block real work
        self._build_ui()
        self._load_profiles()
        global _startup_check_done
        if not _startup_check_done and load_settings().get("check_updates", True):
            _startup_check_done = True  # once per run, not again after a language switch
            QTimer.singleShot(2000, lambda: self._check_updates(silent=True))

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 10, 16, 12)
        root.setSpacing(12)
        root.addWidget(self._build_header())
        root.addWidget(self._build_source_box())
        body = QSplitter(Qt.Horizontal)
        body.setChildrenCollapsible(False)
        body.setHandleWidth(10)
        body.addWidget(self._build_tables_box())
        body.addWidget(self._build_actions_box())
        body.setStretchFactor(0, 2)
        body.setStretchFactor(1, 3)
        body.setSizes([560, 840])
        root.addWidget(body, 1)
        root.addWidget(self._build_progress_box())
        self.setCentralWidget(central)

        theme_menu = self.menuBar().addMenu(_("Theme"))
        theme_group = QActionGroup(self)
        for mode, label in (("auto", _("Automatic (system)")), ("light", _("Light")),
                            ("dark", _("Dark"))):
            act = QAction(label, self, checkable=True)
            act.setChecked(mode == theme.current_mode())
            act.triggered.connect(lambda _c=False, m=mode: self._change_theme(m))
            theme_group.addAction(act)
            theme_menu.addAction(act)

        lang_menu = self.menuBar().addMenu(_("Language"))
        group = QActionGroup(self)
        for code, name in LANGUAGES.items():
            act = QAction(name, self, checkable=True)
            act.setChecked(code == get_language())
            act.triggered.connect(lambda _c=False, c=code: self._change_language(c))
            group.addAction(act)
            lang_menu.addAction(act)
        help_menu = self.menuBar().addMenu(_("Help"))
        guide = QAction(_("User guide"), self)
        guide.setShortcut(QKeySequence.HelpContents)  # F1
        guide.triggered.connect(lambda: HelpDialog(self).exec())
        help_menu.addAction(guide)
        help_menu.addSeparator()
        check = QAction(_("Check for updates…"), self)
        check.triggered.connect(lambda: self._check_updates(silent=False))
        auto_check = QAction(_("Check for updates at startup"), self, checkable=True)
        auto_check.setChecked(bool(load_settings().get("check_updates", True)))
        auto_check.toggled.connect(lambda on: save_settings(check_updates=on))
        about = QAction(_("About"), self)
        about.triggered.connect(self._about)
        for act in (check, auto_check):
            help_menu.addAction(act)
        help_menu.addSeparator()
        contact = QAction(_("Contact the developer (Telegram)"), self)
        contact.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(about_info.TELEGRAM)))
        report = QAction(_("Report a problem / suggestion (GitHub)"), self)
        report.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(about_info.ISSUES)))
        for act in (contact, report):
            help_menu.addAction(act)
        help_menu.addSeparator()
        help_menu.addAction(about)

    # ------------------------------------------------------------ updates
    def _check_updates(self, silent: bool) -> None:
        if self.update_runner.busy:
            return
        token = updater.token_from_env(load_settings())

        def job(progress, message):
            return updater.check(token)

        def done(result):
            newer, release = result
            if newer:
                self._offer_update(release)
            elif not silent:
                QMessageBox.information(self, _("No updates"), _(
                    "You have the latest version ({version}).", version=__version__))

        def failed(err):
            self._log(_("Update check failed: {error}", error=err))
            if not silent:
                QMessageBox.warning(self, _("Update check failed"), err)

        if not silent:
            self._log(_("Checking for updates…"))
        self.update_runner.start(job, on_finished=done, on_failed=failed)

    def _offer_update(self, release) -> None:
        kind = updater.install_kind()
        box = QMessageBox(self)
        box.setWindowTitle(_("Update available"))
        box.setIcon(QMessageBox.Information)
        box.setText(_("Version {new} is available (you have {current}).",
                      new=release.version, current=__version__))
        if kind == "git":
            box.setInformativeText(_("This copy runs from a git checkout; update it with "
                                     "`git pull` instead."))
        if release.notes:
            box.setDetailedText(release.notes)
        update_btn = box.addButton(_("Update now"), QMessageBox.AcceptRole)
        update_btn.setEnabled(kind != "git")
        page_btn = box.addButton(_("Open release page"), QMessageBox.ActionRole)
        box.addButton(_("Later"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is page_btn:
            QDesktopServices.openUrl(QUrl(release.html_url))
        elif box.clickedButton() is update_btn:
            self._apply_update(release)

    def _apply_update(self, release) -> None:
        if self.runner.busy:
            QMessageBox.information(self, _("Busy"), _("Another operation is still running."))
            return
        token = updater.token_from_env(load_settings())

        def job(progress, message):
            return updater.apply_update(release, token, progress=progress, message=message)

        def done(result):
            self.progress.setValue(100)
            if result == "restart":
                QMessageBox.information(self, _("Update ready"), _(
                    "The app will now close and restart on version {version}.",
                    version=release.version))
                QApplication.instance().quit()
            else:
                QMessageBox.information(self, _("Update installed"), _(
                    "Version {version} is installed. Restart the app to use it.",
                    version=release.version))

        self._log(_("Updating to {version}…", version=release.version))
        self._start_custom(job, done)

    def _build_header(self) -> QWidget:
        header = QWidget()
        row = QHBoxLayout(header)
        row.setContentsMargins(2, 2, 2, 6)
        title = QLabel("XUI DB Tool")
        title.setObjectName("appTitle")
        subtitle = QLabel(_("Inspect, back up, repair and migrate panel databases"))
        subtitle.setObjectName("appSubtitle")
        version = QLabel(f"v{__version__}")
        version.setProperty("role", "muted")
        row.addWidget(title)
        row.addSpacing(10)
        row.addWidget(subtitle, 1)
        row.addWidget(version)
        return header

    def _change_theme(self, mode: str) -> None:
        save_settings(theme=mode)
        theme.apply_theme(QApplication.instance(), mode)

    @staticmethod
    def _role(button: QPushButton, role: str) -> QPushButton:
        """Tag a button as "primary" (main action) or "danger" (writes to a server)."""
        button.setProperty("role", role)
        return button

    def _set_status(self, text: str, state: str = "") -> None:
        self.status_lbl.setText(text)
        self.status_lbl.setProperty("state", state)
        self.status_lbl.style().unpolish(self.status_lbl)
        self.status_lbl.style().polish(self.status_lbl)

    def _change_language(self, code: str) -> None:
        """Save the choice and rebuild the window in the new language."""
        if code == get_language():
            return
        if self.runner.busy:
            QMessageBox.information(self, _("Busy"), _("Another operation is still running."))
            return
        save_settings(language=code)
        set_language(code)
        new = MainWindow()
        new.setGeometry(self.geometry())
        new.show()
        QApplication.instance()._xdb_window = new  # keep the new window referenced
        self.close()

    def _build_source_box(self) -> QWidget:
        box = QGroupBox(_("Data source"))
        outer = QVBoxLayout(box)
        outer.setSpacing(10)

        picker = QHBoxLayout()
        self.source_kind = QComboBox()
        self.source_kind.addItems([_("Live MySQL / MariaDB"), _("SQL dump file (.sql)")])
        self.source_kind.currentIndexChanged.connect(self._show_source_page)
        picker.addWidget(QLabel(_("Source type:")))
        picker.addWidget(self.source_kind)
        picker.addStretch(1)
        outer.addLayout(picker)

        self.source_pages = [self._build_mysql_form(), self._build_dump_form()]
        for page in self.source_pages:
            outer.addWidget(page)
        self._show_source_page(0)

        row = QHBoxLayout()
        self.connect_btn = self._role(QPushButton(_("Connect / Load")), "primary")
        self.connect_btn.clicked.connect(self._connect_source)
        self.status_lbl = QLabel(_("Not connected."))
        self.status_lbl.setObjectName("status")
        row.addWidget(self.connect_btn)
        row.addWidget(self.status_lbl, 1)
        outer.addLayout(row)
        return box

    def _show_source_page(self, index: int) -> None:
        """Show only the chosen source form. Hidden forms take no space, so the card
        shrinks to the short dump form (a QStackedWidget kept the MySQL form's height)."""
        for i, page in enumerate(self.source_pages):
            page.setVisible(i == index)

    def _build_mysql_form(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._apply_profile)
        self.in_host = QLineEdit("127.0.0.1")
        self.in_port = QSpinBox()
        self.in_port.setRange(1, 65535)
        self.in_port.setValue(3306)
        self.in_user = QLineEdit("root")
        self.in_pass = QLineEdit()
        self.in_pass.setEchoMode(QLineEdit.Password)
        self.in_db = QLineEdit()
        self.save_pass = QCheckBox(_("Save password in profile"))

        prof_row = QHBoxLayout()
        prof_row.addWidget(self.profile_combo, 1)
        save_btn = QPushButton(_("Save"))
        save_btn.clicked.connect(self._save_profile)
        del_btn = QPushButton(_("Delete"))
        del_btn.clicked.connect(self._delete_profile)
        prof_row.addWidget(save_btn)
        prof_row.addWidget(del_btn)

        form.addRow(_("Profile:"), self._wrap(prof_row))
        self.src_ssh = SshFields(form, self.in_host, port_edit=self.in_port)
        self.src_ssh.add_rows()
        form.addRow(_("Host:"), self.in_host)
        form.addRow(_("Port:"), self.in_port)
        form.addRow(_("User:"), self.in_user)
        form.addRow(_("Password:"), self.in_pass)
        form.addRow(_("Database:"), self.in_db)
        form.addRow("", self.save_pass)
        return w

    def _build_dump_form(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        row = QHBoxLayout()
        self.in_dump = QLineEdit()
        browse = QPushButton(_("Browse…"))
        browse.clicked.connect(self._browse_dump)
        row.addWidget(self.in_dump, 1)
        row.addWidget(browse)
        form.addRow(_("Dump file:"), self._wrap(row))
        note = QLabel(_("The whole dump is parsed into memory. For very large dumps, use a "
                        "live MySQL connection instead."))
        note.setWordWrap(True)
        form.addRow(note)
        return w

    def _build_tables_box(self) -> QWidget:
        box = QGroupBox(_("Tables"))
        v = QVBoxLayout(box)
        top = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(_("Filter tables…"))
        self.filter_edit.textChanged.connect(self._apply_filter)
        all_btn = QPushButton(_("All"))
        all_btn.clicked.connect(lambda: self._set_all_checks(True))
        none_btn = QPushButton(_("None"))
        none_btn.clicked.connect(lambda: self._set_all_checks(False))
        top.addWidget(self.filter_edit, 1)
        top.addWidget(all_btn)
        top.addWidget(none_btn)
        v.addLayout(top)

        self.table_widget = QTableWidget(0, 4)
        self.table_widget.setHorizontalHeaderLabels(
            [_("Table"), _("Category"), _("Rows"), _("Columns")])
        self.table_widget.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table_widget.verticalHeader().setVisible(False)
        self.table_widget.setAlternatingRowColors(True)
        self.table_widget.setShowGrid(False)
        self.table_widget.setEditTriggers(QTableWidget.NoEditTriggers)
        v.addWidget(self.table_widget)
        return box

    def _build_actions_box(self) -> QWidget:
        tabs = QTabWidget()
        tabs.addTab(self._build_export_tab(), _("Export / Backup"))
        tabs.addTab(self._build_maint_tab(), _("Maintenance"))
        tabs.addTab(self._build_templates_tab(), _("Templates / New DB"))
        tabs.addTab(self._build_migrate_tab(), _("Migrate / Restore"))
        tabs.addTab(self._build_xui_migrate_tab(), _("XUI Migration"))
        return tabs

    def _build_export_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.export_fmt = QComboBox()
        self.export_fmt.addItems(["json", "csv", "sql"])
        self.export_dir = QLineEdit(str(Path.cwd() / "exports"))
        dir_btn = QPushButton("…")
        dir_btn.clicked.connect(self._browse_export_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.export_dir, 1)
        dir_row.addWidget(dir_btn)
        self.redact_cb = QCheckBox(_("Redact password/secret-like columns"))
        self.schema_cb = QCheckBox(_("Include CREATE TABLE (SQL format, live source)"))
        self.schema_cb.setChecked(True)
        self.export_btn = self._role(QPushButton(_("Export selected tables")), "primary")
        self.export_btn.clicked.connect(self._do_export)

        form.addRow(_("Format:"), self.export_fmt)
        form.addRow(_("Output folder:"), self._wrap(dir_row))
        form.addRow("", self.redact_cb)
        form.addRow("", self.schema_cb)
        form.addRow(self.export_btn)
        return w

    def _build_maint_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.maint_op = QComboBox()
        for op, label in (("check", _("Check")), ("repair", _("Repair")),
                          ("analyze", _("Analyze")), ("optimize", _("Optimize"))):
            self.maint_op.addItem(label, op)
        run_btn = self._role(QPushButton(_("Run on selected tables")), "primary")
        run_btn.clicked.connect(self._do_maintenance)
        auto_btn = self._role(QPushButton(_("Check & auto-repair broken tables")), "danger")
        auto_btn.clicked.connect(self._do_auto_repair)
        note = QLabel(_("Maintenance requires a live MySQL/MariaDB connection. Always back up "
                        "(Export → SQL) before repairing."))
        note.setWordWrap(True)
        form.addRow(_("Operation:"), self.maint_op)
        form.addRow(run_btn)
        form.addRow(auto_btn)
        form.addRow(note)
        return w

    def _build_templates_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.tmpl_combo = QComboBox()
        refresh = QPushButton(_("Refresh"))
        refresh.clicked.connect(self._refresh_templates)
        reg = QPushButton(_("Register base .sql…"))
        reg.clicked.connect(self._register_template_dialog)
        tmpl_row = QHBoxLayout()
        tmpl_row.addWidget(self.tmpl_combo, 1)
        tmpl_row.addWidget(refresh)

        self.t_host = QLineEdit("127.0.0.1")
        self.t_port = QSpinBox()
        self.t_port.setRange(1, 65535)
        self.t_port.setValue(3306)
        self.t_user = QLineEdit("root")
        self.t_pass = QLineEdit()
        self.t_pass.setEchoMode(QLineEdit.Password)
        self.t_db = QLineEdit()
        self.t_db.setPlaceholderText(_("new database name, e.g. xui_test"))
        self.t_drop = QCheckBox(_("DROP database first if it already exists"))
        create_btn = self._role(QPushButton(_("Create database from template")), "primary")
        create_btn.clicked.connect(self._do_init_db)

        form.addRow(_("Template:"), self._wrap(tmpl_row))
        form.addRow("", reg)
        self.t_ssh = SshFields(form, self.t_host, _("Target host:"), self.t_port)
        self.t_ssh.add_rows()
        form.addRow(_("Target host:"), self.t_host)
        form.addRow(_("Target port:"), self.t_port)
        form.addRow(_("Target user:"), self.t_user)
        form.addRow(_("Target password:"), self.t_pass)
        form.addRow(_("New database:"), self.t_db)
        form.addRow("", self.t_drop)
        form.addRow(create_btn)
        self._refresh_templates()
        return w

    def _refresh_templates(self) -> None:
        self.tmpl_combo.clear()
        for m in templates.list_templates():
            present = "" if m.sql_path().is_file() else "  " + _("[.sql missing]")
            self.tmpl_combo.addItem(
                _("{key} — {name} ({count} tables)", key=m.key, name=m.name,
                  count=m.table_count) + present, m.key)
        if self.tmpl_combo.count() == 0:
            self.tmpl_combo.addItem(_("<no templates — register one>"), None)

    def _register_template_dialog(self) -> None:
        path, _sel = QFileDialog.getOpenFileName(self, _("Select base schema .sql"), "",
                                                 SQL_FILTER)
        if not path:
            return
        panel, ok = QInputDialog.getText(self, _("Panel key"), _("Panel key (e.g. xui_one):"))
        if not ok or not panel:
            return
        version, _ok = QInputDialog.getText(self, _("Version"),
                                            _("Version (optional, e.g. 1.5.13):"))
        default_name = f"{panel} {version}".strip()
        name, _ok = QInputDialog.getText(self, _("Name"), _("Display name:"), text=default_name)

        def job(progress, message):
            message(_("Parsing and registering {file}…", file=Path(path).name))
            return register_template(
                path, name=name or panel, panel_key=panel, version=version or ""
            )

        def done(meta):
            self._log(_("Registered template '{key}' ({count} tables).", key=meta.key,
                        count=meta.table_count))
            self._refresh_templates()

        self._start_custom(job, done)

    def _do_init_db(self) -> None:
        if self.runner.busy:
            QMessageBox.information(self, _("Busy"), _("Another operation is still running."))
            return
        key = self.tmpl_combo.currentData()
        if not key:
            QMessageBox.warning(self, _("No template"),
                                _("Register and select a template first."))
            return
        db_name = self.t_db.text().strip()
        if not db_name:
            QMessageBox.warning(self, _("No name"), _("Enter a name for the new database."))
            return
        if self.t_drop.isChecked() and QMessageBox.question(
            self, _("Confirm drop"),
            _("DROP DATABASE `{db}` if it exists, then recreate it?", db=db_name),
        ) != QMessageBox.Yes:
            return
        try:
            ssh = self.t_ssh.config()
        except ValueError as exc:
            QMessageBox.warning(self, "SSH", str(exc))
            return
        drop = self.t_drop.isChecked()

        def run(allow_drop_panel: bool) -> None:
            cfg = ConnectionConfig(
                host=self.t_host.text().strip() or "127.0.0.1", port=self.t_port.value(),
                user=self.t_user.text().strip(), password=self.t_pass.text(), ssh=ssh,
            )

            def job(progress, message):
                message(_("Creating `{db}` from template '{key}'…", db=db_name, key=key))
                try:
                    return create_from_template(cfg, key, db_name, drop_existing=drop,
                                                allow_drop_panel=allow_drop_panel,
                                                progress=progress)
                except DropRefused as exc:
                    return exc

            def done(result):
                if isinstance(result, DropRefused):
                    if self._confirm_panel_drop(result):
                        run(allow_drop_panel=True)
                    else:
                        self._log(_("Cancelled: `{db}` was left untouched.", db=db_name))
                    return
                self.progress.setValue(100)
                self._log(_("Database `{db}` created from template ({count} statements).",
                            db=db_name, count=result["statements"]))

            self._start_custom(job, done)

        run(allow_drop_panel=False)

    def _confirm_panel_drop(self, exc: DropRefused) -> bool:
        """Second, typed confirmation before dropping a database with panel data."""
        rows = "\n".join("  " + _("{table}: {count} rows", table=t, count=n)
                         for t, n in exc.rows.items())
        self._log(str(exc))
        text, ok = QInputDialog.getText(
            self, _("This database holds panel data"),
            _("`{db}` is NOT empty — it contains panel data:\n{rows}\n\nDropping it destroys "
              "all of this (if it is the live panel database, the panel stops working).\n\n"
              "Type the database name to DROP it anyway:", db=exc.database, rows=rows),
        )
        return ok and text.strip() == exc.database

    def _start_custom(self, job, on_finished) -> None:
        self.progress.setValue(0)

        def failed(err):
            self._log(_("ERROR: {error}", error=err))
            QMessageBox.critical(self, _("Operation failed"), err)

        self.runner.start(
            job, on_progress=self._on_progress, on_message=self._log,
            on_finished=on_finished, on_failed=failed,
        )

    def _build_migrate_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.m_host = QLineEdit("127.0.0.1")
        self.m_port = QSpinBox()
        self.m_port.setRange(1, 65535)
        self.m_port.setValue(3306)
        self.m_user = QLineEdit("root")
        self.m_pass = QLineEdit()
        self.m_pass.setEchoMode(QLineEdit.Password)
        self.m_db = QLineEdit()
        form.addRow(QLabel(_("<b>Target server</b> (data is written here)")))
        self.m_ssh = SshFields(form, self.m_host, port_edit=self.m_port)
        self.m_ssh.add_rows()
        form.addRow(_("Host:"), self.m_host)
        form.addRow(_("Port:"), self.m_port)
        form.addRow(_("User:"), self.m_user)
        form.addRow(_("Password:"), self.m_pass)
        form.addRow(_("Database:"), self.m_db)

        # -- migrate loaded source --
        form.addRow(QLabel(_("<b>Migrate loaded source → target</b>")))
        self.m_truncate = QCheckBox(_("Empty target tables first (TRUNCATE)"))
        self.m_ignore = QCheckBox(_("Skip duplicate rows (INSERT IGNORE)"))
        self.m_ignore.setChecked(True)
        plan_btn = QPushButton(_("Build plan (preview in log)"))
        plan_btn.clicked.connect(self._do_build_plan)
        mig_btn = self._role(QPushButton(_("Run migration → inject into server")), "danger")
        mig_btn.clicked.connect(lambda: self._do_migrate(dry_run=False))
        sql_btn = QPushButton(_("Dry run → write migration .sql"))
        sql_btn.clicked.connect(lambda: self._do_migrate(dry_run=True))
        form.addRow("", self.m_truncate)
        form.addRow("", self.m_ignore)
        form.addRow(plan_btn)
        form.addRow(mig_btn)
        form.addRow(sql_btn)

        # -- restore a dump --
        form.addRow(QLabel(_("<b>Restore a .sql dump → target</b>")))
        self.m_dump = QLineEdit()
        dump_browse = QPushButton("…")
        dump_browse.clicked.connect(self._browse_restore_dump)
        dump_row = QHBoxLayout()
        dump_row.addWidget(self.m_dump, 1)
        dump_row.addWidget(dump_browse)
        self.m_create = QCheckBox(_("Create the target database first"))
        self.m_drop = QCheckBox(_("DROP it first if it exists"))
        restore_btn = self._role(QPushButton(_("Restore dump → inject into server")), "danger")
        restore_btn.clicked.connect(self._do_restore)
        form.addRow(_("Dump file:"), self._wrap(dump_row))
        form.addRow("", self.m_create)
        form.addRow("", self.m_drop)
        form.addRow(restore_btn)
        return w

    def _browse_restore_dump(self) -> None:
        path, _sel = QFileDialog.getOpenFileName(self, _("Select .sql dump to restore"), "",
                                                 SQL_FILTER)
        if path:
            self.m_dump.setText(path)

    def _mig_target_cfg(self) -> ConnectionConfig | None:
        """Target settings of the Migrate / Restore tab (None after a warning)."""
        try:
            ssh = self.m_ssh.config()
        except ValueError as exc:
            QMessageBox.warning(self, "SSH", str(exc))
            return None
        return ConnectionConfig(
            host=self.m_host.text().strip() or "127.0.0.1", port=self.m_port.value(),
            user=self.m_user.text().strip(), password=self.m_pass.text(),
            database=self.m_db.text().strip(), ssh=ssh,
        )

    @staticmethod
    def _where(cfg: ConnectionConfig) -> str:
        return f"{cfg.ssh.host} (SSH)" if cfg.ssh else cfg.host

    def _do_build_plan(self) -> None:
        if self._busy_guard():
            return
        src = self.source
        cfg = self._mig_target_cfg()
        if cfg is None:
            return
        if not cfg.database:
            QMessageBox.warning(self, _("No target DB"), _("Enter the target database name."))
            return

        def job(progress, message):
            with MySQLSource(cfg) as target:
                plan = build_plan(src, target)
            message(_("Migration plan:"))
            for m in plan.mappings:
                message(f"  {m.summary}")
            if plan.missing_in_target:
                message("  " + _("missing in target: {tables}",
                                 tables=", ".join(plan.missing_in_target)))
            return plan

        self._start(job, done_msg=_("Plan built (see log)."))

    def _do_migrate(self, dry_run: bool) -> None:
        if self._busy_guard():
            return
        cfg = self._mig_target_cfg()
        if cfg is None:
            return
        if not cfg.database:
            QMessageBox.warning(self, _("No target DB"), _("Enter the target database name."))
            return
        sql_out = None
        if dry_run:
            sql_out, _sel = QFileDialog.getSaveFileName(self, _("Save migration SQL"),
                                                        "migration.sql", SQL_FILTER)
            if not sql_out:
                return
        elif QMessageBox.question(
            self, _("Confirm migration"),
            _("Write data directly into `{db}` on {where}?\nThis modifies the live target. "
              "Make sure you have a backup.", db=cfg.database, where=self._where(cfg)),
        ) != QMessageBox.Yes:
            return
        src = self.source
        opts = MigrationOptions(
            truncate_target=self.m_truncate.isChecked(),
            ignore_duplicates=self.m_ignore.isChecked(),
            dry_run_sql_path=sql_out,
        )

        def job(progress, message):
            with MySQLSource(cfg) as target:
                plan = build_plan(src, target)
                message(_("Transferring {count} table(s)…", count=len(plan.transferable)))
                written = execute_plan(src, target, plan, opts, progress)
            message(_("Done: {rows} rows across {tables} table(s).",
                      rows=sum(written.values()), tables=len(written)))
            return written

        self._start(job, done_msg=_("Migration finished."))

    def _do_restore(self) -> None:
        if self.runner.busy:
            QMessageBox.information(self, _("Busy"), _("Another operation is still running."))
            return
        dump = self.m_dump.text().strip()
        if not dump:
            QMessageBox.warning(self, _("No dump"), _("Select a .sql dump to restore."))
            return
        cfg = self._mig_target_cfg()
        if cfg is None:
            return
        db = cfg.database or None
        if self.m_create.isChecked() and not db:
            QMessageBox.warning(self, _("No target DB"), _("Enter the database name to create."))
            return
        dest = db or _("(dump's own database)")
        if QMessageBox.question(
            self, _("Confirm restore"),
            _("Execute '{file}' directly into {db} on {where}?\nThis runs the dump as-is on "
              "the live server.", file=Path(dump).name, db=dest, where=self._where(cfg)),
        ) != QMessageBox.Yes:
            return
        create = self.m_create.isChecked()
        drop = self.m_drop.isChecked()

        def run(allow_drop_panel: bool) -> None:
            def job(progress, message):
                message(_("Restoring {file}…", file=Path(dump).name))
                try:
                    return restore_dump(cfg, dump, database=db, create=create,
                                        drop_existing=drop, allow_drop_panel=allow_drop_panel,
                                        progress=progress)
                except DropRefused as exc:
                    return exc

            def done(result):
                if isinstance(result, DropRefused):
                    if self._confirm_panel_drop(result):
                        run(allow_drop_panel=True)
                    else:
                        self._log(_("Cancelled: `{db}` was left untouched.", db=result.database))
                    return
                self.progress.setValue(100)
                self._log(_("Restore finished ({count} statements).",
                            count=result["statements"]))

            self._start_custom(job, done)

        run(allow_drop_panel=False)

    def _build_xui_migrate_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        db = xui_migrate.MIGRATE_DB
        intro = QLabel(_(
            "Stage an old <b>Xtream UI / ZapX / StreamCreed / Xtream Codes v2</b> dump as the "
            "<code>{db}</code> database on the XUI.one server. Then open the XUI panel → "
            "<i>Database Migration</i> → <b>Migrate Database</b>.", db=db))
        intro.setWordWrap(True)
        form.addRow(intro)
        self.x_mode = QComboBox()
        self.x_mode.addItem(_("SSH — run mysql on the server (recommended)"), "ssh")
        self.x_mode.addItem(_("Direct MySQL connection"), "mysql")
        self.x_mode.setToolTip(_("XUI's MySQL normally refuses remote logins (error 1130); "
                                 "SSH mode needs no MySQL change on the server."))
        form.addRow(_("Connect via:"), self.x_mode)

        self.x_ssh_host = QLineEdit()
        self.x_ssh_host.setPlaceholderText(_("XUI server IP, e.g. 192.168.1.106"))
        self.x_ssh_port = QSpinBox()
        self.x_ssh_port.setRange(1, 65535)
        self.x_ssh_port.setValue(22)
        self.x_ssh_user = QLineEdit("root")
        self.x_ssh_pass = QLineEdit()
        self.x_ssh_pass.setEchoMode(QLineEdit.Password)
        self.x_ssh_pass.setPlaceholderText(_("blank = SSH key / agent"))
        self.x_ssh_key = QLineEdit()
        self.x_ssh_key.setPlaceholderText(_("optional private key file"))
        form.addRow(QLabel("<b>SSH</b>"))
        form.addRow(_("SSH host:"), self.x_ssh_host)
        form.addRow(_("SSH port:"), self.x_ssh_port)
        form.addRow(_("SSH user:"), self.x_ssh_user)
        form.addRow(_("SSH password:"), self.x_ssh_pass)
        form.addRow(_("SSH key:"), self.x_ssh_key)
        ssh_rows = [form.rowCount() - i for i in range(1, 7)]

        self.x_host = QLineEdit("127.0.0.1")
        self.x_port = QSpinBox()
        self.x_port.setRange(1, 65535)
        self.x_port.setValue(3306)
        self.x_user = QLineEdit()
        self.x_pass = QLineEdit()
        self.x_pass.setEchoMode(QLineEdit.Password)
        form.addRow(QLabel(_("<b>MySQL</b> (in SSH mode: blank user = root via socket; or "
                             "XUI's user from config.ini)")))
        form.addRow(_("Host:"), self.x_host)
        form.addRow(_("Port:"), self.x_port)
        mysql_rows = [form.rowCount() - 2, form.rowCount() - 1]
        form.addRow(_("User:"), self.x_user)
        form.addRow(_("Password:"), self.x_pass)

        def on_mode(_idx: int) -> None:
            ssh = self.x_mode.currentData() == "ssh"
            for r in ssh_rows:
                form.setRowVisible(r, ssh)
            for r in mysql_rows:
                form.setRowVisible(r, not ssh)
            self.x_user.setPlaceholderText(_("blank = root via socket") if ssh else "")

        self.x_mode.currentIndexChanged.connect(on_mode)
        on_mode(0)

        form.addRow(QLabel(_("<b>Old panel dump</b>")))
        self.x_dump = QLineEdit()
        browse = QPushButton("…")
        browse.clicked.connect(self._browse_xui_dump)
        row = QHBoxLayout()
        row.addWidget(self.x_dump, 1)
        row.addWidget(browse)
        form.addRow(_("Dump file:"), self._wrap(row))
        self.x_drop = QCheckBox(_("Replace an existing `{db}` database", db=db))
        self.x_drop.setChecked(True)
        self.x_skip_logs = QCheckBox(_("Skip log table rows (XUI does not migrate logs)"))
        self.x_skip_logs.setChecked(True)
        self.x_force = QCheckBox(_("Load even if it does not look like an XC v2-family dump"))
        self.x_grant = QLineEdit()
        self.x_grant.setPlaceholderText(_("optional: XUI MySQL user, e.g. user@localhost"))
        form.addRow("", self.x_drop)
        form.addRow("", self.x_skip_logs)
        form.addRow("", self.x_force)
        form.addRow(_("Grant to:"), self.x_grant)

        check_btn = QPushButton(_("Check dump"))
        check_btn.clicked.connect(self._do_xui_check)
        load_btn = self._role(QPushButton(_("Load into `{db}` on server", db=db)), "primary")
        load_btn.clicked.connect(self._do_xui_stage)
        script_btn = QPushButton(_("Write script to run on the server…"))
        script_btn.clicked.connect(self._do_xui_script)
        form.addRow(check_btn)
        form.addRow(load_btn)
        form.addRow(script_btn)

        form.addRow(QLabel(_("<b>Import an XUI.one backup → this XUI</b> (XUI to XUI, "
                             "over SSH)")))
        content_box = QWidget()
        grid = QGridLayout(content_box)
        grid.setContentsMargins(0, 0, 0, 0)
        self.xi_content: dict[str, QCheckBox] = {}
        for i, (key, group) in enumerate(xui_import.CONTENT_GROUPS.items()):
            box = QCheckBox(_(group.label))
            box.setChecked(True)
            self.xi_content[key] = box
            grid.addWidget(box, i // 2, i % 2)
        pick = QHBoxLayout()
        for text, state in ((_("All"), True), (_("None"), False)):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, s=state: [c.setChecked(s)
                                                         for c in self.xi_content.values()])
            pick.addWidget(b)
        pick.addStretch(1)
        form.addRow(_("Restore:"), content_box)
        form.addRow("", self._wrap(pick))
        self.xi_servers = QComboBox()
        self.xi_servers.addItem(_("Move all streams to this XUI's main server"), "main")
        self.xi_servers.addItem(_("Add the backup's load balancers as extra servers"), "import")
        self.xi_backup = QCheckBox(_("Back up the current `xui` database on the server first"))
        self.xi_backup.setChecked(True)
        self.xi_logs = QCheckBox(_("Also import logs / statistics"))
        self.xi_access = QCheckBox(_("Import access codes (old admin/reseller URLs replace "
                                     "this server's)"))
        self.xi_settings = QCheckBox(_("Import settings"))
        self.xi_srv = QCheckBox(_("Import servers (old IPs; replaces the 'Streams' choice)"))
        self.xi_srv.toggled.connect(lambda on: self.xi_servers.setEnabled(not on))
        self.xi_rescue = QCheckBox(_("Create a rescue admin access code + user at the end"))
        self.xi_rescue.setChecked(True)
        form.addRow(_("Streams:"), self.xi_servers)
        form.addRow("", self.xi_backup)
        form.addRow("", self.xi_logs)
        form.addRow(_("System:"), self.xi_access)
        form.addRow("", self.xi_settings)
        form.addRow("", self.xi_srv)
        form.addRow("", self.xi_rescue)
        preview_btn = QPushButton(_("Preview XUI import"))
        preview_btn.clicked.connect(lambda: self._do_xui_import(plan_only=True))
        import_btn = self._role(QPushButton(_("Import backup into `xui` on server")), "danger")
        import_btn.clicked.connect(lambda: self._do_xui_import(plan_only=False))
        form.addRow(preview_btn)
        form.addRow(import_btn)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # let long text wrap
        scroll.setWidget(w)
        return scroll

    def _browse_xui_dump(self) -> None:
        path, _sel = QFileDialog.getOpenFileName(self, _("Select old panel dump"), "",
                                                 "SQL (*.sql *.gz);;*.*")
        if path:
            self.x_dump.setText(path)

    def _xui_prepare_job(self):
        """Validate the form; return a function that loads+prepares the dump, or None."""
        if self.runner.busy:
            QMessageBox.information(self, _("Busy"), _("Another operation is still running."))
            return None
        dump = self.x_dump.text().strip()
        if not dump:
            QMessageBox.warning(self, _("No dump"), _("Select the old panel's .sql dump."))
            return None
        skip_logs, force = self.x_skip_logs.isChecked(), self.x_force.isChecked()

        def prepare(message):
            message(_("Analysing {file}…", file=Path(dump).name))
            prepared = xui_migrate.load_dump(dump, skip_logs=skip_logs, force=force)
            for line in prepared.summary_lines():
                message(f"  {line}")
            return prepared

        return prepare

    def _do_xui_check(self) -> None:
        prepare = self._xui_prepare_job()
        if prepare:
            self._start(lambda progress, message: prepare(message),
                        done_msg=_("Dump looks OK."))

    def _do_xui_script(self) -> None:
        prepare = self._xui_prepare_job()
        if not prepare:
            return
        out, _sel = QFileDialog.getSaveFileName(self, _("Save xui_migrate script"),
                                                "xui_migrate.sql", SQL_FILTER)
        if not out:
            return
        drop, grant = self.x_drop.isChecked(), self.x_grant.text().strip() or None

        def job(progress, message):
            path = xui_migrate.write_script(prepare(message), out,
                                            drop_existing=drop, grant_to=grant)
            message(_("Copy {file} to the XUI server and run: mysql -u root -p < {file}",
                      file=path.name))
            return path

        self._start(job, done_msg=_("Script written."))

    def _do_xui_stage(self) -> None:
        prepare = self._xui_prepare_job()
        if not prepare:
            return
        use_ssh = self.x_mode.currentData() == "ssh"
        mysql_user, mysql_pass = self.x_user.text().strip(), self.x_pass.text()
        if use_ssh:
            ssh_cfg = SshConfig(
                host=self.x_ssh_host.text().strip(), port=self.x_ssh_port.value(),
                user=self.x_ssh_user.text().strip(), password=self.x_ssh_pass.text(),
                key_file=self.x_ssh_key.text().strip(),
            )
            host = ssh_cfg.host
        else:
            cfg = ConnectionConfig(
                host=self.x_host.text().strip(), port=self.x_port.value(),
                user=mysql_user or "root", password=mysql_pass,
            )
            host = cfg.host
        if not host:
            QMessageBox.warning(self, _("No server"), _("Enter the XUI server host."))
            return
        drop, grant = self.x_drop.isChecked(), self.x_grant.text().strip() or None
        db = xui_migrate.MIGRATE_DB
        warn = "\n" + _("The existing `{db}` will be DROPPED.", db=db) if drop else ""
        if QMessageBox.question(
            self, _("Confirm"),
            _("Load '{file}' into `{db}` on {host}?", file=Path(self.x_dump.text()).name,
              db=db, host=host) + warn + "\n"
            + _("Only this database is written; the live `xui` database is not touched."),
        ) != QMessageBox.Yes:
            return

        def job(progress, message):
            prepared = prepare(message)
            message(_("Loading into `{db}` on {host}…", db=db,
                      host=f"{host} (SSH)" if use_ssh else host))
            if use_ssh:
                result = xui_migrate.stage_via_ssh(
                    ssh_cfg, prepared, mysql_user=mysql_user, mysql_password=mysql_pass,
                    drop_existing=drop, grant_to=grant, progress=progress, message=message,
                )
            else:
                result = xui_migrate.stage_live(cfg, prepared, drop_existing=drop,
                                                grant_to=grant, progress=progress)
            message(_("Loaded {tables} tables ({count} statements).", tables=result["tables"],
                      count=result["statements"]))
            for table, n in result["row_counts"].items():
                message("  " + _("{table}: {count} rows", table=table, count=n))
            if not result["xui_server"]:
                message(_("Note: no `xui` database on this server — is it the XUI.one host?"))
            message(_("Next: XUI panel → Database Migration → Migrate Database."))
            return result

        self._start(job, done_msg=_("`{db}` is ready for XUI's migration page.", db=db))

    def _do_xui_import(self, plan_only: bool) -> None:
        if self.runner.busy:
            QMessageBox.information(self, _("Busy"), _("Another operation is still running."))
            return
        dump = self.x_dump.text().strip()
        if not dump:
            QMessageBox.warning(self, _("No dump"), _("Select the XUI.one backup (.sql)."))
            return
        ssh_cfg = SshConfig(
            host=self.x_ssh_host.text().strip(), port=self.x_ssh_port.value(),
            user=self.x_ssh_user.text().strip(), password=self.x_ssh_pass.text(),
            key_file=self.x_ssh_key.text().strip(),
        )
        if self.x_mode.currentData() != "ssh" or not ssh_cfg.host:
            QMessageBox.warning(self, _("SSH needed"),
                                _("XUI import runs over SSH: choose SSH and enter the SSH "
                                  "host."))
            return
        backup = self.xi_backup.isChecked()
        chosen = [t for t, box in (("access_codes", self.xi_access),
                                   ("settings", self.xi_settings),
                                   ("servers", self.xi_srv)) if box.isChecked()]
        rescue = self.xi_rescue.isChecked()
        content = [k for k, box in self.xi_content.items() if box.isChecked()]
        if not content and not chosen:
            QMessageBox.warning(self, _("Nothing chosen"),
                                _("Tick at least one kind of content."))
            return
        if not plan_only:
            warn = "" if backup else "\n\n" + _("No backup of the current `xui` will be taken!")
            if len(content) < len(self.xi_content):
                labels = ", ".join(_(xui_import.CONTENT_GROUPS[k].label) for k in content)
                warn += "\n\n" + _("Only this content is replaced: {labels}.",
                                   labels=labels or _("(none)"))
            if chosen:
                warn += "\n\n" + _("Replaced with the backup's: {tables}.",
                                   tables=", ".join(chosen))
            if "access_codes" in chosen and not rescue:
                warn += "\n" + _("The current admin URL will stop working and no rescue is "
                                 "created!")
            if QMessageBox.question(
                self, _("Confirm XUI import"),
                _("Replace the data of the live `xui` database on {host} with '{file}'?",
                  host=ssh_cfg.host, file=Path(dump).name) + warn,
            ) != QMessageBox.Yes:
                return
        opts = dict(
            mysql_user=self.x_user.text().strip(), mysql_password=self.x_pass.text(),
            import_tables=chosen, content=content, include_logs=self.xi_logs.isChecked(),
            server_mode=self.xi_servers.currentData(), backup=backup,
            create_rescue=rescue, plan_only=plan_only,
        )

        def job(progress, message):
            message(_("Reading {file} and the target schema…", file=Path(dump).name))
            result = xui_import.import_via_ssh(ssh_cfg, dump, progress=progress,
                                               message=message, **opts)
            if not plan_only:
                for line in xui_import.report_lines(result):
                    message(line)
            return result

        if plan_only:
            self._start(job, done_msg=_("Preview ready (see log)."))
            return

        def done(result):
            self.progress.setValue(100)
            self._log(_("XUI backup imported."))
            box = QMessageBox(self)
            box.setWindowTitle(_("XUI import finished"))
            box.setIcon(QMessageBox.Information)
            box.setText(_("Import finished. Access details:") if result.get("rescue")
                        else _("Import finished."))
            box.setDetailedText("\n".join(xui_import.report_lines(result)))
            rescue_acc = result.get("rescue")
            if rescue_acc:
                box.setInformativeText(_(
                    "URL: {url}\nUser: {user}\nPassword: {password}\n\nSave these now — the "
                    "password is not stored anywhere.",
                    url=f"{result.get('panel_base', '')}/{rescue_acc.code}/",
                    user=rescue_acc.username, password=rescue_acc.password))
            box.setTextInteractionFlags(Qt.TextSelectableByMouse)
            box.exec()

        self._start_custom(job, done)

    def _build_progress_box(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setFixedHeight(160)
        v.addWidget(self.progress)
        v.addWidget(self.log)
        return box

    @staticmethod
    def _wrap(layout) -> QWidget:
        c = QWidget()
        c.setLayout(layout)
        return c

    # -------------------------------------------------------------- helpers
    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _on_progress(self, label: str, done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 0
        self.progress.setValue(pct)
        self.progress.setFormat(f"{label}: {done}/{total} (%p%)")

    def _selected_tables(self) -> list[str]:
        out = []
        for r in range(self.table_widget.rowCount()):
            item = self.table_widget.item(r, 0)
            if item and item.checkState() == Qt.Checked:
                out.append(item.text())
        return out

    def _set_all_checks(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for r in range(self.table_widget.rowCount()):
            if not self.table_widget.isRowHidden(r):
                self.table_widget.item(r, 0).setCheckState(state)

    def _apply_filter(self, text: str) -> None:
        text = text.lower()
        for r in range(self.table_widget.rowCount()):
            name = self.table_widget.item(r, 0).text().lower()
            self.table_widget.setRowHidden(r, text not in name)

    def _busy_guard(self) -> bool:
        if self.runner.busy:
            QMessageBox.information(self, _("Busy"), _("Another operation is still running."))
            return True
        if self.source is None:
            QMessageBox.warning(self, _("No source"), _("Connect or load a source first."))
            return True
        return False

    # ---------------------------------------------------------- profiles
    def _load_profiles(self) -> None:
        self.store = load_store()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem(_("<new>"))
        self.profile_combo.addItems([p.name for p in self.store.profiles])
        self.profile_combo.blockSignals(False)

    def _apply_profile(self, index: int) -> None:
        if index <= 0:  # "<new>"
            return
        name = self.profile_combo.itemText(index)
        p = next((x for x in self.store.profiles if x.name == name), None)
        if not p:
            return
        self.in_host.setText(p.host)
        self.in_port.setValue(p.port)
        self.in_user.setText(p.user)
        self.in_db.setText(p.database)
        self.in_pass.setText(p.password)
        self.save_pass.setChecked(p.save_password)
        s = self.src_ssh
        s.host.setText(p.ssh_host)
        s.port.setValue(p.ssh_port)
        s.user.setText(p.ssh_user)
        s.key.setText(p.ssh_key)
        s.password.setText(p.ssh_password)
        s.set_mode(("socket" if p.ssh_socket else "ssh") if p.use_ssh else "direct")

    def _save_profile(self) -> None:
        s = self.src_ssh
        ssh = s.enabled
        where = f"{s.host.text()} (ssh)" if ssh else self.in_host.text()
        name = f"{self.in_user.text()}@{where}/{self.in_db.text()}"
        prof = ConnectionProfile(
            name=name, host=self.in_host.text(), port=self.in_port.value(),
            user=self.in_user.text(), database=self.in_db.text(),
            password=self.in_pass.text(), save_password=self.save_pass.isChecked(),
            use_ssh=ssh, ssh_socket=s.mode == "socket", ssh_host=s.host.text().strip(),
            ssh_port=s.port.value(),
            ssh_user=s.user.text().strip(), ssh_key=s.key.text().strip(),
            ssh_password=s.password.text(),
        )
        self.store.upsert(prof)
        save_store(self.store)
        self._load_profiles()
        self.profile_combo.setCurrentText(name)
        self._log(_("Saved profile: {name}", name=name))

    def _delete_profile(self) -> None:
        if self.profile_combo.currentIndex() <= 0:
            return
        name = self.profile_combo.currentText()
        self.store.remove(name)
        save_store(self.store)
        self._load_profiles()
        self._log(_("Deleted profile: {name}", name=name))

    # ---------------------------------------------------------- source
    def _browse_dump(self) -> None:
        path, _sel = QFileDialog.getOpenFileName(self, _("Select SQL dump"), "", SQL_FILTER)
        if path:
            self.in_dump.setText(path)

    def _browse_export_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, _("Select output folder"))
        if path:
            self.export_dir.setText(path)

    def _connect_source(self) -> None:
        if self.source is not None:
            try:
                self.source.close()
            except Exception:  # noqa: BLE001
                pass
            self.source = None
        try:
            if self.source_kind.currentIndex() == 0:
                cfg = ConnectionConfig(
                    host=self.in_host.text().strip() or "127.0.0.1", port=self.in_port.value(),
                    user=self.in_user.text().strip(), password=self.in_pass.text(),
                    database=self.in_db.text().strip(), ssh=self.src_ssh.config(),
                )
                src: DataSource = MySQLSource(cfg)
            else:
                src = SqlDumpSource(self.in_dump.text().strip())
            src.open()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, _("Connection failed"), str(exc))
            self._log(_("ERROR: {error}", error=exc))
            self._set_status(_("Not connected."), "error")
            return
        self.source = src
        self._log(_("Connected: {label}", label=src.label))
        self._populate_tables()

    def _populate_tables(self) -> None:
        assert self.source is not None
        tables = self.source.list_tables()
        det = profiles.detect_panel(tables)
        cats = profiles.categorise_tables(tables)
        cat_of = {t: c for c, ts in cats.items() for t in ts}

        info = self.source.server_info()
        extra = info.get("server_version", info.get("source", ""))
        self._set_status(_(
            "Panel: {panel} ({confidence:.0%} conf.)  •  {count} tables  •  {extra}",
            panel=det.profile.name, confidence=det.confidence, count=len(tables), extra=extra),
            "ok")

        self.table_widget.setRowCount(len(tables))
        for r, t in enumerate(tables):
            try:
                rc = self.source.row_count(t)
                nc = len(self.source.table_info(t).columns)
            except Exception:  # noqa: BLE001
                rc, nc = -1, -1
            item = QTableWidgetItem(t)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.table_widget.setItem(r, 0, item)
            self.table_widget.setItem(r, 1, QTableWidgetItem(cat_of.get(t, "other")))
            self.table_widget.setItem(r, 2, QTableWidgetItem(str(rc) if rc >= 0 else "?"))
            self.table_widget.setItem(r, 3, QTableWidgetItem(str(nc) if nc >= 0 else "?"))
        self._log(_("Detected {panel}; loaded {count} tables.", panel=det.profile.name,
                    count=len(tables)))

    # ---------------------------------------------------------- actions
    def _do_export(self) -> None:
        if self._busy_guard():
            return
        tables = self._selected_tables()
        if not tables:
            QMessageBox.warning(self, _("No tables"), _("Select at least one table."))
            return
        redact = REDACT_HINTS if self.redact_cb.isChecked() else frozenset()
        opts = ExportOptions(
            fmt=self.export_fmt.currentText(),
            out_dir=Path(self.export_dir.text()),
            redact_columns=redact,
            include_schema=self.schema_cb.isChecked(),
        )
        src = self.source

        def job(progress, message):
            message(_("Exporting {count} table(s) as {fmt} → {folder}", count=len(tables),
                      fmt=opts.fmt, folder=opts.out_dir))
            return export_tables(src, tables, opts, progress)

        self._start(job, done_msg=_("Export complete."))

    def _do_maintenance(self) -> None:
        if self._busy_guard():
            return
        tables = self._selected_tables()
        if not tables:
            QMessageBox.warning(self, _("No tables"), _("Select at least one table."))
            return
        op, label = self.maint_op.currentData(), self.maint_op.currentText()
        src = self.source

        def job(progress, message):
            message(_("Running {operation} on {count} table(s)…", operation=label,
                      count=len(tables)))
            results = run_maintenance(src, tables, op, progress)
            for r in results:
                message(f"  {r.table}: {r.status} — {r.message}")
            return results

        self._start(job, done_msg=_("{operation} finished.", operation=label))

    def _do_auto_repair(self) -> None:
        if self._busy_guard():
            return
        tables = self._selected_tables()
        if not tables:
            QMessageBox.warning(self, _("No tables"), _("Select at least one table."))
            return
        if QMessageBox.question(
            self, _("Confirm repair"),
            _("REPAIR will rewrite affected tables. Have you exported a backup first?"),
        ) != QMessageBox.Yes:
            return
        src = self.source

        def job(progress, message):
            message(_("Checking tables…"))
            results = check_and_repair(src, tables, progress)
            for r in results:
                message(f"  [{r.operation}] {r.table}: {r.status} — {r.message}")
            return results

        self._start(job, done_msg=_("Check & repair finished."))

    def _start(self, job, done_msg: str) -> None:
        self.progress.setValue(0)

        def on_finished(_res):
            self.progress.setValue(100)
            self._log(done_msg)

        def on_failed(err):
            self._log(_("ERROR: {error}", error=err))
            QMessageBox.critical(self, _("Operation failed"), err)

        self.runner.start(
            job,
            on_progress=self._on_progress,
            on_message=self._log,
            on_finished=on_finished,
            on_failed=on_failed,
        )

    def _about(self) -> None:
        AboutDialog(self).exec()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.runner.busy:
            self.runner.wait(5000)  # let a running job stop cleanly before teardown
        if self.source is not None:
            try:
                self.source.close()
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(event)
