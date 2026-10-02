"""
Main Window — PyQt6 GUI for iPhone Mirror.
Displays the iPhone screen and handles user interaction.
"""

import logging

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QImage, QPixmap, QIcon
from PyQt6.QtWidgets import (
    QMainWindow, QLabel, QVBoxLayout, QWidget, QToolBar,
    QStatusBar, QMessageBox, QSizePolicy,
)

from device_manager import DeviceManager, ConnectionState
from screen_capture import ScreenCaptureThread
from input_handler import InputHandler

logger = logging.getLogger(__name__)


class ScreenView(QLabel):
    """Custom QLabel that displays the iPhone screen and captures mouse events."""

    def __init__(self, input_handler: InputHandler, parent=None):
        super().__init__(parent)
        self._input_handler = input_handler
        self._has_frame = False

        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 568)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("background-color: #1a1a1a;")

        # Show placeholder text
        self._show_placeholder()

    def _show_placeholder(self):
        self.setText(
            "iPhone Mirror\n\n"
            "Verbinde dein iPhone per USB...\n\n"
            "Voraussetzungen:\n"
            "- Developer Mode aktiviert\n"
            "- Computer vertraut\n"
            "- iOS 17+: tunneld gestartet"
        )
        self.setStyleSheet(
            "background-color: #1a1a1a; color: #888; "
            "font-size: 14px; padding: 40px;"
        )

    def update_frame(self, qimage: QImage):
        """Update the displayed frame. Called from main thread."""
        if not self._has_frame:
            self._has_frame = True
            self.setStyleSheet("background-color: #000;")
            self.setText("")

        # Update iPhone screen size for coordinate mapping
        self._input_handler.update_screen_size(qimage.width(), qimage.height())

        # Convert to pixmap and scale to fit label (fast mode for better FPS)
        pixmap = QPixmap.fromImage(qimage)
        scaled = pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        self.setPixmap(scaled)

    def show_disconnected(self):
        """Show disconnected state."""
        self._has_frame = False
        self._show_placeholder()

    # --- Mouse event handling ---

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._has_frame:
            pos = event.position()
            self._input_handler.on_mouse_press(
                pos.x(), pos.y(),
                self.width(), self.height(),
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._has_frame:
            pos = event.position()
            self._input_handler.on_mouse_move(
                pos.x(), pos.y(),
                self.width(), self.height(),
            )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._has_frame:
            pos = event.position()
            self._input_handler.on_mouse_release(
                pos.x(), pos.y(),
                self.width(), self.height(),
            )
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._has_frame:
            pos = event.position()
            delta = event.angleDelta()
            self._input_handler.on_scroll(
                pos.x(), pos.y(),
                delta.x(), delta.y(),
                self.width(), self.height(),
            )
        super().wheelEvent(event)


class MainWindow(QMainWindow):
    """Main application window for iPhone Mirror."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("iPhone Mirror")
        self.setMinimumSize(400, 700)
        self.resize(430, 860)

        # Core components
        self.device_manager = DeviceManager(self)
        self.input_handler = InputHandler(self)
        self.capture_thread = None
        self._connected = False

        # UI
        self.screen_view = ScreenView(self.input_handler, self)
        self._setup_ui()
        self._setup_toolbar()
        self._setup_statusbar()
        self._connect_signals()

        # Battery update timer
        self._battery_timer = QTimer(self)
        self._battery_timer.timeout.connect(self._update_battery)

        # WDA connection retry timer
        self._wda_retry_timer = QTimer(self)
        self._wda_retry_timer.timeout.connect(self._try_wda)
        self._wda_retry_timer.setInterval(5000)

        # Start device discovery
        self.device_manager.start_discovery()

    def _setup_ui(self):
        """Set up the central widget."""
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # WDA status banner (hidden by default)
        self._wda_banner = QLabel(
            "Touch-Steuerung nicht verfügbar — Starte WDA in Xcode (Product > Test) oder per CLI"
        )
        self._wda_banner.setStyleSheet(
            "background-color: #f59e0b; color: #000; "
            "padding: 6px; font-size: 12px;"
        )
        self._wda_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._wda_banner.setVisible(False)
        layout.addWidget(self._wda_banner)

        # Screen view
        layout.addWidget(self.screen_view, stretch=1)

        self.setCentralWidget(central)

    def _setup_toolbar(self):
        """Set up the hardware button toolbar."""
        toolbar = QToolBar("Steuerung")
        toolbar.setMovable(False)
        toolbar.setIconSize(toolbar.iconSize())
        self.addToolBar(Qt.ToolBarArea.BottomToolBarArea, toolbar)

        # Home button
        home_action = QAction("Home", self)
        home_action.setToolTip("Home-Button (Cmd+H)")
        home_action.setShortcut("Ctrl+H")
        home_action.triggered.connect(self._on_home)
        toolbar.addAction(home_action)

        toolbar.addSeparator()

        # Lock button
        lock_action = QAction("Lock", self)
        lock_action.setToolTip("Sperren/Entsperren (Cmd+L)")
        lock_action.setShortcut("Ctrl+L")
        lock_action.triggered.connect(self._on_lock)
        toolbar.addAction(lock_action)

        toolbar.addSeparator()

        # Volume Up
        vol_up_action = QAction("Vol +", self)
        vol_up_action.setToolTip("Lauter")
        vol_up_action.triggered.connect(lambda: self.input_handler.press_button('volumeUp'))
        toolbar.addAction(vol_up_action)

        # Volume Down
        vol_down_action = QAction("Vol -", self)
        vol_down_action.setToolTip("Leiser")
        vol_down_action.triggered.connect(lambda: self.input_handler.press_button('volumeDown'))
        toolbar.addAction(vol_down_action)

        toolbar.addSeparator()

        # Reconnect
        reconnect_action = QAction("Reconnect", self)
        reconnect_action.setToolTip("Verbindung neu herstellen")
        reconnect_action.triggered.connect(self._on_reconnect)
        toolbar.addAction(reconnect_action)

    def _setup_statusbar(self):
        """Set up the status bar with connection info, FPS, and battery."""
        statusbar = QStatusBar()
        self.setStatusBar(statusbar)

        self._status_label = QLabel("Nicht verbunden")
        self._fps_label = QLabel("")
        self._battery_label = QLabel("")
        self._device_label = QLabel("")

        statusbar.addWidget(self._status_label, stretch=1)
        statusbar.addPermanentWidget(self._device_label)
        statusbar.addPermanentWidget(self._fps_label)
        statusbar.addPermanentWidget(self._battery_label)

    def _connect_signals(self):
        """Wire up all signals."""
        self.device_manager.device_connected.connect(self._on_device_connected)
        self.device_manager.device_disconnected.connect(self._on_device_disconnected)
        self.device_manager.connection_error.connect(self._on_connection_error)
        self.device_manager.connection_state_changed.connect(self._on_state_changed)
        self.input_handler.wda_status_changed.connect(self._on_wda_status)

    # --- Device connection handlers ---

    def _on_device_connected(self, info: dict):
        """Called when device is successfully connected."""
        self._connected = True
        self._device_label.setText(f"{info.get('name', 'iPhone')} ({info.get('ios_version', '?')})")
        self._status_label.setText("Verbunden")

        # Start screen capture
        self._start_capture()

        # Start WDA automatically (builds + runs via xcodebuild)
        self._start_wda_auto()

        # Start battery updates
        self._battery_timer.start(30000)
        self._update_battery()

    def _on_device_disconnected(self):
        """Called when device is disconnected."""
        self._connected = False
        self._stop_capture()
        self._battery_timer.stop()
        self._wda_retry_timer.stop()

        self._status_label.setText("Nicht verbunden")
        self._fps_label.setText("")
        self._battery_label.setText("")
        self._device_label.setText("")
        self._wda_banner.setVisible(False)

        self.screen_view.show_disconnected()

    def _on_connection_error(self, msg: str):
        """Show connection error to user."""
        self._status_label.setText("Fehler")
        QMessageBox.warning(self, "Verbindungsfehler", msg)

    def _on_state_changed(self, state: ConnectionState):
        """Update status label based on connection state."""
        labels = {
            ConnectionState.DISCONNECTED: "Nicht verbunden",
            ConnectionState.CONNECTING: "Verbinde...",
            ConnectionState.CONNECTED: "Verbunden",
            ConnectionState.ERROR: "Fehler",
        }
        self._status_label.setText(labels.get(state, "?"))

    # --- Screen capture ---

    def _start_capture(self):
        """Start the screen capture thread."""
        if self.capture_thread and self.capture_thread.isRunning():
            self.capture_thread.stop()

        self.capture_thread = ScreenCaptureThread(self.device_manager, target_fps=15)
        self.capture_thread.frame_ready.connect(self._on_frame)
        self.capture_thread.fps_updated.connect(self._on_fps)
        self.capture_thread.capture_error.connect(self._on_capture_error)
        self.capture_thread.start()

    def _stop_capture(self):
        """Stop the screen capture thread."""
        if self.capture_thread:
            self.capture_thread.stop()
            self.capture_thread = None

    def _on_frame(self, qimage: QImage):
        """New frame from capture thread."""
        self.screen_view.update_frame(qimage)

    def _on_fps(self, fps: float):
        """Update FPS display."""
        self._fps_label.setText(f"{fps:.0f} FPS")

    def _on_capture_error(self, msg: str):
        """Handle capture error."""
        logger.warning(f"Capture error: {msg}")
        self._status_label.setText(f"Fehler: {msg[:50]}")

    # --- WDA ---

    def _start_wda_auto(self):
        """Auto-start WDA: port forward + xcodebuild + retry loop."""
        import threading

        def _setup():
            # 1. Start port forwarding (localhost:8100 -> device:8100)
            self.device_manager.start_port_forward(8100, 8100)

            # 2. Start WDA via xcodebuild test (builds + installs + runs)
            self.device_manager.start_wda()

        threading.Thread(target=_setup, daemon=True).start()

        # 3. Start retry timer to connect WDA client once it's ready
        self._wda_retry_timer.start()

    def _try_wda(self):
        """Try to connect to WebDriverAgent."""
        if self.input_handler.wda.is_connected:
            return
        self.input_handler.try_connect_wda()

    def _on_wda_status(self, connected: bool):
        """Update WDA status banner."""
        self._wda_banner.setVisible(not connected)
        if connected:
            self._wda_retry_timer.stop()
            logger.info("WDA connected — touch control enabled")

    # --- Battery ---

    def _update_battery(self):
        """Update battery display."""
        try:
            # Try WDA first (less intrusive)
            if self.input_handler.wda.is_connected:
                info = self.input_handler.wda.get_battery_info()
                if info and info.get('level', -1) >= 0:
                    level = info['level']
                    state = info.get('state', 0)
                    charging = " +" if state == 2 else ""
                    self._battery_label.setText(f"Akku: {level}%{charging}")
                    return

            # Fallback to device manager
            if self.device_manager.is_connected:
                info = self.device_manager.get_battery_info()
                level = info.get('level', -1)
                if level >= 0:
                    charging = " +" if info.get('charging', False) else ""
                    self._battery_label.setText(f"Akku: {level}%{charging}")
        except Exception as e:
            logger.debug(f"Battery update failed: {e}")

    # --- Button handlers ---

    def _on_home(self):
        """Home button pressed."""
        self.input_handler.go_home()

    def _on_lock(self):
        """Lock button pressed."""
        self.input_handler.lock_device()

    def _on_reconnect(self):
        """Reconnect button pressed."""
        self._on_device_disconnected()
        self.device_manager.disconnect()
        self.device_manager.start_discovery()

    # --- Window lifecycle ---

    def closeEvent(self, event):
        """Clean up on window close."""
        self._stop_capture()
        self.input_handler.cleanup()
        self.device_manager.cleanup()
        super().closeEvent(event)

    def resizeEvent(self, event):
        """Handle window resize — the ScreenView handles scaling automatically."""
        super().resizeEvent(event)
        # Re-render current frame at new size if we have one
        if self.screen_view._has_frame and self.screen_view.pixmap():
            # The next frame from capture thread will auto-scale
            pass
