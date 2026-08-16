"""Selected-skill planning checks over explicit Plan structure only.

The checks in this module never inspect commands, step titles, or filesystem
paths.  They activate from a selected skill's loaded Planning Contract and
validate only declared dependencies and artifact references.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping


def _declares_cross_step_artifact_contract(document: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(document or "")).lower()
    return bool(
        "planning contract" in normalized
        and "build step must produce it" in normalized
        and "annotation step must consume the same artifact" in normalized
        and "cross-step carrier" in normalized
    )


def validate_selected_skill_planning_contracts(
    plan_state: Mapping[str, Any],
    selected_skill_documents: Mapping[str, str],
) -> List[Dict[str, Any]]:
    """Return Planner-owned violations for undeclared cross-step state."""
    governed_skills = {
        str(skill_id)
        for skill_id, document in dict(selected_skill_documents or {}).items()
        if _declares_cross_step_artifact_contract(str(document or ""))
    }
    if not governed_skills:
        return []

    steps = [
        dict(step)
        for step in plan_state.get("steps", []) or []
        if isinstance(step, Mapping)
    ]
    by_id = {
        str(step.get("step_id") or ""): step
        for step in steps
        if str(step.get("step_id") or "")
    }
    violations: List[Dict[str, Any]] = []
    for consumer in steps:
        consumer_skills = {
            str(value) for value in consumer.get("skill_ids", []) or [] if str(value)
        }
        if not consumer_skills.intersection(governed_skills):
            continue
        consumer_inputs = {
            str(value) for value in consumer.get("inputs", []) or [] if str(value)
        }
        for dependency_id in consumer.get("dependencies", []) or []:
            producer = by_id.get(str(dependency_id or ""))
            if not producer:
                continue
            producer_skills = {
                str(value) for value in producer.get("skill_ids", []) or [] if str(value)
            }
            governed_pair = sorted(
                consumer_skills.intersection(producer_skills).intersection(governed_skills)
            )
            if not governed_pair:
                continue
            producer_outputs = {
                str(value) for value in producer.get("outputs", []) or [] if str(value)
            }
            if producer_outputs.intersection(consumer_inputs):
                continue
            violations.append({
                "code": "PLAN_INTERMEDIATE_ARTIFACT_UNDECLARED",
                "owner": "PLANNER",
                "route": "planner_candidate_repair",
                "severity": "blocking",
                "producer_step_id": str(producer.get("step_id") or ""),
                "consumer_step_id": str(consumer.get("step_id") or ""),
                "skill_ids": governed_pair,
                "message": (
                    "A dependent selected-skill step has no explicit artifact shared "
                    "between producer outputs and consumer inputs. Declare the intermediate "
                    "directory as an existing plan-local directory artifact, or combine "
                    "build and annotation into one step."
                ),
            })
    return violations


__all__ = ["validate_selected_skill_planning_contracts"]
