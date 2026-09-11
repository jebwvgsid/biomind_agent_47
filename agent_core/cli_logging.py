"""Owned diagnostic destinations. Importing this module performs no I/O."""
import logging
import sys
import threading
from pathlib import Path

_LOCK = threading.RLock()
_OWNER = "biomind.cli"
_failed_paths = set()
_notice_sink = None


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
        from agent_core.cli_display import safe_text
        message = (safe_text(record.getMessage()).splitlines() or [""])[0][:240]
        _notice(f"{record.levelname} · {record.name}: {message} · 详情见诊断日志")


class DiagnosticFile(logging.FileHandler):
    def handleError(self, record):
        _file_failure(self.baseFilename)


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
                file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
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
