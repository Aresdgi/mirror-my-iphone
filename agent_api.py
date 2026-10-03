"""
Agent API — lets AI agents and scripts see and control the iPhone through the running app, over
HTTP on 127.0.0.1. The MCP server (mcp_server.py) and the CLI's device commands are its clients.

    GET  /v1/info          device, screen size in points, touch control and capture status
    GET  /v1/screenshot    the screen as PNG, one pixel per iPhone point (?scale=, ?format=jpeg, ?settle=)
    GET  /v1/ui            the app in front and its elements: type, label, value, center and frame
    GET  /v1/apps          installed apps: bundle ID and name
    POST /v1/tap           {"x": 196, "y": 400}
    POST /v1/double_tap    {"x": 196, "y": 400}
    POST /v1/long_press    {"x": 196, "y": 400, "duration": 1.0}
    POST /v1/swipe         {"x1": 196, "y1": 700, "x2": 196, "y2": 300, "duration": 0.4}
    POST /v1/type          {"text": "Hello\\n"}
    POST /v1/button        {"name": "home" | "lock" | "volume_up" | "volume_down"}
    POST /v1/open_app      {"bundle_id": "com.apple.mobilesafari"}

Coordinates are iPhone points, the same as screenshot pixels at scale 1. Gestures return once the
iPhone has performed them. A screenshot taken within `settle` seconds (3 by default) of a gesture
waits until the screen stops changing, so it shows the result rather than an animation.

Every request needs `Authorization: Bearer <token>`. The app writes its URL and token to
paths.API_FILE, readable only by this user. Requests from web pages (they carry an Origin header)
or addressed to another host name are refused, so websites can't reach the API through a browser.
"""

import hmac
import json
import logging
import os
import secrets
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageChops
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QPointF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QImage

import paths
from input_handler import WDAError
from version import __version__

logger = logging.getLogger(__name__)

GESTURE_TIMEOUT = 60  # seconds; a gesture may wait behind others in WDA's queue
# After a gesture, a screenshot waits until the screen has been still for STILL seconds (checked
# from MIN_SETTLE on, as the screen may not have started to change yet), for at most SETTLE seconds
# (videos and live content never hold still)
MIN_SETTLE = 0.3
STILL = 0.25
SETTLE = 3.0
NOISE = 8  # color levels; frames of a still screen differ by about 1 in a few pixels
FRAME_TIMEOUT = 1.0   # longest wait for a first frame
MAX_BODY = 1_000_000

# Element types worth listing even without a label
INTERACTIVE = {
    'Button', 'Cell', 'Icon', 'Link', 'MenuItem', 'PickerWheel', 'SearchField', 'SecureTextField',
    'SegmentedControl', 'Slider', 'Stepper', 'Switch', 'Tab', 'TextField', 'TextView', 'Toggle',
}


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class AgentAPI(QObject):
    """Serves the agent API from a background thread while the app runs."""

    # iPhone points of a gesture an agent just sent, for the touch indicator (emitted off the UI thread)
    gesture_sent = pyqtSignal(list)

    def __init__(self, device_manager, input_handler, screen_points, parent=None):
        """`screen_points()` returns the iPhone screen in points, in its current orientation."""
        super().__init__(parent)
        self._device = device_manager
        self._wda = input_handler.wda
        self._screen_points = screen_points
        self._frame: QImage | None = None
        self._frame_time = 0.0
        self._frame_changed = threading.Condition()
        self._last_gesture = 0.0  # when the last agent gesture finished
        self._settled = 0.0       # the last gesture after which the screen was seen to settle
        self._server: ThreadingHTTPServer | None = None
        self.capture_mode = ''
        self.fps = 0.0

    # --- Lifecycle (UI thread) ---

    def start(self):
        token = _load_token()
        for port in (paths.API_PORT, 0):
            try:
                server = ThreadingHTTPServer(('127.0.0.1', port), _Handler)
                break
            except OSError as e:
                logger.info(f"Agent API: port {port} unavailable ({e})")
        else:
            logger.error("Agent API couldn't start")
            return
        server.daemon_threads = True
        server.api, server.token = self, token
        self._server = server
        threading.Thread(target=server.serve_forever, name='agent-api', daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            _write_api_file({'url': url, 'token': token, 'pid': os.getpid(), 'version': __version__})
        except OSError as e:
            logger.error(f"Agent API: can't write {paths.API_FILE}: {e}")
        logger.info(f"Agent API listening on {url} (token in {paths.API_FILE})")

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def set_frame(self, image: QImage):
        with self._frame_changed:
            self._frame = image
            self._frame_time = time.monotonic()
            self._frame_changed.notify_all()

    def clear_frame(self):
        """The iPhone is gone: forget its screen."""
        with self._frame_changed:
            self._frame = None
        self.capture_mode, self.fps = '', 0.0

    # --- Endpoints (server threads) ---

    def index(self, _params) -> dict:
        return {'name': paths.APP_NAME, 'version': __version__, 'endpoints': sorted(
            f'{method} {path}' for method, path in ROUTES if path != '/v1')}

    def info(self, _params) -> dict:
        device = self._device.device_info
        size = self._screen_points()
        with self._frame_changed:
            frame_age = time.monotonic() - self._frame_time if self._frame is not None else None
        return {
            'app': {'name': paths.APP_NAME, 'version': __version__},
            'connected': self._device.is_connected,
            'device': {key: device.get(key) for key in ('name', 'model_name', 'ios_version', 'udid')}
            if device else None,
            'screen': {'width': size.width(), 'height': size.height(), 'unit': 'points',
                       'scale': self._wda.scale or device.get('screen_scale')},
            'touch': {'ready': self._wda.is_connected, 'status': self._touch_status()},
            'capture': {'mode': self.capture_mode or None, 'fps': round(self.fps),
                        'frame_age': round(frame_age, 1) if frame_age is not None else None},
        }

    def screenshot(self, params) -> tuple[bytes, str, dict]:
        scale = _number(params, 'scale', 1.0, 0.1, 3.0)
        fmt = str(params.get('format') or 'png').lower().replace('jpg', 'jpeg')
        if fmt not in ('png', 'jpeg'):
            raise APIError(400, "format must be png or jpeg")
        quality = round(_number(params, 'quality', 80, 1, 100))
        settle = _number(params, 'settle', SETTLE, 0, 10)

        image, frame_time = self._settled_frame(settle)
        if image is None:
            image, frame_time = self._wda_screenshot(), time.monotonic()
        size = self._screen_points()
        target = QSize(round(size.width() * scale), round(size.height() * scale))
        if target != image.size():
            image = image.scaled(target, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        headers = {'X-Frame-Age': f'{time.monotonic() - frame_time:.1f}',
                   'X-Screen-Points': f'{size.width():g}x{size.height():g}'}
        return _encode(image, fmt, quality), f'image/{fmt}', headers

    def ui(self, _params) -> dict:
        self._require_touch()
        app_info, tree = self._wda.active_app(), self._wda.ui_tree()
        tree = self._result(tree, timeout=90)
        tree = tree if isinstance(tree, dict) else {}
        try:
            app = self._result(app_info)
        except APIError:
            app = {}
        size = self._screen_points()
        elements, keyboard = _flatten(tree, size.width(), size.height())
        bundle_id = app.get('bundleId') if isinstance(app, dict) else None
        name = str(tree.get('label') or '').strip() or (
            'Home Screen' if bundle_id == 'com.apple.springboard' else None)
        return {'app': {'name': name, 'bundle_id': bundle_id},
                'screen': {'width': size.width(), 'height': size.height()},
                'keyboard': keyboard, 'elements': elements}

    def apps(self, _params) -> dict:
        if not self._device.is_connected:
            raise APIError(503, "No iPhone connected. Connect it with a USB cable.")
        try:
            return {'apps': self._device.list_apps()}
        except Exception as e:
            raise APIError(502, f"Couldn't list the iPhone's apps: {e}")

    def tap(self, params) -> dict:
        point = self._point(params)
        return self._gesture(self._wda.tap(point.x(), point.y()), [point])

    def double_tap(self, params) -> dict:
        point = self._point(params)
        return self._gesture(self._wda.double_tap(point.x(), point.y()), [point])

    def long_press(self, params) -> dict:
        point = self._point(params)
        duration = _number(params, 'duration', 1.0, 0.1, 10)
        return self._gesture(self._wda.long_press(point.x(), point.y(), duration), [point])

    def swipe(self, params) -> dict:
        start, end = self._point(params, 'x1', 'y1'), self._point(params, 'x2', 'y2')
        duration = _number(params, 'duration', 0.4, 0.05, 10)
        path = [(start.x(), start.y(), 0.0), (end.x(), end.y(), duration)]
        return self._gesture(self._wda.swipe(path), [start, end])

    def type_text(self, params) -> dict:
        text = params.get('text')
        if not isinstance(text, str) or not text:
            raise APIError(400, "text must be a non-empty string")
        self._require_touch()
        return self._gesture(self._wda.type_text(text), [], timeout=GESTURE_TIMEOUT + len(text) / 5)

    def button(self, params) -> dict:
        name = str(params.get('name') or '')
        buttons = {
            'home': lambda: self._wda.press_button('home'),  # unlike /wda/homescreen, also closes Spotlight
            'lock': self._wda.lock,
            'volume_up': lambda: self._wda.press_button('volumeUp'),
            'volume_down': lambda: self._wda.press_button('volumeDown'),
        }
        if name not in buttons:
            raise APIError(400, f"name must be one of: {', '.join(buttons)}")
        self._require_touch()
        return self._gesture(buttons[name](), [])

    def open_app(self, params) -> dict:
        bundle_id = params.get('bundle_id')
        if not isinstance(bundle_id, str) or not bundle_id:
            raise APIError(400, "bundle_id must be an app's bundle ID, e.g. com.apple.Preferences (see /v1/apps)")
        self._require_touch()
        return self._gesture(self._wda.activate_app(bundle_id), [])

    # --- Helpers ---

    def _touch_status(self) -> str:
        if self._wda.is_connected:
            return "Touch control is ready."
        if not self._device.is_connected:
            return "No iPhone connected. Connect it with a USB cable."
        state, message = self._device.wda_state
        if state == 'running':
            return "Touch control is connecting to WebDriverAgent. Try again in a few seconds."
        return (f"Touch control isn't ready: {message or 'WebDriverAgent is starting.'} "
                "The Doctor tab in Mirror my iPhone shows what's missing.")

    def _require_touch(self):
        if not self._wda.is_connected:
            raise APIError(503, self._touch_status())

    def _point(self, params, x: str = 'x', y: str = 'y') -> QPointF:
        self._require_touch()
        size = self._screen_points()
        px, py = _number(params, x), _number(params, y)
        if not (0 <= px <= size.width() and 0 <= py <= size.height()):
            raise APIError(400, f"({px:g}, {py:g}) is outside the screen, which is "
                                f"{size.width():g} × {size.height():g} points")
        return QPointF(px, py)

    def _gesture(self, future, points: list[QPointF], timeout: float = GESTURE_TIMEOUT) -> dict:
        if points:
            self.gesture_sent.emit(points)
        started = time.monotonic()
        self._result(future, timeout)
        self._last_gesture = time.monotonic()
        return {'ok': True, 'ms': round((self._last_gesture - started) * 1000)}

    @staticmethod
    def _result(future, timeout: float = GESTURE_TIMEOUT):
        try:
            return future.result(timeout)
        except FutureTimeout:
            raise APIError(504, "WebDriverAgent didn't respond in time")
        except WDAError as e:
            raise APIError(502, f"WebDriverAgent: {e}")

    def _settled_frame(self, settle: float) -> tuple[QImage | None, float]:
        """The newest frame and when it arrived. Within `settle` seconds of the last gesture, first
        wait until the screen stops changing, so it shows the result rather than an animation.
        The stream keeps sending frames while the screen is still, so a still screen is one whose
        frames stay the same."""
        gesture = self._last_gesture
        deadline = gesture + settle if self._settled != gesture else 0.0
        delay = min(gesture + MIN_SETTLE, deadline) - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        previous, unchanged_since = None, 0.0
        while True:
            with self._frame_changed:
                if self._frame is None:
                    self._frame_changed.wait(FRAME_TIMEOUT)
                frame, frame_time = self._frame, self._frame_time
            now = time.monotonic()
            if frame is None or now >= deadline:
                return frame, frame_time
            signature = _signature(frame)
            if previous is None or _changed(previous, signature):
                previous, unchanged_since = signature, now
            elif now - unchanged_since >= STILL:
                self._settled = gesture
                return frame, frame_time
            time.sleep(0.05)

    def _wda_screenshot(self) -> QImage:
        """Fallback while there's no video frame. Like the stream, it's dark while the display is off."""
        if not self._wda.is_connected:
            raise APIError(503, "No screen image yet. Connect the iPhone and unlock it; "
                                "the picture pauses while its display is off.")
        image = QImage.fromData(self._result(self._wda.screenshot()))
        if image.isNull():
            raise APIError(502, "WebDriverAgent sent an unreadable screenshot")
        return image


class _Handler(BaseHTTPRequestHandler):
    server_version = f'MirrorMyIPhone/{__version__}'

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def log_message(self, format, *args):
        logger.debug(f"Agent API: {format % args}")

    def _handle(self, method: str):
        url = urlsplit(self.path)
        try:
            self._check_access()
            endpoint = ROUTES.get((method, url.path.rstrip('/') or '/v1'))
            if endpoint is None:
                raise APIError(404, f"No such endpoint: {method} {url.path}. GET /v1 lists them.")
            params = {key: values[-1] for key, values in parse_qs(url.query).items()}
            if method == 'POST':
                params.update(self._read_json())
            result = endpoint(self.server.api, params)
        except APIError as e:
            self._send(e.status, _json({'error': str(e)}), 'application/json')
            return
        except Exception as e:
            logger.exception(f"Agent API: {method} {url.path} failed")
            self._send(500, _json({'error': f"Internal error: {e}"}), 'application/json')
            return
        if isinstance(result, tuple):
            self._send(200, *result)
        else:
            self._send(200, _json(result), 'application/json')

    def _check_access(self):
        host = self.headers.get('Host', '').rsplit(':', 1)[0]
        if host not in ('127.0.0.1', 'localhost') or self.headers.get('Origin'):
            raise APIError(403, "Only local programs can use the agent API, not web pages")
        expected = f'Bearer {self.server.token}'.encode()
        if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), expected):
            raise APIError(401, f"Missing or wrong token. Send 'Authorization: Bearer <token>' "
                                f"with the token from {paths.API_FILE}")

    def _read_json(self) -> dict:
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY:
            raise APIError(413, "Request body too large")
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            raise APIError(400, "The request body isn't valid JSON")
        if not isinstance(body, dict):
            raise APIError(400, "The request body must be a JSON object")
        return body

    def _send(self, status: int, body: bytes, content_type: str, headers: dict | None = None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


ROUTES = {
    ('GET', '/v1'): AgentAPI.index,
    ('GET', '/v1/info'): AgentAPI.info,
    ('GET', '/v1/screenshot'): AgentAPI.screenshot,
    ('GET', '/v1/ui'): AgentAPI.ui,
    ('GET', '/v1/apps'): AgentAPI.apps,
    ('POST', '/v1/tap'): AgentAPI.tap,
    ('POST', '/v1/double_tap'): AgentAPI.double_tap,
    ('POST', '/v1/long_press'): AgentAPI.long_press,
    ('POST', '/v1/swipe'): AgentAPI.swipe,
    ('POST', '/v1/type'): AgentAPI.type_text,
    ('POST', '/v1/button'): AgentAPI.button,
    ('POST', '/v1/open_app'): AgentAPI.open_app,
}


def _number(params: dict, name: str, default: float | None = None, low: float = -1e9, high: float = 1e9) -> float:
    value = params.get(name, default)
    if value is None:
        raise APIError(400, f"{name} is missing")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise APIError(400, f"{name} must be a number")
    if not low <= number <= high:
        raise APIError(400, f"{name} must be between {low:g} and {high:g}")
    return number


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode()


def _signature(frame: QImage) -> Image.Image:
    """A small copy of a frame, to tell whether the screen is changing."""
    small = frame.scaled(max(1, frame.width() // 8), max(1, frame.height() // 8)).convertToFormat(
        QImage.Format.Format_RGB888)
    pixels = small.constBits().asstring(small.sizeInBytes())
    return Image.frombuffer('RGB', (small.width(), small.height()), pixels, 'raw', 'RGB', small.bytesPerLine(), 1)


def _changed(a: Image.Image, b: Image.Image) -> bool:
    return a.size != b.size or max(high for _, high in ImageChops.difference(a, b).getextrema()) > NOISE


def _encode(image: QImage, fmt: str, quality: int) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, fmt.upper(), quality if fmt == 'jpeg' else -1)
    buffer.close()
    return bytes(data)


def _flatten(tree: dict, width: float, height: float) -> tuple[list[dict], bool]:
    """WDA's JSON source → the visible elements worth acting on, and whether the keyboard is up.
    The app itself and the keyboard's keys aren't listed (typing goes through /v1/type)."""
    elements, seen, keyboard = [], set(), False

    def visit(node: dict):
        nonlocal keyboard
        kind = str(node.get('type') or '').removeprefix('XCUIElementType')
        if kind == 'Keyboard':
            keyboard = True
            return
        if kind == 'Application':
            for child in node.get('children') or []:
                visit(child)
            return
        rect = node.get('rect') or {}
        x, y, w, h = (float(rect.get(key) or 0) for key in ('x', 'y', 'width', 'height'))
        label, name, value = (str(node.get(key) or '').strip() for key in ('label', 'name', 'value'))
        visible = str(node.get('isVisible', '1')).lower() not in ('0', 'false')
        # The part of the element that's on screen
        left, top, right, bottom = max(x, 0), max(y, 0), min(x + w, width), min(y + h, height)
        if visible and right > left and bottom > top and (label or value or kind in INTERACTIVE):
            key = (label, value, round(x), round(y), round(w), round(h))
            if key not in seen:
                seen.add(key)
                element = {'type': kind, 'label': label, 'name': name if name != label else '', 'value': value,
                           'x': round((left + right) / 2, 1), 'y': round((top + bottom) / 2, 1),
                           'frame': [round(x, 1), round(y, 1), round(w, 1), round(h, 1)]}
                if str(node.get('isEnabled', '1')).lower() in ('0', 'false'):
                    element['enabled'] = False
                elements.append({k: v for k, v in element.items() if v != ''})
        for child in node.get('children') or []:
            visit(child)

    visit(tree)
    return elements, keyboard


def _load_token() -> str:
    """The token from the last run (so configured clients keep working), or a new one."""
    try:
        token = json.loads(paths.API_FILE.read_text()).get('token')
        if isinstance(token, str) and len(token) >= 32:
            return token
    except (OSError, ValueError, AttributeError):
        pass
    return secrets.token_urlsafe(32)


def _write_api_file(content: dict):
    """Write paths.API_FILE atomically, readable only by this user."""
    paths.SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = paths.API_FILE.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as file:
        json.dump(content, file, indent=2)
    os.replace(temporary, paths.API_FILE)
