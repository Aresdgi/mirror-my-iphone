#!/usr/bin/env python3
"""
iPhone Mirror — See and control your iPhone from your Mac.
Alternative to Apple's iPhone Mirroring for EU users.

Usage:
    python3 main.py
"""

import logging
import logging.handlers
import signal
import sys
import threading

import paths
from version import __version__


def check_dependencies():
    """Check that required packages are installed."""
    missing = []
    for module, package in (('pymobiledevice3', 'pymobiledevice3'), ('PyQt6', 'PyQt6'),
                            ('PIL', 'Pillow'), ('requests', 'requests')):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        print(f"Missing packages: {', '.join(missing)}")
        print("Install them with: pip install -r requirements.txt  (or run: bash setup.sh)")
        sys.exit(1)


def setup_logging():
    """Log to the terminal (info and up), and at debug level to ~/Library/Logs/iPhone Mirror
    and the Logs tab."""
    from log_panel import LOG_BUFFER, LOG_DATE_FORMAT, LOG_FORMAT

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    root.addHandler(console)

    try:
        paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = logging.handlers.RotatingFileHandler(
            paths.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding='utf-8',
        )
        log_file.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(name)s: %(message)s'))
        root.addHandler(log_file)
    except OSError as e:
        logging.warning(f"Can't write the log file: {e}")

    root.addHandler(LOG_BUFFER)

    # Third-party debug chatter
    for name in ('urllib3', 'asyncio', 'PIL', 'parso', 'humanfriendly'):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Log uncaught exceptions instead of losing them (no terminal when started from the Dock),
    # and keep PyQt from aborting the app on an exception in a slot
    def log_uncaught(exc_type, exc, tb):
        logging.getLogger('main').critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = log_uncaught
    threading.excepthook = lambda args: log_uncaught(args.exc_type, args.exc_value, args.exc_traceback)


def set_macos_app_name():
    """When run as `python main.py`, show "iPhone Mirror" in the menu bar instead of "Python".
    (The .app bundle's Info.plist takes care of this when running from the bundle.)"""
    try:
        from Foundation import NSBundle
        NSBundle.mainBundle().infoDictionary()['CFBundleName'] = paths.APP_NAME
    except Exception:
        pass


def main():
    check_dependencies()
    setup_logging()
    logging.getLogger('main').info(
        f"iPhone Mirror {__version__} starting (Python {sys.version.split()[0]}, "
        f"{'app bundle ' + str(paths.bundle_path()) if paths.bundle_path() else 'from source'})"
    )

    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QColor, QIcon, QPalette
    from PyQt6.QtWidgets import QApplication

    from main_window import MainWindow

    if not paths.bundle_path():
        set_macos_app_name()

    # High DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName(paths.APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName('iPhoneMirroring')
    icon = paths.APP_DIR / 'assets' / 'AppIcon.png'
    if not paths.bundle_path() and icon.exists():
        app.setWindowIcon(QIcon(str(icon)))  # Dock icon when running from source

    # Dark theme for macOS
    app.setStyle('Fusion')
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

    # Ctrl-C / SIGTERM close the window like ⌘Q does, so its cleanup stops WebDriverAgent and
    # the port forwarder. The timer gives Python a chance to run signal handlers while Qt waits.
    signal.signal(signal.SIGINT, lambda *_: window.close())
    signal.signal(signal.SIGTERM, lambda *_: window.close())
    signal_timer = QTimer()
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(500)

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
