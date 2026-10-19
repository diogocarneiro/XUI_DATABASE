# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Diogo Carneiro <diogoncarneiro@outlook.com>
# XUI DB Tool — https://github.com/diogocarneiro/XUI_DATABASE
"""Background worker plumbing so long DB operations never block the UI thread."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal

# A job receives two callbacks it may call to report progress/log, and returns a result.
Job = Callable[["ProgressEmitter", "MessageEmitter"], object]
ProgressEmitter = Callable[[str, int, int], None]
MessageEmitter = Callable[[str], None]


class Worker(QObject):
    """Runs a single ``job`` callable and emits lifecycle signals."""

    progress = Signal(str, int, int)   # (label, done, total)
    message = Signal(str)              # human-readable log line
    finished = Signal(object)          # job return value
    failed = Signal(str)               # error message

    def __init__(self, job: Job):
        super().__init__()
        self._job = job

    def run(self) -> None:
        try:
            result = self._job(self.progress.emit, self.message.emit)
            self.finished.emit(result)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self.failed.emit(str(exc))


class JobRunner(QObject):
    """Owns a worker + thread pair and keeps references alive for their lifetime.

    ``JobRunner`` must live in the GUI (main) thread — give it the main window as
    ``parent``. Its handler slots then run in the main thread (Qt queues them from
    the worker thread), so finished/failed callbacks can safely touch widgets.
    """

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: Worker | None = None
        self._on_finished: Callable[[object], None] | None = None
        self._on_failed: Callable[[str], None] | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(
        self,
        job: Job,
        *,
        on_progress: ProgressEmitter | None = None,
        on_message: MessageEmitter | None = None,
        on_finished: Callable[[object], None] | None = None,
        on_failed: Callable[[str], None] | None = None,
    ) -> bool:
        if self.busy:
            return False

        thread = QThread()
        worker = Worker(job)
        worker.moveToThread(thread)
        self._thread = thread
        self._worker = worker
        self._on_finished = on_finished
        self._on_failed = on_failed

        thread.started.connect(worker.run)
        if on_progress:
            worker.progress.connect(on_progress)
        if on_message:
            worker.message.connect(on_message)

        # Result/error handling runs on the main thread (these are slots of this
        # QObject, which lives in the main thread -> Qt uses a queued connection).
        worker.finished.connect(self._handle_finished)
        worker.failed.connect(self._handle_failed)

        # Teardown: stop the loop and delete objects via the event loop. No wait().
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_refs)

        thread.start()
        return True

    def _handle_finished(self, result: object) -> None:
        if self._on_finished is not None:
            self._on_finished(result)

    def _handle_failed(self, error: str) -> None:
        if self._on_failed is not None:
            self._on_failed(error)

    def _clear_refs(self) -> None:
        self._thread = None
        self._worker = None
        self._on_finished = None
        self._on_failed = None

    def wait(self, msec: int | None = None) -> None:
        """Block until the current job's thread finishes (for shutdown only)."""
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait() if msec is None else self._thread.wait(msec)
