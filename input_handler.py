"""
Input Handler — turns mouse and trackpad input on the mirrored screen into iPhone touches,
sent through the WebDriverAgent (WDA) HTTP API.

    click              → tap, sent right away (each click of a double click is its own tap)
    click and hold     → long press (right-click does the same)
    drag               → swipe along the same path (long drags are replayed faster,
                         keeping the speed at release)
    scroll / trackpad  → short swipes in the scroll direction

Positions arrive in iPhone points (ScreenView maps them). WDA requests run one at a time,
in order, on a worker thread, so the UI never waits for the device.

Every gesture is sent as W3C actions. WDA reads the foreground app's accessibility tree once
per touch point to place it, so the fewer points a gesture has, the sooner it runs.
"""

import logging
import math
import queue
import threading
import time
from enum import Enum

import requests
from PyQt6.QtCore import QObject, QPointF, QSizeF, QTimer, pyqtSignal

import paths

logger = logging.getLogger(__name__)


class WDAError(Exception):
    pass


class WDAClient:
    """Minimal WebDriverAgent client. Gestures are queued and sent in order on one worker thread."""

    def __init__(self, base_url: str = f'http://127.0.0.1:{paths.WDA_PORT}', on_disconnect=None):
        self.base_url = base_url
        self.session_id: str | None = None
        self.scale: float | None = None  # iPhone pixels per point, from /wda/screen
        self._on_disconnect = on_disconnect
        self._http = requests.Session()
        self._actions: queue.Queue = queue.Queue()
        threading.Thread(target=self._run_actions, name='wda-actions', daemon=True).start()

    @property
    def is_connected(self) -> bool:
        return self.session_id is not None

    @property
    def is_idle(self) -> bool:
        """No gesture queued or in flight."""
        return self._actions.unfinished_tasks == 0

    def connect(self) -> bool:
        """Create a WDA session (blocking). Returns True on success."""
        try:
            self._http.get(f'{self.base_url}/status', timeout=3).raise_for_status()
            value = self._call('POST', '/session', {'capabilities': {'alwaysMatch': {
                # Don't wait for the app to settle before every gesture — that's what makes WDA feel slow
                'waitForIdleTimeout': 0,
                'shouldWaitForQuiescence': False,
            }}}, session=False, timeout=30)
            self.session_id = value.get('sessionId')
            if not self.session_id:
                raise WDAError(f"no session in response: {value}")
            self._call('POST', '/appium/settings', {'settings': {
                'waitForIdleTimeout': 0, 'animationCoolOffTimeout': 0,
                # Placing a touch point makes XCTest snapshot the foreground app's accessibility
                # tree to this depth (50 by default), but gestures only need the app's frame. Full
                # snapshots took ~100 ms per point: over 1 s per tap, up to 20 s per swipe.
                'snapshotMaxDepth': 0,
            }})
            self.scale = float(self._call('GET', '/wda/screen').get('scale') or 0) or None
            logger.info(f"WDA session {self.session_id} (screen scale {self.scale})")
            return True
        except requests.ConnectionError:
            logger.debug("WDA not reachable")
        except Exception as e:
            logger.warning(f"WDA connection failed: {e}")
        self.session_id = None
        return False

    def disconnect(self):
        if self.session_id:
            try:
                self._call('DELETE', '', timeout=3)
            except Exception:
                pass
            self.session_id = None

    def _call(self, method: str, path: str, payload: dict | None = None,
              session: bool = True, timeout: float = 10):
        """Send a request and return its `value`. Raises WDAError if WDA reports an error."""
        if session:
            if not self.session_id:
                raise WDAError("no WDA session")
            path = f'/session/{self.session_id}{path}'
        resp = self._http.request(method, f'{self.base_url}{path}', json=payload, timeout=timeout)
        try:
            value = resp.json().get('value')
        except ValueError:
            value = None
        if resp.status_code >= 400:
            error = value if isinstance(value, dict) else {}
            if error.get('error') == 'invalid session id':
                self.session_id = None
            raise WDAError(f"{method} {path}: {error.get('message') or resp.status_code}")
        return value if value is not None else {}

    def _run_actions(self):
        while True:
            name, call = self._actions.get()
            had_session = self.session_id is not None
            try:
                started = time.monotonic()
                call()
                logger.debug(f"WDA {name} took {(time.monotonic() - started) * 1000:.0f} ms")
            except WDAError as e:
                logger.warning(f"WDA {name} failed: {e}")
            except requests.ConnectionError:
                logger.error(f"WDA connection lost during {name}")
                self.session_id = None
            except Exception as e:
                logger.error(f"WDA {name} failed: {e}")
            finally:
                self._actions.task_done()
            if had_session and self.session_id is None and self._on_disconnect:
                self._on_disconnect()

    def _enqueue(self, name: str, *args, **kwargs):
        self._actions.put((name, lambda: self._call(*args, **kwargs)))

    def _touch(self, name: str, actions: list[dict], timeout: float = 15):
        """Queue one finger's W3C actions. Taps go this way too: /wda/tap and its siblings
        place the touch point four or five times over, each time with a snapshot."""
        payload = {'actions': [{
            'type': 'pointer', 'id': 'finger1', 'parameters': {'pointerType': 'touch'}, 'actions': actions,
        }]}
        self._enqueue(name, 'POST', '/actions', payload, timeout=timeout)

    # --- Gestures (non-blocking; coordinates in points) ---

    TAP_MS = 50  # how long the finger stays down in a tap

    def tap(self, x: float, y: float):
        self._touch('tap', [_move(x, y), _DOWN, _pause(self.TAP_MS), _UP])

    def long_press(self, x: float, y: float, duration: float = 1.0):
        self._touch('long press', [_move(x, y), _DOWN, _pause(round(duration * 1000)), _UP],
                    timeout=duration + 15)

    def swipe(self, path: list[tuple[float, float, float]]):
        """Move a finger along (x, y, seconds) samples, in straight lines between them."""
        (x0, y0, t0), rest = path[0], path[1:]
        actions = [_move(x0, y0), _DOWN]
        previous = t0
        for x, y, t in rest:
            actions.append(_move(x, y, max(1, round((t - previous) * 1000))))
            previous = t
        actions.append(_UP)
        self._touch('swipe', actions, timeout=(previous - t0) + 15)

    def press_button(self, name: str):
        """'home', 'volumeUp' or 'volumeDown'."""
        self._enqueue(f'{name} button', 'POST', '/wda/pressButton', {'name': name})

    def home_screen(self):
        self._enqueue('home', 'POST', '/wda/homescreen', {}, session=False)

    def lock(self):
        self._enqueue('lock', 'POST', '/wda/lock', {}, session=False)

    def get_battery_info(self) -> dict:
        """Blocking — call off the UI thread."""
        try:
            value = self._call('GET', '/wda/batteryInfo', timeout=3)
            return {'level': int(value.get('level', -1) * 100), 'state': value.get('state', 0)}
        except Exception:
            return {}


class _Gesture(Enum):
    IDLE = 0
    PRESSED = 1
    DRAGGING = 2
    LONG_PRESSING = 3


class InputHandler(QObject):
    """Recognizes taps, long presses, drags and scrolls and sends them to WDA."""

    wda_status_changed = pyqtSignal(bool)
    _wda_connected = pyqtSignal(bool)  # from worker threads

    LONG_PRESS_MS = 500
    DRAG_THRESHOLD = 6         # points before a press becomes a drag
    SAMPLE_INTERVAL = 0.012     # seconds between recorded drag samples
    MAX_SWIPE_POINTS = 5        # per swipe sent to WDA, which spends 30-150 ms placing each point
    # The iPhone only gets a drag once the mouse is released, so it's replayed faster: the
    # last LIFT_OFF seconds keep their timing (the speed at release decides whether content
    # keeps scrolling), everything before is squeezed into at most MAX_DRAG_LEAD seconds
    LIFT_OFF = 0.08
    MAX_DRAG_LEAD = 0.2
    SCROLL_BATCH_MS = 120       # scroll events are summed into one swipe per batch
    SCROLL_MAX_FRACTION = 0.6   # longest scroll swipe, as a fraction of the screen

    def __init__(self, parent=None):
        super().__init__(parent)
        self.wda = WDAClient(on_disconnect=lambda: self._wda_connected.emit(False))
        self._wda_connected.connect(self.wda_status_changed)
        self._connecting = threading.Lock()
        self._screen = QSizeF(393, 852)  # iPhone screen in points

        self._state = _Gesture.IDLE
        self._press_pos = QPointF()
        self._path: list[tuple[float, float, float]] = []

        self._long_press_timer = QTimer(self, singleShot=True, interval=self.LONG_PRESS_MS)
        self._long_press_timer.timeout.connect(self._on_long_press)

        self._scroll_origin = QPointF()
        self._scroll_delta = QPointF()
        self._scroll_timer = QTimer(self, singleShot=True, interval=self.SCROLL_BATCH_MS)
        self._scroll_timer.timeout.connect(self._flush_scroll)

    # --- WDA connection ---

    def try_connect_wda(self):
        """Try to open a WDA session in the background; emits wda_status_changed(True) on success.
        (Failed attempts don't emit — wda_status_changed only reports changes.)"""
        if self._connecting.locked():
            return

        def connect():
            with self._connecting:
                if self.wda.connect():
                    self._wda_connected.emit(True)

        threading.Thread(target=connect, daemon=True).start()

    def forget_wda(self):
        """Drop the WDA session without talking to WDA, e.g. after the iPhone was unplugged."""
        if self.wda.session_id:
            self.wda.session_id = None
            self.wda_status_changed.emit(False)

    def set_screen_points(self, size: QSizeF):
        self._screen = size

    # --- Mouse ---

    def press(self, pos: QPointF, right_button: bool = False):
        if not self.wda.is_connected:
            return
        if right_button:
            self.wda.long_press(pos.x(), pos.y())
            return
        self._state = _Gesture.PRESSED
        self._press_pos = pos
        self._path = [(pos.x(), pos.y(), time.monotonic())]
        self._long_press_timer.start()

    def move(self, pos: QPointF):
        if self._state not in (_Gesture.PRESSED, _Gesture.DRAGGING):
            return
        now = time.monotonic()
        if now - self._path[-1][2] >= self.SAMPLE_INTERVAL:
            self._path.append((pos.x(), pos.y(), now))
        if self._state == _Gesture.PRESSED and _distance(pos, self._press_pos) > self.DRAG_THRESHOLD:
            self._state = _Gesture.DRAGGING
            self._long_press_timer.stop()

    def release(self, pos: QPointF):
        self._long_press_timer.stop()
        state, self._state = self._state, _Gesture.IDLE
        if state == _Gesture.PRESSED:
            self.wda.tap(self._press_pos.x(), self._press_pos.y())
        elif state == _Gesture.DRAGGING:
            self._path.append((pos.x(), pos.y(), time.monotonic()))
            path = _speed_up(self._path, self.MAX_DRAG_LEAD, self.LIFT_OFF)
            self.wda.swipe(_simplify(path, self.MAX_SWIPE_POINTS, self.LIFT_OFF))

    def _on_long_press(self):
        if self._state != _Gesture.PRESSED:
            return
        self._state = _Gesture.LONG_PRESSING
        self.wda.long_press(self._press_pos.x(), self._press_pos.y())

    # --- Scroll wheel / trackpad ---

    def scroll(self, pos: QPointF, delta: QPointF):
        """`delta` is how far the content should move, in points (positive = down/right)."""
        if not self.wda.is_connected:
            return
        if not self._scroll_timer.isActive() and self._scroll_delta.isNull():
            self._scroll_origin = pos
        self._scroll_delta += delta
        if not self._scroll_timer.isActive():
            self._scroll_timer.start()

    def _flush_scroll(self):
        delta, self._scroll_delta = self._scroll_delta, QPointF()
        if math.hypot(delta.x(), delta.y()) < 2:
            return
        if not self.wda.is_idle:
            # Still busy with the previous swipe: keep collecting instead of queueing more
            self._scroll_delta = delta
            self._scroll_timer.start()
            return
        w, h = self._screen.width(), self._screen.height()
        limit = self.SCROLL_MAX_FRACTION
        dx = _clamp(delta.x(), -w * limit, w * limit)
        dy = _clamp(delta.y(), -h * limit, h * limit)
        # Start away from the edges, where swipes would open Notification Center or go home,
        # and so that the whole swipe stays on screen
        x0 = _clamp(self._scroll_origin.x(), w * 0.1 + max(0, -dx), w * 0.9 - max(0, dx))
        y0 = _clamp(self._scroll_origin.y(), h * 0.15 + max(0, -dy), h * 0.85 - max(0, dy))
        self.wda.swipe([(x0, y0, 0), (x0 + dx, y0 + dy, self.SCROLL_BATCH_MS / 1000)])

    # --- Hardware buttons ---

    def press_button(self, name: str):
        self.wda.press_button(name)

    def go_home(self):
        self.wda.home_screen()

    def lock_device(self):
        self.wda.lock()

    def cleanup(self):
        self.wda.disconnect()


def _distance(a: QPointF, b: QPointF) -> float:
    return math.hypot(a.x() - b.x(), a.y() - b.y())


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value)) if low <= high else (low + high) / 2


def _move(x: float, y: float, ms: int = 0) -> dict:
    return {'type': 'pointerMove', 'duration': ms, 'x': x, 'y': y}


def _pause(ms: int) -> dict:
    return {'type': 'pause', 'duration': ms}


_DOWN = {'type': 'pointerDown', 'button': 0}
_UP = {'type': 'pointerUp', 'button': 0}

Path = list[tuple[float, float, float]]  # (x, y, seconds) samples


def _speed_up(path: Path, max_lead: float, tail: float) -> Path:
    """Keep the timing of the last `tail` seconds and squeeze everything before into `max_lead`."""
    start, end = path[0][2], path[-1][2]
    split = max(start, end - tail)
    factor = min(1.0, max_lead / (split - start)) if split > start else 1.0
    return [(x, y, start + (min(t, split) - start) * factor + max(0.0, t - split)) for x, y, t in path]


def _simplify(path: Path, limit: int, lift_off: float) -> Path:
    """Keep at most `limit` samples: the first and last, the last one at least `lift_off` seconds
    before the end (so the speed at release stays the same), and evenly spaced ones before it."""
    if len(path) <= limit:
        return path
    end = path[-1][2]
    anchor = max((i for i, (_, _, t) in enumerate(path[:-1]) if end - t >= lift_off), default=0)
    return _thin_out(path[:anchor + 1], limit - 1) + [path[-1]]


def _thin_out(path: Path, limit: int) -> Path:
    """Keep at most `limit` samples, evenly spread, always keeping the first and last."""
    if len(path) <= limit:
        return path
    step = (len(path) - 1) / (limit - 1)
    return [path[round(i * step)] for i in range(limit)]
