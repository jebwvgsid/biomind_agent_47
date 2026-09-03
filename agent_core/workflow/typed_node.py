"""Route-neutral observability wrapper for typed LangGraph nodes."""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from typing import Any, Awaitable, Callable, Mapping, TypeVar

from langgraph.errors import GraphBubbleUp
from agent_core.workflow.outcomes import route_blind_state_projection


logger = logging.getLogger(__name__)

OutcomeT = TypeVar("OutcomeT")
HarnessFactory = Callable[[Exception, Mapping[str, Any]], Any]


def profile_typed_node(
    node_name: str,
    func: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
    *,
    outcome_field: str,
    outcome_contract: type[OutcomeT],
    profiler: Any = None,
    harness_error_factory: HarnessFactory | None = None,
) -> Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]:
    """Observe and validate a typed node without participating in routing.

    ``outcome_contract`` needs ``from_mapping`` and ``to_dict``.  This keeps the
    wrapper usable by the existing StepOutcome without coupling that frozen
    worker boundary to the new runtime outcome hierarchy.
    """

    @wraps(func)
    async def wrapped(state: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(state.get("task_id") or state.get("session_id") or "default")

        async def invoke() -> dict[str, Any]:
            try:
                raw_result = await func(route_blind_state_projection(state))
                if not isinstance(raw_result, Mapping):
                    raise TypeError(
                        f"{node_name} returned {type(raw_result).__name__}, expected mapping"
                    )
                result = dict(raw_result)
                raw_outcome = result.get(outcome_field)
                if isinstance(raw_outcome, outcome_contract):
                    outcome = raw_outcome
                else:
                    outcome = outcome_contract.from_mapping(raw_outcome or {})
                result[outcome_field] = outcome.to_dict()
                logger.info(
                    "TYPED_NODE_OUTCOME task=%s node=%s type=%s",
                    task_id,
                    node_name,
                    str(result[outcome_field].get("type") or ""),
                )
                return result
            except GraphBubbleUp:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if harness_error_factory is None:
                    raise
                outcome = harness_error_factory(exc, state)
                if not isinstance(outcome, outcome_contract):
                    outcome = outcome_contract.from_mapping(outcome)
                logger.warning(
                    "TYPED_NODE_HARNESS_ERROR task=%s node=%s exception=%s",
                    task_id,
                    node_name,
                    type(exc).__name__,
                )
                return {outcome_field: outcome.to_dict()}

        if profiler is None:
            return await invoke()
        with profiler.node(task_id, node_name):
            return await invoke()

    return wrapped


__all__ = ["profile_typed_node"]
