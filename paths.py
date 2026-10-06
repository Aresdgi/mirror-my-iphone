"""
Paths — where Mirror my iPhone keeps its Python environment, logs, settings and WebDriverAgent.
Shared by the app, the doctor and the CLI.
"""

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

# The doctor downloads WebDriverAgent (touch control) here, and it's the only copy the app ever builds.
# It's pinned to one commit of the official repository (tag v16.13.6), checked after every download
# and before every build. The repo's WebDriverAgent submodule points at the same commit.
WDA_DIR = SUPPORT_DIR / 'WebDriverAgent'
WDA_REPO = 'https://github.com/appium/WebDriverAgent.git'
WDA_VERSION = 'v16.13.6'
WDA_COMMIT = '9d1d17ddb59e6097ddc3324b23ca9f4174507b12'
WDA_PORT = 8100

# Agent API (agent_api.py): the app serves it on 127.0.0.1 and writes its port and token here
API_PORT = 8101  # preferred; the app picks a free port when it's taken
API_FILE = SUPPORT_DIR / 'api.json'

# PID of the WDA port forwarder the app started, to recognise one left over by a crash
FORWARDER_PID_FILE = SUPPORT_DIR / 'port-forward.pid'


def bundle_path() -> Path | None:
    """The enclosing .app when running from one (sources live in <app>/Contents/Resources/app)."""
    contents = APP_DIR.parent.parent
    if contents.name == 'Contents' and contents.parent.suffix == '.app':
        return contents.parent
    return None

