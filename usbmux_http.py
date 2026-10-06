"""
HTTP over usbmux — talks to a TCP port on the iPhone (WebDriverAgent's) straight through usbmuxd,
from inside this process. No port is opened on the Mac, so browsers and other programs can't reach
WebDriverAgent through one.

Standard library and pymobiledevice3 only, no Qt: the CLI's doctor uses it too.
"""

import asyncio
import http.client
import socket
import threading


class USBConnectionError(ConnectionError):
    """The iPhone, or the server on it, can't be reached (unplugged, or nothing listening)."""


def open_socket(udid: str, port: int) -> socket.socket:
    """A blocking socket connected to `port` on the iPhone with this UDID, through usbmuxd."""
    from pymobiledevice3.usbmux import list_devices

    async def connect():
        devices = [d for d in await list_devices() if d.is_usb and d.matches_udid(udid)]
        if not devices:
            raise USBConnectionError("the iPhone isn't connected by USB")
        return await devices[0].connect(port)

    try:
        sock = asyncio.run(connect())
    except USBConnectionError:
        raise
    except Exception as e:
        raise USBConnectionError(f"can't reach port {port} on the iPhone: {e}") from e
    sock.setblocking(True)
    return sock


class _Connection(http.client.HTTPConnection):
    def __init__(self, udid: str, port: int):
        super().__init__('127.0.0.1', port)  # only used for the Host header
        self._udid = udid

    def connect(self):
        self.sock = open_socket(self._udid, self.port)


class USBHTTPClient:
    """A small HTTP/1.1 client for one port on the iPhone. Keeps one connection open and reuses it.
    Thread-safe: requests are serialized."""

    def __init__(self, port: int):
        self.port = port
        self._udid: str | None = None
        self._connection: _Connection | None = None
        self._lock = threading.Lock()

    @property
    def udid(self) -> str | None:
        return self._udid

    def set_device(self, udid: str | None, port: int | None = None):
        """Talk to this iPhone (None: to none), on `port` if given. Drops the open connection."""
        with self._lock:
            self._udid = udid
            if port is not None:
                self.port = port
            self._close()

    def close(self):
        with self._lock:
            self._close()

    def _close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict | None = None, timeout: float = 10) -> tuple[int, bytes]:
        """Send a request and return (status, body). Raises USBConnectionError if it can't be sent."""
        with self._lock:
            if self._udid is None:
                raise USBConnectionError("no iPhone connected")
            # A kept-alive connection the server has meanwhile closed fails before the request is
            # read, so it's retried once on a fresh connection
            for attempt in range(2):
                reused = self._connection is not None
                if not reused:
                    self._connection = _Connection(self._udid, self.port)
                connection = self._connection
                connection.timeout = timeout
                try:
                    if connection.sock is not None:
                        connection.sock.settimeout(timeout)
                    connection.request(method, path, body=body, headers=headers or {})
                    response = connection.getresponse()
                    data = response.read()
                    if response.will_close:
                        self._close()
                    return response.status, data
                except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError) as e:
                    self._close()
                    if reused and attempt == 0:
                        continue
                    raise USBConnectionError(f"the iPhone closed the connection: {e}") from e
                except USBConnectionError:
                    self._close()
                    raise
                except (OSError, http.client.HTTPException) as e:
                    self._close()
                    if isinstance(e, TimeoutError):
                        raise
                    raise USBConnectionError(str(e) or type(e).__name__) from e
            raise USBConnectionError("couldn't send the request")
