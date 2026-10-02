#!/usr/bin/env python3
"""
iPhone Mirror — See and control your iPhone from your Mac.
Alternative to Apple's iPhone Mirroring for EU users.

Usage:
    python3 main.py
"""

import logging
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from main_window import MainWindow


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )


def check_dependencies():
    """Check that required packages are installed."""
    missing = []

    try:
        import pymobiledevice3  # noqa: F401
    except ImportError:
        missing.append('pymobiledevice3')

    try:
        import PyQt6  # noqa: F401
    except ImportError:
        missing.append('PyQt6')

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing.append('Pillow')

    try:
        import requests  # noqa: F401
    except ImportError:
        missing.append('requests')

    if missing:
        print(f"Fehlende Pakete: {', '.join(missing)}")
        print("Installiere mit: pip install " + " ".join(missing))
        print("Oder fuehre setup.sh aus: bash setup.sh")
        sys.exit(1)


def main():
    setup_logging()
    check_dependencies()

    # High DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName('iPhone Mirror')
    app.setOrganizationName('iPhoneMirroring')

    # Dark theme for macOS
    app.setStyle('Fusion')
    from PyQt6.QtGui import QPalette, QColor
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(30, 30, 30))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(35, 35, 35))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(50, 50, 50))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Text, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Button, QColor(45, 45, 45))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 50, 50))
    palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
