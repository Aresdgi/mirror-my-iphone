"""
Device Manager — handles iPhone discovery, connection, DVT services, and device info.
Uses pymobiledevice3 for all device communication over USB.
"""

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

logger = logging.getLogger(__name__)


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

        # pymobiledevice3 is async-only — all device I/O runs on this dedicated event loop
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()

        # WDA project path — auto-detected
        self._wda_project = self._find_wda_project()

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
            if devices and not self.is_connected:
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
            self._set_state(ConnectionState.ERROR)
            self.connection_error.emit(error_msg)

    async def _connect_async(self, udid: str):
        from pymobiledevice3.lockdown import create_using_usbmux

        self._lockdown = await create_using_usbmux(serial=udid)

        # Get device info via lockdown (works without tunnel)
        self._device_info = {
            'name': await self._lockdown.get_value(key='DeviceName'),
            'model': await self._lockdown.get_value(key='ProductType'),
            'ios_version': self._lockdown.product_version,
            'udid': self._lockdown.identifier,
        }
        self._current_udid = udid

        # Set up DVT for screenshots
        # Try tunnel first (needed for iOS 17+), then direct
        dvt_connected = False
        try:
            await self._connect_dvt_tunnel()
            dvt_connected = True
        except Exception as tunnel_err:
            logger.info(f"Tunnel DVT failed ({tunnel_err}), trying direct...")
            try:
                await self._connect_dvt_direct()
                dvt_connected = True
            except Exception as direct_err:
                logger.error(f"Direct DVT also failed: {direct_err}")

        if not dvt_connected:
            raise ConnectionError(
                "Developer-Tunnel wird benötigt (iOS 17+).\n"
                "Starte in einem Terminal:\n"
                "sudo python3 -m pymobiledevice3 remote tunneld"
            )

    async def _connect_dvt_tunnel(self):
        """Connect DVT via tunneld (required for iOS 17+).

        Uses pymobiledevice3's tunneld API to get a RemoteServiceDiscoveryService
        which provides access to developer services through the tunnel.
        """
        from pymobiledevice3.tunneld.api import get_tunneld_devices

        tunneld_devices = await get_tunneld_devices()
        if not tunneld_devices:
            raise ConnectionError("Kein Tunnel gefunden. Starte tunneld zuerst.")

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
                stderr=subprocess.PIPE,
            )
            time.sleep(0.5)

            if self._port_forward_proc.poll() is not None:
                stderr = self._port_forward_proc.stderr.read().decode()
                logger.error(f"Port forward failed: {stderr}")
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

    def _find_wda_project(self) -> str | None:
        """Find WebDriverAgent.xcodeproj near the app directory."""
        app_dir = Path(__file__).parent
        # Check common locations
        candidates = [
            app_dir / 'WebDriverAgent' / 'WebDriverAgent.xcodeproj',
            app_dir.parent / 'WebDriverAgent' / 'WebDriverAgent.xcodeproj',
            Path.home() / 'WebDriverAgent' / 'WebDriverAgent.xcodeproj',
        ]
        for path in candidates:
            if path.exists():
                logger.info(f"Found WDA project: {path}")
                return str(path)
        # Spotlight search as fallback
        try:
            result = subprocess.run(
                ['mdfind', 'kMDItemFSName == "WebDriverAgent.xcodeproj"'],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split('\n'):
                if line and 'WebDriverAgent' in line:
                    logger.info(f"Found WDA project via Spotlight: {line}")
                    return line
        except Exception:
            pass
        return None

    def start_wda(self) -> bool:
        """Start WebDriverAgent via xcodebuild test in background.
        Returns True if launch was initiated.
        """
        if self._wda_proc and self._wda_proc.poll() is None:
            logger.info("WDA already running")
            return True

        if not self._wda_project:
            logger.warning("WDA project not found")
            return False

        if not self._current_udid:
            logger.warning("No device connected")
            return False

        # Find development team from the project
        team_id = self._get_dev_team()
        if not team_id:
            logger.error("No development team found in WDA project")
            return False

        logger.info(f"Starting WDA (team: {team_id}, device: {self._current_udid})...")

        cmd = [
            'xcodebuild', 'test',
            '-project', self._wda_project,
            '-scheme', 'WebDriverAgentRunner',
            '-destination', f'id={self._current_udid}',
            '-allowProvisioningUpdates',
            f'DEVELOPMENT_TEAM={team_id}',
            'CODE_SIGN_IDENTITY=Apple Development',
        ]

        try:
            self._wda_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=os.path.dirname(self._wda_project),
            )
            logger.info("WDA xcodebuild started")
            return True
        except Exception as e:
            logger.error(f"Failed to start WDA: {e}")
            return False

    def _get_dev_team(self) -> str | None:
        """Extract development team ID from WDA project file."""
        if not self._wda_project:
            return None
        try:
            pbxproj = os.path.join(self._wda_project, 'project.pbxproj')
            with open(pbxproj, 'r') as f:
                content = f.read()
            # Find DEVELOPMENT_TEAM = XXXXX; patterns
            import re
            matches = re.findall(r'DEVELOPMENT_TEAM\s*=\s*([A-Z0-9]{10})', content)
            if matches:
                return matches[0]
        except Exception as e:
            logger.debug(f"Failed to read dev team: {e}")
        return None

    def stop_wda(self):
        """Stop the WDA xcodebuild process."""
        if self._wda_proc:
            try:
                self._wda_proc.terminate()
                self._wda_proc.wait(timeout=5)
            except Exception:
                try:
                    self._wda_proc.kill()
                except Exception:
                    pass
            self._wda_proc = None
            logger.info("WDA stopped")

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
