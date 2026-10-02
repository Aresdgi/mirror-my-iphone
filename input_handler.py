"""
Input Handler — translates mouse events to iPhone touch coordinates
and sends them to the device via WebDriverAgent HTTP API.
All WDA calls run in background threads to avoid blocking the UI.
"""

import logging
import threading
import time
from enum import Enum

import requests
from PyQt6.QtCore import QObject, QTimer, QPointF, pyqtSignal

logger = logging.getLogger(__name__)


class GestureState(Enum):
    IDLE = 0
    PRESSED = 1
    DRAGGING = 2
    LONG_PRESSING = 3


def _fire_and_forget(fn):
    """Run a function in a daemon thread (non-blocking)."""
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()


class WDAClient:
    """HTTP client for WebDriverAgent REST API. All actions are non-blocking."""

    def __init__(self, base_url: str = 'http://localhost:8100'):
        self.base_url = base_url.rstrip('/')
        self.session_id = None
        self._session = requests.Session()
        self._screen_scale = 3
        self._screen_size = None
        self._lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        return self.session_id is not None

    def connect(self) -> bool:
        """Create a WDA session. Returns True on success."""
        try:
            resp = self._session.get(f'{self.base_url}/status', timeout=3)
            if resp.status_code != 200:
                return False

            resp = self._session.post(f'{self.base_url}/session', json={
                'capabilities': {}
            }, timeout=10)
            data = resp.json()
            self.session_id = data.get('sessionId')

            if not self.session_id and 'value' in data:
                self.session_id = data['value'].get('sessionId')

            self._fetch_screen_info()
            logger.info(f"WDA session created: {self.session_id}")
            return True

        except requests.ConnectionError:
            logger.debug("WDA not reachable")
            return False
        except Exception as e:
            logger.error(f"WDA connection failed: {e}")
            return False

    def _fetch_screen_info(self):
        """Get screen scale factor from WDA."""
        try:
            resp = self._session.get(
                f'{self.base_url}/session/{self.session_id}/wda/screen',
                timeout=5
            )
            data = resp.json()
            if 'value' in data:
                self._screen_scale = data['value'].get('scale', 3)
                self._screen_size = {
                    'width': data['value'].get('width', 390),
                    'height': data['value'].get('height', 844),
                }
            logger.info(f"Screen scale: {self._screen_scale}, size: {self._screen_size}")
        except Exception as e:
            logger.debug(f"Screen info fetch failed: {e}")

    @property
    def screen_scale(self) -> int:
        return self._screen_scale

    def disconnect(self):
        if self.session_id:
            try:
                self._session.delete(
                    f'{self.base_url}/session/{self.session_id}',
                    timeout=3
                )
            except Exception:
                pass
            self.session_id = None

    def _post(self, path: str, json_data: dict, timeout: float = 10):
        """Thread-safe POST to WDA. Longer timeout to avoid false failures."""
        if not self.session_id:
            return
        with self._lock:
            try:
                self._session.post(
                    f'{self.base_url}/session/{self.session_id}/{path}',
                    json=json_data,
                    timeout=timeout,
                )
            except requests.Timeout:
                logger.warning(f"WDA timeout: {path}")
            except requests.ConnectionError:
                logger.error(f"WDA connection lost: {path}")
                self.session_id = None
            except Exception as e:
                logger.error(f"WDA error ({path}): {e}")

    def tap(self, x: float, y: float):
        _fire_and_forget(lambda: self._post('wda/tap/0', {'x': x, 'y': y}))

    def long_press(self, x: float, y: float, duration: float = 1.0):
        _fire_and_forget(lambda: self._post(
            'wda/touchAndHold', {'x': x, 'y': y, 'duration': duration}, timeout=15
        ))

    def swipe(self, x1: float, y1: float, x2: float, y2: float, duration: float = 0.3):
        _fire_and_forget(lambda: self._post(
            'wda/dragfromtoforduration',
            {'fromX': x1, 'fromY': y1, 'toX': x2, 'toY': y2, 'duration': duration},
            timeout=10,
        ))

    def press_button(self, name: str):
        _fire_and_forget(lambda: self._post('wda/pressButton', {'name': name}))

    def home_screen(self):
        _fire_and_forget(lambda: self._post('wda/homescreen', {}))

    def lock(self):
        _fire_and_forget(lambda: self._post('wda/lock', {}))

    def unlock(self):
        _fire_and_forget(lambda: self._post('wda/unlock', {}))

    def get_battery_info(self) -> dict:
        if not self.session_id:
            return {}
        try:
            resp = self._session.get(
                f'{self.base_url}/session/{self.session_id}/wda/batteryInfo',
                timeout=5
            )
            data = resp.json()
            value = data.get('value', {})
            return {
                'level': int(value.get('level', -1) * 100),
                'state': value.get('state', 0),
            }
        except Exception:
            return {}


class InputHandler(QObject):
    """Translates mouse events on the screen view to iPhone touch events."""

    wda_status_changed = pyqtSignal(bool)

    TAP_MAX_DURATION = 0.3
    LONG_PRESS_DELAY = 500
    DRAG_THRESHOLD = 15

    def __init__(self, parent=None):
        super().__init__(parent)
        self.wda = WDAClient()

        self._state = GestureState.IDLE
        self._press_pos = QPointF(0, 0)
        self._press_time = 0.0
        self._iphone_press_pos = (0.0, 0.0)

        self._iphone_width = 1170
        self._iphone_height = 2532

        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.timeout.connect(self._on_long_press_timeout)

    def try_connect_wda(self) -> bool:
        connected = self.wda.connect()
        self.wda_status_changed.emit(connected)
        return connected

    def update_screen_size(self, width: int, height: int):
        self._iphone_width = width
        self._iphone_height = height

    def translate_coordinates(
        self,
        mouse_x: float, mouse_y: float,
        label_width: float, label_height: float
    ) -> tuple[float, float] | None:
        """Convert mouse position on QLabel to iPhone logical points.
        Returns None if outside the displayed image area.
        """
        iphone_w = self._iphone_width
        iphone_h = self._iphone_height

        iphone_aspect = iphone_w / iphone_h
        label_aspect = label_width / label_height

        if label_aspect > iphone_aspect:
            display_height = label_height
            display_width = label_height * iphone_aspect
            offset_x = (label_width - display_width) / 2
            offset_y = 0
        else:
            display_width = label_width
            display_height = label_width / iphone_aspect
            offset_x = 0
            offset_y = (label_height - display_height) / 2

        rel_x = mouse_x - offset_x
        rel_y = mouse_y - offset_y

        if rel_x < 0 or rel_x > display_width or rel_y < 0 or rel_y > display_height:
            return None

        pixel_x = (rel_x / display_width) * iphone_w
        pixel_y = (rel_y / display_height) * iphone_h

        scale = self.wda.screen_scale
        return (pixel_x / scale, pixel_y / scale)

    def on_mouse_press(self, mouse_x: float, mouse_y: float,
                       label_width: float, label_height: float):
        if not self.wda.is_connected:
            return
        coords = self.translate_coordinates(mouse_x, mouse_y, label_width, label_height)
        if coords is None:
            return

        self._state = GestureState.PRESSED
        self._press_pos = QPointF(mouse_x, mouse_y)
        self._press_time = time.time()
        self._iphone_press_pos = coords
        self._long_press_timer.start(self.LONG_PRESS_DELAY)

    def on_mouse_move(self, mouse_x: float, mouse_y: float,
                      label_width: float, label_height: float):
        if self._state == GestureState.IDLE:
            return
        dx = mouse_x - self._press_pos.x()
        dy = mouse_y - self._press_pos.y()
        distance = (dx * dx + dy * dy) ** 0.5
        if self._state == GestureState.PRESSED and distance > self.DRAG_THRESHOLD:
            self._state = GestureState.DRAGGING
            self._long_press_timer.stop()

    def on_mouse_release(self, mouse_x: float, mouse_y: float,
                         label_width: float, label_height: float):
        if not self.wda.is_connected:
            self._state = GestureState.IDLE
            return

        self._long_press_timer.stop()
        coords = self.translate_coordinates(mouse_x, mouse_y, label_width, label_height)

        if self._state == GestureState.PRESSED:
            elapsed = time.time() - self._press_time
            if elapsed < self.TAP_MAX_DURATION:
                x, y = self._iphone_press_pos
                self.wda.tap(x, y)

        elif self._state == GestureState.DRAGGING and coords:
            x1, y1 = self._iphone_press_pos
            x2, y2 = coords
            self.wda.swipe(x1, y1, x2, y2, duration=0.3)

        self._state = GestureState.IDLE

    def _on_long_press_timeout(self):
        if self._state == GestureState.PRESSED:
            self._state = GestureState.LONG_PRESSING
            x, y = self._iphone_press_pos
            self.wda.long_press(x, y, duration=1.0)

    def on_scroll(self, mouse_x: float, mouse_y: float,
                  delta_x: float, delta_y: float,
                  label_width: float, label_height: float):
        if not self.wda.is_connected:
            return
        coords = self.translate_coordinates(mouse_x, mouse_y, label_width, label_height)
        if coords is None:
            return

        x, y = coords
        swipe_distance = 100

        if abs(delta_y) > abs(delta_x):
            if delta_y > 0:
                self.wda.swipe(x, y - swipe_distance, x, y + swipe_distance, duration=0.3)
            else:
                self.wda.swipe(x, y + swipe_distance, x, y - swipe_distance, duration=0.3)
        else:
            if delta_x > 0:
                self.wda.swipe(x - swipe_distance, y, x + swipe_distance, y, duration=0.3)
            else:
                self.wda.swipe(x + swipe_distance, y, x - swipe_distance, y, duration=0.3)

    def press_button(self, name: str):
        self.wda.press_button(name)

    def go_home(self):
        self.wda.home_screen()

    def lock_device(self):
        self.wda.lock()

    def cleanup(self):
        self.wda.disconnect()
