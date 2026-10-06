"""
Client of the agent API (agent_api.py), shared by the CLI's device commands and the MCP server.
Standard library only, and no Qt: it runs in short-lived processes next to the app.
"""

import json
import urllib.error
import urllib.request
from urllib.parse import urlencode

import paths

NOT_RUNNING = ("Mirror my iPhone isn't running, or agent control is off. Open the app, turn on "
               "Settings › \"Allow AI agents and scripts to control the iPhone\", then connect the "
               "iPhone with a USB cable and unlock it.")


class AgentAPIError(Exception):
    pass


class AgentClient:
    def __init__(self, timeout: float = 120):
        self.timeout = timeout
        # Never send local requests through an HTTP proxy from the environment or System Settings
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path: str, **params) -> dict:
        return json.loads(self.request('GET', path, params=params)[0])

    def post(self, path: str, **body) -> dict:
        return json.loads(self.request('POST', path, body=body)[0])

    def screenshot(self, **params) -> tuple[bytes, dict]:
        """Image bytes and the response headers (X-Screen-Points, X-Frame-Age)."""
        data, headers = self.request('GET', '/v1/screenshot', params=params)
        return data, headers

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None) -> tuple[bytes, dict]:
        # Read the address every time: the app may have restarted on another port
        try:
            config = json.loads(paths.API_FILE.read_text())
            url, token = config['url'], config['token']
        except (OSError, ValueError, KeyError):
            raise AgentAPIError(NOT_RUNNING)
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        request = urllib.request.Request(
            f"{url}{path}{'?' + query if query else ''}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.read(), dict(response.headers)
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read()).get('error')
            except ValueError:
                message = None
            raise AgentAPIError(message or f"HTTP {e.code} from the agent API")
        except (urllib.error.URLError, ConnectionError):
            raise AgentAPIError(NOT_RUNNING)
        except TimeoutError:
            raise AgentAPIError("The agent API didn't answer in time")


def describe_ui(result: dict) -> str:
    """/v1/ui as text: one element per line, with the point to tap."""
    screen = result.get('screen') or {}
    app = result.get('app') or {}
    lines = [f"In front: {app.get('name') or 'unknown app'} ({app.get('bundle_id') or '?'}). "
             f"Screen {screen.get('width', 0):g} × {screen.get('height', 0):g} points. "
             f"Keyboard {'shown' if result.get('keyboard') else 'hidden'}."]
    for element in result.get('elements') or []:
        text = element['type']
        if element.get('label'):
            text += f' "{element["label"]}"'
        if element.get('name'):
            text += f' (id {element["name"]})'
        if element.get('value'):
            text += f' value "{element["value"]}"'
        text += f" at ({element['x']:g}, {element['y']:g})"
        if element.get('enabled') is False:
            text += ', disabled'
        lines.append(text)
    if len(lines) == 1:
        lines.append("No elements found. The app may not expose accessibility information; use a screenshot.")
    return '\n'.join(lines)


def describe_apps(result: dict) -> str:
    return '\n'.join(f"{app['name']}  {app['bundle_id']}" for app in result.get('apps') or [])
