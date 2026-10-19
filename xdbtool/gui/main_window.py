# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Main application window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
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
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import profiles, templates, xui_import, xui_migrate
from ..core.config import ConnectionProfile, load_store, save_store
from ..core.dump_source import SqlDumpSource
from ..core.exporter import ExportOptions, export_tables
from ..core.migrator import MigrationOptions, build_plan, execute_plan
from ..core.mysql_source import ConnectionConfig, MySQLSource
from ..core.repair import check_and_repair, run_maintenance
from ..core.restore import restore_dump
from ..core.source import DataSource
from ..core.ssh_remote import SshConfig
from ..core.templates import create_from_template, register_template
from .workers import JobRunner

REDACT_HINTS = frozenset({"password", "pass", "pwd", "secret", "token", "api_key", "apikey"})


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("XUI DB Tool")
        self.resize(1040, 720)
        self.source: DataSource | None = None
        self.runner = JobRunner(self)
        self._build_ui()
        self._load_profiles()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.addWidget(self._build_source_box())
        body = QHBoxLayout()
        body.addWidget(self._build_tables_box(), 3)
        body.addWidget(self._build_actions_box(), 2)
        root.addLayout(body, 1)
        root.addWidget(self._build_progress_box())
        self.setCentralWidget(central)

        about = QAction("About", self)
        about.triggered.connect(self._about)
        self.menuBar().addMenu("Help").addAction(about)

    def _build_source_box(self) -> QWidget:
        box = QGroupBox("Data source")
        outer = QVBoxLayout(box)

        picker = QHBoxLayout()
        self.source_kind = QComboBox()
        self.source_kind.addItems(["Live MySQL / MariaDB", "SQL dump file (.sql)"])
        self.source_kind.currentIndexChanged.connect(lambda i: self.source_stack.setCurrentIndex(i))
        picker.addWidget(QLabel("Source type:"))
        picker.addWidget(self.source_kind)
        picker.addStretch(1)
        outer.addLayout(picker)

        self.source_stack = QStackedWidget()
        self.source_stack.addWidget(self._build_mysql_form())
        self.source_stack.addWidget(self._build_dump_form())
        outer.addWidget(self.source_stack)

        row = QHBoxLayout()
        self.connect_btn = QPushButton("Connect / Load")
        self.connect_btn.clicked.connect(self._connect_source)
        self.status_lbl = QLabel("Not connected.")
        self.status_lbl.setStyleSheet("color: #888;")
        row.addWidget(self.connect_btn)
        row.addWidget(self.status_lbl, 1)
        outer.addLayout(row)
        return box

    def _build_mysql_form(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.profile_combo = QComboBox()
        self.profile_combo.currentTextChanged.connect(self._apply_profile)
        self.in_host = QLineEdit("127.0.0.1")
        self.in_port = QSpinBox()
        self.in_port.setRange(1, 65535)
        self.in_port.setValue(3306)
        self.in_user = QLineEdit("root")
        self.in_pass = QLineEdit()
        self.in_pass.setEchoMode(QLineEdit.Password)
        self.in_db = QLineEdit()
        self.save_pass = QCheckBox("Save password in profile")

        prof_row = QHBoxLayout()
        prof_row.addWidget(self.profile_combo, 1)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save_profile)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._delete_profile)
        prof_row.addWidget(save_btn)
        prof_row.addWidget(del_btn)

        form.addRow("Profile:", self._wrap(prof_row))
        form.addRow("Host:", self.in_host)
        form.addRow("Port:", self.in_port)
        form.addRow("User:", self.in_user)
        form.addRow("Password:", self.in_pass)
        form.addRow("Database:", self.in_db)
        form.addRow("", self.save_pass)
        return w

    def _build_dump_form(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        row = QHBoxLayout()
        self.in_dump = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_dump)
        row.addWidget(self.in_dump, 1)
        row.addWidget(browse)
        form.addRow("Dump file:", self._wrap(row))
        form.addRow(QLabel("The whole dump is parsed into memory. For very large\n"
                           "dumps, use a live MySQL connection instead."))
        return w

    def _build_tables_box(self) -> QWidget:
        box = QGroupBox("Tables")
        v = QVBoxLayout(box)
        top = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter tables…")
        self.filter_edit.textChanged.connect(self._apply_filter)
        all_btn = QPushButton("All")
        all_btn.clicked.connect(lambda: self._set_all_checks(True))
        none_btn = QPushButton("None")
        none_btn.clicked.connect(lambda: self._set_all_checks(False))
        top.addWidget(self.filter_edit, 1)
        top.addWidget(all_btn)
        top.addWidget(none_btn)
        v.addLayout(top)

        self.table_widget = QTableWidget(0, 4)
        self.table_widget.setHorizontalHeaderLabels(["Table", "Category", "Rows", "Columns"])
        self.table_widget.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table_widget.verticalHeader().setVisible(False)
        self.table_widget.setEditTriggers(QTableWidget.NoEditTriggers)
        v.addWidget(self.table_widget)
        return box

    def _build_actions_box(self) -> QWidget:
        tabs = QTabWidget()
        tabs.addTab(self._build_export_tab(), "Export / Backup")
        tabs.addTab(self._build_maint_tab(), "Maintenance")
        tabs.addTab(self._build_templates_tab(), "Templates / New DB")
        tabs.addTab(self._build_migrate_tab(), "Migrate / Restore")
        tabs.addTab(self._build_xui_migrate_tab(), "XUI Migration")
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
        self.redact_cb = QCheckBox("Redact password/secret-like columns")
        self.schema_cb = QCheckBox("Include CREATE TABLE (SQL format, live source)")
        self.schema_cb.setChecked(True)
        self.export_btn = QPushButton("Export selected tables")
        self.export_btn.clicked.connect(self._do_export)

        form.addRow("Format:", self.export_fmt)
        form.addRow("Output folder:", self._wrap(dir_row))
        form.addRow("", self.redact_cb)
        form.addRow("", self.schema_cb)
        form.addRow(self.export_btn)
        return w

    def _build_maint_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.maint_op = QComboBox()
        self.maint_op.addItems(["check", "repair", "analyze", "optimize"])
        run_btn = QPushButton("Run on selected tables")
        run_btn.clicked.connect(self._do_maintenance)
        auto_btn = QPushButton("Check & auto-repair broken tables")
        auto_btn.clicked.connect(self._do_auto_repair)
        note = QLabel("Maintenance requires a live MySQL/MariaDB connection.\n"
                      "Always back up (Export → SQL) before repairing.")
        note.setWordWrap(True)
        form.addRow("Operation:", self.maint_op)
        form.addRow(run_btn)
        form.addRow(auto_btn)
        form.addRow(note)
        return w

    def _build_templates_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.tmpl_combo = QComboBox()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_templates)
        reg = QPushButton("Register base .sql…")
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
        self.t_drop = QCheckBox("DROP database first if it already exists")
        create_btn = QPushButton("Create database from template")
        create_btn.clicked.connect(self._do_init_db)

        form.addRow("Template:", self._wrap(tmpl_row))
        form.addRow("", reg)
        form.addRow("Target host:", self.t_host)
        form.addRow("Target port:", self.t_port)
        form.addRow("Target user:", self.t_user)
        form.addRow("Target password:", self.t_pass)
        form.addRow("New database:", self.t_db)
        form.addRow("", self.t_drop)
        form.addRow(create_btn)
        self._refresh_templates()
        return w

    def _refresh_templates(self) -> None:
        self.tmpl_combo.clear()
        for m in templates.list_templates():
            present = "" if m.sql_path().is_file() else "  [.sql missing]"
            self.tmpl_combo.addItem(f"{m.key} — {m.name} ({m.table_count} tbl){present}", m.key)
        if self.tmpl_combo.count() == 0:
            self.tmpl_combo.addItem("<no templates — register one>", None)

    def _register_template_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select base schema .sql", "", "SQL dumps (*.sql);;All files (*)"
        )
        if not path:
            return
        panel, ok = QInputDialog.getText(self, "Panel key", "Panel key (e.g. xui_one):")
        if not ok or not panel:
            return
        version, _ = QInputDialog.getText(self, "Version", "Version (optional, e.g. 1.5.13):")
        default_name = f"{panel} {version}".strip()
        name, _ = QInputDialog.getText(self, "Name", "Display name:", text=default_name)

        def job(progress, message):
            message(f"Parsing and registering {Path(path).name}…")
            return register_template(
                path, name=name or panel, panel_key=panel, version=version or ""
            )

        def done(meta):
            self._log(f"Registered template '{meta.key}' ({meta.table_count} tables).")
            self._refresh_templates()

        self._start_custom(job, done)

    def _do_init_db(self) -> None:
        if self.runner.busy:
            QMessageBox.information(self, "Busy", "Another operation is still running.")
            return
        key = self.tmpl_combo.currentData()
        if not key:
            QMessageBox.warning(self, "No template", "Register and select a template first.")
            return
        db_name = self.t_db.text().strip()
        if not db_name:
            QMessageBox.warning(self, "No name", "Enter a name for the new database.")
            return
        if self.t_drop.isChecked() and QMessageBox.question(
            self, "Confirm drop",
            f"DROP DATABASE `{db_name}` if it exists, then recreate it?",
        ) != QMessageBox.Yes:
            return
        cfg = ConnectionConfig(
            host=self.t_host.text().strip(), port=self.t_port.value(),
            user=self.t_user.text().strip(), password=self.t_pass.text(),
        )
        drop = self.t_drop.isChecked()

        def job(progress, message):
            message(f"Creating `{db_name}` from template '{key}'…")
            return create_from_template(cfg, key, db_name, drop_existing=drop, progress=progress)

        self._start(job, done_msg=f"Database `{db_name}` created from template.")

    def _start_custom(self, job, on_finished) -> None:
        self.progress.setValue(0)

        def failed(err):
            self._log(f"ERROR: {err}")
            QMessageBox.critical(self, "Operation failed", err)

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
        form.addRow(QLabel("<b>Target server</b> (data is written here)"))
        form.addRow("Host:", self.m_host)
        form.addRow("Port:", self.m_port)
        form.addRow("User:", self.m_user)
        form.addRow("Password:", self.m_pass)
        form.addRow("Database:", self.m_db)

        # -- migrate loaded source --
        form.addRow(QLabel("<b>Migrate loaded source → target</b>"))
        self.m_truncate = QCheckBox("Empty target tables first (TRUNCATE)")
        self.m_ignore = QCheckBox("Skip duplicate rows (INSERT IGNORE)")
        self.m_ignore.setChecked(True)
        plan_btn = QPushButton("Build plan (preview in log)")
        plan_btn.clicked.connect(self._do_build_plan)
        mig_btn = QPushButton("Run migration → inject into server")
        mig_btn.clicked.connect(lambda: self._do_migrate(dry_run=False))
        sql_btn = QPushButton("Dry run → write migration .sql")
        sql_btn.clicked.connect(lambda: self._do_migrate(dry_run=True))
        form.addRow("", self.m_truncate)
        form.addRow("", self.m_ignore)
        form.addRow(plan_btn)
        form.addRow(mig_btn)
        form.addRow(sql_btn)

        # -- restore a dump --
        form.addRow(QLabel("<b>Restore a .sql dump → target</b>"))
        self.m_dump = QLineEdit()
        dump_browse = QPushButton("…")
        dump_browse.clicked.connect(self._browse_restore_dump)
        dump_row = QHBoxLayout()
        dump_row.addWidget(self.m_dump, 1)
        dump_row.addWidget(dump_browse)
        self.m_create = QCheckBox("Create the target database first")
        self.m_drop = QCheckBox("DROP it first if it exists")
        restore_btn = QPushButton("Restore dump → inject into server")
        restore_btn.clicked.connect(self._do_restore)
        form.addRow("Dump file:", self._wrap(dump_row))
        form.addRow("", self.m_create)
        form.addRow("", self.m_drop)
        form.addRow(restore_btn)
        return w

    def _browse_restore_dump(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select .sql dump to restore", "", "SQL dumps (*.sql);;All files (*)"
        )
        if path:
            self.m_dump.setText(path)

    def _mig_target_cfg(self) -> ConnectionConfig:
        return ConnectionConfig(
            host=self.m_host.text().strip(), port=self.m_port.value(),
            user=self.m_user.text().strip(), password=self.m_pass.text(),
            database=self.m_db.text().strip(),
        )

    def _do_build_plan(self) -> None:
        if self._busy_guard():
            return
        src = self.source
        cfg = self._mig_target_cfg()
        if not cfg.database:
            QMessageBox.warning(self, "No target DB", "Enter the target database name.")
            return

        def job(progress, message):
            with MySQLSource(cfg) as target:
                plan = build_plan(src, target)
            message("Migration plan:")
            for m in plan.mappings:
                message(f"  {m.summary}")
            if plan.missing_in_target:
                message(f"  missing in target: {', '.join(plan.missing_in_target)}")
            return plan

        self._start(job, done_msg="Plan built (see log).")

    def _do_migrate(self, dry_run: bool) -> None:
        if self._busy_guard():
            return
        cfg = self._mig_target_cfg()
        if not cfg.database:
            QMessageBox.warning(self, "No target DB", "Enter the target database name.")
            return
        sql_out = None
        if dry_run:
            sql_out, _ = QFileDialog.getSaveFileName(self, "Save migration SQL", "migration.sql",
                                                     "SQL (*.sql)")
            if not sql_out:
                return
        elif QMessageBox.question(
            self, "Confirm migration",
            f"Write data directly into `{cfg.database}` on {cfg.host}?\n"
            "This modifies the live target. Make sure you have a backup.",
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
                message(f"Transferring {len(plan.transferable)} table(s)…")
                written = execute_plan(src, target, plan, opts, progress)
            total = sum(written.values())
            message(f"Done: {total} rows across {len(written)} table(s).")
            return written

        self._start(job, done_msg="Migration finished.")

    def _do_restore(self) -> None:
        if self.runner.busy:
            QMessageBox.information(self, "Busy", "Another operation is still running.")
            return
        dump = self.m_dump.text().strip()
        if not dump:
            QMessageBox.warning(self, "No dump", "Select a .sql dump to restore.")
            return
        cfg = self._mig_target_cfg()
        db = cfg.database or None
        if self.m_create.isChecked() and not db:
            QMessageBox.warning(self, "No target DB", "Enter the database name to create.")
            return
        dest = db or "(dump's own database)"
        if QMessageBox.question(
            self, "Confirm restore",
            f"Execute '{Path(dump).name}' directly into {dest} on {cfg.host}?\n"
            "This runs the dump as-is on the live server.",
        ) != QMessageBox.Yes:
            return
        create = self.m_create.isChecked()
        drop = self.m_drop.isChecked()

        def job(progress, message):
            message(f"Restoring {Path(dump).name}…")
            return restore_dump(cfg, dump, database=db, create=create,
                                drop_existing=drop, progress=progress)

        self._start(job, done_msg="Restore finished.")

    def _build_xui_migrate_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        intro = QLabel(
            "Stage an old <b>Xtream UI / ZapX / StreamCreed / Xtream Codes v2</b> dump as the "
            f"<code>{xui_migrate.MIGRATE_DB}</code> database on the XUI.one server. Then open the "
            "XUI panel → <i>Database Migration</i> → <b>Migrate Database</b>."
        )
        intro.setWordWrap(True)
        form.addRow(intro)
        self.x_mode = QComboBox()
        self.x_mode.addItem("SSH — run mysql on the server (recommended)", "ssh")
        self.x_mode.addItem("Direct MySQL connection", "mysql")
        self.x_mode.setToolTip("XUI's MySQL normally refuses remote logins (error 1130); "
                               "SSH mode needs no MySQL change on the server.")
        form.addRow("Connect via:", self.x_mode)

        self.x_ssh_host = QLineEdit()
        self.x_ssh_host.setPlaceholderText("XUI server IP, e.g. 192.168.1.106")
        self.x_ssh_port = QSpinBox()
        self.x_ssh_port.setRange(1, 65535)
        self.x_ssh_port.setValue(22)
        self.x_ssh_user = QLineEdit("root")
        self.x_ssh_pass = QLineEdit()
        self.x_ssh_pass.setEchoMode(QLineEdit.Password)
        self.x_ssh_pass.setPlaceholderText("blank = SSH key / agent")
        self.x_ssh_key = QLineEdit()
        self.x_ssh_key.setPlaceholderText("optional private key file")
        form.addRow(QLabel("<b>SSH</b>"))
        form.addRow("SSH host:", self.x_ssh_host)
        form.addRow("SSH port:", self.x_ssh_port)
        form.addRow("SSH user:", self.x_ssh_user)
        form.addRow("SSH password:", self.x_ssh_pass)
        form.addRow("SSH key:", self.x_ssh_key)
        ssh_rows = [form.rowCount() - i for i in range(1, 7)]

        self.x_host = QLineEdit("127.0.0.1")
        self.x_port = QSpinBox()
        self.x_port.setRange(1, 65535)
        self.x_port.setValue(3306)
        self.x_user = QLineEdit()
        self.x_pass = QLineEdit()
        self.x_pass.setEchoMode(QLineEdit.Password)
        form.addRow(QLabel("<b>MySQL</b> (in SSH mode: blank user = root via socket; "
                           "or XUI's user from config.ini)"))
        form.addRow("Host:", self.x_host)
        form.addRow("Port:", self.x_port)
        mysql_rows = [form.rowCount() - 2, form.rowCount() - 1]
        form.addRow("User:", self.x_user)
        form.addRow("Password:", self.x_pass)

        def on_mode(_idx: int) -> None:
            ssh = self.x_mode.currentData() == "ssh"
            for r in ssh_rows:
                form.setRowVisible(r, ssh)
            for r in mysql_rows:
                form.setRowVisible(r, not ssh)
            self.x_user.setPlaceholderText("blank = root via socket" if ssh else "")

        self.x_mode.currentIndexChanged.connect(on_mode)
        on_mode(0)

        form.addRow(QLabel("<b>Old panel dump</b>"))
        self.x_dump = QLineEdit()
        browse = QPushButton("…")
        browse.clicked.connect(self._browse_xui_dump)
        row = QHBoxLayout()
        row.addWidget(self.x_dump, 1)
        row.addWidget(browse)
        form.addRow("Dump file:", self._wrap(row))
        self.x_drop = QCheckBox(f"Replace an existing `{xui_migrate.MIGRATE_DB}` database")
        self.x_drop.setChecked(True)
        self.x_skip_logs = QCheckBox("Skip log table rows (XUI does not migrate logs)")
        self.x_skip_logs.setChecked(True)
        self.x_force = QCheckBox("Load even if it does not look like an XC v2-family dump")
        self.x_grant = QLineEdit()
        self.x_grant.setPlaceholderText("optional: XUI MySQL user, e.g. user@localhost")
        form.addRow("", self.x_drop)
        form.addRow("", self.x_skip_logs)
        form.addRow("", self.x_force)
        form.addRow("Grant to:", self.x_grant)

        check_btn = QPushButton("Check dump")
        check_btn.clicked.connect(self._do_xui_check)
        load_btn = QPushButton(f"Load into `{xui_migrate.MIGRATE_DB}` on server")
        load_btn.clicked.connect(self._do_xui_stage)
        script_btn = QPushButton("Write script to run on the server…")
        script_btn.clicked.connect(self._do_xui_script)
        form.addRow(check_btn)
        form.addRow(load_btn)
        form.addRow(script_btn)

        form.addRow(QLabel("<b>Import an XUI.one backup → this XUI</b> (XUI to XUI, over SSH)"))
        self.xi_servers = QComboBox()
        self.xi_servers.addItem("Move all streams to this XUI's main server", "main")
        self.xi_servers.addItem("Add the backup's load balancers as extra servers", "import")
        self.xi_backup = QCheckBox("Back up the current `xui` database on the server first")
        self.xi_backup.setChecked(True)
        self.xi_logs = QCheckBox("Also import logs / statistics")
        self.xi_system = QCheckBox("Also replace servers / settings / access codes "
                                   "(changes the admin URL!)")
        form.addRow("Streams:", self.xi_servers)
        form.addRow("", self.xi_backup)
        form.addRow("", self.xi_logs)
        form.addRow("", self.xi_system)
        preview_btn = QPushButton("Preview XUI import")
        preview_btn.clicked.connect(lambda: self._do_xui_import(plan_only=True))
        import_btn = QPushButton("Import backup into `xui` on server")
        import_btn.clicked.connect(lambda: self._do_xui_import(plan_only=False))
        form.addRow(preview_btn)
        form.addRow(import_btn)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        return scroll

    def _browse_xui_dump(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select old panel dump", "", "SQL dumps (*.sql *.gz);;All files (*)"
        )
        if path:
            self.x_dump.setText(path)

    def _xui_prepare_job(self):
        """Validate the form; return a function that loads+prepares the dump, or None."""
        if self.runner.busy:
            QMessageBox.information(self, "Busy", "Another operation is still running.")
            return None
        dump = self.x_dump.text().strip()
        if not dump:
            QMessageBox.warning(self, "No dump", "Select the old panel's .sql dump.")
            return None
        skip_logs, force = self.x_skip_logs.isChecked(), self.x_force.isChecked()

        def prepare(message):
            message(f"Analysing {Path(dump).name}…")
            prepared = xui_migrate.load_dump(dump, skip_logs=skip_logs, force=force)
            for line in prepared.summary_lines():
                message(f"  {line}")
            return prepared

        return prepare

    def _do_xui_check(self) -> None:
        prepare = self._xui_prepare_job()
        if prepare:
            self._start(lambda progress, message: prepare(message), done_msg="Dump looks OK.")

    def _do_xui_script(self) -> None:
        prepare = self._xui_prepare_job()
        if not prepare:
            return
        out, _ = QFileDialog.getSaveFileName(self, "Save xui_migrate script",
                                             "xui_migrate.sql", "SQL (*.sql)")
        if not out:
            return
        drop, grant = self.x_drop.isChecked(), self.x_grant.text().strip() or None

        def job(progress, message):
            path = xui_migrate.write_script(prepare(message), out,
                                            drop_existing=drop, grant_to=grant)
            message(f"Copy {path.name} to the XUI server and run: "
                    f"mysql -u root -p < {path.name}")
            return path

        self._start(job, done_msg="Script written.")

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
            QMessageBox.warning(self, "No server", "Enter the XUI server host.")
            return
        drop, grant = self.x_drop.isChecked(), self.x_grant.text().strip() or None
        db = xui_migrate.MIGRATE_DB
        warn = f"\nThe existing `{db}` will be DROPPED." if drop else ""
        if QMessageBox.question(
            self, "Confirm",
            f"Load '{Path(self.x_dump.text()).name}' into `{db}` on {host}?{warn}\n"
            "Only this database is written; the live `xui` database is not touched.",
        ) != QMessageBox.Yes:
            return

        def job(progress, message):
            prepared = prepare(message)
            message(f"Loading into `{db}` on {host}{' over SSH' if use_ssh else ''}…")
            if use_ssh:
                result = xui_migrate.stage_via_ssh(
                    ssh_cfg, prepared, mysql_user=mysql_user, mysql_password=mysql_pass,
                    drop_existing=drop, grant_to=grant, progress=progress, message=message,
                )
            else:
                result = xui_migrate.stage_live(cfg, prepared, drop_existing=drop,
                                                grant_to=grant, progress=progress)
            message(f"Loaded {result['tables']} tables ({result['statements']} statements).")
            for table, n in result["row_counts"].items():
                message(f"  {table}: {n} rows")
            if not result["xui_server"]:
                message("Note: no `xui` database on this server — is it the XUI.one host?")
            message("Next: XUI panel → Database Migration → Migrate Database.")
            return result

        self._start(job, done_msg=f"`{db}` is ready for XUI's migration page.")

    def _do_xui_import(self, plan_only: bool) -> None:
        if self.runner.busy:
            QMessageBox.information(self, "Busy", "Another operation is still running.")
            return
        dump = self.x_dump.text().strip()
        if not dump:
            QMessageBox.warning(self, "No dump", "Select the XUI.one backup (.sql).")
            return
        ssh_cfg = SshConfig(
            host=self.x_ssh_host.text().strip(), port=self.x_ssh_port.value(),
            user=self.x_ssh_user.text().strip(), password=self.x_ssh_pass.text(),
            key_file=self.x_ssh_key.text().strip(),
        )
        if self.x_mode.currentData() != "ssh" or not ssh_cfg.host:
            QMessageBox.warning(self, "SSH needed",
                                "XUI import runs over SSH: choose SSH and enter the SSH host.")
            return
        backup = self.xi_backup.isChecked()
        system = self.xi_system.isChecked()
        if not plan_only:
            warn = "" if backup else "\n\nNo backup of the current `xui` will be taken!"
            if system:
                warn += ("\n\nServers, settings and access codes will be REPLACED: the admin "
                         "URL and server IPs will be the backup's.")
            if QMessageBox.question(
                self, "Confirm XUI import",
                f"Replace the data of the live `xui` database on {ssh_cfg.host} with "
                f"'{Path(dump).name}'?{warn}",
            ) != QMessageBox.Yes:
                return
        opts = dict(
            mysql_user=self.x_user.text().strip(), mysql_password=self.x_pass.text(),
            include_system=system, include_logs=self.xi_logs.isChecked(),
            server_mode=self.xi_servers.currentData(), backup=backup, plan_only=plan_only,
        )

        def job(progress, message):
            message(f"Reading {Path(dump).name} and the target schema…")
            result = xui_import.import_via_ssh(ssh_cfg, dump, progress=progress,
                                               message=message, **opts)
            if plan_only:
                return result
            if result["backup"]:
                message(f"Previous `xui` saved on the server: {result['backup']}")
            for table, n in result["row_counts"].items():
                message(f"  {table}: {n} rows")
            message("Restart XUI on the server so it reloads its caches.")
            return result

        self._start(job, done_msg="Preview ready (see log)." if plan_only
                    else "XUI backup imported.")

    def _build_progress_box(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.log = QPlainTextEdit()
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
            QMessageBox.information(self, "Busy", "Another operation is still running.")
            return True
        if self.source is None:
            QMessageBox.warning(self, "No source", "Connect or load a source first.")
            return True
        return False

    # ---------------------------------------------------------- profiles
    def _load_profiles(self) -> None:
        self.store = load_store()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("<new>")
        self.profile_combo.addItems([p.name for p in self.store.profiles])
        self.profile_combo.blockSignals(False)

    def _apply_profile(self, name: str) -> None:
        p = next((x for x in self.store.profiles if x.name == name), None)
        if not p:
            return
        self.in_host.setText(p.host)
        self.in_port.setValue(p.port)
        self.in_user.setText(p.user)
        self.in_db.setText(p.database)
        self.in_pass.setText(p.password)
        self.save_pass.setChecked(p.save_password)

    def _save_profile(self) -> None:
        name = f"{self.in_user.text()}@{self.in_host.text()}/{self.in_db.text()}"
        prof = ConnectionProfile(
            name=name, host=self.in_host.text(), port=self.in_port.value(),
            user=self.in_user.text(), database=self.in_db.text(),
            password=self.in_pass.text(), save_password=self.save_pass.isChecked(),
        )
        self.store.upsert(prof)
        save_store(self.store)
        self._load_profiles()
        self.profile_combo.setCurrentText(name)
        self._log(f"Saved profile: {name}")

    def _delete_profile(self) -> None:
        name = self.profile_combo.currentText()
        if name and name != "<new>":
            self.store.remove(name)
            save_store(self.store)
            self._load_profiles()
            self._log(f"Deleted profile: {name}")

    # ---------------------------------------------------------- source
    def _browse_dump(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select SQL dump", "", "SQL dumps (*.sql);;All files (*)"
        )
        if path:
            self.in_dump.setText(path)

    def _browse_export_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select output folder")
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
                    host=self.in_host.text().strip(), port=self.in_port.value(),
                    user=self.in_user.text().strip(), password=self.in_pass.text(),
                    database=self.in_db.text().strip(),
                )
                src: DataSource = MySQLSource(cfg)
            else:
                src = SqlDumpSource(self.in_dump.text().strip())
            src.open()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Connection failed", str(exc))
            self._log(f"ERROR: {exc}")
            return
        self.source = src
        self._log(f"Connected: {src.label}")
        self._populate_tables()

    def _populate_tables(self) -> None:
        assert self.source is not None
        tables = self.source.list_tables()
        det = profiles.detect_panel(tables)
        cats = profiles.categorise_tables(tables)
        cat_of = {t: c for c, ts in cats.items() for t in ts}

        info = self.source.server_info()
        extra = info.get("server_version", info.get("source", ""))
        self.status_lbl.setText(
            f"Panel: {det.profile.name} ({det.confidence:.0%} conf.)  •  "
            f"{len(tables)} tables  •  {extra}"
        )
        self.status_lbl.setStyleSheet("color: #2a7;")

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
        self._log(f"Detected {det.profile.name}; loaded {len(tables)} tables.")

    # ---------------------------------------------------------- actions
    def _do_export(self) -> None:
        if self._busy_guard():
            return
        tables = self._selected_tables()
        if not tables:
            QMessageBox.warning(self, "No tables", "Select at least one table.")
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
            message(f"Exporting {len(tables)} table(s) as {opts.fmt} → {opts.out_dir}")
            paths = export_tables(src, tables, opts, progress)
            return paths

        self._start(job, done_msg="Export complete.")

    def _do_maintenance(self) -> None:
        if self._busy_guard():
            return
        tables = self._selected_tables()
        if not tables:
            QMessageBox.warning(self, "No tables", "Select at least one table.")
            return
        op = self.maint_op.currentText()
        src = self.source

        def job(progress, message):
            message(f"Running {op} on {len(tables)} table(s)…")
            results = run_maintenance(src, tables, op, progress)
            for r in results:
                message(f"  {r.table}: {r.status} — {r.message}")
            return results

        self._start(job, done_msg=f"{op.title()} finished.")

    def _do_auto_repair(self) -> None:
        if self._busy_guard():
            return
        tables = self._selected_tables()
        if not tables:
            QMessageBox.warning(self, "No tables", "Select at least one table.")
            return
        if QMessageBox.question(
            self, "Confirm repair",
            "REPAIR will rewrite affected tables. Have you exported a backup first?",
        ) != QMessageBox.Yes:
            return
        src = self.source

        def job(progress, message):
            message("Checking tables…")
            results = check_and_repair(src, tables, progress)
            for r in results:
                message(f"  [{r.operation}] {r.table}: {r.status} — {r.message}")
            return results

        self._start(job, done_msg="Check & repair finished.")

    def _start(self, job, done_msg: str) -> None:
        self.progress.setValue(0)

        def on_finished(_res):
            self.progress.setValue(100)
            self._log(done_msg)

        def on_failed(err):
            self._log(f"ERROR: {err}")
            QMessageBox.critical(self, "Operation failed", err)

        self.runner.start(
            job,
            on_progress=self._on_progress,
            on_message=self._log,
            on_finished=on_finished,
            on_failed=on_failed,
        )

    def _about(self) -> None:
        QMessageBox.about(
            self, "About XUI DB Tool",
            "XUI DB Tool\n\nInspect, back up, repair and migrate XUI / Xtream-UI /\n"
            "StreamCreed panel databases you own or are authorised to manage.\n\n"
            "Credentials are migrated verbatim; hashes are never decrypted.\n\n"
            "Copyright © 2026 Diogo Carneiro <diogoncarneiro@outlook.com>\n"
            "MIT License.",
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.runner.busy:
            self.runner.wait(5000)  # let a running job stop cleanly before teardown
        if self.source is not None:
            try:
                self.source.close()
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(event)
