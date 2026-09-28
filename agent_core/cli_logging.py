"""Owned diagnostic destinations. Importing this module performs no I/O."""
import logging
import sys
import threading
from pathlib import Path
from contextlib import contextmanager
import os
import warnings
from copy import copy

_LOCK = threading.RLock()
_OWNER = "biomind.cli"
_failed_paths = set()
_notice_sink = None


@contextmanager
def coordinate_terminal_writes(owner):
    """Temporarily route only handlers sharing the human output destination.

    Handler levels, filters, logger propagation and diagnostic destinations
    remain authoritative. Restore the exact methods, including on exceptions.
    """
    def shares_terminal(stream):
        if stream is sys.stdout or stream is sys.stderr:
            return True
        try:
            fd = stream.fileno()
            return os.isatty(fd) and any(os.isatty(other.fileno()) and
                os.fstat(fd) == os.fstat(other.fileno()) for other in (sys.stdout, sys.stderr))
        except (AttributeError, OSError, ValueError):
            return False
    changed = []
    loggers = [logging.getLogger(), *[v for v in logging.Logger.manager.loggerDict.values()
                                    if isinstance(v, logging.Logger)]]
    visited = set()
    for handlers in [logger.handlers for logger in loggers] + [[logging.lastResort] if logging.lastResort else []]:
        for handler in handlers:
            if (id(handler) in visited or isinstance(handler, logging.FileHandler)
                    or not shares_terminal(getattr(handler, 'stream', None))):
                continue
            visited.add(id(handler))
            previous = handler.__dict__.get('emit')
            def route(record, handler=handler):
                from agent_core.cli_tool_activity import clean as safe_text
                if getattr(record, '_biomind_terminal_rendered', False):
                    return
                record._biomind_terminal_rendered = True
                try:
                    owner.receive('error' if record.levelno >= logging.WARNING else 'details',
                                  safe_text(handler.format(record)), source='diagnostic')
                except Exception:
                    pass
            changed.append((handler, previous))
            handler.emit = route
    original_warning = warnings.showwarning
    def showwarning(message, category, filename, lineno, file=None, line=None):
        if file is None or shares_terminal(file):
            try:
                owner.receive('error', warnings.formatwarning(message, category, filename, lineno, line),
                              source='diagnostic')
            except Exception:
                pass
        else:
            original_warning(message, category, filename, lineno, file=file, line=line)
    warnings.showwarning = showwarning
    try:
        yield
    finally:
        if warnings.showwarning is showwarning:
            warnings.showwarning = original_warning
        for handler, previous in changed:
            if previous is None:
                handler.__dict__.pop('emit', None)
            else:
                handler.emit = previous


def set_notice_sink(sink):
    global _notice_sink
    previous, _notice_sink = _notice_sink, sink
    return previous


def _notice(text):
    if _notice_sink is not None:
        try:
            _notice_sink(text)
            return
        except Exception:
            pass
    sys.stderr.write(text + "\n")


class ConsoleSummary(logging.Handler):
    def emit(self, record):
        # No traceback, ANSI, multiline JSON or prompt is copied onto the screen.
        from agent_core.cli_tool_activity import clean as safe_text
        if _notice_sink is not None:
            if getattr(record, '_biomind_terminal_rendered', False):
                return
            record._biomind_terminal_rendered = True
        message = (safe_text(record.getMessage()).splitlines() or [""])[0][:240]
        _notice(f"{record.levelname} · {record.name}: {message} · 详情见诊断日志")


class DiagnosticFile(logging.FileHandler):
    def handleError(self, record):
        _file_failure(self.baseFilename)


class RedactingFormatter(logging.Formatter):
    """Sanitize the rendered destination copy, including args and traceback.

    Formatting may cache exc_text: never mutate the shared LogRecord used by
    another handler or by controlled execution evidence.
    """
    def format(self, record):
        from agent_core.cli_tool_activity import clean
        local = copy(record)
        local.exc_text = None
        return clean(super().format(local), limit=65536)


def _file_failure(path):
    if path not in _failed_paths:
        _failed_paths.add(path)
        _notice(f"诊断日志不可写：{path}；文件诊断可能不完整，业务证据策略未改变。")


def configure_logging(level=logging.INFO, log_file=None):
    """Replace only our destinations; keep foreign handlers and propagation intact."""
    root = logging.getLogger()
    with _LOCK:
        owned = [h for h in root.handlers if getattr(h, '_biomind_owner', '') == _OWNER]
        destination = str(Path(log_file).absolute()) if log_file else None
        signature = (level, destination)
        if owned and all(getattr(h, '_biomind_signature', None) == signature for h in owned):
            return
        handlers = [ConsoleSummary(logging.WARNING)]
        if destination:
            try:
                file_handler = DiagnosticFile(destination, encoding='utf-8')
                file_handler.setLevel(level)
                file_handler.setFormatter(RedactingFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
                handlers.append(file_handler)
            except OSError:
                _file_failure(destination)
        for handler in handlers:
            handler._biomind_owner = _OWNER
            handler._biomind_signature = signature
            root.addHandler(handler)
        for handler in owned:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(min(root.level, level) if root.level else level)
