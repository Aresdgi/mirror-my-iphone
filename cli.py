#!/usr/bin/env python3
"""
iphone-mirror — command-line companion of the iPhone Mirror app.

    iphone-mirror            open the app
    iphone-mirror doctor     check the prerequisites and explain how to fix what's missing
    iphone-mirror tunnel     run the developer tunnel in this terminal (iOS 17+, needs sudo)
    iphone-mirror logs       follow the app's log
"""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import textwrap

import paths
from version import __version__

_ICONS = {'ok': '✓', 'warn': '!', 'fail': '✗', 'info': 'i', 'skip': '–'}
_COLORS = {'ok': '32', 'warn': '33', 'fail': '31', 'info': '36', 'skip': '90'}
_TITLE_WIDTH = 32


def _style(text: str, code: str, enabled: bool) -> str:
    return f'\033[{code}m{text}\033[0m' if enabled else text


def cmd_open(_args) -> int:
    bundle = paths.bundle_path()
    if bundle:
        return subprocess.run(['open', str(bundle)]).returncode
    os.execv(sys.executable, [sys.executable, str(paths.APP_DIR / 'main.py')])


def cmd_doctor(_args) -> int:
    import doctor

    try:
        # Make the iPhone show up as a video source; CoreMediaIO needs a run loop turn for that
        import video_stream
        from Foundation import NSDate, NSRunLoop
        video_stream.enable_screen_capture_devices()
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(1.5))
    except Exception:
        pass

    color = sys.stdout.isatty() and 'NO_COLOR' not in os.environ
    width = min(shutil.get_terminal_size().columns, 110)
    detail_width = max(30, width - _TITLE_WIDTH - 7)
    wrap = textwrap.TextWrapper(break_long_words=False, break_on_hyphens=False)
    indent = ' ' * (_TITLE_WIDTH + 7)

    print(_style(f"iPhone Mirror {__version__} — doctor", '1', color))
    checks, group = [], None
    for check in doctor.iter_checks(in_app=False):
        checks.append(check)
        if check.group != group:
            group = check.group
            print('\n' + _style(group, '1', color))
        key = check.status.value
        icon = _style(_ICONS[key], _COLORS[key], color)
        wrap.width = detail_width
        lines = wrap.wrap(check.detail) or ['']
        print(f"  {icon}  {check.title:<{_TITLE_WIDTH}}  {lines[0]}")
        for line in lines[1:]:
            print(indent + line)
        if check.fix and check.status in (doctor.Status.FAIL, doctor.Status.WARN):
            wrap.width = detail_width - 2
            for i, line in enumerate(wrap.wrap(check.fix)):
                print(indent + _style(('→ ' if i == 0 else '  ') + line, '90', color))

    status, headline = doctor.summarize(checks)
    print('\n' + _style(headline, _COLORS[status.value], color))
    return 1 if status == doctor.Status.FAIL else 0


def cmd_tunnel(_args) -> int:
    command = ['sudo', *paths.tunneld_command()]
    print(f"Starting the developer tunnel (stop it with Ctrl-C):\n  {shlex.join(command)}\n", flush=True)
    os.execvp('sudo', command)


def cmd_logs(_args) -> int:
    if not paths.LOG_FILE.exists():
        print(f"No log yet — it's created when iPhone Mirror first starts ({paths.LOG_FILE}).")
        return 1
    os.execvp('tail', ['tail', '-n', '200', '-F', str(paths.LOG_FILE)])


def main() -> int:
    parser = argparse.ArgumentParser(
        prog='iphone-mirror', description="Mirror and control your iPhone from your Mac.",
    )
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    commands = parser.add_subparsers(dest='command', metavar='command')
    commands.add_parser('open', help="open the app (default)").set_defaults(run=cmd_open)
    commands.add_parser('doctor', help="check prerequisites").set_defaults(run=cmd_doctor)
    commands.add_parser('tunnel', help="run the developer tunnel (sudo)").set_defaults(run=cmd_tunnel)
    commands.add_parser('logs', help="follow the app's log").set_defaults(run=cmd_logs)
    args = parser.parse_args()
    return getattr(args, 'run', cmd_open)(args)


if __name__ == '__main__':
    sys.exit(main())
