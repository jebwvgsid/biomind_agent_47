import asyncio
import io
import hashlib
import sys
from unittest.mock import patch

from agent_core.cli_display import StreamDisplayCopy, bind_terminal, emit
from execution.executor import _BoundedStreamCapture, _read_stream_chunks










def test_final_partial_utf8_display_line():
    copy=StreamDisplayCopy('stdout')
    copy.feed('末尾中文'.encode())
    copy.feed(b'',final=True)
    assert copy.tail=='末尾中文'


