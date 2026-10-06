"""
WebDriverAgent project — downloads and verifies the app's own copy of WDA, picks a signing team,
and builds the xcodebuild command that installs and runs it on the iPhone. WDA provides touch control.

Only the copy in paths.WDA_DIR is ever built, and only while it's exactly the pinned commit
(paths.WDA_COMMIT) of the official repository plus this app's patches (wda-patches/): xcodebuild runs
the project's build scripts and signs the result with the user's Apple ID, so an unchecked project
would be arbitrary code.
"""

import hashlib
import logging
import os
import plistlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import paths

logger = logging.getLogger(__name__)

# WDA ships with Facebook's bundle IDs, which no other team can register
DEFAULT_RUNNER_BUNDLE_ID = 'com.facebook.WebDriverAgentRunner'

# Printed by WDA once its HTTP server is up
SERVER_URL_PATTERN = re.compile(r'ServerURLHere->(.+?)<-ServerURLHere')

# xcodebuild output → what the user should do about it, checked in this order after a failed run
_FAILURE_HINTS = [
    (re.compile(r'not (been )?(explicitly )?trusted|Untrusted Developer|invalid code signature', re.I),
     "Trust the developer on the iPhone: Settings › General › VPN & Device Management › "
     "Apple Development › Trust, then reconnect."),
    (re.compile(r'Developer ?Mode (is )?(disabled|not enabled|off)|DeveloperModeDisabled', re.I),
     "Turn on Developer Mode: Settings › Privacy & Security › Developer Mode, then restart the iPhone."),
    (re.compile(r'device (is|was) locked|is passcode protected|Unlock .+ to Continue', re.I),
     "Unlock the iPhone and keep it unlocked while WebDriverAgent starts, then reconnect."),
    (re.compile(r'maximum (number of )?(apps|App IDs)|App ID limit', re.I),
     "Your free Apple ID hit its limit of new app IDs (10 per week). Try again in a few days."),
    (re.compile(r'UI ?Automation|automation mode', re.I),
     "Turn on Settings › Developer › Enable UI Automation on the iPhone, then reconnect."),
    (re.compile(r'No Account for Team|No signing certificate|requires a development team|'
                r'No profiles for|Signing certificate is invalid', re.I),
     "Code signing failed. Open Xcode › Settings › Accounts and sign in with your Apple ID "
     "(a free one works), then reconnect."),
]


@dataclass
class SigningTeam:
    id: str
    name: str
    free: bool  # personal team of a free Apple ID: profiles expire after 7 days (renewed on each start)


# Local changes applied on top of the pinned commit (wda-patches/*.patch, in order), and the SHA-256
# of every file they change. Nothing else in the checkout may differ from the commit.
PATCHES_DIR = paths.APP_DIR / 'wda-patches'
PATCHED_FILES = {
    # 0001-bind-mjpeg-server-to-binding-ip.patch: the screen stream (MJPEG, port 9100) listens only
    # where the HTTP server does, instead of on every interface including Wi-Fi
    'WebDriverAgentLib/Routing/FBWebServer.m': '0129a0af0eab33446da6879362eabcec88fd4125aedd40faf46c715caa7b79ac',
}

# git, hardened against configuration in the checkout (hooks, fsmonitor) running anything
_GIT = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'protocol.allow=never',
        '-c', 'protocol.https.allow=always', '-c', 'advice.detachedHead=false']


def find_project() -> Path | None:
    """The app's own WebDriverAgent.xcodeproj (in paths.WDA_DIR), if it has been downloaded. Nothing
    else is ever used: no environment variables, other checkouts or Spotlight results."""
    project = paths.WDA_DIR / 'WebDriverAgent.xcodeproj'
    return project if (project / 'project.pbxproj').exists() else None


def _git(checkout: Path, *args: str, timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run([*_GIT, '-C', str(checkout), *args], capture_output=True, text=True, timeout=timeout)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(project: Path, patched: bool = True) -> str | None:
    """Why `project` can't be built, or None if it's exactly the pinned commit plus this app's
    patches (`patched`) or nothing else (not `patched`)."""
    checkout = project.parent
    try:
        head = _git(checkout, 'rev-parse', 'HEAD').stdout.strip()
        if head != paths.WDA_COMMIT:
            return (f"The WebDriverAgent copy is at commit {head[:12] or 'unknown'}, not the pinned "
                    f"{paths.WDA_COMMIT[:12]} ({paths.WDA_VERSION}). Download it again in the Doctor tab.")
        status = _git(checkout, 'status', '--porcelain', '--untracked-files=no', '-z').stdout
        changed = {entry[3:] for entry in status.split('\0') if entry}
        expected = set(PATCHED_FILES) if patched else set()
        if changed != expected or any(_sha256(checkout / name) != digest
                                      for name, digest in PATCHED_FILES.items() if patched):
            return "The WebDriverAgent copy has been modified. Download it again in the Doctor tab."
    except Exception as e:
        return f"Couldn't check the WebDriverAgent copy: {e}"
    return None


def xcodebuild_environment() -> dict:
    """Environment for xcodebuild. TEST_RUNNER_* variables reach WDA on the iPhone: USE_IP makes it
    listen on the iPhone's loopback only (usbmux connects there), not on Wi-Fi or other networks."""
    return {**os.environ, 'TEST_RUNNER_USE_IP': '127.0.0.1'}


def _read_pbxproj(project: Path) -> str:
    try:
        return (project / 'project.pbxproj').read_text(errors='replace')
    except OSError:
        return ''


def xcode_teams() -> list[SigningTeam]:
    """Teams of the Apple IDs signed in to Xcode (Xcode › Settings › Accounts)."""
    try:
        exported = subprocess.run(
            ['defaults', 'export', 'com.apple.dt.Xcode', '-'], capture_output=True, timeout=5,
        ).stdout
        prefs = plistlib.loads(exported)
    except Exception:
        return []
    # Xcode 14+ keys teams by account UUID; older versions by Apple ID
    accounts = prefs.get('IDEProvisioningTeamByIdentifier') or prefs.get('IDEProvisioningTeams') or {}
    teams = []
    for account_teams in accounts.values():
        for team in account_teams:
            if team.get('teamID'):
                teams.append(SigningTeam(
                    id=team['teamID'],
                    name=team.get('teamName', team['teamID']),
                    free=bool(team.get('isFreeProvisioningTeam')),
                ))
    return teams


def signing_team(project: Path | None) -> SigningTeam | None:
    """The team configured in the WDA project, else the first team signed in to Xcode (paid before free)."""
    if project:
        match = re.search(r'DEVELOPMENT_TEAM\s*=\s*"?([A-Z0-9]{10})"?;', _read_pbxproj(project))
        if match:
            known = {team.id: team for team in xcode_teams()}
            return known.get(match.group(1), SigningTeam(match.group(1), match.group(1), free=False))
    teams = xcode_teams()
    teams.sort(key=lambda team: team.free)
    return teams[0] if teams else None


def runner_bundle_id(project: Path, team: SigningTeam) -> str | None:
    """A bundle ID this team can register, if the project still uses Facebook's."""
    if f'PRODUCT_BUNDLE_IDENTIFIER = {DEFAULT_RUNNER_BUNDLE_ID};' in _read_pbxproj(project):
        # Keeps the app's old name: a new prefix would register another App ID (free teams get
        # 10 a week) and install a second WebDriverAgent next to the one already on the iPhone.
        return f'io.github.iphonemirror.{team.id.lower()}.WebDriverAgentRunner'
    return None


def xcodebuild_command(project: Path, udid: str, team: SigningTeam) -> list[str]:
    """xcodebuild invocation that builds, installs and runs WDA on the device (blocks while WDA runs)."""
    cmd = [
        'xcodebuild', 'test',
        '-project', str(project),
        '-scheme', 'WebDriverAgentRunner',
        '-destination', f'id={udid}',
        '-allowProvisioningUpdates',
        '-allowProvisioningDeviceRegistration',
        f'DEVELOPMENT_TEAM={team.id}',
        'CODE_SIGN_IDENTITY=Apple Development',
    ]
    bundle_id = runner_bundle_id(project, team)
    if bundle_id:
        cmd.append(f'PRODUCT_BUNDLE_IDENTIFIER={bundle_id}')
    return cmd


def diagnose_failure(output_lines: list[str]) -> str:
    """Explain a failed xcodebuild run from its last output lines."""
    text = '\n'.join(output_lines)
    for pattern, hint in _FAILURE_HINTS:
        if pattern.search(text):
            return hint
    errors = [line.strip() for line in output_lines if 'error' in line.lower()]
    if errors:
        return f"WebDriverAgent failed to start: {errors[-1][:200]}"
    return "WebDriverAgent stopped unexpectedly — see the Logs tab for xcodebuild's output."


def download(dest: Path = paths.WDA_DIR) -> Path:
    """Fetch exactly the pinned commit of the official WDA repository into `dest` and check it.
    Returns the project path; raises on failure, leaving no partial copy behind."""
    if shutil.which('git') is None:
        raise RuntimeError("git is missing — install Xcode or its Command Line Tools first")
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + '.partial')
    shutil.rmtree(partial, ignore_errors=True)
    logger.info(f"Downloading WebDriverAgent {paths.WDA_VERSION} ({paths.WDA_COMMIT[:12]}) from {paths.WDA_REPO}")
    try:
        partial.mkdir()
        for args, timeout in ((['init', '--quiet'], 30),
                              (['fetch', '--quiet', '--depth', '1', paths.WDA_REPO, paths.WDA_COMMIT], 600),
                              (['checkout', '--quiet', '--detach', 'FETCH_HEAD'], 60)):
            result = _git(partial, *args, timeout=timeout)
            if result.returncode != 0:
                raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()[-300:]}")
        problem = verify(partial / 'WebDriverAgent.xcodeproj', patched=False)
        if problem:
            raise RuntimeError(f"Downloaded WebDriverAgent failed verification, discarded it. {problem}")
        for patch in sorted(PATCHES_DIR.glob('*.patch')):
            result = _git(partial, 'apply', str(patch), timeout=30)
            if result.returncode != 0:
                raise RuntimeError(f"Couldn't apply {patch.name}: {result.stderr.strip()[-300:]}")
        problem = verify(partial / 'WebDriverAgent.xcodeproj')
        if problem:
            raise RuntimeError(f"Patched WebDriverAgent failed verification, discarded it. {problem}")
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    shutil.rmtree(dest, ignore_errors=True)
    partial.rename(dest)
    logger.info(f"WebDriverAgent {paths.WDA_VERSION} verified and patched")
    return dest / 'WebDriverAgent.xcodeproj'
