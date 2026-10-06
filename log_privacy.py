"""
Log privacy — keeps identifying data out of the log file, the terminal and the Logs tab: device
UDIDs and other identifiers, signing team IDs and names, email addresses (xcodebuild prints the
signing certificate's), IP addresses, the iPhone's name and the home folder's path.

Known values (the connected iPhone's name and UDID, the Xcode teams) are registered as the app
learns them; patterns catch the rest, e.g. in xcodebuild's and pymobiledevice3's output.
"""

import logging
import re
import threading
from pathlib import Path

_GENERIC_NAMES = {'iphone', 'ipad', 'ipod', 'personal team'}
_known: dict[str, str] = {}
_known_pattern: re.Pattern | None = None
_lock = threading.Lock()

_PATTERNS = [
    (re.compile(r'\b(Apple Development|Apple Distribution|iPhone Developer|iPhone Distribution)'
                r'(:\s*)[^\n"\']+'), r'\1\2<name>'),
    (re.compile(r'[\w.+-]+@[\w-]+(\.[\w-]+)+'), '<email>'),
    (re.compile(r'\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b'), '<uuid>'),
    (re.compile(r'\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b'), '<udid>'),     # iPhone XS and later
    (re.compile(r'\b[0-9a-fA-F]{40}\b'), '<udid>'),                     # older iPhones
    (re.compile(r'\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b'), '<ip>'),
    # IPv6: "::" or at least four colons, so times like 10:12:52 are left alone
    (re.compile(r'(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:.])'),
     lambda m: '<ip>' if '::' in m.group(0) or m.group(0).count(':') >= 4 else m.group(0)),
    (re.compile(r'\bDEVELOPMENT_TEAM\s*=\s*[A-Z0-9]{10}\b'), 'DEVELOPMENT_TEAM=<team>'),
]
_HOME = str(Path.home())


def register(value: str | None, label: str):
    """Redact `value` (e.g. the iPhone's name) as <label> from now on."""
    global _known_pattern
    value = (value or '').strip()
    if len(value) < 4 or value.lower() in _GENERIC_NAMES:
        return
    with _lock:
        if value in _known:
            return
        _known[value] = label
        ordered = sorted(_known, key=len, reverse=True)  # longest first, e.g. a team name before its parts
        _known_pattern = re.compile('|'.join(re.escape(v) for v in ordered))


def redact(text: str) -> str:
    with _lock:
        pattern = _known_pattern
    if pattern is not None:
        text = pattern.sub(lambda m: f'<{_known[m.group(0)]}>', text)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text.replace(_HOME, '~')


class RedactingFilter(logging.Filter):
    """Install on every handler: rewrites the record's message and traceback before formatting."""

    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = None
            if record.exc_info and not record.exc_text:
                record.exc_text = self._formatter.formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
        except Exception:
            pass
        return True
