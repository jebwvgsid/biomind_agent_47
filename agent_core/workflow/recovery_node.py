"""Schema-2 Recovery node: causal Seed -> local agent -> deterministic runtime."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Mapping

from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.recovery_evidence import RecoveryEvidenceReader
from agent_core.skill_access import SkillAccessService
from agent_core.workflow.outcomes import RecoveryOutcome
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_agent import (
    RecoveryAgentLoop,
    RecoveryAgentLoopConfig,
)
from agent_core.workflow.recovery_context import (
    RecoveryObservationStore,
    archive_recovery_episode,
)
from agent_core.workflow.recovery_projection import project_recovery_seed
from agent_core.workflow.recovery_runtime import RecoveryRuntime
from agent_core.workflow.recovery_tools import (
    RecoveryLocalToolDispatcher,
    build_recovery_inspection_registry,
)
from agent_core.workflow.stage_contracts import StageContractError


Loader = Callable[[str, str], dict[str, Any]]
Saver = Callable[[str, str, dict[str, Any]], Any]


class Schema2RecoveryNode:
    """Own one fresh phase-local episode and admit only its final directive."""

    def __init__(
        self,
        *,
        data_path: str,
        model: Any,
        phase_store: PhaseArchiveStore | None = None,
        skill_service: SkillAccessService | None = None,
        evidence_reader: RecoveryEvidenceReader | None = None,
        loader: Loader = load_plan_state,
        saver: Saver = save_plan_state,
        repair_limit: int = 2,
        loop_config: RecoveryAgentLoopConfig | None = None,
    ) -> None:
        self.data_path = str(data_path)
        self.model = model
        self.phase_store = phase_store or PhaseArchiveStore(self.data_path)
        self.skill_service = skill_service
        self.evidence_reader = evidence_reader or RecoveryEvidenceReader(self.data_path)
        self.loader = loader
        self.runtime = RecoveryRuntime(
            data_path=self.data_path,
            loader=loader,
            saver=saver,
            repair_limit=repair_limit,
        )
        self.loop_config = loop_config or RecoveryAgentLoopConfig()

    @staticmethod
    def _internal(
        *,
        task_id: str,
        durable: Mapping[str, Any],
        code: str,
        detail: str = "",
        recovery_archive_ref: str = "",
    ) -> dict[str, Any]:
        scope = {
            "task_id": task_id,
            "plan_id": str(durable.get("plan_id") or ""),
            "plan_version": str(durable.get("plan_version") or ""),
            "step_id": str(durable.get("current_step_id") or ""),
            "state_generation": int(durable.get("state_generation") or 0),
        }
        return {"recovery_outcome": RecoveryOutcome(
            schema_version=1,
            type="internal_protocol_error",
            summary="Recovery phase failed closed before directive admission.",
            scope=scope,
            payload={
                "error_code": str(code),
                "source_scope": scope,
                "identity_conflict": {
                    "detail": str(detail)[:800],
                    **(
                        {"recovery_archive_ref": recovery_archive_ref}
                        if recovery_archive_ref else {}
                    ),
                },
            },
        ).to_dict()}

    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(state.get("task_id") or "")
        if not task_id:
            raise ValueError("SCHEMA2_RECOVERY_TASK_ID_REQUIRED")
        durable = self.loader(self.data_path, task_id)
        try:
            projection = project_recovery_seed(state, durable)
            if projection is None:
                return self._internal(
                    task_id=task_id,
                    durable=durable,
                    code="RECOVERY_CAUSAL_PACKET_MISSING",
                )
            seed = projection.recovery_seed
            registry = build_recovery_inspection_registry(
                seed, durable, phase_store=self.phase_store
            )
            dispatcher = RecoveryLocalToolDispatcher(
                seed=seed,
                durable=durable,
                registry=registry,
                phase_store=self.phase_store,
                skill_service=self.skill_service,
                evidence_reader=self.evidence_reader,
            )
            episode = await RecoveryAgentLoop(
                model=self.model,
                dispatcher=dispatcher,
                config=self.loop_config,
                observation_store=RecoveryObservationStore(self.phase_store),
            ).ainvoke(
                seed=seed,
                durable=durable,
                inspection_manifest=registry.provider_manifest(),
            )
        except StageContractError as exc:
            return self._internal(
                task_id=task_id, durable=durable, code=exc.code, detail=exc.detail
            )
        except Exception as exc:
            return self._internal(
                task_id=task_id,
                durable=durable,
                code=str(getattr(exc, "code", None) or "RECOVERY_AGENT_PHASE_FAILED"),
                detail=str(getattr(exc, "detail", None) or exc),
            )

        try:
            archive = archive_recovery_episode(
                phase_store=self.phase_store,
                seed=seed,
                episode=episode,
            )
        except Exception as exc:
            return self._internal(
                task_id=task_id,
                durable=durable,
                code=str(
                    getattr(exc, "code", None)
                    or "RECOVERY_PHASE_ARCHIVE_FAILED"
                ),
                detail=str(getattr(exc, "detail", None) or exc),
            )

        error = dict(episode.get("terminal_error") or {})
        directive = dict(episode.get("directive") or {})
        if error or not directive:
            return self._internal(
                task_id=task_id,
                durable=durable,
                code=str(error.get("code") or "RECOVERY_DIRECTIVE_MISSING"),
                detail=str(error.get("detail") or ""),
                recovery_archive_ref=archive.archive_ref,
            )
        admitted_state = {
            **deepcopy(dict(state)),
            "recovery_directive": directive,
            "recovery_inspected_refs": list(
                (episode.get("audit") or {}).get("inspected_refs") or ()
            ),
            "recovery_archive_ref": archive.archive_ref,
        }
        # The provider episode stays local.  Only the semantic directive and
        # bounded inspected-ref receipt reach the deterministic runtime.
        return await self.runtime.run(admitted_state)


__all__ = ["Schema2RecoveryNode"]
