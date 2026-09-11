"""Display copies only; never parse, authorize or persist business state."""
import re

_ESCAPE = re.compile(r'\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])')


def safe_text(value):
    text = _ESCAPE.sub('', str(value))
    return ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and not 127 <= ord(c) <= 159))
