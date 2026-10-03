"""
Device Manager — handles iPhone discovery, connection, DVT services, and device info.
Uses pymobiledevice3 for all device communication over USB.
"""

import asyncio
import logging
import subprocess
import sys
import threading
import time
from collections import deque
from enum import Enum

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

import wda_project
from doctor import model_name

logger = logging.getLogger(__name__)
xcodebuild_logger = logging.getLogger('wda.xcodebuild')
forward_logger = logging.getLogger('wda.port_forward')

# After a failed connection attempt, wait this long before retrying the same device
RETRY_DELAY = 10


def _log_output(stream, log: logging.Logger, on_line=None):
    """Forward a subprocess's output to a logger, line by line, until it closes.
    Also keeps the pipe drained — a full pipe would block the subprocess."""
    for line in stream:
        line = line.rstrip()
        if line:
            log.debug(line)
            if on_line:
                on_line(line)


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
        self._rsd = None
        self._screenshot_provider = None   # rsd (tunnel) or lockdown (direct)
        self._screenshot_channels = []      # (DvtProvider, Screenshot) pairs, usable in parallel
        self._device_info = {}
        self._lock = threading.Lock()
        self._current_udid = None
        self._port_forward_proc = None
        self._wda_proc = None
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

        self._lockdown = await create_using_usbmux(serial=udid)

        # Get device info via lockdown (works without tunnel)
        product_type = await self._lockdown.get_value(key='ProductType')
        screen = await self._lockdown.get_value(domain='com.apple.mobile.iTunes') or {}
        scale = float(screen.get('ScreenScaleFactor') or 0)
        self._device_info = {
            'name': await self._lockdown.get_value(key='DeviceName'),
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
        # Try the tunnel first (needed for iOS 17+), then direct.
        try:
            await self._connect_dvt_tunnel()
        except Exception as tunnel_err:
            logger.info(f"Tunnel DVT failed ({tunnel_err}), trying direct...")
            try:
                await self._connect_dvt_direct()
            except Exception as direct_err:
                logger.warning(
                    f"Screenshot fallback unavailable ({direct_err}). Mirroring uses the USB video "
                    "stream, which doesn't need it; on iOS 17+ the fallback needs the developer tunnel."
                )

    async def _connect_dvt_tunnel(self):
        """Connect DVT via tunneld (required for iOS 17+).

        Uses pymobiledevice3's tunneld API to get a RemoteServiceDiscoveryService
        which provides access to developer services through the tunnel.
        """
        from pymobiledevice3.tunneld.api import get_tunneld_devices

        tunneld_devices = await get_tunneld_devices()
        if not tunneld_devices:
            raise ConnectionError("no tunnel found — tunneld isn't running")

        # Find matching device by UDID, or use first available
        target = self._current_udid.replace('-', '')
        rsd = next(
            (d for d in tunneld_devices if (d.udid or '').replace('-', '') == target),
            tunneld_devices[0],
        )
        # get_tunneld_devices opens a connection to every tunnel — close the ones we don't use
        for device in tunneld_devices:
            if device is not rsd:
                await device.close()

        self._rsd = rsd
        await self._open_screenshot_service(rsd)
        logger.info("DVT screenshot service connected via tunnel")

    async def _connect_dvt_direct(self):
        """Connect DVT directly via lockdown (works for iOS < 17)."""
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

    def start_port_forward(self, local_port: int = 8100, device_port: int = 8100):
        """Start USB port forwarding: localhost:local_port -> device:device_port.

        This is needed for WDA access since it listens on the device's port 8100.
        """
        self.stop_port_forward()

        # Kill any existing process on the port
        try:
            subprocess.run(
                ['lsof', '-ti', f':{local_port}'],
                capture_output=True, text=True, timeout=3
            )
            result = subprocess.run(
                ['lsof', '-ti', f':{local_port}'],
                capture_output=True, text=True, timeout=3
            )
            if result.stdout.strip():
                for pid in result.stdout.strip().split('\n'):
                    try:
                        subprocess.run(['kill', '-9', pid.strip()], timeout=2)
                    except Exception:
                        pass
                time.sleep(0.3)
        except Exception:
            pass

        try:
            venv_python = sys.executable
            cmd = [
                venv_python, '-m', 'pymobiledevice3',
                'usbmux', 'forward',
                str(local_port), str(device_port),
            ]
            if self._current_udid:
                cmd.extend(['--udid', self._current_udid])

            self._port_forward_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            threading.Thread(
                target=_log_output, args=(self._port_forward_proc.stdout, forward_logger), daemon=True,
            ).start()
            time.sleep(0.5)

            if self._port_forward_proc.poll() is not None:
                logger.error("Port forward failed — see the wda.port_forward lines in the log")
                self._port_forward_proc = None
                return False

            logger.info(f"Port forwarding started: localhost:{local_port} -> device:{device_port}")
            return True
        except Exception as e:
            logger.error(f"Failed to start port forwarding: {e}")
            return False

    def stop_port_forward(self):
        """Stop the port forwarding subprocess."""
        if self._port_forward_proc:
            try:
                self._port_forward_proc.terminate()
                self._port_forward_proc.wait(timeout=3)
            except Exception:
                try:
                    self._port_forward_proc.kill()
                except Exception:
                    pass
            self._port_forward_proc = None
            logger.info("Port forwarding stopped")

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

        team = wda_project.signing_team(project)
        if team is None:
            self._set_wda_state('unavailable', "No Apple ID is signed in to Xcode — see the Doctor tab.")
            logger.error("No signing team: sign in to Xcode › Settings › Accounts")
            return False

        cmd = wda_project.xcodebuild_command(project, self._current_udid, team)
        logger.info(f"Starting WDA from {project} (team {team.id}, device {self._current_udid})")
        logger.debug(f"WDA command: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=project.parent,
            )
        except Exception as e:
            logger.error(f"Failed to start WDA: {e}")
            self._set_wda_state('failed', f"Couldn't run xcodebuild: {e}")
            return False
        self._wda_proc = proc
        self._set_wda_state('starting', "Building and installing WebDriverAgent… The first time takes a few minutes.")
        threading.Thread(target=self._watch_wda, args=(proc,), daemon=True).start()
        return True

    def _watch_wda(self, proc: subprocess.Popen):
        """Log xcodebuild's output, notice when WDA is up, and explain why it stopped."""
        recent = deque(maxlen=300)

        def on_line(line: str):
            recent.append(line)
            match = wda_project.SERVER_URL_PATTERN.search(line)
            if match:
                logger.info(f"WDA is up at {match.group(1)} on the device")
                self._set_wda_state('running', "WebDriverAgent is running.")

        _log_output(proc.stdout, xcodebuild_logger, on_line)
        code = proc.wait()
        if proc is not self._wda_proc:
            return  # stopped on purpose
        hint = wda_project.diagnose_failure(list(recent))
        logger.error(f"WDA xcodebuild exited with {code}: {hint}")
        self._set_wda_state('failed', hint)

    def stop_wda(self):
        """Stop the WDA xcodebuild process."""
        proc, self._wda_proc = self._wda_proc, None
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            logger.info("WDA stopped")
        self._set_wda_state('idle')

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
        self.stop_port_forward()

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
        """Close screenshot, DVT, tunnel and lockdown connections, ignoring errors."""
        channels = [conn for pair in self._screenshot_channels for conn in reversed(pair)]
        self._screenshot_channels = []
        self._screenshot_provider = None
        for conn in (*channels, self._rsd, self._lockdown):
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
        self._rsd = None
        self._lockdown = None

    def cleanup(self):
        """Full cleanup — call on app exit."""
        self.stop_discovery()
        self.disconnect()
        self._loop.call_soon_threadsafe(self._loop.stop)
