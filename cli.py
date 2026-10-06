#!/usr/bin/env python3
"""
mirror-my-iphone — command-line companion of the Mirror my iPhone app.

    mirror-my-iphone            open the app
    mirror-my-iphone doctor     check the prerequisites and explain how to fix what's missing
    mirror-my-iphone logs       follow the app's log

Control the iPhone through the running app (for scripts and AI agents; see agent_api.py):

    mirror-my-iphone screenshot [FILE]      save the screen, one pixel per iPhone point
    mirror-my-iphone ui                     list the elements on screen and where to tap them
    mirror-my-iphone tap X Y                also: double-tap, long-press, swipe X1 Y1 X2 Y2
    mirror-my-iphone type TEXT              type into the focused text field
    mirror-my-iphone button NAME            home, lock, volume-up or volume-down
    mirror-my-iphone apps / open-app ID     list apps / open one by bundle ID
    mirror-my-iphone info                   device, screen size and status
    mirror-my-iphone mcp                    MCP server on stdio, for Claude Code and other agents
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap

import paths
from version import __version__

_ICONS = {'ok': '✓', 'warn': '!', 'fail': '✗', 'info': 'i', 'skip': '–'}
_COLORS = {'ok': '32', 'warn': '33', 'fail': '31', 'info': '36', 'skip': '90'}
_TITLE_WIDTH = 34


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

    print(_style(f"Mirror my iPhone {__version__} — doctor", '1', color))
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


def cmd_logs(_args) -> int:
    if not paths.LOG_FILE.exists():
        print(f"No log yet — it's created when Mirror my iPhone first starts ({paths.LOG_FILE}).")
        return 1
    os.execvp('tail', ['tail', '-n', '200', '-F', str(paths.LOG_FILE)])


def cmd_mcp(_args) -> int:
    import mcp_server
    return mcp_server.serve()


def _agent(call) -> int:
    """Run `call(client)`, print what it returns, and turn agent API errors into an exit code."""
    from agent_client import AgentAPIError, AgentClient
    try:
        output = call(AgentClient())
    except AgentAPIError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if output:
        print(output)
    return 0


def cmd_screenshot(args) -> int:
    def save(client):
        fmt = 'jpeg' if args.file.lower().endswith(('.jpg', '.jpeg')) else 'png'
        image, headers = client.screenshot(scale=args.scale, format=fmt)
        with open(args.file, 'wb') as file:
            file.write(image)
        points = headers.get('X-Screen-Points', '?').replace('x', ' × ')
        return f"Saved {args.file} (screen {points} points, {args.scale:g} pixel{'s' * (args.scale != 1)} per point)"
    return _agent(save)


def cmd_ui(args) -> int:
    from agent_client import describe_ui
    return _agent(lambda client: (json.dumps if args.json else describe_ui)(client.get('/v1/ui')))


def cmd_gesture(args) -> int:
    """tap, double-tap, long-press, swipe, type, button and open-app: POST to the endpoint of that name."""
    fields = ('x', 'y', 'x1', 'y1', 'x2', 'y2', 'duration', 'text', 'name', 'bundle_id')
    body = {key: value for key, value in vars(args).items() if key in fields and value is not None}
    if 'name' in body:
        body['name'] = body['name'].replace('-', '_')
    endpoint = '/v1/' + args.command.replace('-', '_')

    def send(client):
        client.post(endpoint, **body)
    return _agent(send)


def cmd_apps(_args) -> int:
    from agent_client import describe_apps
    return _agent(lambda client: describe_apps(client.get('/v1/apps')))


def cmd_info(_args) -> int:
    return _agent(lambda client: json.dumps(client.get('/v1/info'), indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(
        prog='mirror-my-iphone', description="Mirror and control your iPhone from your Mac.",
    )
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    commands = parser.add_subparsers(dest='command', metavar='command')
    commands.add_parser('open', help="open the app (default)").set_defaults(run=cmd_open)
    commands.add_parser('doctor', help="check prerequisites").set_defaults(run=cmd_doctor)
    commands.add_parser('logs', help="follow the app's log").set_defaults(run=cmd_logs)

    # Device control through the running app
    screenshot = commands.add_parser('screenshot', help="save the iPhone's screen")
    screenshot.add_argument('file', nargs='?', default='iphone-screenshot.png', help="PNG or JPEG file")
    screenshot.add_argument('--scale', type=float, default=1.0, help="pixels per iPhone point (default 1)")
    screenshot.set_defaults(run=cmd_screenshot)
    ui = commands.add_parser('ui', help="list the elements on screen")
    ui.add_argument('--json', action='store_true', help="print the API's JSON")
    ui.set_defaults(run=cmd_ui)
    for name, help_text in (('tap', "tap a point"), ('double-tap', "double-tap a point"),
                            ('long-press', "touch and hold a point")):
        gesture = commands.add_parser(name, help=help_text)
        gesture.add_argument('x', type=float)
        gesture.add_argument('y', type=float)
        if name == 'long-press':
            gesture.add_argument('--duration', type=float, help="seconds (default 1)")
        gesture.set_defaults(run=cmd_gesture)
    swipe = commands.add_parser('swipe', help="drag from one point to another")
    for coordinate in ('x1', 'y1', 'x2', 'y2'):
        swipe.add_argument(coordinate, type=float)
    swipe.add_argument('--duration', type=float, help="seconds (default 0.4)")
    swipe.set_defaults(run=cmd_gesture)
    type_text = commands.add_parser('type', help="type into the focused text field")
    type_text.add_argument('text')
    type_text.set_defaults(run=cmd_gesture)
    button = commands.add_parser('button', help="press a hardware button")
    button.add_argument('name', choices=['home', 'lock', 'volume-up', 'volume-down'])
    button.set_defaults(run=cmd_gesture)
    open_app = commands.add_parser('open-app', help="open an app by bundle ID")
    open_app.add_argument('bundle_id')
    open_app.set_defaults(run=cmd_gesture)
    commands.add_parser('apps', help="list the iPhone's apps").set_defaults(run=cmd_apps)
    commands.add_parser('info', help="device, screen size and status").set_defaults(run=cmd_info)
    commands.add_parser('mcp', help="run the MCP server on stdio").set_defaults(run=cmd_mcp)
    args = parser.parse_args()
    return getattr(args, 'run', cmd_open)(args)


if __name__ == '__main__':
    sys.exit(main())
