"""
Main Window — the mirrored iPhone on the left, a sidebar with Doctor, Settings and Logs on the right.

The window isn't freely resizable: the phone is shown at a fixed zoom of its real size in
points (View › Actual Size / Larger / Smaller), at the largest step that fits the screen.
That keeps the window phone-shaped and the image scaled by one constant factor (at 75 % on a
3x iPhone, exactly 1/2 or 1/4 of its pixels on Retina or 1x displays).
"""

import logging
import threading

from PyQt6.QtCore import QPointF, QRectF, QSize, QSizeF, Qt, QSettings, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QAction, QColor, QDesktopServices, QGuiApplication, QImage, QKeySequence, QPainter, QPainterPath, QPen,
)
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLayout, QMainWindow, QSizePolicy, QStatusBar, QTabWidget, QToolBar,
    QVBoxLayout, QWidget,
)

import paths
from agent_api import AgentAPI, remove_stale_api_file
from device_manager import ConnectionState, DeviceManager, stop_leftover_wda
from doctor_panel import DoctorPanel
from input_handler import InputHandler
from log_panel import LogPanel
from screen_capture import ScreenCaptureThread, enable_video_capture
from settings_panel import SettingsPanel, agents_enabled

logger = logging.getLogger(__name__)

BACKGROUND = QColor(30, 30, 30)
AGENT_TOUCH = QColor(10, 132, 255)  # touches sent through the agent API


class ScreenView(QWidget):
    """Draws the iPhone screen and turns mouse input into touches, in iPhone points."""

    def __init__(self, input_handler: InputHandler, parent=None):
        super().__init__(parent)
        self._input = input_handler
        self._frame: QImage | None = None
        self._scaled: QImage | None = None
        self._points = QSizeF(393, 852)  # iPhone screen in points
        self._touch: list[QPointF] = []  # current press/drag, for the touch indicator
        self._agent_touch: list[QPointF] = []  # last gesture from the agent API, in iPhone points
        self._agent_touch_timer = QTimer(self, singleShot=True, interval=600)
        self._agent_touch_timer.timeout.connect(lambda: self.show_agent_touch([]))
        self._message = ""
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_message("Connect your iPhone with a USB cable.")

    def set_points(self, size: QSizeF):
        self._points = size
        self._input.set_screen_points(size)
        self.update()

    def update_frame(self, image: QImage):
        """Show a new frame. Qt coalesces update() calls, so painting never falls behind the stream."""
        self._frame = image
        self._scaled = None
        self.update()

    def show_agent_touch(self, points: list):
        """Briefly mark where an agent touched (`points` in iPhone points; a swipe has two)."""
        self._agent_touch = points
        if points:
            self._agent_touch_timer.start()
        self.update()

    @property
    def has_frame(self) -> bool:
        return self._frame is not None

    def show_message(self, text: str):
        """Clear the screen and show `text` instead."""
        self._frame = None
        self._scaled = None
        self._message = text
        self.update()

    def _image_rect(self) -> QRectF:
        """Where the frame is drawn: fitted and centered, in logical pixels."""
        if self._frame is None:
            return QRectF(self.rect())
        frame_w, frame_h = self._frame.width(), self._frame.height()
        scale = min(self.width() / frame_w, self.height() / frame_h)
        w, h = frame_w * scale, frame_h * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _corner_radius(self, rect: QRectF) -> float:
        # Phones with a notch or Dynamic Island have round display corners (~14% of the width);
        # Home-button models (SE) have square ones
        short, long = sorted((rect.width(), rect.height()))
        return short * 0.14 if long / short > 2 else 0

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND)
        rect = self._image_rect()
        radius = self._corner_radius(rect)
        screen = QPainterPath()
        screen.addRoundedRect(rect, radius, radius)

        if self._frame is None:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.fillPath(screen, QColor(17, 17, 17))
            painter.setPen(QColor(140, 140, 140))
            margin = rect.width() * 0.12
            painter.drawText(rect.adjusted(margin, 0, -margin, 0),
                             Qt.AlignmentFlag.AlignCenter.value | Qt.TextFlag.TextWordWrap.value, self._message)
            return

        # Shrink with Qt's area-averaging filter, then draw 1:1 in device pixels. Letting
        # drawImage() scale would resample bilinearly, which breaks up thin text (worst at 1x).
        dpr = self.devicePixelRatioF()
        size = QSize(max(1, round(rect.width() * dpr)), max(1, round(rect.height() * dpr)))
        scaled = self._scaled
        if scaled is None or scaled.size() != size or scaled.devicePixelRatio() != dpr:
            scaled = self._frame.scaled(
                size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(dpr)
            self._scaled = scaled
        origin = QPointF(round(rect.x() * dpr) / dpr, round(rect.y() * dpr) / dpr)
        painter.drawImage(origin, scaled)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if radius:
            # Round the corners by painting the background over them (antialiased, unlike a clip)
            outside = QPainterPath()
            outside.addRect(rect.adjusted(-1, -1, 1, 1))
            painter.fillPath(outside.subtracted(screen), BACKGROUND)

        if self._touch:
            self._draw_touch(painter, self._touch, QColor(255, 255, 255))
        if self._agent_touch:
            self._draw_touch(painter, [self._from_points(p) for p in self._agent_touch], AGENT_TOUCH)

    @staticmethod
    def _draw_touch(painter: QPainter, path: list[QPointF], color: QColor):
        """A finger's path and a circle where it is (or was last)."""
        trail, outline, fill = QColor(color), QColor(color), QColor(color)
        trail.setAlpha(110)
        outline.setAlpha(200)
        fill.setAlpha(90)
        painter.setPen(QPen(trail, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        if len(path) > 1:
            painter.drawPolyline(path)
        painter.setPen(QPen(outline, 1.5))
        painter.setBrush(fill)
        painter.drawEllipse(path[-1], 14, 14)

    # --- Mouse → touch ---

    def _to_points(self, pos: QPointF, clamp: bool) -> QPointF | None:
        """Map a widget position to iPhone points. Outside the image: None, or the nearest edge if clamp."""
        rect = self._image_rect()
        rel_x = (pos.x() - rect.x()) / rect.width()
        rel_y = (pos.y() - rect.y()) / rect.height()
        if not clamp and not (0 <= rel_x <= 1 and 0 <= rel_y <= 1):
            return None
        rel_x, rel_y = min(max(rel_x, 0.0), 1.0), min(max(rel_y, 0.0), 1.0)
        return QPointF(round(rel_x * self._points.width(), 1), round(rel_y * self._points.height(), 1))

    def _from_points(self, point: QPointF) -> QPointF:
        """Map iPhone points to a widget position."""
        rect = self._image_rect()
        return QPointF(rect.x() + point.x() / self._points.width() * rect.width(),
                       rect.y() + point.y() / self._points.height() * rect.height())

    def mousePressEvent(self, event):
        # Also receives the second press of a double click (QWidget's default mouseDoubleClickEvent)
        if self._frame is None or event.button() not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            return
        point = self._to_points(event.position(), clamp=False)
        if point is None:
            return
        right = event.button() == Qt.MouseButton.RightButton
        self._input.press(point, right_button=right)
        if not right and self._input.wda.is_connected:
            self._touch = [event.position()]
            self.update()

    def mouseMoveEvent(self, event):
        if not self._touch:
            return
        self._input.move(self._to_points(event.position(), clamp=True))
        self._touch.append(event.position())
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._frame is None:
            return
        self._input.release(self._to_points(event.position(), clamp=True))
        if self._touch:
            self._touch = []
            self.update()

    def wheelEvent(self, event):
        event.accept()
        if self._frame is None or event.phase() == Qt.ScrollPhase.ScrollMomentum:
            return  # iOS adds its own momentum to the swipes
        point = self._to_points(event.position(), clamp=False)
        if point is None:
            return
        pixels = event.pixelDelta()
        if not pixels.isNull():  # trackpad: move the content as far as the fingers moved
            points_per_pixel = self._points.width() / self._image_rect().width()
            delta = QPointF(pixels.x() * points_per_pixel, pixels.y() * points_per_pixel)
        else:  # mouse wheel: 120 per notch
            angle = event.angleDelta()
            delta = QPointF(angle.x() / 120 * 60, angle.y() / 120 * 60)
        self._input.scroll(point, delta)


class Spinner(QWidget):
    """A small rotating arc, animated only while it's visible."""

    def __init__(self, size: int = 14, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0
        self._timer = QTimer(self, interval=30)
        self._timer.timeout.connect(self._step)

    def _step(self):
        self._angle = (self._angle + 12) % 360
        self.update()

    def showEvent(self, event):
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor('white'), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5), -self._angle * 16, 270 * 16)


class MainWindow(QMainWindow):
    """Main application window for Mirror my iPhone."""

    ZOOM_LEVELS = (0.5, 0.625, 0.75, 0.875, 1.0, 1.25)
    DEFAULT_SCREEN = QSizeF(393, 852)  # iPhone 15/16, until a device reports its own
    SIDEBAR_WIDTH = 360
    PHONE_MARGIN = 14

    _battery_ready = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mirror my iPhone")
        self.settings = QSettings(str(paths.SETTINGS_FILE), QSettings.Format.IniFormat)

        # Core components
        self.device_manager = DeviceManager(self)
        self.input_handler = InputHandler(self)
        self.agent_api = AgentAPI(self.device_manager, self.input_handler, lambda: self._screen_points, self)
        self.capture_thread = None
        self._capture_mode = ""
        self._screen_points = self.DEFAULT_SCREEN
        self._last_frame_size = QSize()
        self._zoom = float(self.settings.value('view/zoom', 1.0))

        # UI
        self.screen_view = ScreenView(self.input_handler)
        self.doctor_panel = DoctorPanel(wda_state=lambda: self.device_manager.wda_state)
        self.settings_panel = SettingsPanel(self.settings)
        self.log_panel = LogPanel()
        self._setup_ui()
        self._setup_toolbar()
        self._setup_menus()
        self._setup_statusbar()
        self._connect_signals()

        self._battery_timer = QTimer(self, interval=30000)
        self._battery_timer.timeout.connect(self._update_battery)
        # Keeps trying to open a WDA session while the iPhone is connected
        self._wda_retry_timer = QTimer(self, interval=3000)
        self._wda_retry_timer.timeout.connect(self._try_wda)

        self._apply_zoom()
        self._restore_sidebar()

        # Start device discovery and the agent API. A WebDriverAgent an earlier run left behind
        # (after a crash) is stopped first.
        enable_video_capture()
        threading.Thread(target=stop_leftover_wda, name='leftover-wda', daemon=True).start()
        self.device_manager.start_discovery()
        remove_stale_api_file()
        self._set_agents_enabled(agents_enabled(self.settings))
        QTimer.singleShot(0, self._run_doctor_on_first_launch)

    # --- UI setup ---

    def _setup_ui(self):
        self._banner = QFrame(objectName='banner')
        self._banner.setStyleSheet("#banner { background: #d70015; border: 2px solid #ff6961; border-radius: 6px; }"
                                   "QLabel { color: white; background: transparent; font-size: 12pt; }")
        self._banner_spinner = Spinner()
        self._banner_text = QLabel(wordWrap=True)
        self._banner_text.linkActivated.connect(lambda _: self.show_tab(self.doctor_panel))
        row = QHBoxLayout(self._banner)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(8)
        row.addWidget(self._banner_spinner, alignment=Qt.AlignmentFlag.AlignTop)
        row.addWidget(self._banner_text, stretch=1)
        self._banner.setVisible(False)

        phone = QWidget()
        column = QVBoxLayout(phone)
        column.setContentsMargins(self.PHONE_MARGIN, self.PHONE_MARGIN, self.PHONE_MARGIN, self.PHONE_MARGIN)
        column.setSpacing(10)
        column.addWidget(self._banner)
        column.addWidget(self.screen_view, alignment=Qt.AlignmentFlag.AlignHCenter)
        column.addStretch()

        self.sidebar = QTabWidget()
        self.sidebar.setDocumentMode(True)
        self.sidebar.addTab(self.doctor_panel, "Doctor")
        self.sidebar.addTab(self.settings_panel, "Settings")
        self.sidebar.addTab(self.log_panel, "Logs")
        self.sidebar.setFixedWidth(self.SIDEBAR_WIDTH)
        # The phone decides the window height; the sidebar takes whatever that leaves
        self.sidebar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Ignored)
        self._divider = QFrame()
        self._divider.setFrameShape(QFrame.Shape.VLine)
        self._divider.setStyleSheet("color: #3a3a3c;")

        central = QWidget()
        central.setAutoFillBackground(True)
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(phone)
        row.addWidget(self._divider)
        row.addWidget(self.sidebar)
        self.setCentralWidget(central)
        # Fixed-size window that follows its content (phone size, sidebar shown or not)
        self.layout().setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

    def _setup_toolbar(self):
        toolbar = QToolBar("Controls")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
        self.setUnifiedTitleAndToolBarOnMac(True)

        self._home_action = self._action("Home", self._on_home, "Ctrl+Shift+H", "Go to the Home Screen (⇧⌘H)")
        self._lock_action = self._action("Lock", self._on_lock, "Ctrl+L", "Lock the iPhone (⌘L)")
        self._volume_up_action = self._action("Vol +", lambda: self.input_handler.press_button('volumeUp'),
                                              tip="Volume up")
        self._volume_down_action = self._action("Vol −", lambda: self.input_handler.press_button('volumeDown'),
                                                tip="Volume down")
        self._reconnect_action = self._action("Reconnect", self._on_reconnect, "Ctrl+R",
                                              "Reconnect to the iPhone (⌘R)")
        self._sidebar_action = self._action("Sidebar", self._toggle_sidebar, "Meta+Ctrl+S",
                                            "Show or hide Doctor, Settings and Logs (⌃⌘S)")
        self._sidebar_action.setCheckable(True)

        for action in (self._home_action, self._lock_action, self._volume_down_action, self._volume_up_action):
            toolbar.addAction(action)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        toolbar.addAction(self._reconnect_action)
        toolbar.addAction(self._sidebar_action)

    def _action(self, text, slot, shortcut=None, tip=None) -> QAction:
        action = QAction(text, self)
        action.triggered.connect(slot)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        if tip:
            action.setToolTip(tip)
        return action

    def _setup_menus(self):
        menubar = self.menuBar()

        device = menubar.addMenu("&Device")
        device.addAction(self._home_action)
        device.addAction(self._lock_action)
        device.addAction(self._volume_up_action)
        device.addAction(self._volume_down_action)
        device.addSeparator()
        device.addAction(self._reconnect_action)

        view = menubar.addMenu("&View")
        view.addAction(self._action("Actual Size", lambda: self._set_zoom(1.0), "Ctrl+0"))
        larger = self._action("Larger", lambda: self._step_zoom(+1), "Ctrl++")
        larger.setShortcuts([QKeySequence("Ctrl++"), QKeySequence("Ctrl+=")])
        view.addAction(larger)
        view.addAction(self._action("Smaller", lambda: self._step_zoom(-1), "Ctrl+-"))
        view.addSeparator()
        view.addAction(self._sidebar_action)
        view.addAction(self._action("Doctor", lambda: self.show_tab(self.doctor_panel), "Ctrl+1"))
        view.addAction(self._action("Logs", lambda: self.show_tab(self.log_panel), "Ctrl+2"))

        # Lands in the app menu as “Settings…” (⌘,) on macOS
        preferences = self._action("Settings…", lambda: self.show_tab(self.settings_panel))
        preferences.setShortcut(QKeySequence.StandardKey.Preferences)
        preferences.setMenuRole(QAction.MenuRole.PreferencesRole)
        view.addAction(preferences)

        help_menu = menubar.addMenu("&Help")
        help_menu.addAction(self._action("Run Doctor", self._run_doctor))
        help_menu.addAction(self._action(
            "Show Log in Finder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.LOG_DIR)))))
        help_menu.addAction(self._action(
            "Mirror my iPhone on GitHub", lambda: QDesktopServices.openUrl(QUrl(paths.HOMEPAGE))))

    def _setup_statusbar(self):
        statusbar = QStatusBar()
        statusbar.setSizeGripEnabled(False)
        self.setStatusBar(statusbar)
        self._status_label = QLabel("Not connected")
        self._device_label = QLabel("")
        self._fps_label = QLabel("")
        self._battery_label = QLabel("")
        self._agents_label = QLabel("Agent control on")
        self._agents_label.setStyleSheet("color: #0a84ff; font-weight: 600;")
        self._agents_label.setToolTip("AI agents and scripts can control the iPhone (Settings)")
        self._agents_label.setVisible(False)
        statusbar.addWidget(self._status_label, stretch=1)
        statusbar.addPermanentWidget(self._agents_label)
        statusbar.addPermanentWidget(self._device_label)
        statusbar.addPermanentWidget(self._fps_label)
        statusbar.addPermanentWidget(self._battery_label)

    def _connect_signals(self):
        self.device_manager.device_connected.connect(self._on_device_connected)
        self.device_manager.device_disconnected.connect(self._on_device_disconnected)
        self.device_manager.connection_error.connect(self._on_connection_error)
        self.device_manager.connection_state_changed.connect(self._on_state_changed)
        self.device_manager.wda_state_changed.connect(self._on_wda_state)
        self.input_handler.wda_status_changed.connect(self._on_wda_status)
        self.doctor_panel.wda_downloaded.connect(self._on_wda_downloaded)
        self.agent_api.gesture_sent.connect(self.screen_view.show_agent_touch)
        self.settings_panel.agents_toggled.connect(self._set_agents_enabled)
        self._battery_ready.connect(self._battery_label.setText)

    # --- Window size ---

    def _fit_zoom(self) -> float:
        """Largest zoom at which the whole window still fits on its screen."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        available = screen.availableGeometry().height()
        if self.isVisible():
            chrome = self.frameGeometry().height() - self.screen_view.height()
        else:
            chrome = 150  # title bar, toolbar, margins, status bar
        return (available - chrome) / self._screen_points.height()

    def _apply_zoom(self):
        fit = self._fit_zoom()
        fitting = [level for level in self.ZOOM_LEVELS if level <= min(self._zoom, fit)]
        zoom = fitting[-1] if fitting else min(fit, self.ZOOM_LEVELS[0])
        size = QSize(round(self._screen_points.width() * zoom), round(self._screen_points.height() * zoom))
        if size != self.screen_view.size():
            self.screen_view.setFixedSize(size)
            self._banner.setFixedWidth(size.width())
            logger.debug(f"Phone shown at {zoom:.0%}: {size.width()}x{size.height()} pt")
            QTimer.singleShot(0, self._keep_on_screen)

    def _set_zoom(self, zoom: float):
        self._zoom = zoom
        self.settings.setValue('view/zoom', zoom)
        self._apply_zoom()

    def _step_zoom(self, step: int):
        shown = self.screen_view.width() / self._screen_points.width()
        index = min(range(len(self.ZOOM_LEVELS)), key=lambda i: abs(self.ZOOM_LEVELS[i] - shown))
        index = max(0, min(len(self.ZOOM_LEVELS) - 1, index + step))
        if self.ZOOM_LEVELS[index] <= self._fit_zoom() or step < 0:
            self._set_zoom(self.ZOOM_LEVELS[index])

    def _keep_on_screen(self):
        screen = self.screen()
        if screen is None or not self.isVisible():
            return
        available, frame = screen.availableGeometry(), self.frameGeometry()
        x = min(max(frame.x(), available.x()), available.right() - frame.width())
        y = min(max(frame.y(), available.y()), available.bottom() - frame.height())
        if (x, y) != (frame.x(), frame.y()):
            self.move(max(x, available.x()), max(y, available.y()))

    def _update_screen_points(self, frame_size: QSize | None = None):
        """The iPhone screen in points, in its current orientation."""
        info = self.device_manager.device_info
        width, height = info.get('screen_size') or (None, None)
        scale = self.input_handler.wda.scale or info.get('screen_scale')
        if not (width and height and scale) and frame_size is not None:
            width, height = frame_size.width(), frame_size.height()
            scale = scale or (3 if min(width, height) >= 1000 else 2)
        if width and height and scale:
            points = QSizeF(width / scale, height / scale)
            if frame_size is not None and (frame_size.width() > frame_size.height()) != (width > height):
                points.transpose()  # rotated to landscape
        else:
            points = self.DEFAULT_SCREEN
        if points != self._screen_points:
            self._screen_points = points
            self.screen_view.set_points(points)
            self._apply_zoom()

    # --- Sidebar ---

    def _restore_sidebar(self):
        self.sidebar.setCurrentIndex(int(self.settings.value('view/tab', 0)))
        self._set_sidebar_visible(self.settings.value('view/sidebar', True, type=bool))
        self.sidebar.currentChanged.connect(lambda index: self.settings.setValue('view/tab', index))

    def _set_sidebar_visible(self, visible: bool):
        self.sidebar.setVisible(visible)
        self._divider.setVisible(visible)
        self._sidebar_action.setChecked(visible)
        self.settings.setValue('view/sidebar', visible)

    def _toggle_sidebar(self):
        self._set_sidebar_visible(not self.sidebar.isVisible())

    def show_tab(self, panel: QWidget):
        self._set_sidebar_visible(True)
        self.sidebar.setCurrentWidget(panel)

    def _run_doctor(self):
        self.show_tab(self.doctor_panel)
        self.doctor_panel.run()

    def _run_doctor_on_first_launch(self):
        if not self.settings.value('doctor/first_launch_done', False, type=bool):
            self._run_doctor()
            self.settings.setValue('doctor/first_launch_done', True)

    # --- Device connection handlers ---

    def _on_device_connected(self, info: dict):
        details = [info.get('name'), info.get('model_name'), f"iOS {info.get('ios_version', '?')}"]
        self._device_label.setText(' · '.join(d for d in details if d))
        self._status_label.setText("Connected")
        self.screen_view.show_message("Waiting for the iPhone's screen…\nUnlock the iPhone if it stays dark.")
        self._update_screen_points()

        self._start_capture()
        self._start_wda_auto()
        self._battery_timer.start()
        self._update_battery()
        self.doctor_panel.refresh_soon()

    def _on_device_disconnected(self):
        self._stop_capture()
        self.agent_api.clear_frame()
        self.input_handler.forget_wda()
        self.input_handler.wda.set_device(None)
        self._battery_timer.stop()
        self._wda_retry_timer.stop()
        self._status_label.setText("Not connected")
        self._fps_label.setText("")
        self._battery_label.setText("")
        self._device_label.setText("")
        self.screen_view.show_message("Connect your iPhone with a USB cable.")
        self._update_banner()
        self.doctor_panel.refresh_soon()

    def _on_connection_error(self, msg: str):
        first_line = msg.strip().splitlines()[0] if msg.strip() else "unknown error"
        self._status_label.setText(f"Connection failed: {first_line[:60]}")
        self.screen_view.show_message(f"Couldn't connect to the iPhone:\n{first_line}\n\nRetrying… "
                                      "See the Doctor tab for help.")

    def _on_state_changed(self, state: ConnectionState):
        labels = {
            ConnectionState.DISCONNECTED: "Not connected",
            ConnectionState.CONNECTING: "Connecting…",
            ConnectionState.CONNECTED: "Connected",
            ConnectionState.ERROR: "Connection failed",
        }
        if state == ConnectionState.CONNECTING and not self.screen_view.has_frame:
            self.screen_view.show_message("Connecting…\nIf the iPhone asks, tap Trust.")
        if state != ConnectionState.ERROR:
            self._status_label.setText(labels.get(state, "?"))

    # --- Screen capture ---

    def _start_capture(self):
        if self.capture_thread and self.capture_thread.isRunning():
            self.capture_thread.stop()

        self.capture_thread = ScreenCaptureThread(self.device_manager)
        self.capture_thread.frame_ready.connect(self._on_frame)
        self.capture_thread.fps_updated.connect(self._on_fps)
        self.capture_thread.mode_changed.connect(self._on_capture_mode)
        self.capture_thread.capture_error.connect(self._on_capture_error)
        self.capture_thread.start()

    def _stop_capture(self):
        if self.capture_thread:
            self.capture_thread.stop()
            self.capture_thread = None

    def _on_frame(self, image: QImage):
        if not self.screen_view.has_frame or image.size() != self._last_frame_size:
            self._last_frame_size = image.size()
            self._update_screen_points(image.size())
        self.screen_view.update_frame(image)
        self.agent_api.set_frame(image)

    def _on_fps(self, fps: float):
        self._fps_label.setText(f"{fps:.0f} FPS ({self._capture_mode})")
        self.agent_api.fps = fps

    def _on_capture_mode(self, mode: str):
        self._capture_mode = mode
        self.agent_api.capture_mode = mode

    def _on_capture_error(self, msg: str):
        logger.warning(f"Capture error: {msg}")
        self._status_label.setText(f"Capture stopped: {msg[:60]}")
        self.screen_view.show_message(f"{msg}\n\nUse Device › Reconnect to try again.")

    # --- WebDriverAgent (touch control) ---

    def _start_wda_auto(self):
        """xcodebuild (in the background), then keep trying to open a session. WDA is reached over
        usbmux from inside this process, so no port is opened on the Mac."""
        self.input_handler.wda.set_device(self.device_manager.device_info.get('udid'), self.device_manager.wda_port)
        threading.Thread(target=self.device_manager.start_wda, daemon=True).start()
        self._wda_retry_timer.start()
        self._update_banner()

    def _on_wda_state(self, state: str, _message: str):
        if state == 'running':  # WDA may have picked another port on the iPhone than the default
            wda = self.input_handler.wda
            if not wda.is_connected:
                wda.set_device(self.device_manager.device_info.get('udid'), self.device_manager.wda_port)
        self._update_banner()

    def _on_wda_downloaded(self):
        if self.device_manager.is_connected and not self.input_handler.wda.is_connected:
            self._start_wda_auto()

    def _try_wda(self):
        if not self.input_handler.wda.is_connected:
            self.input_handler.try_connect_wda()

    def _on_wda_status(self, connected: bool):
        if connected:
            self._wda_retry_timer.stop()
            logger.info("WDA connected — touch control enabled")
            self._update_screen_points(self._last_frame_size if self.screen_view.has_frame else None)
        elif self.device_manager.is_connected:
            self._wda_retry_timer.start()
        self._update_banner()
        self.doctor_panel.refresh_soon()

    def _update_banner(self):
        """Explain why touch control isn't available (yet)."""
        state, message = self.device_manager.wda_state
        waiting = False  # the spinner only shows while something is in progress, not when it needs a fix
        if self.input_handler.wda.is_connected or not self.device_manager.is_connected:
            text = None
        elif state in ('failed', 'unavailable'):
            text = (f"⚠ <b>Touch control unavailable.</b> {message} "
                    "<a href='#' style='color: #ffd60a; font-weight: bold;'>Open Doctor</a>")
        elif state == 'running':
            text, waiting = "<b>Touch not ready</b> — connecting to WebDriverAgent…", True
        else:
            text, waiting = f"<b>Touch not ready</b> — {message or 'starting WebDriverAgent…'}", True
        if text:
            self._banner_text.setText(text)
            self._banner_spinner.setVisible(waiting)
        if bool(text) != self._banner.isVisible():
            self._banner.setVisible(bool(text))
            QTimer.singleShot(0, self._apply_zoom)  # the banner takes height from the phone

    # --- Battery ---

    def _update_battery(self):
        def fetch():
            text = ""
            try:
                info = self.input_handler.wda.get_battery_info() if self.input_handler.wda.is_connected else {}
                if info.get('level', -1) >= 0:
                    text = f"Battery {info['level']}%{' ⚡' if info.get('state') == 2 else ''}"
                elif self.device_manager.is_connected:
                    info = self.device_manager.get_battery_info()
                    if info.get('level', -1) >= 0:
                        text = f"Battery {info['level']}%{' ⚡' if info.get('charging') else ''}"
            except Exception as e:
                logger.debug(f"Battery update failed: {e}")
            if text:
                self._battery_ready.emit(text)

        threading.Thread(target=fetch, daemon=True).start()

    # --- Buttons ---

    def _on_home(self):
        self.input_handler.go_home()

    def _on_lock(self):
        self.input_handler.lock_device()

    def _on_reconnect(self):
        self._on_device_disconnected()
        self.device_manager.disconnect()
        self.device_manager.start_discovery()

    # --- Agent control ---

    def _set_agents_enabled(self, enabled: bool):
        """Start or stop the agent API (and with it the MCP server's and CLI's device control)."""
        if enabled:
            self.agent_api.start()
        else:
            self.agent_api.stop()
        self._agents_label.setVisible(self.agent_api.is_running)

    # --- Window lifecycle ---

    def closeEvent(self, event):
        self.agent_api.stop()
        self._stop_capture()
        self.input_handler.cleanup()
        self.device_manager.cleanup()
        super().closeEvent(event)
