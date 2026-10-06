"""
Settings tab — agent control (wired up), then a preview of the settings to come.
"""

from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QLabel, QScrollArea, QSlider, QVBoxLayout, QWidget,
)

import paths

AGENTS_KEY = 'agents/enabled'


def agents_enabled(settings: QSettings) -> bool:
    """Whether AI agents and scripts may control the iPhone (the agent API). Off unless turned on."""
    return settings.value(AGENTS_KEY, False, type=bool)


class SettingsPanel(QScrollArea):
    agents_toggled = pyqtSignal(bool)

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)
        layout.addWidget(self._agents_group())

        notice = QLabel("Preview — the settings below aren't connected yet.")
        notice.setWordWrap(True)
        notice.setStyleSheet(
            "background: #2d3a4f; color: #c9d7ec; border-radius: 6px; padding: 8px 10px;"
        )
        layout.addWidget(notice)
        layout.addWidget(self._group("Display", [
            ("Window size", self._combo(["Fit to screen", "50 %", "75 %", "Actual size"])),
            ("Frame rate", self._combo(["Up to 60 FPS", "30 FPS (saves power)"])),
            ("", QCheckBox("Show touches")),
            ("", QCheckBox("Keep window on top")),
        ]))
        layout.addWidget(self._group("Input", [
            ("Scroll speed", self._slider()),
            ("", QCheckBox("Right-click sends a long press", checked=True)),
            ("", QCheckBox("Type with the Mac keyboard")),
        ]))
        layout.addWidget(self._group("Connection", [
            ("Screen source", self._combo(["Automatic", "USB video stream", "Screenshots"])),
            ("", QCheckBox("Connect when an iPhone is plugged in", checked=True)),
            ("", QCheckBox("Start WebDriverAgent automatically", checked=True)),
            ("", QCheckBox("Play iPhone audio on this Mac", checked=True)),
        ]))
        layout.addWidget(self._group("General", [
            ("", QCheckBox("Run the doctor at every launch")),
            ("", QCheckBox("Open at login")),
            ("Log level", self._combo(["Info", "Debug"])),
        ]))
        layout.addStretch()
        self.setWidget(content)

    def _agents_group(self) -> QGroupBox:
        group = QGroupBox("AI agents and scripts")
        box = QVBoxLayout(group)
        self._agents = QCheckBox("Allow AI agents and scripts to control the iPhone")
        self._agents.setChecked(agents_enabled(self._settings))
        self._agents.toggled.connect(self._on_agents_toggled)
        explanation = QLabel(
            "Turns on the local agent API that the MCP server (Claude and other agents) and the "
            "command line's device commands use. While it's on, any program you run that reads "
            f"{paths.API_FILE.name} can see the screen, tap, type and open apps on the iPhone. "
            "A new access token is made every time it starts. Off by default.")
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color: #a1a1a6;")
        box.addWidget(self._agents)
        box.addWidget(explanation)
        return group

    def _on_agents_toggled(self, enabled: bool):
        self._settings.setValue(AGENTS_KEY, enabled)
        self.agents_toggled.emit(enabled)

    @staticmethod
    def _group(title: str, rows) -> QGroupBox:
        group = QGroupBox(title)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for label, widget in rows:
            if label:
                form.addRow(label, widget)
            else:
                form.addRow(widget)
        return group

    @staticmethod
    def _combo(items) -> QComboBox:
        combo = QComboBox()
        combo.addItems(items)
        return combo

    @staticmethod
    def _slider() -> QSlider:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(1, 10)
        slider.setValue(5)
        return slider
