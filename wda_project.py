"""
WebDriverAgent project — finds WDA on disk, picks a signing team, downloads WDA, and builds
the xcodebuild command that installs and runs it on the iPhone. WDA provides touch control.
"""

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


def find_project() -> Path | None:
    """Locate WebDriverAgent.xcodeproj: $IPHONE_MIRROR_WDA, the repo submodule, the doctor's
    download location, a sibling checkout, ~/WebDriverAgent, then Spotlight."""
    candidates = []
    if os.environ.get('IPHONE_MIRROR_WDA'):
        candidates.append(Path(os.environ['IPHONE_MIRROR_WDA']).expanduser())
    candidates += [
        paths.APP_DIR / 'WebDriverAgent',
        paths.WDA_DIR,
        paths.APP_DIR.parent / 'WebDriverAgent',
        Path.home() / 'WebDriverAgent',
    ]
    for candidate in candidates:
        project = candidate if candidate.suffix == '.xcodeproj' else candidate / 'WebDriverAgent.xcodeproj'
        if (project / 'project.pbxproj').exists():
            return project
    try:
        result = subprocess.run(
            ['mdfind', 'kMDItemFSName == "WebDriverAgent.xcodeproj"'],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if (Path(line) / 'project.pbxproj').exists():
                return Path(line)
    except Exception:
        pass
    return None


def project_version(project: Path) -> str | None:
    """Release tag of a git checkout of WDA, if it has one."""
    try:
        result = subprocess.run(
            ['git', '-C', str(project.parent), 'describe', '--tags', '--always'],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


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
    """Clone the pinned WDA release into `dest`. Returns the project path; raises on failure."""
    if shutil.which('git') is None:
        raise RuntimeError("git is missing — install Xcode or its Command Line Tools first")
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + '.partial')
    shutil.rmtree(partial, ignore_errors=True)
    logger.info(f"Downloading WebDriverAgent {paths.WDA_VERSION} to {dest}")
    result = subprocess.run(
        ['git', 'clone', '--quiet', '--depth', '1', '--branch', paths.WDA_VERSION, paths.WDA_REPO, str(partial)],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        shutil.rmtree(partial, ignore_errors=True)
        raise RuntimeError(f"git clone failed: {result.stderr.strip()[-300:]}")
    shutil.rmtree(dest, ignore_errors=True)
    partial.rename(dest)
    return dest / 'WebDriverAgent.xcodeproj'
