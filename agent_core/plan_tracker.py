"""Task-level plan tracking for BioMind execution loops.

The tracker keeps checklist state outside chat messages so memory compression
cannot remove the approved plan or the current execution position.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PlanState = Dict[str, Any]
PlanStep = Dict[str, Any]

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_BLOCKED = "blocked"

_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[(?P<mark>[ xX>!])\]\s*(?P<title>.+?)\s*$")
_STEP_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:(?:Step\s*(?P<step_a>\d+))|(?P<step_b>\d+)[.)])\s*[:.)-]?\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^\s*#{2,4}\s+(?P<title>.+?)\s*$")
_STEP_ID_RE = re.compile(r"\bStep\s*(?P<num>\d+)\b", re.IGNORECASE)

_PROGRESS_START = "<!-- BIOMIND_PLAN_PROGRESS_START -->"
_PROGRESS_END = "<!-- BIOMIND_PLAN_PROGRESS_END -->"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def plan_output_dir(data_path: str | Path, task_id: str) -> Path:
    return Path(data_path) / "task_outputs" / str(task_id or "default")


def plan_state_path(data_path: str | Path, task_id: str) -> Path:
    return plan_output_dir(data_path, task_id) / "plan_state.json"


def default_plan_file(data_path: str | Path, task_id: str) -> Path:
    return plan_output_dir(data_path, task_id) / "plan.md"


def _status_from_mark(mark: str) -> str:
    if mark in {"x", "X"}:
        return STATUS_DONE
    if mark == ">":
        return STATUS_RUNNING
    if mark == "!":
        return STATUS_BLOCKED
    return STATUS_PENDING


def _mark_from_status(status: str) -> str:
    return {
        STATUS_DONE: "x",
        STATUS_RUNNING: ">",
        STATUS_BLOCKED: "!",
    }.get(str(status), " ")


def _clean_step_title(title: str) -> str:
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    return title.rstrip()


def _step_id_for(title: str, index: int) -> str:
    match = _STEP_ID_RE.search(title or "")
    if match:
        return f"step-{int(match.group('num'))}"
    return f"step-{index}"


def _dedupe_steps(steps: List[PlanStep]) -> List[PlanStep]:
    seen_titles: set[str] = set()
    seen_ids: set[str] = set()
    deduped: List[PlanStep] = []
    for step in steps:
        title_key = str(step.get("title", "")).strip().lower()
        step_id = str(step.get("step_id", "")).strip()
        if not title_key or title_key in seen_titles:
            continue
        if step_id in seen_ids:
            step_id = f"step-{len(deduped) + 1}"
            step["step_id"] = step_id
        seen_titles.add(title_key)
        seen_ids.add(step_id)
        deduped.append(step)
    return deduped


def parse_plan_steps(plan_text: str) -> List[PlanStep]:
    """Extract executable checklist steps from a planner response.

    Preferred format is "- [ ] Step N: ...". For old plans, fall back to
    explicit "Step N" lines, then numbered list items, then section headings.
    """

    created = _now()
    checkbox_steps: List[PlanStep] = []
    for line in str(plan_text or "").splitlines():
        match = _CHECKBOX_RE.match(line)
        if not match:
            continue
        title = _clean_step_title(match.group("title"))
        index = len(checkbox_steps) + 1
        checkbox_steps.append(
            {
                "step_id": _step_id_for(title, index),
                "title": title,
                "status": _status_from_mark(match.group("mark")),
                "evidence": [],
                "updated_at": created,
            }
        )
    if checkbox_steps:
        return _dedupe_steps(checkbox_steps)

    step_lines: List[Tuple[Optional[int], str]] = []
    for line in str(plan_text or "").splitlines():
        match = _STEP_LINE_RE.match(line)
        if not match:
            continue
        title = _clean_step_title(match.group("title"))
        if not title:
            continue
        number_raw = match.group("step_a") or match.group("step_b")
        number = int(number_raw) if number_raw else None
        step_lines.append((number, title))

    if step_lines:
        steps = []
        for idx, (number, title) in enumerate(step_lines, start=1):
            display_title = title if re.search(r"\bStep\s*\d+\b", title, re.I) else f"Step {number or idx}: {title}"
            steps.append(
                {
                    "step_id": f"step-{number or idx}",
                    "title": display_title,
                    "status": STATUS_PENDING,
                    "evidence": [],
                    "updated_at": created,
                }
            )
        return _dedupe_steps(steps)

    heading_steps: List[PlanStep] = []
    ignored_headings = {
        "requirement analysis",
        "step-by-step execution plan",
        "validation checklist",
        "execution checklist",
        "planner validation warnings",
        "workflow skill validation warnings",
    }
    for line in str(plan_text or "").splitlines():
        match = _HEADING_RE.match(line)
        if not match:
            continue
        title = _clean_step_title(match.group("title"))
        if not title or title.lower() in ignored_headings:
            continue
        index = len(heading_steps) + 1
        heading_steps.append(
            {
                "step_id": f"step-{index}",
                "title": f"Step {index}: {title}",
                "status": STATUS_PENDING,
                "evidence": [],
                "updated_at": created,
            }
        )
    return _dedupe_steps(heading_steps)


def build_plan_state(task_id: str, plan_text: str, plan_file: str | Path | None = None) -> PlanState:
    created = _now()
    return {
        "task_id": str(task_id or "default"),
        "created_at": created,
        "updated_at": created,
        "plan_file": str(plan_file) if plan_file else "",
        "steps": parse_plan_steps(plan_text),
    }


def load_plan_state(data_path: str | Path, task_id: str) -> PlanState:
    path = plan_state_path(data_path, task_id)
    if not path.exists():
        return {
            "task_id": str(task_id or "default"),
            "created_at": "",
            "updated_at": "",
            "plan_file": str(default_plan_file(data_path, task_id)),
            "steps": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "task_id": str(task_id or "default"),
            "created_at": "",
            "updated_at": "",
            "plan_file": str(default_plan_file(data_path, task_id)),
            "steps": [],
        }
    if not isinstance(data, dict):
        data = {}
    data.setdefault("task_id", str(task_id or "default"))
    data.setdefault("created_at", "")
    data.setdefault("updated_at", "")
    data.setdefault("plan_file", str(default_plan_file(data_path, task_id)))
    steps = data.get("steps")
    data["steps"] = steps if isinstance(steps, list) else []
    return data


def save_plan_state(data_path: str | Path, task_id: str, state: PlanState) -> Path:
    path = plan_state_path(data_path, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["task_id"] = str(task_id or state.get("task_id") or "default")
    state["updated_at"] = _now()
    if not state.get("plan_file"):
        state["plan_file"] = str(default_plan_file(data_path, task_id))
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def ensure_plan_state(
    data_path: str | Path,
    task_id: str,
    plan_text: str | None = None,
    plan_file: str | Path | None = None,
) -> PlanState:
    state = load_plan_state(data_path, task_id)
    if state.get("steps"):
        if plan_file and not state.get("plan_file"):
            state["plan_file"] = str(plan_file)
        return state
    if plan_text and str(plan_text).strip() and str(plan_text).strip() != "No plan.":
        state = build_plan_state(task_id, str(plan_text), plan_file=plan_file or default_plan_file(data_path, task_id))
        save_plan_state(data_path, task_id, state)
    return state


def reset_plan_state(data_path: str | Path, task_id: str) -> None:
    path = plan_state_path(data_path, task_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _append_evidence(step: PlanStep, event: str, detail: str = "") -> None:
    evidence = step.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    evidence.append(
        {
            "event": str(event),
            "detail": str(detail)[:2000],
            "at": _now(),
        }
    )
    step["evidence"] = evidence[-20:]
    step["updated_at"] = _now()


def _first_step_by_status(state: PlanState, statuses: Tuple[str, ...]) -> Optional[PlanStep]:
    for step in state.get("steps", []) or []:
        if isinstance(step, dict) and step.get("status") in statuses:
            return step
    return None


def mark_first_pending_running(state: PlanState, evidence: str = "") -> bool:
    if _first_step_by_status(state, (STATUS_RUNNING,)):
        return False
    step = _first_step_by_status(state, (STATUS_PENDING,))
    if not step:
        return False
    step["status"] = STATUS_RUNNING
    _append_evidence(step, "started", evidence)
    state["updated_at"] = _now()
    return True


def mark_plan_step_done(state: PlanState, evidence: str = "") -> bool:
    step = _first_step_by_status(state, (STATUS_RUNNING,))
    if not step:
        return False
    step["status"] = STATUS_DONE
    _append_evidence(step, "done", evidence)
    state["updated_at"] = _now()
    return True


def mark_plan_step_blocked(state: PlanState, evidence: str = "") -> bool:
    step = _first_step_by_status(state, (STATUS_RUNNING,))
    if not step:
        return False
    step["status"] = STATUS_BLOCKED
    _append_evidence(step, "blocked", evidence)
    state["updated_at"] = _now()
    return True


def record_current_step_evidence(state: PlanState, event: str, detail: str = "") -> bool:
    step = _first_step_by_status(state, (STATUS_RUNNING, STATUS_PENDING, STATUS_BLOCKED))
    if not step:
        return False
    _append_evidence(step, event, detail)
    state["updated_at"] = _now()
    return True


def next_action_title(state: PlanState) -> str:
    step = _first_step_by_status(state, (STATUS_RUNNING,))
    if step:
        return str(step.get("title", "")).strip()
    step = _first_step_by_status(state, (STATUS_PENDING,))
    if step:
        return str(step.get("title", "")).strip()
    step = _first_step_by_status(state, (STATUS_BLOCKED,))
    if step:
        return f"BLOCKED: {str(step.get('title', '')).strip()}"
    return "No pending steps."


def render_plan_progress(state: PlanState) -> str:
    steps = [step for step in state.get("steps", []) or [] if isinstance(step, dict)]
    if not steps:
        return "No tracked plan steps."
    lines = ["Plan checklist:"]
    for index, step in enumerate(steps, start=1):
        mark = _mark_from_status(str(step.get("status", STATUS_PENDING)))
        title = str(step.get("title", "")).strip() or f"Step {index}"
        lines.append(f"- [{mark}] {title}")
    lines.append(f"Next step: {next_action_title(state)}")
    return "\n".join(lines)


def _replace_progress_block(text: str, progress: str) -> str:
    block = f"{_PROGRESS_START}\n\n## Plan Progress\n{progress}\n\n{_PROGRESS_END}"
    pattern = re.compile(
        rf"{re.escape(_PROGRESS_START)}.*?{re.escape(_PROGRESS_END)}",
        re.DOTALL,
    )
    if pattern.search(text):
        return pattern.sub(block, text)
    return text.rstrip() + "\n\n" + block + "\n"


def update_plan_markdown_status(plan_file: str | Path, state: PlanState) -> None:
    path = Path(plan_file)
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return

    steps = [step for step in state.get("steps", []) or [] if isinstance(step, dict)]
    if not steps:
        updated = _replace_progress_block(text, render_plan_progress(state))
        path.write_text(updated, encoding="utf-8")
        return

    step_index = 0
    changed = False
    updated_lines: List[str] = []
    for line in text.splitlines():
        match = _CHECKBOX_RE.match(line)
        if match and step_index < len(steps):
            mark = _mark_from_status(str(steps[step_index].get("status", STATUS_PENDING)))
            old_mark = match.group("mark")
            if old_mark != mark:
                start, end = match.span("mark")
                line = line[:start] + mark + line[end:]
                changed = True
            step_index += 1
        updated_lines.append(line)

    if step_index == 0:
        updated = _replace_progress_block(text, render_plan_progress(state))
        path.write_text(updated, encoding="utf-8")
        return

    if changed:
        path.write_text("\n".join(updated_lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
