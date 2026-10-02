"""
Doctor tab — runs the checks from doctor.py, shows what's missing and how to fix it, with
one-click fixes where the app can do them itself. Runs by itself on the first launch.
"""

import logging
import platform
import threading

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

import doctor
from doctor import Status
from version import __version__

logger = logging.getLogger(__name__)

_COLORS = {
    Status.OK: '#32d74b', Status.WARN: '#ff9f0a', Status.FAIL: '#ff453a',
    Status.INFO: '#0a84ff', Status.SKIP: '#636366',
}
_GLYPHS = {Status.OK: '✓', Status.WARN: '!', Status.FAIL: '✕', Status.INFO: 'i', Status.SKIP: '–'}


def _badge(status: Status, size: int = 18) -> QPixmap:
    """Round status badge, drawn at 2x so it stays sharp on Retina displays."""
    dpr = 2.0
    pixmap = QPixmap(round(size * dpr), round(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(_COLORS[status]))
    painter.drawEllipse(QRectF(0, 0, size, size))
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(round(size * 0.62))
    painter.setFont(font)
    painter.setPen(QColor('white'))
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, _GLYPHS[status])
    painter.end()
    return pixmap


def _wrapping_label(text: str, color: str, point_size: int | None = None) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    size = f"font-size: {point_size}pt;" if point_size else ""
    label.setStyleSheet(f"color: {color}; {size}")
    return label


class _CheckRow(QWidget):
    action_requested = pyqtSignal(str)

    def __init__(self, check: doctor.Check, parent=None):
        super().__init__(parent)
        self.check = check
        needs_fix = check.status in (Status.FAIL, Status.WARN)

        icon = QLabel()
        icon.setPixmap(_badge(check.status))
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        icon.setFixedWidth(24)

        title = QLabel(check.title)
        title.setStyleSheet("font-weight: 600;")
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(title, stretch=1)
        self._button = None
        if check.action and needs_fix:
            self._button = QPushButton(check.action_label)
            self._button.clicked.connect(lambda: self.action_requested.emit(check.action))
            title_row.addWidget(self._button)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        text.addLayout(title_row)
        text.addWidget(_wrapping_label(check.detail, '#a1a1a6'))
        if check.fix and needs_fix:
            text.addWidget(_wrapping_label(f"→ {check.fix}", '#d1d1d6'))
        self._result = _wrapping_label('', '#a1a1a6')
        self._result.hide()
        text.addWidget(self._result)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.addWidget(icon)
        layout.addLayout(text, stretch=1)

    def set_busy(self):
        if self._button:
            self._button.setEnabled(False)
            self._button.setText("Working…")

    def show_result(self, ok: bool, message: str):
        if self._button:
            self._button.setEnabled(True)
            self._button.setText(self.check.action_label)
        if message:
            self._result.setText(message)
            self._result.setStyleSheet(f"color: {_COLORS[Status.OK] if ok else _COLORS[Status.FAIL]};")
            self._result.show()


class DoctorPanel(QWidget):
    wda_downloaded = pyqtSignal()
    _check_ready = pyqtSignal(object)
    _run_finished = pyqtSignal()
    _action_finished = pyqtSignal(object, str, bool, str)  # row, action, ok, message

    def __init__(self, wda_state=None, parent=None):
        """wda_state: returns the app's WebDriverAgent (state, message), see DeviceManager.wda_state."""
        super().__init__(parent)
        self._wda_state = wda_state
        self._checks: list[doctor.Check] = []
        self._group = None
        self._running = False
        self._run_again = False
        self._has_run = False

        title = QLabel("Setup check")
        title.setStyleSheet("font-size: 15pt; font-weight: 600;")
        self._recheck = QPushButton("Check Again")
        self._recheck.clicked.connect(self.run)
        header = QHBoxLayout()
        header.addWidget(title, stretch=1)
        header.addWidget(self._recheck)
        self._summary = _wrapping_label(
            "Checks the iPhone, the Mac and everything mirroring and touch control need.", '#a1a1a6')

        content = QWidget()
        self._rows = QVBoxLayout(content)
        self._rows.setContentsMargins(10, 0, 10, 10)
        self._rows.setSpacing(2)
        self._rows.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)

        footer = _wrapping_label(
            f"iPhone Mirror {__version__} · Python {platform.python_version()} · macOS {platform.mac_ver()[0]}",
            '#636366', 10)

        top = QVBoxLayout()
        top.setContentsMargins(10, 10, 10, 6)
        top.addLayout(header)
        top.addWidget(self._summary)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.addLayout(top)
        layout.addWidget(scroll, stretch=1)
        footer.setContentsMargins(10, 0, 10, 0)
        layout.addWidget(footer)

        self._refresh_timer = QTimer(self, singleShot=True, interval=1500)
        self._refresh_timer.timeout.connect(self.run)
        self._check_ready.connect(self._add_check)
        self._run_finished.connect(self._finish)
        self._action_finished.connect(self._on_action_finished)

    @property
    def is_running(self) -> bool:
        return self._running

    def run(self):
        """(Re-)run all checks in the background."""
        if self._running:
            self._run_again = True
            return
        self._running = True
        self._has_run = True
        self._checks = []
        self._group = None
        while self._rows.count() > 1:
            widget = self._rows.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        self._summary.setText("Checking…")
        self._summary.setStyleSheet("color: #a1a1a6;")
        self._recheck.setEnabled(False)
        threading.Thread(target=self._run_checks, name='doctor', daemon=True).start()

    def refresh_soon(self):
        """Re-run shortly, if the results are on screen (e.g. after the iPhone was plugged in)."""
        if self._has_run and self.isVisible():
            self._refresh_timer.start()

    def _run_checks(self):
        try:
            for check in doctor.iter_checks(in_app=True, wda_state=self._wda_state):
                self._check_ready.emit(check)
        except Exception as e:
            logger.exception("Doctor checks failed")
            self._check_ready.emit(doctor.Check(doctor.MIRRORING, "Doctor", Status.FAIL, f"The checks crashed: {e}"))
        finally:
            self._run_finished.emit()

    def _add_check(self, check: doctor.Check):
        self._checks.append(check)
        logger.debug(f"Doctor: {check.title}: {check.status.value} — {check.detail}")
        position = self._rows.count() - 1  # before the stretch
        if check.group != self._group:
            self._group = check.group
            header = QLabel(check.group.upper())
            header.setStyleSheet("color: #8e8e93; font-size: 10pt; font-weight: 600; margin-top: 10px;")
            self._rows.insertWidget(position, header)
            position += 1
        row = _CheckRow(check)
        row.action_requested.connect(lambda action, row=row: self._run_action(row, action))
        self._rows.insertWidget(position, row)

    def _finish(self):
        self._running = False
        self._recheck.setEnabled(True)
        status, headline = doctor.summarize(self._checks)
        self._summary.setText(headline)
        self._summary.setStyleSheet(f"color: {_COLORS[status]}; font-weight: 600;")
        if self._run_again:
            self._run_again = False
            self.run()

    def _run_action(self, row: _CheckRow, action: str):
        row.set_busy()
        logger.info(f"Doctor fix: {action}")

        def work():
            try:
                ok, message = True, doctor.ACTIONS[action]()
            except Exception as e:
                ok, message = False, str(e) or type(e).__name__
                logger.warning(f"Doctor fix {action} failed: {message}")
            self._action_finished.emit(row, action, ok, message)

        threading.Thread(target=work, daemon=True).start()

    def _on_action_finished(self, row: _CheckRow, action: str, ok: bool, message: str):
        try:
            row.show_result(ok, message)
        except RuntimeError:
            pass  # the row was replaced by a re-run in the meantime
        if ok and action == 'download_wda':
            self.wda_downloaded.emit()
        if ok and message:  # actions that only open another app return no message
            QTimer.singleShot(1500, self.run)
