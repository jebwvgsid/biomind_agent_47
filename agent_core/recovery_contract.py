"""Dependency-free recovery disposition and route contract."""

from __future__ import annotations


RECOVERY_DISPOSITION_TO_ROUTE = {
    "ACCEPT_EXECUTION_FOR_VERIFICATION": "supervisor",
    "RETRY_GENERATE": "generate",
    "RETRY_EXECUTE": "execute",
    "BLOCK": "blocked",
    "INTERNAL_PROTOCOL_ERROR": "supervisor",
    "PLAN_PATCH": "planner",
    "RECONSULT": "consultant",
    "REVALIDATE": "supervisor",
    "REWIND_MANIFEST_STAGE": "generate",
    "ROLLBACK_UPSTREAM_STEP": "generate",
    "COMPLETE_STEP": "generate",
    "COMPLETE_TASK": "finish",
}


def recovery_route_for_disposition(
    disposition: str,
    *,
    decision_kind: str = "",
    requires_user_input: bool = False,
) -> str:
    """Return the sole Controller route authorized by a typed disposition."""
    if (
        str(decision_kind or "").strip().upper() == "ESCALATE"
        and bool(requires_user_input)
    ):
        return "ask_user"
    normalized = str(disposition or "").strip().upper()
    if normalized not in RECOVERY_DISPOSITION_TO_ROUTE:
        raise ValueError(
            f"Unsupported recovery disposition: {normalized or '<missing>'}"
        )
    return RECOVERY_DISPOSITION_TO_ROUTE[normalized]
