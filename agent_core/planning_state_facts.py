"""Historical planning record shape used by draft storage and saved diagnostics.

This constructor is data compatibility only.  It grants no planning authority,
starts no lifecycle, and does not select, review or execute scientific work.
"""
from datetime import datetime, timezone
from typing import Dict, Any

PLANNER_LOOP_SCHEMA_VERSION = 1
DEFAULT_PLANNER_NO_PROGRESS_LIMIT = 2

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_planner_loop_state(
    task_id: str,
    *,
    max_identical_no_progress: int = DEFAULT_PLANNER_NO_PROGRESS_LIMIT,
) -> Dict[str, Any]:
    return {
        "schema_version": PLANNER_LOOP_SCHEMA_VERSION,
        "task_id": str(task_id or "default"),
        "phase": "reasoning",
        "status": "active",
        "agent_disposition": "continue",
        "observations": [],
        "progress": {
            "last_fingerprint": "",
            "identical_no_progress_count": 0,
            "max_identical_no_progress": max(
                1, int(max_identical_no_progress or DEFAULT_PLANNER_NO_PROGRESS_LIMIT)
            ),
        },
        "updated_at": _now(),
    }
