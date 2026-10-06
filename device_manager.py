"""
Device Manager — handles iPhone discovery, connection, DVT services, and device info.
Uses pymobiledevice3 for all device communication over USB.
"""

import asyncio
import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from enum import Enum
from urllib.parse import urlsplit

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

import log_privacy
import paths
import wda_project
from doctor import model_name

logger = logging.getLogger(__name__)
xcodebuild_logger = logging.getLogger('wda.xcodebuild')

# After a failed connection attempt, wait this long before retrying the same device
RETRY_DELAY = 10


# Once WDA runs, xcodebuild prints XCTest's activity, which names the app in front after every
# gesture ("Find the Application 'com.example.app'"). Only lines like these are logged from then on.
_XCODEBUILD_PROBLEM = re.compile(r'error|fail|exception|crash|\*\* TEST', re.I)


def _log_output(stream, log: logging.Logger, on_line=None, should_log=None):
    """Forward a subprocess's output to a logger, line by line, until it closes (only the lines
    `should_log` accepts, if given). Also keeps the pipe drained — a full pipe would block the
    subprocess."""
    for line in stream:
        line = line.rstrip()
        if line:
            if should_log is None or should_log(line):
                log.debug(line)
            if on_line:
                on_line(line)


def _write_wda_pid(pid: int | None):
    """Remember the xcodebuild running WDA, so a later run can stop it if this one crashes."""
    try:
        if pid is None:
            paths.WDA_PID_FILE.unlink(missing_ok=True)
        else:
            paths.SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
            paths.WDA_PID_FILE.write_text(str(pid))
    except OSError as e:
        logger.debug(f"Can't update {paths.WDA_PID_FILE.name}: {e}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _stop_pid(pid: int):
    """Stop a process the way Ctrl-C would, so xcodebuild ends the test run on the iPhone; then harder."""
    for sig, wait in ((signal.SIGINT, 15), (signal.SIGTERM, 5), (signal.SIGKILL, 2)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not _alive(pid):
                return
            time.sleep(0.1)


def stop_leftover_wda():
    """Stop the xcodebuild (and so WDA on the iPhone) an earlier run of the app left running, e.g.
    after a crash. Only that process: it must be the recorded PID, ours, and xcodebuild running
    the app's own WebDriverAgent."""
    try:
        pid = int(paths.WDA_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        result = subprocess.run(['ps', '-o', 'uid=,command=', '-p', str(pid)],
                                capture_output=True, text=True, timeout=3)
        uid, _, command = result.stdout.strip().partition(' ')
        if (uid.isdigit() and int(uid) == os.getuid() and 'xcodebuild' in command
                and 'WebDriverAgentRunner' in command and str(paths.WDA_DIR) in command):
            logger.info("Stopping WebDriverAgent left running by an earlier run of the app")
            _stop_pid(pid)
    except Exception as e:
        logger.warning(f"Couldn't check for a leftover WebDriverAgent: {e}")
    _write_wda_pid(None)


class ConnectionState(Enum):
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2
    ERROR = 3


class DeviceManager(QObject):
    """Manages iPhone USB connection and provides DVT services."""

    device_connected = pyqtSignal(dict)      # device info dict
    device_disconnected = pyqtSignal()
    connection_error = pyqtSignal(str)        # error message
    connection_state_changed = pyqtSignal(ConnectionState)
    wda_state_changed = pyqtSignal(str, str)  # state ('idle', 'starting', 'running', 'failed', 'unavailable'), message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = ConnectionState.DISCONNECTED
        self._lockdown = None
        self._screenshot_provider = None   # the lockdown connection
        self._screenshot_channels = []      # (DvtProvider, Screenshot) pairs, usable in parallel
        self._device_info = {}
        self._lock = threading.Lock()
        self._current_udid = None
        self._wda_proc = None
        self.wda_port = paths.WDA_PORT  # WDA's port on the iPhone, from its startup message
        self._wda_state = ('idle', '')
        self._retry_at = 0.0

        # pymobiledevice3 is async-only — all device I/O runs on this dedicated event loop
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()

        # Device discovery timer
        self._discovery_timer = QTimer(self)
        self._discovery_timer.timeout.connect(self._poll_devices)

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def device_info(self) -> dict:
        return self._device_info

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @property
    def wda_state(self) -> tuple[str, str]:
        return self._wda_state

    def start_discovery(self, interval_ms: int = 2000):
        """Start polling for USB devices."""
        self._discovery_timer.start(interval_ms)
        # Do an immediate check
        self._poll_devices()

    def stop_discovery(self):
        """Stop polling for devices."""
        self._discovery_timer.stop()

    def _run(self, coro, timeout: float | None = 10):
        """Run a coroutine on the device event loop and block until it finishes. Callable from any thread."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def _set_state(self, state: ConnectionState):
        self._state = state
        self.connection_state_changed.emit(state)

    def _poll_devices(self):
        """Check for connected USB devices."""
        try:
            devices = self.discover_devices()
            if devices and not self.is_connected and time.monotonic() >= self._retry_at:
                # Auto-connect to first device
                udid = devices[0].serial
                self._connect_in_background(udid)
            elif not devices and self.is_connected:
                self._handle_disconnect()
        except Exception as e:
            logger.debug(f"Discovery poll error: {e}")

    def discover_devices(self) -> list:
        """Returns list of connected USB devices."""
        try:
            from pymobiledevice3.usbmux import list_devices
            devices = self._run(list_devices(), timeout=5)
            return [d for d in devices if d.is_usb]
        except Exception as e:
            logger.error(f"Failed to list devices: {e}")
            return []

    def _connect_in_background(self, udid: str):
        """Connect to device in a background thread to avoid blocking the UI."""
        if self._state == ConnectionState.CONNECTING:
            return
        self._set_state(ConnectionState.CONNECTING)
        thread = threading.Thread(target=self._connect, args=(udid,), daemon=True)
        thread.start()

    def _connect(self, udid: str):
        """Connect to a device and set up DVT services. Runs in background thread."""
        try:
            # No timeout: pairing waits for the user to tap "Trust" on the iPhone
            self._run(self._connect_async(udid), timeout=None)

            self._set_state(ConnectionState.CONNECTED)
            self.device_connected.emit(self._device_info)
            logger.info(f"Connected to {self._device_info['name']} ({self._device_info['ios_version']})")

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Connection failed: {error_msg}")
            # Release half-open connections so the 2s discovery retry doesn't leak sockets
            try:
                self._run(self._close_connections_async(), timeout=5)
            except Exception:
                pass
            self._retry_at = time.monotonic() + RETRY_DELAY
            self._set_state(ConnectionState.ERROR)
            self.connection_error.emit(error_msg)

    async def _connect_async(self, udid: str):
        from pymobiledevice3.lockdown import create_using_usbmux

        log_privacy.register(udid, 'udid')
        self._lockdown = await create_using_usbmux(serial=udid)

        # Get device info via lockdown (works without tunnel)
        product_type = await self._lockdown.get_value(key='ProductType')
        screen = await self._lockdown.get_value(domain='com.apple.mobile.iTunes') or {}
        scale = float(screen.get('ScreenScaleFactor') or 0)
        name = await self._lockdown.get_value(key='DeviceName')
        log_privacy.register(name, 'iphone')
        log_privacy.register(self._lockdown.identifier, 'udid')
        self._device_info = {
            'name': name,
            'model': product_type,
            'model_name': model_name(product_type),
            'ios_version': self._lockdown.product_version,
            'udid': self._lockdown.identifier,
            # Native screen in pixels and its scale, e.g. 1179 x 2556 @3x
            'screen_size': (screen.get('ScreenWidth'), screen.get('ScreenHeight')),
            'screen_scale': scale or None,
        }
        self._current_udid = udid

        # DVT screenshots are only the fallback for when the USB video stream is unavailable.
        # They work over lockdown up to iOS 16; iOS 17 and later would need the developer tunnel
        # (tunneld, which runs as root), which this app deliberately doesn't use.
        try:
            await self._connect_dvt_direct()
        except Exception as e:
            logger.info(f"Screenshot fallback unavailable ({e}). Mirroring uses the USB video stream, "
                        "which doesn't need it.")

    async def _connect_dvt_direct(self):
        """Connect DVT via lockdown (iOS 16 and earlier)."""
        await self._open_screenshot_service(self._lockdown)
        logger.info("DVT screenshot service connected (direct)")

    async def _open_screenshot_service(self, service_provider):
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot

        dvt = DvtProvider(service_provider)
        await dvt.connect()
        try:
            screenshot = Screenshot(dvt)
            await screenshot.connect()
        except BaseException:
            await dvt.close()
            raise
        self._screenshot_provider = service_provider
        self._screenshot_channels.append((dvt, screenshot))

    def open_screenshot_channels(self, count: int) -> int:
        """Open additional DVT screenshot channels, up to `count` in total.

        The device serves each channel independently, so capturing on several channels
        in parallel multiplies the screenshot rate. Returns the number of open channels.
        """
        while self._screenshot_provider and len(self._screenshot_channels) < count:
            try:
                self._run(self._open_screenshot_service(self._screenshot_provider))
            except Exception as e:
                logger.warning(f"Could not open extra screenshot channel: {e}")
                break
        return len(self._screenshot_channels)

    def take_screenshot(self, channel: int = 0) -> bytes:
        """Take a screenshot on the given channel. Returns PNG bytes.
        Thread-safe; different channels can be used in parallel."""
        with self._lock:
            if channel >= len(self._screenshot_channels):
                raise ConnectionError("Screenshot service not available")
            _, screenshot = self._screenshot_channels[channel]
        return self._run(screenshot.get_screenshot())

    def get_battery_info(self) -> dict:
        """Query battery level and charging status."""
        from pymobiledevice3.services.diagnostics import DiagnosticsService

        async def query():
            async with DiagnosticsService(self._lockdown) as diag:
                return await diag.get_battery() or {}

        try:
            battery = self._run(query(), timeout=5)
            return {
                'level': battery.get('CurrentCapacity', -1),
                'charging': battery.get('IsCharging', False),
            }
        except Exception as e:
            logger.debug(f"Battery info failed: {e}")
            return {'level': -1, 'charging': False}

    def list_apps(self) -> list[dict]:
        """Installed apps, except hidden ones: [{'bundle_id', 'name', 'type'}], sorted by name. Raises on failure."""
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        async def query():
            async with InstallationProxyService(self._lockdown) as proxy:
                return await proxy.browse({'ApplicationType': 'Any'}, attributes=[
                    'CFBundleIdentifier', 'CFBundleDisplayName', 'CFBundleName', 'ApplicationType', 'SBAppTags',
                ])

        if self._lockdown is None:
            raise ConnectionError("no iPhone connected")
        apps = [
            {
                'bundle_id': app['CFBundleIdentifier'],
                'name': app.get('CFBundleDisplayName') or app.get('CFBundleName') or app['CFBundleIdentifier'],
                'type': (app.get('ApplicationType') or '').lower(),
            }
            for app in self._run(query(), timeout=20)
            if app.get('CFBundleIdentifier') and 'hidden' not in (app.get('SBAppTags') or [])
        ]
        return sorted(apps, key=lambda app: app['name'].lower())

    def get_orientation(self) -> int:
        """Get screen orientation (1=portrait, 2=upside-down, 3=landscape-left, 4=landscape-right)."""
        from pymobiledevice3.services.springboard import SpringBoardServicesService

        async def query():
            async with SpringBoardServicesService(lockdown=self._lockdown) as sb:
                return int(await sb.get_interface_orientation())

        try:
            return self._run(query(), timeout=5)
        except Exception as e:
            logger.debug(f"Orientation query failed: {e}")
            return 1  # Default to portrait

    def _set_wda_state(self, state: str, message: str = ''):
        self._wda_state = (state, message)
        self.wda_state_changed.emit(state, message)

    def start_wda(self) -> bool:
        """Build, install and run WebDriverAgent on the device via `xcodebuild test`, in the background.
        Returns True if it was launched (or is already running)."""
        if self._wda_proc and self._wda_proc.poll() is None:
            logger.info("WDA already running")
            return True

        if not self._current_udid:
            logger.warning("No device connected")
            return False

        project = wda_project.find_project()
        if project is None:
            self._set_wda_state('unavailable', "WebDriverAgent isn't downloaded yet — get it in the Doctor tab.")
            logger.warning("WDA project not found")
            return False
        problem = wda_project.verify(project)
        if problem:
            self._set_wda_state('failed', problem)
            logger.error(f"Not building WebDriverAgent: {problem}")
            return False

        team = wda_project.signing_team(project)
        if team is None:
            self._set_wda_state('unavailable', "No Apple ID is signed in to Xcode — see the Doctor tab.")
            logger.error("No signing team: sign in to Xcode › Settings › Accounts")
            return False

        cmd = wda_project.xcodebuild_command(project, self._current_udid, team)
        logger.info(f"Starting WebDriverAgent {paths.WDA_VERSION} ({'free' if team.free else 'paid'} signing team)")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=project.parent,
                env=wda_project.xcodebuild_environment(),
            )
        except Exception as e:
            logger.error(f"Failed to start WDA: {e}")
            self._set_wda_state('failed', f"Couldn't run xcodebuild: {e}")
            return False
        self._wda_proc = proc
        _write_wda_pid(proc.pid)
        self._set_wda_state('starting', "Building and installing WebDriverAgent… The first time takes a few minutes.")
        threading.Thread(target=self._watch_wda, args=(proc,), daemon=True).start()
        return True

    def _watch_wda(self, proc: subprocess.Popen):
        """Log xcodebuild's output, notice when WDA is up, and explain why it stopped. While WDA
        runs only problems are logged; the last lines are kept in memory to explain a failure."""
        recent = deque(maxlen=300)
        running = threading.Event()

        def on_line(line: str):
            recent.append(line)
            match = wda_project.SERVER_URL_PATTERN.search(line)
            if match:
                url = urlsplit(match.group(1))
                if url.hostname != '127.0.0.1':
                    # USE_IP was ignored: WDA would answer anyone on the iPhone's networks
                    logger.error("WebDriverAgent isn't bound to the iPhone's loopback; stopping it")
                    threading.Thread(target=self.stop_wda, daemon=True).start()
                    return
                self.wda_port = url.port or paths.WDA_PORT
                running.set()
                logger.info(f"WebDriverAgent is up on the iPhone (loopback only, port {self.wda_port})")
                self._set_wda_state('running', "WebDriverAgent is running.")

        _log_output(proc.stdout, xcodebuild_logger, on_line,
                    should_log=lambda line: not running.is_set() or _XCODEBUILD_PROBLEM.search(line))
        code = proc.wait()
        if proc is not self._wda_proc:
            return  # stopped on purpose
        hint = wda_project.diagnose_failure(list(recent))
        logger.error(f"WDA xcodebuild exited with {code}: {hint}")
        self._set_wda_state('failed', hint)

    def stop_wda(self):
        """Stop the WDA xcodebuild process — which ends WebDriverAgent on the iPhone — and check that
        WDA no longer answers there."""
        proc, self._wda_proc = self._wda_proc, None
        if proc:
            if proc.poll() is None:
                _stop_pid(proc.pid)
                proc.wait()
            _write_wda_pid(None)
            udid = self._current_udid
            if udid and self._wda_answers(udid):
                logger.warning("WebDriverAgent still answers on the iPhone after stopping xcodebuild. "
                               "Close WebDriverAgentRunner on the iPhone (or restart it).")
            else:
                logger.info("WebDriverAgent stopped")
        self._set_wda_state('idle')

    def _wda_answers(self, udid: str) -> bool:
        """Whether something still accepts connections on WDA's port on the iPhone, after giving it
        a few seconds to go away."""
        from usbmux_http import USBConnectionError, open_socket
        for _ in range(10):
            try:
                open_socket(udid, self.wda_port).close()
            except USBConnectionError:
                return False
            time.sleep(0.5)
        return True

    def is_wda_running(self) -> bool:
        """Check if WDA process is still alive."""
        return self._wda_proc is not None and self._wda_proc.poll() is None

    def _handle_disconnect(self):
        """Clean up after device disconnect."""
        self.disconnect()
        self.device_disconnected.emit()

    def disconnect(self):
        """Disconnect from device and clean up resources."""
        self.stop_wda()

        with self._lock:
            try:
                self._run(self._close_connections_async(), timeout=5)
            except Exception:
                pass
            self._current_udid = None
            self._device_info = {}
        self._retry_at = 0.0

        self._set_state(ConnectionState.DISCONNECTED)
        logger.info("Disconnected from device")

    async def _close_connections_async(self):
        """Close screenshot, DVT and lockdown connections, ignoring errors."""
        channels = [conn for pair in self._screenshot_channels for conn in reversed(pair)]
        self._screenshot_channels = []
        self._screenshot_provider = None
        for conn in (*channels, self._lockdown):
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
        self._lockdown = None

    def cleanup(self):
        """Full cleanup — call on app exit."""
        self.stop_discovery()
        self.disconnect()
        self._loop.call_soon_threadsafe(self._loop.stop)
