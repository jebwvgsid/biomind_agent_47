from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent_core.workflow.context_compaction import (
    ContextBudgetPolicy,
    ContextCompactionAttemptsExhausted,
    ContextCompactionError,
    measure_compactor_wire_tokens,
)
from agent_core.skill_access import (
    SkillAccessService,
    new_skill_access_state,
    record_provider_visible_materials,
    selected_skill_materials_for_carry,
)
from agent_core.skill_manager import SkillManager
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.scoped_context import provider_visible_payload


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_from_wire(messages: Sequence[BaseMessage]) -> Mapping[str, Any]:
    assert len(messages) == 2
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    rendered = str(messages[1].content)
    prefix = rendered.index("\n") + 1
    suffix = rendered.rindex("\n</context_compaction_request>")
    envelope = json.loads(rendered[prefix:suffix])
    return envelope["request"]


def _envelope_from_wire(messages: Sequence[BaseMessage]) -> Mapping[str, Any]:
    assert len(messages) == 2
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    rendered = str(messages[1].content)
    prefix = rendered.index("\n") + 1
    suffix = rendered.rindex("\n</")
    return json.loads(rendered[prefix:suffix])


def _record_from_unit(unit: Mapping[str, Any]) -> Mapping[str, Any]:
    return json.loads(str(unit["body"]))


def _valid_plan(
    request: Mapping[str, Any],
    *,
    retain: Sequence[str],
    summarize: Sequence[str],
    summary: str = "lossy working-memory summary",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "compaction_request_id": request["compaction_request_id"],
        "source_epoch_id": request["source_epoch_id"],
        "source_manifest_hash": request["source_manifest_hash"],
        "mode": "normal",
        "retain_exact_unit_ids": list(retain),
        "summary_source_unit_ids": list(summarize),
        "summary": summary if summarize else "",
        "material_retrieval_state": [],
        "unresolved_items": [],
        "selection_rationale": "offline deterministic fixture",
    }


def _valid_segment_map(request: Mapping[str, Any]) -> dict[str, Any]:
    parent = request["parent_source_unit"]
    segment = request["segment_source_unit"]
    return {
        "schema_version": 1,
        "operation": "map_source_segment",
        "compaction_request_id": request["compaction_request_id"],
        "source_epoch_id": request["source_epoch_id"],
        "source_manifest_hash": request["source_manifest_hash"],
        "segment_unit_id": segment["unit_id"],
        "parent_source_unit_id": parent["unit_id"],
        "parent_source_content_hash": parent["content_hash"],
        "segment_content_hash": segment["content_hash"],
        "coverage": segment["coverage"],
        "summary": (
            "Lossy map for exact range "
            f"{segment['coverage']}; source remains externally recoverable."
        ),
        "missing_details": "Exact characters are retained only in source archive.",
    }


def _valid_summary_merge(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "operation": "merge_segment_summaries",
        "compaction_request_id": request["compaction_request_id"],
        "source_epoch_id": request["source_epoch_id"],
        "source_manifest_hash": request["source_manifest_hash"],
        "merge_request_id": request["merge_request_id"],
        "input_summary_ids": request["input_summary_ids"],
        "input_summary_hashes": request["input_summary_hashes"],
        "coverage_spans": request["coverage_spans"],
        "summary": (
            "Merged lossy coverage for "
            f"{len(request['input_summary_ids'])} child nodes."
        ),
    }


def _policy(
    *,
    limit: int = 10_000,
    trigger_ratio: float = 0.20,
    target_ratio: float = 0.10,
    compressor_safe_input_tokens: int | None = None,
) -> ContextBudgetPolicy:
    return ContextBudgetPolicy(
        application_context_limit_tokens=limit,
        output_reserve_tokens=0,
        estimation_margin_tokens=0,
        safety_headroom_tokens=0,
        trigger_ratio=trigger_ratio,
        target_ratio=target_ratio,
        compressor_safe_input_tokens=compressor_safe_input_tokens,
    )




class _RecordingArchiveStore(PhaseArchiveStore):
    def __init__(self, data_path: str | Path, events: list[str]) -> None:
        super().__init__(data_path)
        self.events = events

    def archive(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        record = super().archive(**kwargs)
        self.events.append(f"archive:{kwargs['phase']}")
        return record


class PhaseContextCompactionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.system = SystemMessage(id="system-1", content="stable system policy")
        self.seed = HumanMessage(id="seed-1", content="immutable phase seed")


    def _selected_skill_material(
        self,
        *,
        selected_revision: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        skills_root = Path(self.temporary.name) / "skills"
        skill_id = "fixture/local-material"
        directory = skills_root / skill_id
        directory.mkdir(parents=True, exist_ok=True)
        exact_body = (
            "---\nname: Local material\ndescription: phase-local fixture\n---\n"
            "# Procedure\nLOCAL_EXACT_SKILL_SENTINEL\n"
            + "preserve exact mutable-workspace rule\n" * 2_000
        )
        (directory / "SKILL.md").write_text(exact_body, encoding="utf-8")
        service = SkillAccessService(SkillManager(str(skills_root)))
        state = new_skill_access_state()
        state, snapshot, _created = service.ensure_snapshot(
            state,
            skill_id,
            scope="task",
            status="active",
            selected_at_task_revision=selected_revision,
        )
        state, _documents = service.load_skill_documents(
            state,
            [skill_id],
            task_revision=selected_revision,
            request_id="load-phase-local-material",
        )
        state = record_provider_visible_materials(
            state,
            [{
                "kind": "skill_document",
                "snapshot_id": snapshot["snapshot_id"],
                "content_hash": snapshot["content_hash"],
            }],
            phase="planner",
            invocation_id="planner-local-material",
            event_id="visible-local-material",
        )
        material = selected_skill_materials_for_carry(state)[0]
        return state, material
















if __name__ == "__main__":
    unittest.main()
