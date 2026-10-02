"""
Settings tab — a preview of the settings to come. Nothing here is wired up yet.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QLabel, QScrollArea, QSlider, QVBoxLayout, QWidget,
)


class SettingsPanel(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)

        notice = QLabel("Preview — these settings aren't connected yet.")
        notice.setWordWrap(True)
        notice.setStyleSheet(
            "background: #2d3a4f; color: #c9d7ec; border-radius: 6px; padding: 8px 10px;"
        )

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)
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
