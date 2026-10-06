"""
Input Handler — turns mouse and trackpad input on the mirrored screen into iPhone touches,
sent through the WebDriverAgent (WDA) HTTP API, which the app reaches over usbmux from inside its
own process (usbmux_http.py): no port on the Mac leads to WDA.

    click              → tap, sent right away (each click of a double click is its own tap)
    click and hold     → long press (right-click does the same)
    drag               → swipe along the same path (long drags are replayed faster,
                         keeping the speed at release)
    scroll / trackpad  → short swipes in the scroll direction

Positions arrive in iPhone points (ScreenView maps them). WDA requests run one at a time,
in order, on a worker thread, so the UI never waits for the device. Each queued request returns
a Future, which the agent API (agent_api.py) waits on; the UI ignores it.

Every gesture is sent as W3C actions. WDA reads the foreground app's accessibility tree once
per touch point to place it, so the fewer points a gesture has, the sooner it runs.
"""

import base64
import json
import logging
import math
import queue
import threading
import time
from concurrent.futures import Future
from enum import Enum

from PyQt6.QtCore import QObject, QPointF, QSizeF, QTimer, pyqtSignal

import paths
from usbmux_http import USBConnectionError, USBHTTPClient

logger = logging.getLogger(__name__)


class WDAError(Exception):
    pass


class WDAClient:
    """Minimal WebDriverAgent client. Gestures are queued and sent in order on one worker thread."""

    SNAPSHOT_DEPTH = 50  # WDA's default, for requests that need the accessibility tree
    # Attributes ui_tree() leaves out: working out visibility and accessibility made it 6x slower
    # (10 s for the Home Screen), and the agent API filters by frame instead
    UI_TREE_EXCLUDED = ('visible,accessible,nativeAccessibilityElement,traits,nativeFrame,frame,focused,'
                        'placeholderValue,minValue,maxValue')

    def __init__(self, on_disconnect=None):
        self.session_id: str | None = None
        self.scale: float | None = None  # iPhone pixels per point, from /wda/screen
        self._on_disconnect = on_disconnect
        self._http = USBHTTPClient(paths.WDA_PORT)
        self._actions: queue.Queue = queue.Queue()
        threading.Thread(target=self._run_actions, name='wda-actions', daemon=True).start()

    @property
    def is_connected(self) -> bool:
        return self.session_id is not None

    @property
    def is_idle(self) -> bool:
        """No gesture queued or in flight."""
        return self._actions.unfinished_tasks == 0

    def set_device(self, udid: str | None, port: int | None = None):
        """Talk to WDA on this iPhone (None: on none), on its port on the iPhone if given."""
        self.session_id = None
        self._http.set_device(udid, port)

    def connect(self) -> bool:
        """Create a WDA session (blocking). Returns True on success."""
        try:
            self._call('GET', '/status', session=False, timeout=3)
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
        except (USBConnectionError, TimeoutError):
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
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {'Content-Type': 'application/json'} if body is not None else {}
        status, data = self._http.request(method, path, body=body, headers=headers, timeout=timeout)
        try:
            value = json.loads(data).get('value')
        except (ValueError, AttributeError):
            value = None
        if status >= 400:
            error = value if isinstance(value, dict) else {}
            if error.get('error') == 'invalid session id':
                self.session_id = None
            raise WDAError(f"{method} {path}: {error.get('message') or status}")
        return value if value is not None else {}

    def _run_actions(self):
        while True:
            name, call, future = self._actions.get()
            had_session = self.session_id is not None
            try:
                started = time.monotonic()
                future.set_result(call())
                logger.debug(f"WDA {name} took {(time.monotonic() - started) * 1000:.0f} ms")
            except WDAError as e:
                logger.warning(f"WDA {name} failed: {e}")
                future.set_exception(e)
            except USBConnectionError:
                logger.error(f"WDA connection lost during {name}")
                self.session_id = None
                future.set_exception(WDAError("lost the connection to WebDriverAgent"))
            except Exception as e:
                logger.error(f"WDA {name} failed: {e}")
                future.set_exception(e)
            finally:
                self._actions.task_done()
            if had_session and self.session_id is None and self._on_disconnect:
                self._on_disconnect()

    def _submit(self, name: str, call) -> Future:
        """Queue `call` for the worker thread. The Future holds its result or exception."""
        future = Future()
        self._actions.put((name, call, future))
        return future

    def _enqueue(self, name: str, *args, **kwargs) -> Future:
        return self._submit(name, lambda: self._call(*args, **kwargs))

    def _touch(self, name: str, actions: list[dict], timeout: float = 15) -> Future:
        """Queue one finger's W3C actions. Taps go this way too: /wda/tap and its siblings
        place the touch point four or five times over, each time with a snapshot."""
        payload = {'actions': [{
            'type': 'pointer', 'id': 'finger1', 'parameters': {'pointerType': 'touch'}, 'actions': actions,
        }]}
        return self._enqueue(name, 'POST', '/actions', payload, timeout=timeout)

    def _with_snapshots(self, call):
        """Wrap `call` to run with accessibility snapshots, which connect() turns off for gestures."""
        def run():
            self._call('POST', '/appium/settings', {'settings': {'snapshotMaxDepth': self.SNAPSHOT_DEPTH}})
            try:
                return call()
            finally:
                self._call('POST', '/appium/settings', {'settings': {'snapshotMaxDepth': 0}})
        return run

    # --- Gestures (non-blocking; coordinates in points) ---

    TAP_MS = 50  # how long the finger stays down in a tap
    DOUBLE_TAP_GAP_MS = 100  # between the two taps of a double tap

    def tap(self, x: float, y: float) -> Future:
        return self._touch('tap', [_move(x, y), _DOWN, _pause(self.TAP_MS), _UP])

    def double_tap(self, x: float, y: float) -> Future:
        """Both taps in one gesture, so iOS sees them close enough together to count as a double tap."""
        tap = [_DOWN, _pause(self.TAP_MS), _UP]
        return self._touch('double tap', [_move(x, y), *tap, _pause(self.DOUBLE_TAP_GAP_MS), *tap])

    def long_press(self, x: float, y: float, duration: float = 1.0) -> Future:
        return self._touch('long press', [_move(x, y), _DOWN, _pause(round(duration * 1000)), _UP],
                           timeout=duration + 15)

    def swipe(self, path: list[tuple[float, float, float]]) -> Future:
        """Move a finger along (x, y, seconds) samples, in straight lines between them."""
        (x0, y0, t0), rest = path[0], path[1:]
        actions = [_move(x0, y0), _DOWN]
        previous = t0
        for x, y, t in rest:
            actions.append(_move(x, y, max(1, round((t - previous) * 1000))))
            previous = t
        actions.append(_UP)
        return self._touch('swipe', actions, timeout=(previous - t0) + 15)

    def press_button(self, name: str) -> Future:
        """'home', 'volumeUp' or 'volumeDown'."""
        return self._enqueue(f'{name} button', 'POST', '/wda/pressButton', {'name': name})

    def home_screen(self) -> Future:
        return self._enqueue('home', 'POST', '/wda/homescreen', {}, session=False)

    def lock(self) -> Future:
        return self._enqueue('lock', 'POST', '/wda/lock', {}, session=False)

    # --- For the agent API (non-blocking) ---

    def type_text(self, text: str) -> Future:
        """Type into the focused text field. WDA finds the keyboard through a snapshot."""
        return self._submit('type', self._with_snapshots(
            lambda: self._call('POST', '/wda/keys', {'value': list(text)}, timeout=30 + len(text) / 5)))

    def activate_app(self, bundle_id: str) -> Future:
        """Bring the app to the front, launching it if needed (without restarting it)."""
        return self._enqueue('open app', 'POST', '/wda/apps/activate', {'bundleId': bundle_id}, timeout=30)

    def ui_tree(self) -> Future:
        """The foreground app's accessibility tree, as WDA's JSON source."""
        return self._submit('ui tree', self._with_snapshots(lambda: self._call(
            'GET', f'/source?format=json&excluded_attributes={self.UI_TREE_EXCLUDED}', timeout=60)))

    def active_app(self) -> Future:
        """The foreground app: {'bundleId', 'name', 'pid', ...}."""
        return self._enqueue('active app', 'GET', '/wda/activeAppInfo', session=False)

    def screenshot(self) -> Future:
        """PNG of the screen in pixels."""
        return self._submit('screenshot', lambda: base64.b64decode(
            self._call('GET', '/screenshot', session=False, timeout=15)))

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
        self.wda.set_device(None)


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
