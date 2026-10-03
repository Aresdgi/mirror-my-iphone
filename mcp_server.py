"""
MCP server — gives AI agents (Claude Code, Claude Desktop, Cursor, …) tools to see and control the
iPhone through the running Mirror my iPhone app. It speaks MCP over stdio:

    claude mcp add mirror-my-iphone -- mirror-my-iphone mcp

Every tool is a call to the app's agent API (agent_api.py). Gestures answer with a screenshot taken
once the screen has settled, so the agent sees the result of each step without asking for it.
"""

import base64
import json
import sys

from agent_client import AgentAPIError, AgentClient, describe_apps, describe_ui
from version import __version__

PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')

INSTRUCTIONS = """\
These tools control a real iPhone connected to this Mac by USB, through the Mirror my iPhone app,
which must be open. Coordinates are iPhone points: the pixels of the `screenshot` image, with (0, 0)
at the top left. Start with `screenshot`. For exact positions of buttons, fields and cells, use
`describe_ui`. Gestures return a screenshot of the result. To type, tap a text field first, then use
`type_text`. To scroll down, swipe up. This is the user's own phone: ask before anything that is
hard to undo, such as sending messages, buying, deleting data or changing security settings."""

_SCREENSHOT = {'screenshot': {'type': 'boolean', 'default': True,
                              'description': "Return a screenshot after the action (default true)"}}
_POINT = {
    'x': {'type': 'number', 'description': "Points from the left edge (screenshot pixels)"},
    'y': {'type': 'number', 'description': "Points from the top edge (screenshot pixels)"},
}


def _tool(name: str, description: str, properties: dict | None = None, required: tuple = ()) -> dict:
    return {'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties or {}, 'required': list(required)}}


TOOLS = [
    _tool('screenshot', "Take a screenshot of the iPhone. One pixel is one iPhone point, the unit of every "
                        "coordinate the other tools take."),
    _tool('describe_ui', "List the elements on the iPhone's screen (buttons, text fields, cells, labels) with "
                         "the point to tap for each, from iOS accessibility. More exact than estimating "
                         "positions from a screenshot. Takes a second or two."),
    _tool('tap', "Tap the iPhone screen at a point.", {**_POINT, **_SCREENSHOT}, ('x', 'y')),
    _tool('double_tap', "Double-tap at a point, e.g. to zoom in on a photo or map.",
          {**_POINT, **_SCREENSHOT}, ('x', 'y')),
    _tool('long_press', "Touch and hold at a point, e.g. for context menus or to rearrange icons.",
          {**_POINT, 'duration': {'type': 'number', 'description': "Seconds to hold (default 1)"},
           **_SCREENSHOT}, ('x', 'y')),
    _tool('swipe', "Drag a finger from (x1, y1) to (x2, y2). To scroll down, swipe up (y2 < y1). A short "
                   "duration flicks with momentum; 1 second or more moves the content exactly as far as the "
                   "finger. Swiping up from the bottom edge goes home; down from the top-right corner opens "
                   "Control Center.",
          {'x1': _POINT['x'], 'y1': _POINT['y'], 'x2': _POINT['x'], 'y2': _POINT['y'],
           'duration': {'type': 'number', 'description': "Seconds (default 0.4)"}, **_SCREENSHOT},
          ('x1', 'y1', 'x2', 'y2')),
    _tool('type_text', "Type text into the focused text field (tap the field first). \"\\n\" presses Return.",
          {'text': {'type': 'string'}, **_SCREENSHOT}, ('text',)),
    _tool('press_button', "Press a hardware button. home also closes apps and Spotlight; lock turns the "
                          "display off.",
          {'name': {'type': 'string', 'enum': ['home', 'lock', 'volume_up', 'volume_down']}, **_SCREENSHOT},
          ('name',)),
    _tool('open_app', "Open an app, or bring it to the front, by bundle ID: e.g. com.apple.Preferences "
                      "(Settings), com.apple.mobilesafari (Safari). list_apps has the rest.",
          {'bundle_id': {'type': 'string'}, **_SCREENSHOT}, ('bundle_id',)),
    _tool('list_apps', "List the apps on the iPhone with their bundle IDs."),
    _tool('device_info', "The connected iPhone (name, model, iOS version), its screen size in points, and "
                         "whether touch control and the screen stream are working."),
]


class _UnknownMethod(Exception):
    pass


class _UnknownTool(Exception):
    pass


def _text(text: str) -> dict:
    return {'type': 'text', 'text': text}


def _screenshot(client: AgentClient) -> list[dict]:
    image, headers = client.screenshot(format='jpeg', quality=80)
    note = f"Screen {headers.get('X-Screen-Points', '?').replace('x', ' × ')} points."
    try:
        if float(headers.get('X-Frame-Age') or 0) > 5:
            note += (" This image is several seconds old: the iPhone's display may be off. "
                     "Ask the user to unlock it.")
    except ValueError:
        pass
    return [{'type': 'image', 'data': base64.b64encode(image).decode(), 'mimeType': 'image/jpeg'}, _text(note)]


def call_tool(client: AgentClient, name: str, args: dict) -> list[dict]:
    """Run a tool and return its MCP content. Raises AgentAPIError when the app reports a problem."""
    point = {key: args.get(key) for key in ('x', 'y')}
    gestures = {
        'tap': lambda: (client.post('/v1/tap', **point), f"Tapped ({args.get('x')}, {args.get('y')})."),
        'double_tap': lambda: (client.post('/v1/double_tap', **point),
                               f"Double-tapped ({args.get('x')}, {args.get('y')})."),
        'long_press': lambda: (client.post('/v1/long_press', **point, duration=args.get('duration', 1.0)),
                               f"Pressed and held ({args.get('x')}, {args.get('y')})."),
        'swipe': lambda: (client.post('/v1/swipe', **{key: args.get(key) for key in ('x1', 'y1', 'x2', 'y2')},
                                      duration=args.get('duration', 0.4)),
                          f"Swiped from ({args.get('x1')}, {args.get('y1')}) to ({args.get('x2')}, {args.get('y2')})."),
        'type_text': lambda: (client.post('/v1/type', text=args.get('text')), "Typed the text."),
        'press_button': lambda: (client.post('/v1/button', name=args.get('name')),
                                 f"Pressed {args.get('name')}."),
        'open_app': lambda: (client.post('/v1/open_app', bundle_id=args.get('bundle_id')),
                             f"Opened {args.get('bundle_id')}."),
    }
    if name in gestures:
        _, done = gestures[name]()
        if args.get('screenshot', True):
            return [_text(done), *_screenshot(client)]
        return [_text(done)]
    if name == 'screenshot':
        return _screenshot(client)
    if name == 'describe_ui':
        return [_text(describe_ui(client.get('/v1/ui')))]
    if name == 'list_apps':
        return [_text(describe_apps(client.get('/v1/apps')))]
    if name == 'device_info':
        return [_text(json.dumps(client.get('/v1/info'), indent=2))]
    raise _UnknownTool(name)


def _handle(client: AgentClient, method: str, params: dict) -> dict:
    if method == 'initialize':
        requested = params.get('protocolVersion')
        return {
            'protocolVersion': requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'mirror-my-iphone', 'title': 'Mirror my iPhone', 'version': __version__},
            'instructions': INSTRUCTIONS,
        }
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method == 'tools/call':
        try:
            return {'content': call_tool(client, params.get('name'), params.get('arguments') or {})}
        except AgentAPIError as e:
            return {'content': [_text(str(e))], 'isError': True}
    raise _UnknownMethod(method)


def _respond(client: AgentClient, message) -> dict | None:
    """The JSON-RPC response to one message, or None for notifications and responses."""
    if not isinstance(message, dict) or 'method' not in message or message.get('id') is None:
        return None
    response = {'jsonrpc': '2.0', 'id': message['id']}
    try:
        response['result'] = _handle(client, message['method'], message.get('params') or {})
    except _UnknownMethod:
        response['error'] = {'code': -32601, 'message': f"Method not found: {message['method']}"}
    except _UnknownTool as e:
        response['error'] = {'code': -32602, 'message': f"Unknown tool: {e}"}
    except Exception as e:
        response['error'] = {'code': -32603, 'message': f"Internal error: {e}"}
    return response


def _send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def serve() -> int:
    """Answer JSON-RPC messages from stdin, one per line, until stdin closes."""
    client = AgentClient()
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': "Parse error"}})
            continue
        if isinstance(message, list):  # a batch (protocol 2025-03-26)
            responses = [response for item in message if (response := _respond(client, item))]
            if responses:
                _send(responses)
        elif (response := _respond(client, message)) is not None:
            _send(response)
    return 0


if __name__ == '__main__':
    sys.exit(serve())
