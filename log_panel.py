"""
Logs tab — the developer log, live: everything iPhone Mirror, pymobiledevice3 and xcodebuild
(WebDriverAgent) write. The same log is kept in ~/Library/Logs/iPhone Mirror.
"""

import collections
import html
import logging

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QFontDatabase, QGuiApplication
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

import paths

LOG_FORMAT = '%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s'
LOG_DATE_FORMAT = '%H:%M:%S'

_LEVELS = [('Debug', logging.DEBUG), ('Info', logging.INFO), ('Warnings', logging.WARNING), ('Errors', logging.ERROR)]
_COLORS = {logging.DEBUG: '#8b8b8b', logging.INFO: '#d4d4d4', logging.WARNING: '#e8b339', logging.ERROR: '#ff6b6b'}


class LogBuffer(logging.Handler):
    """Keeps the most recent log lines in memory for the Logs tab. Thread-safe; no Qt involved,
    so it can be installed before the QApplication exists."""

    def __init__(self, capacity: int = 5000):
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        self._lines = collections.deque(maxlen=capacity)  # (seq, levelno, text)
        self._seq = 0

    def emit(self, record: logging.LogRecord):
        try:
            text = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self._seq += 1  # emit() runs under self.lock
        self._lines.append((self._seq, record.levelno, text))

    @property
    def capacity(self) -> int:
        return self._lines.maxlen

    def since(self, seq: int) -> list[tuple[int, int, str]]:
        """Lines newer than `seq`, oldest first."""
        with self.lock:
            newer = []
            for line in reversed(self._lines):
                if line[0] <= seq:
                    break
                newer.append(line)
        newer.reverse()
        return newer

    def clear(self):
        with self.lock:
            self._lines.clear()


LOG_BUFFER = LogBuffer()


class LogPanel(QWidget):
    POLL_MS = 250

    def __init__(self, buffer: LogBuffer = LOG_BUFFER, parent=None):
        super().__init__(parent)
        self._buffer = buffer
        self._seq = 0

        self._level = QComboBox()
        for label, level in _LEVELS:
            self._level.addItem(label, level)
        self._level.setCurrentIndex(1)
        self._level.setToolTip("Lowest level to show")
        self._filter = QLineEdit(placeholderText="Filter")
        self._filter.setClearButtonEnabled(True)

        self._view = QPlainTextEdit(readOnly=True)
        self._view.setMaximumBlockCount(self._buffer.capacity)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(10)
        self._view.setFont(font)
        self._view.setStyleSheet("QPlainTextEdit { background: #161616; border: none; }")

        copy_button = QPushButton("Copy")
        copy_button.setToolTip("Copy the visible lines")
        clear_button = QPushButton("Clear")
        folder_button = QPushButton("Show in Finder")
        folder_button.setToolTip(str(paths.LOG_FILE))

        top = QHBoxLayout()
        top.addWidget(self._level)
        top.addWidget(self._filter, stretch=1)
        bottom = QHBoxLayout()
        bottom.addWidget(copy_button)
        bottom.addWidget(clear_button)
        bottom.addStretch()
        bottom.addWidget(folder_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(top)
        layout.addWidget(self._view, stretch=1)
        layout.addLayout(bottom)

        self._level.currentIndexChanged.connect(self._rerender)
        self._filter.textChanged.connect(self._rerender)
        copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(self._view.toPlainText()))
        clear_button.clicked.connect(self._clear)
        folder_button.clicked.connect(self._show_in_finder)

        self._timer = QTimer(self, interval=self.POLL_MS)
        self._timer.timeout.connect(self._append_new)
        self._timer.start()
        self._rerender()

    def _matches(self, level: int, text: str) -> bool:
        needle = self._filter.text().strip().lower()
        return level >= self._level.currentData() and (not needle or needle in text.lower())

    def _append(self, lines):
        shown = [line for line in lines if self._matches(line[1], line[2])]
        if not shown:
            return
        scrollbar = self._view.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        for _, level, text in shown:
            color = _COLORS.get(level, _COLORS[logging.ERROR] if level > logging.ERROR else _COLORS[logging.INFO])
            self._view.appendHtml(f'<span style="color:{color}; white-space:pre-wrap">{html.escape(text)}</span>')
        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _append_new(self):
        lines = self._buffer.since(self._seq)
        if lines:
            self._seq = lines[-1][0]
            self._append(lines)

    def _rerender(self):
        self._view.clear()
        lines = self._buffer.since(0)
        self._seq = lines[-1][0] if lines else self._seq
        self._append(lines)

    def _clear(self):
        self._buffer.clear()
        self._view.clear()

    def _show_in_finder(self):
        if paths.LOG_FILE.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.LOG_DIR)))
