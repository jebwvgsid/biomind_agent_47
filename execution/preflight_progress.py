"""Optional display records from a new, approved short-inspection wrapper.

These are neither effect evidence nor model commentary. Raw execution logs keep
the exact records; only terminal and model-facing copies use these helpers.
"""
import json
from collections.abc import Mapping

PREFIX = "BIOMIND_PREFLIGHT_PROGRESS_JSON="
EVENTS = {"item_started", "process_started", "item_returned", "item_timeout",
          "item_cancelled", "item_error", "summary"}


def parse_progress(line):
    text = str(line).strip()
    if not text.startswith(PREFIX) or len(text) > 2048:
        return None
    try:
        value = json.loads(text[len(PREFIX):])
    except (TypeError, ValueError):
        return None
    if (not isinstance(value, dict) or type(value.get("version")) is not int
            or value["version"] != 1 or not isinstance(value.get("event"), str)
            or value["event"] not in EVENTS
            or set(value) - {"version", "event", "item", "index", "total", "completed", "passed",
                             "at_monotonic", "pid", "process_reaped", "returncode", "check_status", "status"}):
        return None
    if any(type(value.get(key)) is not int or value[key] < 0
           for key in ("index", "total", "completed", "passed")):
        return None
    if not 0 <= value["passed"] <= value["completed"] <= value["total"]:
        return None
    if not 0 <= value["index"] <= value["total"]:
        return None
    if not isinstance(value.get("item"), str) or len(value["item"]) > 160:
        return None
    return value


def progress_text(value):
    label = value["item"]
    count = f"已返回 {value['completed']}/{value['total']} · 范围内通过 {value['passed']}"
    event = value["event"]
    if event == "summary":
        return f"短预检收尾 · {count}"
    state = {"item_started": "开始检查", "process_started": "检查进程已启动",
             "item_returned": "检查已返回", "item_timeout": "检查超时/未完成",
             "item_cancelled": "检查已取消/未完成", "item_error": "检查未完成"}[event]
    return f"{state} {label} · {count}"


def without_progress(text):
    """Strip only complete valid progress lines from a model-facing copy."""
    return "".join(line for line in str(text).splitlines(keepends=True)
                   if parse_progress(line) is None)


def model_copy_without_progress(value):
    """Project selected tool/model data; never mutate its archived raw source."""
    if isinstance(value, str):
        return without_progress(value)
    if isinstance(value, Mapping):
        return {key: model_copy_without_progress(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [model_copy_without_progress(item) for item in value]
    return value
