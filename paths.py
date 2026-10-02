"""
Paths — where Mirror my iPhone keeps its Python environment, logs, settings and WebDriverAgent.
Shared by the app, the doctor and the CLI.
"""

import sys
from pathlib import Path

APP_NAME = 'Mirror my iPhone'
BUNDLE_ID = 'io.github.bhuwanadhikari.mirrormyiphone'
HOMEPAGE = 'https://github.com/bhuwanadhikari/Mirror-my-iPhone'

# Directory holding the app's Python sources (the repo, or Contents/Resources/app in the .app bundle)
APP_DIR = Path(__file__).resolve().parent

SUPPORT_DIR = Path.home() / 'Library' / 'Application Support' / APP_NAME
SETTINGS_FILE = SUPPORT_DIR / 'settings.ini'
LOG_DIR = Path.home() / 'Library' / 'Logs' / APP_NAME
LOG_FILE = LOG_DIR / 'mirror-my-iphone.log'

# The doctor downloads WebDriverAgent (touch control) here, pinned to a release it was tested with
WDA_DIR = SUPPORT_DIR / 'WebDriverAgent'
WDA_REPO = 'https://github.com/appium/WebDriverAgent.git'
WDA_VERSION = 'v16.13.6'
WDA_PORT = 8100

# tunneld runs as root, so it logs to /tmp rather than into the user's Library
TUNNELD_LOG = Path('/tmp/mirror-my-iphone-tunneld.log')


def bundle_path() -> Path | None:
    """The enclosing .app when running from one (sources live in <app>/Contents/Resources/app)."""
    contents = APP_DIR.parent.parent
    if contents.name == 'Contents' and contents.parent.suffix == '.app':
        return contents.parent
    return None


def tunneld_command() -> list[str]:
    """Command that starts pymobiledevice3's developer tunnel daemon (needs root)."""
    return [sys.executable, '-m', 'pymobiledevice3', 'remote', 'tunneld']
