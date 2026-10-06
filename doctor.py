"""
Doctor — checks everything Mirror my iPhone needs and explains how to fix what's missing.

UI-independent: the Doctor tab and `mirror-my-iphone doctor` both render these results.
Checks only read state; fixes in ACTIONS run only when the user asks for them.
"""

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator

import requests

import paths
import wda_project

logger = logging.getLogger(__name__)

MIRRORING = 'Mirroring'
TOUCH = 'Touch control (click, swipe, scroll)'
OPTIONAL = 'Optional'
GROUPS = (MIRRORING, TOUCH, OPTIONAL)


class Status(Enum):
    OK = 'ok'
    WARN = 'warn'  # works, but degraded or worth a look
    FAIL = 'fail'  # blocks what its group is about
    INFO = 'info'  # can't be checked automatically, instructions only
    SKIP = 'skip'  # depends on an earlier check that didn't pass


@dataclass
class Check:
    group: str
    title: str
    status: Status
    detail: str
    fix: str = ''              # how to fix it, shown when not OK
    action: str | None = None  # key into ACTIONS, for fixes the app can do itself
    action_label: str = ''


def model_name(product_type: str) -> str:
    """'iPhone15,4' → 'iPhone 15'."""
    try:
        from pymobiledevice3.irecv_devices import IRECV_DEVICES
        return next(d.display_name for d in IRECV_DEVICES if d.product_type == product_type)
    except Exception:
        return product_type


def _short_path(path: Path) -> str:
    return str(path).replace(str(Path.home()), '~', 1)


def _ios_major(version: str | None) -> int:
    try:
        return int((version or '0').split('.')[0])
    except ValueError:
        return 0


class _Device:
    """Read-only lockdown connection to the first USB iPhone, on a private event loop."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self.usb = None         # usbmux device
        self.count = 0
        self.lockdown = None
        self.name = None
        self.error = None

    def run(self, coro, timeout: float = 10):
        return self._loop.run_until_complete(asyncio.wait_for(coro, timeout))

    def open(self):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.usbmux import list_devices

        try:
            devices = [d for d in self.run(list_devices(), 5) if d.is_usb]
        except Exception as e:
            self.error = f"Can't reach usbmuxd: {e}"
            return
        self.count = len(devices)
        if not devices:
            return
        self.usb = devices[0]
        try:
            self.lockdown = self.run(create_using_usbmux(serial=self.usb.serial, autopair=False))
        except Exception as e:
            self.error = str(e) or type(e).__name__
            return
        try:
            self.name = self.run(self.lockdown.get_value(key='DeviceName'), 5)
        except Exception:
            pass

    @property
    def paired(self) -> bool:
        return bool(self.lockdown and self.lockdown.paired)

    @property
    def ios_version(self) -> str | None:
        return self.lockdown.product_version if self.lockdown else None

    def close(self):
        if self.lockdown is not None:
            try:
                self.run(self.lockdown.close(), 5)
            except Exception:
                pass
        self._loop.close()


# --- Checks ---

def iter_checks(in_app: bool = True, wda_state: Callable[[], tuple[str, str]] | None = None) -> Iterator[Check]:
    """Run all checks, yielding each result as soon as it is known.

    in_app: running inside Mirror my iPhone (rather than the CLI), so per-app state like
        camera access refers to the app itself.
    wda_state: returns the app's WebDriverAgent (state, message), if the app is running.
    """
    device = _Device()
    try:
        device.open()
        yield _check_connected(device)
        yield _check_trusted(device)
        yield _check_video_source(device)
        yield _check_camera_access(in_app)

        yield _check_developer_mode(device)
        yield _check_xcode()
        project = wda_project.find_project()
        yield _check_signing_team(project)
        yield _check_wda_project(project)
        running = _wda_running(wda_state)
        yield _check_wda_running(device, running, wda_state)
        yield _manual_check(
            'Developer trusted on iPhone', running,
            "Needed once, after WebDriverAgent is installed the first time: on the iPhone open "
            "Settings › General › VPN & Device Management, tap your Apple Development profile and tap Trust.",
        )
        yield _manual_check(
            'UI Automation enabled', running,
            "On the iPhone: Settings › Developer › Enable UI Automation → On.",
        )

        yield _check_disk_image(device)
    finally:
        device.close()


def _check_connected(device: _Device) -> Check:
    title = 'iPhone connected via USB'
    if device.usb is None and device.error:
        return Check(MIRRORING, title, Status.FAIL, device.error,
                     fix="Restart the Mac if this persists — usbmuxd is the macOS service that talks to iPhones.")
    if device.usb is None:
        return Check(MIRRORING, title, Status.FAIL, "No iPhone found.",
                     fix="Connect the iPhone with a USB data cable (not a charge-only one) and unlock it.")
    parts = [device.name, model_name(device.lockdown.product_type) if device.lockdown else None,
             f"iOS {device.ios_version}" if device.ios_version else None]
    detail = ' · '.join(p for p in parts if p) or device.usb.serial
    if device.count > 1:
        detail += f" (+{device.count - 1} more — the first one is used)"
    return Check(MIRRORING, title, Status.OK, detail)


def _check_trusted(device: _Device) -> Check:
    title = 'iPhone trusts this Mac'
    if device.usb is None:
        return Check(MIRRORING, title, Status.SKIP, "Connect an iPhone first.")
    if device.lockdown is None:
        return Check(MIRRORING, title, Status.FAIL, f"Couldn't talk to the iPhone: {device.error}",
                     fix="Unplug and reconnect the iPhone, then unlock it.")
    if not device.paired:
        return Check(MIRRORING, title, Status.FAIL, "This Mac isn't trusted yet.",
                     fix="Unlock the iPhone and tap Trust when it asks “Trust This Computer?”.",
                     action='pair', action_label='Ask iPhone')
    return Check(MIRRORING, title, Status.OK, "Paired.")


def _check_video_source(device: _Device) -> Check:
    title = 'USB screen stream'
    if not device.paired:
        return Check(MIRRORING, title, Status.SKIP, "Needs a connected, trusted iPhone.")
    try:
        import video_stream
    except ImportError as e:
        return Check(MIRRORING, title, Status.WARN, f"AVFoundation bindings are missing ({e}).",
                     fix="Reinstall Mirror my iPhone. Until then it falls back to screenshots (about 20 FPS).")
    sources = video_stream.ios_capture_devices()
    if any(d.localizedName() == device.name for d in sources) or len(sources) == 1:
        return Check(MIRRORING, title, Status.OK, "Available — up to 60 FPS.")
    return Check(MIRRORING, title, Status.WARN, "The iPhone isn't offered as a video source yet.",
                 fix="Unlock the iPhone and re-check. Until then Mirror my iPhone falls back to "
                     "screenshots, which are slower and only work up to iOS 16.")


def _check_camera_access(in_app: bool) -> Check:
    title = 'Camera access for Mirror my iPhone'
    if not in_app:
        return Check(MIRRORING, title, Status.INFO,
                     "macOS treats the iPhone's screen stream like a camera and asks Mirror my iPhone "
                     "for access the first time it mirrors. Checked inside the app.")
    try:
        import AVFoundation
        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeVideo)
    except Exception as e:
        return Check(MIRRORING, title, Status.WARN, f"Couldn't check ({e}).")
    if status == 3:  # authorized
        return Check(MIRRORING, title, Status.OK, "Allowed.")
    if status == 0:  # not determined
        return Check(MIRRORING, title, Status.WARN,
                     "Not asked yet. macOS treats the iPhone's screen stream like a camera.",
                     fix="Click Allow, or accept the prompt when mirroring starts.",
                     action='request_camera', action_label='Allow…')
    return Check(MIRRORING, title, Status.FAIL, "Denied — the screen stream can't start.",
                 fix="System Settings › Privacy & Security › Camera › turn on Mirror my iPhone, then restart the app.",
                 action='open_camera_settings', action_label='Open Settings')


def _check_developer_mode(device: _Device) -> Check:
    title = 'Developer Mode on iPhone'
    if not device.paired:
        return Check(TOUCH, title, Status.SKIP, "Needs a connected, trusted iPhone.")
    if _ios_major(device.ios_version) < 16:
        return Check(TOUCH, title, Status.OK, "Not needed before iOS 16.")
    try:
        enabled = device.run(device.lockdown.get_developer_mode_status(), 5)
    except Exception as e:
        return Check(TOUCH, title, Status.WARN, f"Couldn't read it ({e}).")
    if enabled:
        return Check(TOUCH, title, Status.OK, "On.")
    return Check(TOUCH, title, Status.FAIL, "Off.",
                 fix="On the iPhone: Settings › Privacy & Security › Developer Mode → On, then restart it. "
                     "If the option is missing, Mirror my iPhone can reveal it (Show on iPhone).",
                 action='reveal_developer_mode', action_label='Show on iPhone')


def _check_xcode() -> Check:
    title = 'Xcode'
    try:
        developer_dir = subprocess.run(['xcode-select', '-p'], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        developer_dir = ''
    if 'Xcode' not in developer_dir:
        if Path('/Applications/Xcode.app').exists():
            return Check(TOUCH, title, Status.FAIL, "The Command Line Tools are selected instead of Xcode.",
                         fix="Run: sudo xcode-select -s /Applications/Xcode.app")
        return Check(TOUCH, title, Status.FAIL, "Not installed.",
                     fix="Install Xcode from the App Store (free). The Command Line Tools alone "
                         "can't install apps on an iPhone.",
                     action='install_xcode', action_label='App Store')
    try:
        version = subprocess.run(['xcodebuild', '-version'], capture_output=True, text=True, timeout=20).stdout
        version = version.splitlines()[0] if version else 'Xcode'
        first_launch = subprocess.run(['xcodebuild', '-checkFirstLaunchStatus'], capture_output=True, timeout=20)
    except Exception as e:
        return Check(TOUCH, title, Status.WARN, f"Couldn't run xcodebuild ({e}).")
    if first_launch.returncode != 0:
        return Check(TOUCH, title, Status.WARN, f"{version} still needs to install components.",
                     fix="Open Xcode once and let it finish, or run: sudo xcodebuild -runFirstLaunch",
                     action='open_xcode', action_label='Open Xcode')
    return Check(TOUCH, title, Status.OK, f"{version}.")


def _check_signing_team(project: Path | None) -> Check:
    title = 'Apple ID for code signing'
    team = wda_project.signing_team(project)
    if team is None:
        return Check(TOUCH, title, Status.FAIL, "No Apple ID is signed in to Xcode.",
                     fix="Open Xcode › Settings › Accounts › + › Apple ID. A free Apple ID works.",
                     action='open_xcode', action_label='Open Xcode')
    detail = f"{team.name} ({team.id})."
    if team.free:
        detail += " Free team: WebDriverAgent is re-signed automatically, its profile lasts 7 days."
    return Check(TOUCH, title, Status.OK, detail)


def _check_wda_project(project: Path | None) -> Check:
    title = 'WebDriverAgent downloaded'
    if project is None:
        return Check(TOUCH, title, Status.FAIL, "Not found.",
                     fix="Mirror my iPhone can download it for you (about 5 MB), or clone it yourself: "
                         f"git clone --depth 1 --branch {paths.WDA_VERSION} {paths.WDA_REPO} ~/WebDriverAgent",
                     action='download_wda', action_label='Download')
    version = wda_project.project_version(project)
    return Check(TOUCH, title, Status.OK, f"{version + ' at ' if version else ''}{_short_path(project.parent)}.")


def _wda_running(wda_state) -> bool:
    if wda_state is not None and wda_state()[0] == 'running':
        return True
    try:
        return requests.get(f'http://127.0.0.1:{paths.WDA_PORT}/status', timeout=1).ok
    except requests.RequestException:
        return False


def _check_wda_running(device: _Device, running: bool, wda_state) -> Check:
    title = 'WebDriverAgent running'
    if running:
        return Check(TOUCH, title, Status.OK, "Running — touch control is active.")
    state, message = wda_state() if wda_state else ('idle', '')
    if state == 'starting':
        return Check(TOUCH, title, Status.INFO, message)
    if state in ('failed', 'unavailable'):
        return Check(TOUCH, title, Status.FAIL, message, fix="Fix the items above, then use Device › Reconnect.")
    if device.usb is None:
        return Check(TOUCH, title, Status.SKIP, "Connect an iPhone first.")
    return Check(TOUCH, title, Status.INFO,
                 "Not running. Mirror my iPhone builds and starts it whenever the iPhone connects "
                 "(the first build takes a few minutes).")


def _manual_check(title: str, done: bool, instructions: str) -> Check:
    if done:
        return Check(TOUCH, title, Status.OK, "Yes — WebDriverAgent is running.")
    return Check(TOUCH, title, Status.INFO, instructions)


def _check_disk_image(device: _Device) -> Check:
    title = 'Developer disk image'
    if not device.paired:
        return Check(OPTIONAL, title, Status.SKIP, "Needs a connected, trusted iPhone.")
    from pymobiledevice3.services.mobile_image_mounter import MobileImageMounterService, image_type_for_device

    async def mounted():
        async with MobileImageMounterService(lockdown=device.lockdown) as mounter:
            return await mounter.is_image_mounted(image_type_for_device(device.lockdown))

    try:
        if device.run(mounted(), 10):
            return Check(OPTIONAL, title, Status.OK, "Mounted.")
    except Exception as e:
        return Check(OPTIONAL, title, Status.INFO, f"Couldn't check ({e or type(e).__name__}). "
                     "Needs Developer Mode.")
    return Check(OPTIONAL, title, Status.INFO,
                 "Not mounted. Xcode mounts it the first time it installs WebDriverAgent.")


def summarize(checks: list[Check]) -> tuple[Status, str]:
    """Overall verdict for a finished run."""
    def failed(group):
        return any(c.status == Status.FAIL for c in checks if c.group == group)

    if failed(MIRRORING):
        return Status.FAIL, "Mirroring isn't ready yet — fix the items marked below."
    if failed(TOUCH):
        return Status.WARN, "Mirroring is ready. Touch control needs a few more steps."
    warnings = sum(c.status == Status.WARN for c in checks)
    if warnings:
        return Status.OK, f"Ready to mirror and control — {warnings} optional item{'s' * (warnings > 1)} to look at."
    return Status.OK, "All set — mirroring and touch control are ready."


# --- Fixes the user can trigger (blocking; run them off the UI thread) ---

def _first_usb_serial() -> str:
    from pymobiledevice3.usbmux import list_devices

    devices = [d for d in asyncio.run(list_devices()) if d.is_usb]
    if not devices:
        raise RuntimeError("No iPhone connected")
    return devices[0].serial


def _pair() -> str:
    from pymobiledevice3.lockdown import create_using_usbmux

    serial = _first_usb_serial()

    async def pair():
        lockdown = await create_using_usbmux(serial=serial, autopair=True, pair_timeout=60)
        await lockdown.close()

    asyncio.run(pair())
    return "The iPhone trusts this Mac now."


def _reveal_developer_mode() -> str:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.amfi import AmfiService

    serial = _first_usb_serial()

    async def reveal():
        lockdown = await create_using_usbmux(serial=serial, autopair=False)
        try:
            await AmfiService(lockdown).reveal_developer_mode_option_in_ui()
        finally:
            await lockdown.close()

    asyncio.run(reveal())
    return "Developer Mode now shows up in Settings › Privacy & Security on the iPhone."


def _request_camera() -> str:
    import threading
    import AVFoundation

    answered = threading.Event()
    result = {}

    def handler(granted):
        result['granted'] = granted
        answered.set()

    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVFoundation.AVMediaTypeVideo, handler)
    if not answered.wait(120):
        raise RuntimeError("No answer to the camera prompt")
    if not result['granted']:
        raise RuntimeError("Camera access was denied")
    return "Camera access allowed."


def _open(target: str, *args: str) -> Callable[[], str]:
    def run():
        subprocess.run(['open', *args, target], check=True, timeout=10)
        return ''
    return run


def _download_wda() -> str:
    project = wda_project.download()
    return f"Downloaded to {_short_path(project.parent)}."


ACTIONS: dict[str, Callable[[], str]] = {
    'pair': _pair,
    'reveal_developer_mode': _reveal_developer_mode,
    'request_camera': _request_camera,
    'open_camera_settings': _open('x-apple.systempreferences:com.apple.preference.security?Privacy_Camera'),
    'install_xcode': _open('macappstore://apps.apple.com/app/id497799835'),
    'open_xcode': _open('Xcode', '-a'),
    'download_wda': _download_wda,
}
