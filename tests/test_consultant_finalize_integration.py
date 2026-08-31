import asyncio
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage

from agent_core.consultation_state import (
    begin_finalize,
    commit_finalize_decision,
    initialize_finalize_protocol,
    new_consultation_state,
    seed_structured_evidence,
)
from agent_core.consultant_finalize import (
    build_finalize_context,
    parse_finalize_response,
    thaw_finalize_value,
)
from agent_core.context_lineage import ContextLineageStore, SnapshotConflictError
from agent_core.task_spec import (
    create_task_spec,
    finalize_task_spec_binding_hash,
    register_validated_semantic_ambiguity,
    resolve_registered_semantic_ambiguity,
    task_spec_revision,
)
from bio_agents import BioAnalystAgent, _classify_finalize_provider_exception


SKILL_ID = "genomics/snpeff-annotation"


_COVERAGE_DOMAIN_KEYS = (
    "artifact_schema_version",
    "protocol_schema_version",
    "wire_schema_version",
    "finalize_context_id",
    "base_contract_hash",
    "directive_count",
    "entries",
    "deterministic_constraints",
    "model_resolution_items",
    "counts",
)
_COMPILED_DOMAIN_KEYS = (
    "artifact_schema_version",
    "protocol_schema_version",
    "wire_schema_version",
    "finalize_context_id",
    "base_contract_hash",
    "coverage_artifact_hash",
    "candidate_catalog_hash",
    "decision_artifact_hashes",
    "compile_mode",
    "selected_skills",
    "constraints",
    "covered_directive_ids",
)


def _reseal_finalize_domain(
    artifact,
    *,
    domain_keys,
    id_key,
    hash_key,
    id_prefix,
):
    """Recompute the controller-domain identity, not the storage envelope."""
    resealed = deepcopy(artifact)
    domain = {
        "record_type": str(
            resealed.get("domain_record_type")
            or resealed.get("record_type")
            or ""
        ),
        **{
            key: deepcopy(resealed.get(key))
            for key in domain_keys
        },
    }
    digest = hashlib.sha256(json.dumps(
        domain,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    resealed[id_key] = f"{id_prefix}:{digest}"
    resealed[hash_key] = digest
    return resealed


class _SkillCatalog:
    def normalize_selected_skills(self, values, max_results=None):
        result = []
        for value in values or []:
            if str(value) == SKILL_ID and SKILL_ID not in result:
                result.append(SKILL_ID)
        return result[: max_results or len(result)]


def _context_from_messages(messages):
    payload = json.loads(messages[1].content)
    return payload["finalize_context"], payload


def _valid_finish(messages, *, selected_skills=None, summary="verified handoff"):
    context, _ = _context_from_messages(messages)
    catalog = {
        str(item.get("source_id")): str(item.get("source_type") or "task_contract")
        for item in context.get("semantic_provenance_catalog", [])
    }
    constraints = []
    for index, gap in enumerate(context.get("coverage_gaps", [])):
        source_ids = list(gap.get("source_ids") or [])
        constraints.append({
            "constraint_id": f"coverage-{index}-{gap['directive_id']}",
            "semantic_key": gap["semantic_key"],
            "operator": gap["operator"],
            "value": deepcopy(gap.get("normalized_value")),
            "strength": "hard",
            "confidence": 1.0,
            "provenance": [
                {
                    "source_type": catalog[source_id],
                    "source_id": source_id,
                }
                for source_id in source_ids
            ],
        })
    return {
        "action_type": "finish",
        "finalize_context_id": context["finalize_context_id"],
        "base_contract_hash": context["base_contract_hash"],
        "selected_skills": (
            list(context["resolved_selected_skill_ids"])
            if selected_skills is None
            else list(selected_skills)
        ),
        "summary": summary,
        "semantic_constraints": constraints,
    }


class _ScriptedLLM:
    def __init__(self, responders):
        self.responders = list(responders)
        self.calls = []

    def bind(self, **_kwargs):
        return self

    def model_copy(self, update=None):
        return self

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        if not self.responders:
            raise AssertionError("unexpected extra finalizer provider call")
        responder = self.responders.pop(0)
        response = responder(messages) if callable(responder) else responder
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, AIMessage):
            return response
        return AIMessage(content=str(response))


class _DirectDeepSeekLLM:
    """Small AsyncCompletions double that records the exact SDK kwargs."""

    def __init__(self, responder):
        self.calls = []
        self._responder = responder
        self.async_client = self

    async def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return self._responder(kwargs)


def _direct_completion(kwargs, *, finish_reason="stop", completion_tokens=128):
    payload = json.loads(kwargs["messages"][1]["content"])
    context = payload["finalize_context"]
    catalog = {
        str(item.get("source_id")): str(item.get("source_type") or "task_contract")
        for item in context.get("semantic_provenance_catalog", [])
    }
    constraints = []
    for index, gap in enumerate(context.get("coverage_gaps", [])):
        constraints.append({
            "constraint_id": f"direct-{index}",
            "semantic_key": gap["semantic_key"],
            "operator": gap["operator"],
            "value": deepcopy(gap.get("normalized_value")),
            "strength": gap.get("strength") or "hard",
            "confidence": 1.0,
            "provenance": [
                {"source_id": source_id, "source_type": catalog[source_id]}
                for source_id in gap.get("source_ids", [])
            ],
        })
    content = json.dumps({
        "action_type": "finish",
        "finalize_context_id": context["finalize_context_id"],
        "base_contract_hash": context["base_contract_hash"],
        "selected_skills": list(context["resolved_selected_skill_ids"]),
        "summary": "direct adapter verified",
        "semantic_constraints": constraints,
    })
    if finish_reason == "length":
        content = content[: max(1, len(content) // 2)]
    usage = SimpleNamespace(
        prompt_tokens=25_162,
        completion_tokens=completion_tokens,
        total_tokens=25_162 + completion_tokens,
    )
    return SimpleNamespace(
        id="completion-direct-1",
        model=kwargs["model"],
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=content),
        )],
        usage=usage,
    )


def _bounded_model_context(task_id="bounded-model-v4", item_count=2):
    source_id = "msg-bounded-model-v4"
    directives = []
    gaps = []
    for index in range(item_count):
        directive_id = f"bounded-ambiguity-{index}"
        candidates = []
        for label in ("a", "b"):
            candidates.append({
                "candidate_id": f"candidate-{index}-{label}",
                "meaning": f"Bounded meaning {index}-{label}",
                "normalized_value": f"value-{index}-{label}",
                "constraint": {
                    "semantic_key": f"required_behavior.choice_{index}",
                    "operator": "eq",
                    "value": f"value-{index}-{label}",
                    "strength": "hard",
                    "confidence": 1.0,
                    "provenance": [{
                        "source_type": "user_explicit",
                        "source_id": source_id,
                    }],
                },
            })
        row = {
            "directive_id": directive_id,
            "directive_kind": "required_behavior",
            "semantic_key": f"required_behavior.user_directive_{index:040x}",
            "operator": "require",
            "normalized_value": f"Choose bounded interpretation {index}",
            "normalized_text": f"Choose bounded interpretation {index}",
            "strength": "hard",
            "source_ids": [source_id],
            "needs_model_resolution": True,
            "semantic_candidates": candidates,
        }
        directives.append(row)
        gaps.append({
            key: deepcopy(value)
            for key, value in row.items()
            if key not in {"directive_kind", "needs_model_resolution"}
        })
    bound_task_spec = create_task_spec(
        "Compile the verified semantic handoff.",
        task_id=task_id,
    )
    return thaw_finalize_value(build_finalize_context(
        task_id=task_id,
        task_spec_revision=1,
        effective_facts_revision=hashlib.sha256(
            f"{task_id}:facts".encode("utf-8")
        ).hexdigest(),
        resolved_selected_skill_ids=[SKILL_ID],
        semantic_provenance_catalog=[{
            "source_id": source_id,
            "source_type": "user_explicit",
        }],
        deterministic_base_constraints=[],
        coverage_gaps=gaps,
        unresolved_semantics=[],
        effective_task_contract={
            "goal": "Resolve bounded semantic candidates",
            "required_inputs": [],
            "required_outputs": [],
            "required_behaviors": [],
            "forbidden_behaviors": [],
            "preferences": [],
            "conditional_policies": [],
            "failure_policies": [],
            "runtime_constraints": [],
            "parameters": [],
            "decisions": [],
            "source_refs": [],
            "source_addressable_directives": directives,
        },
        input_audit={"relevant": False, "status": "not_applicable"},
        consultation_requirements=[{"id": "skill-routing", "status": "satisfied"}],
        task_spec_binding_hash=finalize_task_spec_binding_hash(
            bound_task_spec,
            selected_skill_ids=[SKILL_ID],
        ),
        controller_required_constraints=[],
    ))


def _bounded_batch_completion(kwargs, *, finish_reason="stop", completion_tokens=64):
    payload = json.loads(kwargs["messages"][1]["content"])
    content = json.dumps({
        "action_type": "resolve_batch",
        "resolutions": [
            {
                "resolution_handle": item["resolution_handle"],
                "candidate_handle": item["candidates"][0]["candidate_handle"],
            }
            for item in payload["items"]
        ],
    }, separators=(",", ":"))
    if finish_reason == "length":
        content = content[: max(1, len(content) // 2)]
    usage = SimpleNamespace(
        prompt_tokens=512,
        completion_tokens=completion_tokens,
        total_tokens=512 + completion_tokens,
    )
    return SimpleNamespace(
        id="bounded-batch-completion",
        model=kwargs["model"],
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=content),
        )],
        usage=usage,
    )


class ConsultantFinalizeIntegrationTests(unittest.TestCase):
    def _agent(self, root, llm):
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.data_path = root
        agent.llm = llm
        agent.llm_timeout_seconds = 5
        agent.llm_context_limit_tokens = 200_000
        agent.llm_endpoint_id = "https://api.deepseek.com"
        agent.llm_model_id = "deepseek-chat"
        agent.llm_provider_id = "deepseek"
        agent.llm_response_extra_body = {"thinking": {"type": "disabled"}}
        agent.skill_manager = _SkillCatalog()
        agent.runtime_status = {}
        agent._consultant_runtime_preflight = lambda _skills, _task_id: ([], "")
        return agent

    def _inputs(self, task_id="finalize-integration"):
        task_spec = create_task_spec(
            "Compile the verified semantic handoff.",
            task_id=task_id,
        )
        consultation = new_consultation_state(
            f"{task_id}:consultation",
            task_id,
        )
        consultation = seed_structured_evidence(
            consultation,
            input_audit={"relevant": False, "status": "not_applicable"},
            candidate_skill_ids=[SKILL_ID],
            routing_candidate_ids=[SKILL_ID],
            runtime_evidence={SKILL_ID: {"status": "ready"}},
        )
        consultation["resolved_selected_skill_ids"] = [SKILL_ID]
        consultation = begin_finalize(consultation)
        state = {
            "task_id": task_id,
            "selected_skills": [SKILL_ID],
            "pending_human_question": {},
        }
        audit = {"relevant": False, "status": "not_applicable"}
        return state, task_spec, consultation, audit

    def _run(self, agent, state, task_spec, consultation, audit):
        return asyncio.run(agent._run_consultant_finalize_v2(
            state=state,
            task_id=state["task_id"],
            task_spec=task_spec,
            consultation_state=consultation,
            resolved_selected_skill_ids=[SKILL_ID],
            input_audit=audit,
        ))

    def _run_default(self, agent, state, task_spec, consultation, audit):
        return asyncio.run(agent._run_consultant_finalize(
            state=state,
            task_id=state["task_id"],
            task_spec=task_spec,
            consultation_state=consultation,
            resolved_selected_skill_ids=[SKILL_ID],
            input_audit=audit,
        ))

    def test_fresh_default_runner_is_v4_deterministic_and_zero_call(self):
        llm = _ScriptedLLM([])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "fresh-v4-default"
            )
            result = self._run_default(
                agent, state, task_spec, consultation, audit
            )

            self.assertEqual(result["next_step"], "planner")
            self.assertEqual(llm.calls, [])
            self.assertEqual(result["selected_skills"], [SKILL_ID])
            metrics = result["consultant_constraint_metrics"]
            self.assertEqual(metrics["compile_mode"], "controller_deterministic")
            self.assertEqual(metrics["provider_status"], "not_called")
            protocol = result["consultation_state"]["finalize_protocol"]
            self.assertEqual(protocol["schema_version"], 4)
            self.assertEqual(protocol["wire_schema_version"], 3)
            self.assertEqual(protocol["finalize_status"], "compiled")
            self.assertEqual(protocol["attempts"], [])
            self.assertTrue(Path(
                metrics["compiled_contract_ref"]
            ).is_file())

            replay = self._run_default(
                agent,
                {**state, **result},
                result["task_spec"],
                result["consultation_state"],
                audit,
            )
            self.assertEqual(replay["next_step"], "planner")
            self.assertTrue(
                replay["consultant_constraint_metrics"]["reused_from_checkpoint"]
            )
            self.assertEqual(llm.calls, [])

    def test_planner_reload_can_skip_only_live_task_spec_binding_hash(self):
        llm = _ScriptedLLM([])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "planner-live-task-binding-bypass"
            )
            result = self._run_default(
                agent, state, task_spec, consultation, audit
            )
            protocol = result["consultation_state"]["finalize_protocol"]
            drifted_task_spec = deepcopy(result["task_spec"])
            drifted_task_spec["goal"] = "Controller-projected live TaskSpec drift"

            with self.assertRaises(SnapshotConflictError):
                agent._load_verified_finalize_v4_contract(
                    task_id=state["task_id"],
                    protocol=protocol,
                    task_spec=drifted_task_spec,
                    selected_skill_ids=[SKILL_ID],
                )

            loaded = agent._load_verified_finalize_v4_contract(
                task_id=state["task_id"],
                protocol=protocol,
                task_spec=drifted_task_spec,
                selected_skill_ids=[SKILL_ID],
                enforce_task_spec_binding_hash=False,
            )

            self.assertEqual(
                loaded["compiled_contract_hash"],
                protocol["compile_outcome"]["compiled_contract_hash"],
            )
            self.assertEqual(llm.calls, [])

    def test_ready_legacy_without_intent_migrates_to_v4_child(self):
        llm = _ScriptedLLM([])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "legacy-ready-v4-child"
            )
            consultation = initialize_finalize_protocol(
                consultation,
                finalize_context_id="legacy-parent-context",
                base_contract_hash="legacy-parent-base",
            )
            result = self._run_default(
                agent, state, task_spec, consultation, audit
            )

            self.assertEqual(result["next_step"], "planner")
            self.assertEqual(llm.calls, [])
            protocol = result["consultation_state"]["finalize_protocol"]
            self.assertEqual(protocol["schema_version"], 4)
            self.assertEqual(
                protocol["parent_finalize_context_id"],
                "legacy-parent-context",
            )
            self.assertEqual(
                protocol["migration_reason"],
                "ready_without_provider_intent",
            )

    def test_inflight_legacy_dispatches_once_to_frozen_v2_runner(self):
        llm = _ScriptedLLM([])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "legacy-inflight-v2-pinned"
            )
            consultation = initialize_finalize_protocol(
                consultation,
                finalize_context_id="legacy-inflight-context",
                base_contract_hash="legacy-inflight-base",
            )
            consultation["finalize_protocol"]["status"] = "invoking"
            consultation["finalize_protocol"]["attempts"] = [
                {"attempt_id": "legacy-attempt-1"}
            ]
            calls = []

            async def frozen_v2_runner(**kwargs):
                calls.append(kwargs)
                return {"next_step": "finish", "runner": "legacy_v2"}

            agent._run_consultant_finalize_v2 = frozen_v2_runner
            result = self._run_default(
                agent, state, task_spec, consultation, audit
            )

            self.assertEqual(result["runner"], "legacy_v2")
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                calls[0]["consultation_state"]["finalize_protocol"]["status"],
                "invoking",
            )
            self.assertEqual(llm.calls, [])

    def test_real_session_1786266235_replay_reaches_planner_without_provider_or_healthcheck(self):
        run_state_path = (
            Path(__file__).resolve().parents[2]
            / "task_outputs"
            / "session_1786266235"
            / "plan_state.json"
        )
        if not run_state_path.is_file():
            self.skipTest("real session_1786266235 plan_state fixture is unavailable")
        persisted = json.loads(run_state_path.read_text(encoding="utf-8"))
        task_state = persisted["task_state"]
        consultation = deepcopy(task_state["consultation_state"])
        consultation.update({
            "phase": "finalize",
            "consultation_status": "ready",
            "completion_decision": "ready_to_finalize",
            "completion_reason": "incident_replay_v4",
            "finalize_protocol": None,
            "pending_human_question": None,
        })
        consultation.pop("terminal_diagnostic", None)
        llm = _ScriptedLLM([])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)

            def forbidden_healthcheck(*_args, **_kwargs):
                raise AssertionError("replay must not rerun the successful healthcheck")

            agent._consultant_runtime_preflight = forbidden_healthcheck
            state = {
                "task_id": "session_1786266235",
                "selected_skills": [SKILL_ID],
                "pending_human_question": {},
            }
            result = self._run_default(
                agent,
                state,
                task_state["task_spec"],
                consultation,
                task_state["input_audit"],
            )

            self.assertEqual(result["next_step"], "planner")
            self.assertEqual(llm.calls, [])
            metrics = result["consultant_constraint_metrics"]
            self.assertEqual(metrics["directive_count"], 57)
            self.assertEqual(metrics["coverage_counts"]["covered_by_base"], 12)
            self.assertEqual(
                metrics["coverage_counts"]["model_resolution_required"], 0
            )
            self.assertEqual(result["selected_skills"], [SKILL_ID])
            protocol = result["consultation_state"]["finalize_protocol"]
            self.assertEqual(protocol["attempts"], [])
            self.assertEqual(protocol["finalize_status"], "compiled")

    def test_v4_genuine_model_resolution_uses_one_bounded_batch(self):
        llm = _DirectDeepSeekLLM(
            lambda kwargs: _bounded_batch_completion(kwargs)
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "bounded-model-v4"
            )
            context = _bounded_model_context(state["task_id"], item_count=2)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )

            result = self._run_default(
                agent, state, task_spec, consultation, audit
            )

            self.assertEqual(result["next_step"], "planner")
            self.assertEqual(len(llm.calls), 1)
            self.assertGreater(llm.calls[0]["max_tokens"], 2_048)
            self.assertLessEqual(llm.calls[0]["max_tokens"], 100_000)
            self.assertFalse(llm.calls[0]["stream"])
            self.assertEqual(
                [row["role"] for row in llm.calls[0]["messages"]],
                ["system", "user"],
            )
            metrics = result["consultant_constraint_metrics"]
            self.assertEqual(metrics["compile_mode"], "model_batched")
            self.assertEqual(metrics["provider_call_count"], 1)
            self.assertEqual(metrics["completion_tokens"], 64)
            protocol = result["consultation_state"]["finalize_protocol"]
            self.assertEqual(protocol["finalize_status"], "compiled")
            capacity = json.loads(Path(
                protocol["batches"][0]["capacity_profile_ref"]
            ).read_text(encoding="utf-8"))
            self.assertEqual(
                llm.calls[0]["max_tokens"],
                capacity["effective_output_tokens"],
            )
            self.assertEqual(capacity["available_output_tokens"], 100_000)
            self.assertEqual(
                capacity["effective_output_tokens"],
                capacity["maximum_canonical_output_tokens"]
                + capacity["safety_headroom_tokens"],
            )
            preflight = json.loads(Path(
                protocol["work_preflight_ref"]
            ).read_text(encoding="utf-8"))
            self.assertTrue(preflight["admitted"])
            self.assertLessEqual(
                preflight["componentwise_worst_case"]["completion_tokens"],
                preflight["limits"]["completion_tokens"],
            )
            store = ContextLineageStore(root, state["task_id"])
            durable_preflight = store.lookup_finalize_controller_artifact(
                "work_preflight",
                artifact_hash=protocol["work_preflight_hash"],
                finalize_context_id=protocol["finalize_context_id"],
            )
            self.assertIsNotNone(durable_preflight)
            self.assertEqual(
                durable_preflight["work_preflight_ref"],
                protocol["work_preflight_ref"],
            )
            self.assertEqual(protocol["aggregate"]["resolved_resolution_ids"],
                             protocol["aggregate"]["expected_resolution_ids"])
            self.assertTrue(Path(protocol["reservation_ledger_ref"]).is_file())
            reservations = store.list_finalize_budget_reservations()
            self.assertEqual(len(reservations), 1)
            self.assertEqual(reservations[0]["status"], "consumed")

            replay = self._run_default(
                agent,
                {**state, **result},
                result["task_spec"],
                result["consultation_state"],
                audit,
            )
            self.assertEqual(replay["next_step"], "planner")
            self.assertEqual(len(llm.calls), 1)
            self.assertTrue(
                replay["consultant_constraint_metrics"]
                ["reused_from_checkpoint"]
            )

            revised_task_spec = deepcopy(result["task_spec"])
            revised_task_spec["revision"] = int(
                revised_task_spec.get("revision") or 0
            ) + 1
            stale_replay = self._run_default(
                agent,
                {**state, **result},
                revised_task_spec,
                result["consultation_state"],
                audit,
            )
            self.assertEqual(stale_replay["next_step"], "finish")
            self.assertEqual(
                stale_replay["failure_type"],
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self.assertEqual(len(llm.calls), 1)

    def test_v4_compiled_replay_requires_every_active_leaf_decision(self):
        llm = _DirectDeepSeekLLM(
            lambda kwargs: _bounded_batch_completion(kwargs)
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "bounded-model-v4-missing-leaf"
            )
            context = _bounded_model_context(state["task_id"], item_count=2)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )

            first = self._run_default(
                agent, state, task_spec, consultation, audit
            )
            self.assertEqual(first["next_step"], "planner")
            compiled = json.loads(Path(
                first["consultant_constraint_metrics"]["compiled_contract_ref"]
            ).read_text(encoding="utf-8"))
            self.assertTrue(compiled["active_leaf_decisions"])
            Path(
                compiled["active_leaf_decisions"][0]["decision_artifact_ref"]
            ).unlink()

            resumed = self._run_default(
                agent,
                {**state, **first},
                first["task_spec"],
                first["consultation_state"],
                audit,
            )

            self.assertEqual(resumed["next_step"], "finish")
            self.assertEqual(len(llm.calls), 1)
            self.assertEqual(
                resumed["failure_type"],
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self.assertEqual(
                resumed["failure_owner"],
                "controller_storage",
            )

    def test_v4_resealed_semantic_drift_is_rejected_before_replay(self):
        llm = _DirectDeepSeekLLM(
            lambda kwargs: _bounded_batch_completion(kwargs)
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "bounded-model-v4-resealed-drift"
            )
            context = _bounded_model_context(state["task_id"], item_count=2)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )

            first = self._run_default(
                agent, state, task_spec, consultation, audit
            )
            self.assertEqual(first["next_step"], "planner")
            self.assertEqual(len(llm.calls), 1)
            protocol = first["consultation_state"]["finalize_protocol"]
            store = ContextLineageStore(root, state["task_id"])
            coverage = json.loads(Path(
                protocol["coverage_artifact_ref"]
            ).read_text(encoding="utf-8"))
            compiled = json.loads(Path(
                first["consultant_constraint_metrics"]["compiled_contract_ref"]
            ).read_text(encoding="utf-8"))

            with self.subTest("deleted model-resolution rows"):
                missing_model_rows = deepcopy(coverage)
                self.assertEqual(
                    len(missing_model_rows["model_resolution_items"]),
                    2,
                )
                missing_model_rows["model_resolution_items"] = []
                missing_model_rows = _reseal_finalize_domain(
                    missing_model_rows,
                    domain_keys=_COVERAGE_DOMAIN_KEYS,
                    id_key="coverage_artifact_id",
                    hash_key="coverage_artifact_hash",
                    id_prefix="coverage",
                )
                with self.assertRaises(SnapshotConflictError):
                    store.persist_finalize_coverage_artifact(
                        missing_model_rows
                    )

            with self.subTest("model coverage disguised as deterministic"):
                disguised = deepcopy(compiled)
                disguised["compile_mode"] = "controller_deterministic"
                disguised["candidate_catalog_hash"] = ""
                disguised["decision_artifact_hashes"] = []
                disguised["active_leaf_decisions"] = []
                disguised.pop("candidate_catalog_ref", None)
                disguised = _reseal_finalize_domain(
                    disguised,
                    domain_keys=_COMPILED_DOMAIN_KEYS,
                    id_key="compiled_contract_id",
                    hash_key="compiled_contract_hash",
                    id_prefix="compiled-contract",
                )
                with self.assertRaisesRegex(
                    SnapshotConflictError,
                    "mode conflicts with coverage",
                ):
                    store.persist_finalize_compiled_contract(disguised)

            with self.subTest("selected candidate constraint deleted"):
                missing_selected_constraint = deepcopy(compiled)
                prior_constraints = list(
                    missing_selected_constraint["constraints"]
                )
                missing_selected_constraint["constraints"] = [
                    item
                    for item in prior_constraints
                    if str(item.get("semantic_key") or "")
                    != "required_behavior.choice_0"
                ]
                self.assertEqual(
                    len(missing_selected_constraint["constraints"]),
                    len(prior_constraints) - 1,
                )
                missing_selected_constraint = _reseal_finalize_domain(
                    missing_selected_constraint,
                    domain_keys=_COMPILED_DOMAIN_KEYS,
                    id_key="compiled_contract_id",
                    hash_key="compiled_contract_hash",
                    id_prefix="compiled-contract",
                )
                with self.assertRaisesRegex(
                    SnapshotConflictError,
                    "active-leaf decision set is incomplete",
                ):
                    store.persist_finalize_compiled_contract(
                        missing_selected_constraint
                    )

            with self.subTest("selected skills changed on replay"):
                changed_skill_replay = asyncio.run(
                    agent._run_consultant_finalize(
                        state={
                            **state,
                            **first,
                            "selected_skills": [],
                        },
                        task_id=state["task_id"],
                        task_spec=first["task_spec"],
                        consultation_state=first["consultation_state"],
                        resolved_selected_skill_ids=[],
                        input_audit=audit,
                    )
                )
                self.assertEqual(changed_skill_replay["next_step"], "finish")
                self.assertEqual(
                    changed_skill_replay["failure_type"],
                    "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
                )
                self.assertEqual(len(llm.calls), 1)

    def test_v4_complete_artifact_resumes_after_graph_transition_crash(self):
        llm = _DirectDeepSeekLLM(
            lambda kwargs: _bounded_batch_completion(kwargs)
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "bounded-complete-crash-resume-v4"
            )

            context = _bounded_model_context(state["task_id"], item_count=1)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )

            from agent_core.consultation_state import (
                commit_finalize_v4_transition as real_commit_v4,
            )
            crashed = {"done": False}

            def crash_after_response_artifact(owner, decision):
                if (
                    str((decision or {}).get("transition") or "")
                    == "response_complete"
                    and not crashed["done"]
                ):
                    crashed["done"] = True
                    raise RuntimeError("synthetic crash after protected response")
                return real_commit_v4(owner, decision)

            with patch(
                "bio_agents.commit_finalize_v4_transition",
                side_effect=crash_after_response_artifact,
            ):
                interrupted = self._run_default(
                    agent, state, task_spec, consultation, audit
                )
            self.assertEqual(interrupted["next_step"], "finish")
            self.assertEqual(len(llm.calls), 1)

            resumed = self._run_default(
                agent, state, task_spec, consultation, audit
            )
            self.assertEqual(resumed["next_step"], "planner")
            self.assertEqual(len(llm.calls), 1)
            self.assertEqual(
                resumed["consultation_state"]["finalize_protocol"][
                    "finalize_status"
                ],
                "compiled",
            )
            reservations = ContextLineageStore(
                root, state["task_id"]
            ).list_finalize_budget_reservations()
            self.assertEqual(len(reservations), 1)
            self.assertEqual(reservations[0]["status"], "consumed")

    def test_v4_denied_work_preflight_is_durable_and_calls_no_provider(self):
        llm = _DirectDeepSeekLLM(
            lambda kwargs: _bounded_batch_completion(kwargs)
        )
        with tempfile.TemporaryDirectory() as root, patch(
            "bio_agents.CONSULTANT_FINALIZE_MAX_PROVIDER_CALLS", 1
        ):
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "bounded-model-v4-denied"
            )
            context = _bounded_model_context(state["task_id"], item_count=2)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )

            result = self._run_default(
                agent, state, task_spec, consultation, audit
            )

            self.assertEqual(result["next_step"], "finish")
            self.assertEqual(
                result["failure_type"],
                "FINALIZE_GLOBAL_BUDGET_EXHAUSTED",
            )
            self.assertEqual(llm.calls, [])
            protocol = result["consultation_state"]["finalize_protocol"]
            self.assertEqual(protocol["provider_status"], "not_called")
            proof_path = Path(protocol["work_preflight_ref"])
            budget_path = Path(protocol["work_budget_ref"])
            self.assertTrue(proof_path.is_file())
            self.assertTrue(budget_path.is_file())
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertFalse(proof["admitted"])
            self.assertIn("provider_calls", proof["exceeded_fields"])
            diagnostic = result["consultation_state"]["terminal_diagnostic"]
            self.assertEqual(
                diagnostic["work_preflight_ref"], str(proof_path)
            )

    def test_v4_resolution_item_limit_persists_denial_before_provider_io(self):
        llm = _DirectDeepSeekLLM(
            lambda kwargs: _bounded_batch_completion(kwargs)
        )
        with tempfile.TemporaryDirectory() as root, patch(
            "bio_agents.CONSULTANT_FINALIZE_MAX_RESOLUTION_ITEMS", 1
        ):
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "bounded-model-v4-resolution-limit"
            )
            context = _bounded_model_context(state["task_id"], item_count=2)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )

            result = self._run_default(
                agent, state, task_spec, consultation, audit
            )

            self.assertEqual(result["next_step"], "finish")
            self.assertEqual(
                result["failure_type"],
                "FINALIZE_GLOBAL_BUDGET_EXHAUSTED",
            )
            self.assertEqual(llm.calls, [])
            protocol = result["consultation_state"]["finalize_protocol"]
            self.assertEqual(protocol["provider_status"], "not_called")
            preflight_path = Path(protocol["work_preflight_ref"])
            self.assertTrue(preflight_path.is_file())
            preflight = json.loads(
                preflight_path.read_text(encoding="utf-8")
            )
            self.assertFalse(preflight["admitted"])
            self.assertIn(
                "resolution_items",
                preflight["exceeded_fields"],
            )
            durable = ContextLineageStore(
                root, state["task_id"]
            ).lookup_finalize_controller_artifact(
                "work_preflight",
                artifact_hash=protocol["work_preflight_hash"],
                finalize_context_id=protocol["finalize_context_id"],
            )
            self.assertIsNotNone(durable)
            self.assertFalse(durable["admitted"])

    def test_v4_held_reservation_resumes_original_attempt_without_duplicate_call(self):
        llm = _DirectDeepSeekLLM(
            lambda kwargs: _bounded_batch_completion(kwargs)
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "bounded-held-crash-resume-v4"
            )
            context = _bounded_model_context(state["task_id"], item_count=1)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )

            with patch.object(
                ContextLineageStore,
                "dispatch_finalize_budget_reservation",
                side_effect=RuntimeError("synthetic crash before dispatch"),
            ), patch.object(
                ContextLineageStore,
                "release_finalize_budget_reservation",
                side_effect=RuntimeError("process died before held release"),
            ):
                interrupted = self._run_default(
                    agent, state, task_spec, consultation, audit
                )
            self.assertEqual(interrupted["next_step"], "finish")
            self.assertEqual(llm.calls, [])
            held = ContextLineageStore(
                root, state["task_id"]
            ).list_finalize_budget_reservations(status="held")
            self.assertEqual(len(held), 1)
            original_attempt = held[0]["attempt_id"]

            resumed = self._run_default(
                agent, state, task_spec, consultation, audit
            )
            self.assertEqual(resumed["next_step"], "planner")
            self.assertEqual(len(llm.calls), 1)
            reservations = ContextLineageStore(
                root, state["task_id"]
            ).list_finalize_budget_reservations()
            self.assertEqual(len(reservations), 1)
            self.assertEqual(reservations[0]["attempt_id"], original_attempt)
            self.assertEqual(reservations[0]["status"], "consumed")

    def test_v4_length_partial_is_never_parsed_and_repartitions_smaller(self):
        llm = None

        def responder(kwargs):
            assert llm is not None
            if len(llm.calls) == 1:
                return _bounded_batch_completion(
                    kwargs,
                    finish_reason="length",
                    completion_tokens=8_192,
                )
            return _bounded_batch_completion(
                kwargs,
                finish_reason="stop",
                completion_tokens=32,
            )

        llm = _DirectDeepSeekLLM(responder)
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "bounded-length-v4"
            )
            context = _bounded_model_context(state["task_id"], item_count=2)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )

            result = self._run_default(
                agent, state, task_spec, consultation, audit
            )

            self.assertEqual(result["next_step"], "planner")
            self.assertEqual(len(llm.calls), 3)
            first_payload = json.loads(llm.calls[0]["messages"][1]["content"])
            child_payloads = [
                json.loads(call["messages"][1]["content"])
                for call in llm.calls[1:]
            ]
            self.assertEqual(len(first_payload["items"]), 2)
            self.assertTrue(all(len(payload["items"]) == 1 for payload in child_payloads))
            self.assertNotIn("decision_batch_hash", first_payload)
            self.assertTrue(all("decision_batch_hash" not in payload for payload in child_payloads))
            protocol = result["consultation_state"]["finalize_protocol"]
            first_attempt = protocol["attempts"][0]
            self.assertEqual(first_attempt["provider_status"], "succeeded")
            self.assertEqual(first_attempt["completion_integrity"], "truncated")
            self.assertEqual(first_attempt["protocol_status"], "not_validated")
            self.assertEqual(first_attempt["finish_reason"], "length")
            self.assertEqual(first_attempt["usage"]["completion_tokens"], 8_192)
            self.assertTrue(first_attempt["partial_response_artifact_ref"])
            self.assertFalse(first_attempt.get("response_artifact_ref"))
            self.assertEqual(
                result["consultant_constraint_metrics"]["completion_tokens"],
                8_256,
            )
            reservations = ContextLineageStore(
                root, state["task_id"]
            ).list_finalize_budget_reservations()
            self.assertEqual(len(reservations), 3)
            self.assertTrue(all(row["status"] == "consumed" for row in reservations))

    def test_v4_partial_artifact_resumes_after_graph_transition_crash(self):
        llm = None

        def responder(kwargs):
            assert llm is not None
            return _bounded_batch_completion(
                kwargs,
                finish_reason="length" if len(llm.calls) == 1 else "stop",
                completion_tokens=8_192 if len(llm.calls) == 1 else 23,
            )

        llm = _DirectDeepSeekLLM(responder)
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "bounded-partial-crash-resume-v4"
            )
            context = _bounded_model_context(state["task_id"], item_count=2)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )
            from agent_core.consultation_state import (
                commit_finalize_v4_transition as real_commit_v4,
            )
            crashed = {"done": False}

            def crash_after_partial_artifact(owner, decision):
                if (
                    str((decision or {}).get("transition") or "")
                    == "response_truncated"
                    and not crashed["done"]
                ):
                    crashed["done"] = True
                    raise RuntimeError("synthetic crash after protected partial")
                return real_commit_v4(owner, decision)

            with patch(
                "bio_agents.commit_finalize_v4_transition",
                side_effect=crash_after_partial_artifact,
            ):
                interrupted = self._run_default(
                    agent, state, task_spec, consultation, audit
                )
            self.assertEqual(interrupted["next_step"], "finish")
            self.assertEqual(len(llm.calls), 1)

            resumed = self._run_default(
                agent, state, task_spec, consultation, audit
            )
            self.assertEqual(resumed["next_step"], "planner")
            self.assertEqual(len(llm.calls), 3)
            reservations = ContextLineageStore(
                root, state["task_id"]
            ).list_finalize_budget_reservations()
            self.assertEqual(len(reservations), 3)
            self.assertTrue(all(
                row["status"] == "consumed" for row in reservations
            ))

    def test_v4_unknown_finish_reason_is_preserved_and_blocks_without_crashing(self):
        llm = _DirectDeepSeekLLM(
            lambda kwargs: _bounded_batch_completion(
                kwargs,
                finish_reason="provider_specific_unknown",
                completion_tokens=17,
            )
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "bounded-unknown-finish-v4"
            )
            context = _bounded_model_context(state["task_id"], item_count=1)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )

            result = self._run_default(
                agent, state, task_spec, consultation, audit
            )

            self.assertEqual(result["next_step"], "finish")
            self.assertEqual(
                result["failure_type"],
                "FINALIZE_PROVIDER_RESOURCE_UNKNOWN",
            )
            self.assertEqual(len(llm.calls), 1)
            protocol = result["consultation_state"]["finalize_protocol"]
            attempt = protocol["attempts"][0]
            self.assertEqual(attempt["provider_status"], "succeeded")
            self.assertEqual(attempt["completion_integrity"], "unknown")
            self.assertEqual(attempt["protocol_status"], "not_validated")
            self.assertEqual(attempt["usage"]["completion_tokens"], 17)
            self.assertTrue(attempt["partial_response_artifact_ref"])
            reservations = ContextLineageStore(
                root, state["task_id"]
            ).list_finalize_budget_reservations()
            self.assertEqual(len(reservations), 1)
            self.assertEqual(reservations[0]["status"], "consumed")
            self.assertEqual(
                reservations[0]["completion_integrity"], "unknown"
            )

    def test_v4_resource_finish_without_content_uses_typed_unavailable_partial(self):
        def responder(kwargs):
            return SimpleNamespace(
                id="resource-partial",
                model=kwargs["model"],
                choices=[SimpleNamespace(
                    finish_reason="insufficient_system_resource",
                    message=SimpleNamespace(content=""),
                )],
                usage=None,
            )

        llm = _DirectDeepSeekLLM(responder)
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "bounded-resource-partial-v4"
            )
            context = _bounded_model_context(state["task_id"], item_count=1)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )
            result = self._run_default(
                agent, state, task_spec, consultation, audit
            )

            self.assertEqual(result["next_step"], "finish")
            self.assertEqual(
                result["failure_type"], "FINALIZE_PROVIDER_RESOURCE_UNKNOWN"
            )
            attempt = result["consultation_state"]["finalize_protocol"][
                "attempts"
            ][0]
            self.assertEqual(attempt["completion_integrity"], "unknown")
            partial = ContextLineageStore(
                root, state["task_id"]
            ).lookup_finalize_partial_response(
                attempt_id=attempt["attempt_id"]
            )
            self.assertIsNotNone(partial)
            self.assertFalse(partial["content_available"])
            self.assertIsNone(partial["response_content_hash"])
            reservation = ContextLineageStore(
                root, state["task_id"]
            ).list_finalize_budget_reservations()[0]
            self.assertIsNone(reservation["actual_completion_tokens"])
            self.assertGreater(reservation["reserved_completion_tokens"], 0)
            self.assertGreater(reservation["reserved_cost_microusd"], 0)

    def test_v4_tool_calls_finish_is_complete_but_wire_invalid(self):
        llm = _DirectDeepSeekLLM(
            lambda kwargs: _bounded_batch_completion(
                kwargs,
                finish_reason="tool_calls",
                completion_tokens=19,
            )
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "bounded-tool-calls-v4"
            )
            context = _bounded_model_context(state["task_id"], item_count=1)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )
            result = self._run_default(
                agent, state, task_spec, consultation, audit
            )

            self.assertEqual(result["next_step"], "finish")
            self.assertEqual(
                result["failure_type"], "FINALIZE_BATCH_PROTOCOL_INVALID"
            )
            self.assertEqual(len(llm.calls), 2)
            attempt = result["consultation_state"]["finalize_protocol"][
                "attempts"
            ][0]
            self.assertEqual(attempt["provider_status"], "succeeded")
            self.assertEqual(attempt["completion_integrity"], "complete")
            self.assertEqual(attempt["protocol_status"], "wire_invalid")
            self.assertTrue(attempt["response_artifact_ref"])
            protocol = result["consultation_state"]["finalize_protocol"]
            self.assertEqual(
                [row["call_kind"] for row in protocol["attempts"]],
                ["decision_batch", "wire_repair"],
            )
            self.assertEqual(protocol["batches"][0]["status"], "superseded")
            self.assertEqual(protocol["batches"][1]["status"], "protocol_invalid")

    def test_v4_wire_invalid_response_gets_one_bounded_repair_then_planner(self):
        llm = None

        def responder(kwargs):
            assert llm is not None
            completion = _bounded_batch_completion(kwargs)
            if len(llm.calls) == 1:
                completion.choices[0].message.content = "not-json"
            return completion

        llm = _DirectDeepSeekLLM(responder)
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "bounded-wire-repair-v4"
            )
            context = _bounded_model_context(state["task_id"], item_count=1)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )
            result = self._run_default(
                agent, state, task_spec, consultation, audit
            )

            self.assertEqual(result["next_step"], "planner")
            self.assertEqual(len(llm.calls), 2)
            first_payload = json.loads(llm.calls[0]["messages"][1]["content"])
            repair_payload = json.loads(llm.calls[1]["messages"][1]["content"])
            self.assertEqual(repair_payload["operation"], "resolve_semantic_choices")
            self.assertEqual(repair_payload["items"], first_payload["items"])
            self.assertNotIn("derivation_kind", repair_payload)
            self.assertNotIn("parent_batch_id", repair_payload)
            self.assertFalse(any(key.endswith("_hash") for key in repair_payload))
            protocol = result["consultation_state"]["finalize_protocol"]
            self.assertEqual(
                [row["call_kind"] for row in protocol["attempts"]],
                ["decision_batch", "wire_repair"],
            )
            self.assertEqual(protocol["batches"][0]["status"], "superseded")
            self.assertEqual(protocol["batches"][1]["status"], "resolved")

    def test_observation_echo_repairs_once_then_reaches_planner(self):
        echo = "✅\nRESOLVED_ENV_PREFIX=/env\nRESOLVED_SNPEFF_JAR=/env/snpEff.jar\n"
        llm = _ScriptedLLM([
            echo,
            lambda messages: json.dumps(_valid_finish(messages), ensure_ascii=False),
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs()
            first = self._run(agent, state, task_spec, consultation, audit)
            self.assertEqual(first["next_step"], "consultant")
            self.assertEqual(first["pending_human_question"], {})
            second = self._run(
                agent,
                {**state, **first},
                first["task_spec"],
                first["consultation_state"],
                audit,
            )

            self.assertEqual(second["next_step"], "planner")
            self.assertEqual(second["contract_status"], "semantic_contract_compiled")
            self.assertEqual(len(llm.calls), 2)
            self.assertTrue(all(
                [message.type for message in call] == ["system", "human"]
                for call in llm.calls
            ))
            protocol = second["consultation_state"]["finalize_protocol"]
            self.assertEqual(protocol["repair"]["wire_repair_attempt"], 1)
            self.assertEqual(protocol["provider_status"], "succeeded")
            self.assertEqual(protocol["protocol_status"], "valid")

            store = ContextLineageStore(root, state["task_id"])
            snapshots = store.list_snapshots()
            self.assertEqual(len(snapshots), 2)
            for snapshot in snapshots:
                request = json.loads(
                    Path(snapshot["request_payload_ref"]).read_text(encoding="utf-8")
                )
                self.assertTrue(all(row["diagnostic_only"] for row in request["messages"]))
                self.assertTrue(all(len(row["content"]) <= 2048 for row in request["messages"]))
            response_json = Path(snapshots[0]["response_payload_ref"]).read_text(encoding="utf-8")
            self.assertNotIn(echo, response_json)

    def test_active_parameter_facts_are_preserved_and_reach_planner(self):
        llm = _ScriptedLLM([
            lambda messages: json.dumps(_valid_finish(messages), ensure_ascii=False),
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, _, consultation, audit = self._inputs("parameter-finalize")
            task_spec = create_task_spec(
                "Run snpEff and set -gtf22.",
                task_id=state["task_id"],
            )

            result = self._run(agent, state, task_spec, consultation, audit)

            context, _ = _context_from_messages(llm.calls[0])
            parameters = context["effective_task_contract"]["parameters"]
            self.assertIsInstance(parameters, list)
            self.assertTrue(any(
                row.get("namespace") == "cli"
                and row.get("key") == "gtf22"
                and row.get("value") is True
                for row in parameters
            ))
            self.assertEqual(result["next_step"], "planner")
            self.assertEqual(result["contract_status"], "semantic_contract_compiled")

    def test_repeated_echo_blocks_without_a_third_call_or_user_question(self):
        echo = "same non-json observation"
        llm = _ScriptedLLM([echo, echo])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs("repeat-echo")
            first = self._run(agent, state, task_spec, consultation, audit)
            second = self._run(
                agent,
                {**state, **first},
                first["task_spec"],
                first["consultation_state"],
                audit,
            )

            self.assertEqual(second["next_step"], "finish")
            self.assertEqual(second["pending_human_question"], {})
            self.assertEqual(second["task_status"], "blocked")
            self.assertEqual(len(llm.calls), 2)
            protocol = second["consultation_state"]["finalize_protocol"]
            self.assertEqual(protocol["terminal_error_code"], "FINALIZE_IDENTICAL_RESPONSE_LOOP")

    def test_protected_response_wins_over_stale_graph_without_provider_reinvoke(self):
        llm = _ScriptedLLM([
            lambda messages: json.dumps(_valid_finish(messages), ensure_ascii=False),
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, stale_consultation, audit = self._inputs("checkpoint-replay")
            first = self._run(agent, state, task_spec, stale_consultation, audit)
            self.assertEqual(first["next_step"], "planner")
            replay = self._run(agent, state, task_spec, stale_consultation, audit)

            self.assertEqual(replay["next_step"], "planner")
            self.assertEqual(len(llm.calls), 1)
            self.assertTrue(
                replay["consultation_state"]["final_contract"]["response_reused_from_checkpoint"]
            )

    def test_invalid_field_patch_keeps_targets_then_second_patch_converges(self):
        def wrong_selected(messages):
            return json.dumps(
                _valid_finish(messages, selected_skills=["wrong/skill"]),
                ensure_ascii=False,
            )

        def invalid_patch(messages):
            context, human = _context_from_messages(messages)
            return json.dumps({
                "action_type": "repair_finish",
                "finalize_context_id": context["finalize_context_id"],
                "base_contract_hash": context["base_contract_hash"],
                "base_payload_hash": "wrong-hash",
                "constraint_patches": [],
                "set": {"selected_skills": [SKILL_ID]},
            }, ensure_ascii=False)

        def valid_patch(messages):
            context, human = _context_from_messages(messages)
            return json.dumps({
                "action_type": "repair_finish",
                "finalize_context_id": context["finalize_context_id"],
                "base_contract_hash": context["base_contract_hash"],
                "base_payload_hash": human["base_payload_hash"],
                "constraint_patches": [],
                "set": {"selected_skills": [SKILL_ID]},
            }, ensure_ascii=False)

        llm = _ScriptedLLM([wrong_selected, invalid_patch, valid_patch])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs("field-repair")
            first = self._run(agent, state, task_spec, consultation, audit)
            second = self._run(agent, {**state, **first}, first["task_spec"], first["consultation_state"], audit)
            repair = second["consultation_state"]["finalize_protocol"]["repair"]
            self.assertEqual(repair["allowed_payload_fields"], ["selected_skills"])
            self.assertTrue(repair["last_patch_protocol_errors"])
            third = self._run(agent, {**state, **second}, second["task_spec"], second["consultation_state"], audit)

            self.assertEqual(third["next_step"], "planner")
            self.assertEqual(len(llm.calls), 3)
            self.assertEqual(
                third["consultation_state"]["finalize_protocol"]["repair"]["field_repair_attempt"],
                2,
            )

    def test_provider_structured_object_replays_identically(self):
        def structured(messages):
            return AIMessage(
                content="{}",
                additional_kwargs={"parsed": _valid_finish(messages)},
            )

        llm = _ScriptedLLM([structured])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, stale_consultation, audit = self._inputs("parsed-replay")
            first = self._run(agent, state, task_spec, stale_consultation, audit)
            replay = self._run(agent, state, task_spec, stale_consultation, audit)

            self.assertEqual(first["next_step"], "planner")
            self.assertEqual(replay["next_step"], "planner")
            self.assertEqual(len(llm.calls), 1)
            self.assertEqual(
                first["consultation_state"]["final_contract"]["constraints"],
                replay["consultation_state"]["final_contract"]["constraints"],
            )
            self.assertTrue(
                replay["consultation_state"]["final_contract"]["response_reused_from_checkpoint"]
            )

    def test_provider_success_followed_by_artifact_write_failure_is_storage_terminal(self):
        llm = _ScriptedLLM([
            lambda messages: json.dumps(_valid_finish(messages), ensure_ascii=False),
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs("storage-failure")
            with patch.object(
                ContextLineageStore,
                "persist_finalize_response",
                side_effect=OSError("simulated protected write failure"),
            ):
                result = self._run(agent, state, task_spec, consultation, audit)

            self.assertEqual(result["next_step"], "finish")
            self.assertEqual(result["pending_human_question"], {})
            self.assertEqual(len(llm.calls), 1)
            protocol = result["consultation_state"]["finalize_protocol"]
            self.assertEqual(protocol["status"], "blocked_internal_contract")
            self.assertEqual(protocol["provider_status"], "succeeded")
            self.assertEqual(protocol["protocol_status"], "not_validated")
            self.assertEqual(protocol["terminal_owner"], "controller_storage")
            self.assertEqual(
                protocol["terminal_error_code"],
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )

    def test_idempotency_protected_transient_failure_retries_exactly_once(self):
        llm = _ScriptedLLM([
            ConnectionError("connection reset before a response"),
            lambda messages: json.dumps(_valid_finish(messages), ensure_ascii=False),
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            agent.llm_endpoint_id = "https://api.openai.com/v1"
            agent.llm_provider_id = "openai"
            agent.llm_model_id = "gpt-test"
            agent.llm_finalize_idempotency_supported = True
            state, task_spec, stale_consultation, audit = self._inputs("safe-transport")
            first = self._run(agent, state, task_spec, stale_consultation, audit)
            self.assertEqual(first["next_step"], "consultant")
            self.assertEqual(
                first["consultation_state"]["finalize_protocol"]["provider_status"],
                "transient_failed",
            )
            second = self._run(
                agent,
                state,
                task_spec,
                stale_consultation,
                audit,
            )

            self.assertEqual(second["next_step"], "planner")
            self.assertEqual(len(llm.calls), 2)
            attempts = second["consultation_state"]["finalize_protocol"]["attempts"]
            self.assertEqual([item["call_kind"] for item in attempts], ["initial", "transport_retry"])
            self.assertEqual(
                attempts[0]["canonical_request_hash"],
                attempts[1]["canonical_request_hash"],
            )
            self.assertNotEqual(attempts[0]["attempt_id"], attempts[1]["attempt_id"])
            self.assertNotEqual(attempts[0]["call_id"], attempts[1]["call_id"])

    def test_v4_proven_no_completion_transport_failure_retries_once(self):
        llm = None

        def responder(kwargs):
            assert llm is not None
            if len(llm.calls) == 1:
                raise ConnectionRefusedError(
                    "provider rejected before accepting a request"
                )
            return _bounded_batch_completion(kwargs)

        llm = _DirectDeepSeekLLM(responder)
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "safe-transport-v4"
            )
            context = _bounded_model_context(state["task_id"], item_count=1)
            agent._build_consultant_finalize_context = lambda **_kwargs: deepcopy(
                context
            )

            first = self._run_default(
                agent, state, task_spec, consultation, audit
            )
            self.assertEqual(first["next_step"], "consultant")
            self.assertEqual(len(llm.calls), 1)
            second = self._run_default(
                agent,
                {**state, **first},
                first["task_spec"],
                first["consultation_state"],
                audit,
            )

            self.assertEqual(second["next_step"], "planner")
            self.assertEqual(len(llm.calls), 2)
            attempts = second["consultation_state"]["finalize_protocol"][
                "attempts"
            ]
            self.assertEqual(
                [row["call_kind"] for row in attempts],
                ["decision_batch", "transport_retry"],
            )
            self.assertEqual(
                attempts[0]["wire_request_hash"],
                attempts[1]["wire_request_hash"],
            )
            self.assertNotEqual(
                attempts[0]["attempt_lineage_hash"],
                attempts[1]["attempt_lineage_hash"],
            )
            reservations = ContextLineageStore(
                root, state["task_id"]
            ).list_finalize_budget_reservations()
            self.assertEqual(len(reservations), 2)
            reservations_by_kind = {
                row["call_kind"]: row for row in reservations
            }
            self.assertEqual(
                reservations_by_kind["decision_batch"]["provider_status"],
                "transient_failed",
            )
            self.assertEqual(
                reservations_by_kind["transport_retry"]["provider_status"],
                "succeeded",
            )
            self.assertTrue(
                reservations_by_kind["transport_retry"][
                    "retry_authorization_hash"
                ]
            )

    def test_untyped_provider_failure_is_outcome_unknown_and_never_reinvoked(self):
        # Retry words in prose are not a machine failure API.
        llm = _ScriptedLLM([
            RuntimeError("connection reset timeout status code 503")
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, stale_consultation, audit = self._inputs("fatal-replay")
            first = self._run(agent, state, task_spec, stale_consultation, audit)
            self.assertEqual(first["next_step"], "finish")
            self.assertEqual(
                first["consultation_state"]["finalize_protocol"]["provider_status"],
                "outcome_unknown",
            )
            replay = self._run(agent, state, task_spec, stale_consultation, audit)

            self.assertEqual(replay["next_step"], "finish")
            self.assertEqual(replay["pending_human_question"], {})
            self.assertEqual(len(llm.calls), 1)
            protocol = replay["consultation_state"]["finalize_protocol"]
            self.assertEqual(len(protocol["attempts"]), 1)
            self.assertEqual(protocol["provider_status"], "outcome_unknown")
            self.assertEqual(
                protocol["terminal_error_code"],
                "FINALIZE_PROVIDER_OUTCOME_UNKNOWN",
            )

    def test_typed_retryable_http_status_requires_explicit_idempotency_support(self):
        class _TypedHttpError(RuntimeError):
            status_code = 429

        without_idempotency = _classify_finalize_provider_exception(
            _TypedHttpError("fatal timeout prose must not affect classification"),
            idempotency_supported=False,
        )
        with_idempotency = _classify_finalize_provider_exception(
            _TypedHttpError("arbitrary prose"),
            idempotency_supported=True,
        )
        unknown_exception = _classify_finalize_provider_exception(
            RuntimeError("connection reset timeout status code 503"),
            idempotency_supported=True,
        )

        self.assertEqual(without_idempotency["provider_status"], "outcome_unknown")
        self.assertFalse(without_idempotency["retry_safe"])
        self.assertEqual(with_idempotency["provider_status"], "transient_failed")
        self.assertTrue(with_idempotency["retry_safe"])
        self.assertEqual(unknown_exception["provider_status"], "outcome_unknown")
        self.assertFalse(unknown_exception["retry_safe"])

    def test_explicit_idempotency_support_sends_key_in_json_object_mode(self):
        class _BindingLLM(_ScriptedLLM):
            def __init__(self):
                super().__init__([
                    lambda messages: json.dumps(
                        _valid_finish(messages),
                        ensure_ascii=False,
                    )
                ])
                self.bind_calls = []

            def bind(self, **kwargs):
                self.bind_calls.append(deepcopy(kwargs))
                return self

        llm = _BindingLLM()
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            agent.llm_finalize_idempotency_supported = True
            state, task_spec, consultation, audit = self._inputs(
                "json-mode-idempotency"
            )
            result = self._run(agent, state, task_spec, consultation, audit)

            self.assertEqual(result["next_step"], "planner")
            self.assertEqual(len(llm.bind_calls), 1)
            headers = llm.bind_calls[0].get("extra_headers") or {}
            self.assertEqual(set(headers), {"Idempotency-Key"})
            self.assertTrue(headers["Idempotency-Key"].startswith("finalize:"))

    def test_compiled_graph_with_missing_protected_artifact_blocks_as_checkpoint_error(self):
        llm = _ScriptedLLM([
            lambda messages: json.dumps(_valid_finish(messages), ensure_ascii=False),
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs("compiled-artifact-missing")
            first = self._run(agent, state, task_spec, consultation, audit)
            protocol = first["consultation_state"]["finalize_protocol"]
            Path(protocol["attempts"][-1]["response_artifact_ref"]).unlink()

            resumed = self._run(
                agent,
                {**state, **first},
                first["task_spec"],
                first["consultation_state"],
                audit,
            )
            self.assertEqual(resumed["next_step"], "finish")
            self.assertEqual(len(llm.calls), 1)
            resumed_protocol = resumed["consultation_state"]["finalize_protocol"]
            self.assertEqual(
                resumed_protocol["terminal_error_code"],
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self.assertEqual(resumed_protocol["terminal_owner"], "controller_storage")

    def test_compiled_graph_without_durable_protocol_outcome_cannot_reach_planner(self):
        llm = _ScriptedLLM([
            lambda messages: json.dumps(_valid_finish(messages), ensure_ascii=False),
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "compiled-outcome-missing"
            )
            first = self._run(agent, state, task_spec, consultation, audit)
            attempt = first["consultation_state"]["finalize_protocol"]["attempts"][-1]
            manifest_path = Path(attempt["response_artifact_ref"]).with_name(
                "response.manifest.json"
            )
            (manifest_path.parent / "protocol_outcome.json").unlink()

            resumed = self._run(
                agent,
                {**state, **first},
                first["task_spec"],
                first["consultation_state"],
                audit,
            )

            self.assertEqual(resumed["next_step"], "finish")
            self.assertEqual(len(llm.calls), 1)
            resumed_protocol = resumed["consultation_state"]["finalize_protocol"]
            self.assertEqual(
                resumed_protocol["terminal_error_code"],
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self.assertEqual(resumed_protocol["terminal_owner"], "controller_storage")

    def test_compiled_graph_with_tampered_final_contract_cannot_reach_planner(self):
        llm = _ScriptedLLM([
            lambda messages: json.dumps(_valid_finish(messages), ensure_ascii=False),
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "compiled-contract-tamper"
            )
            first = self._run(agent, state, task_spec, consultation, audit)
            tampered_consultation = deepcopy(first["consultation_state"])
            tampered_consultation["final_contract"]["summary"] = "TAMPERED HANDOFF"
            tampered_consultation["final_contract"]["constraints"] = []

            resumed = self._run(
                agent,
                {**state, **first},
                first["task_spec"],
                tampered_consultation,
                audit,
            )

            self.assertEqual(resumed["next_step"], "finish")
            self.assertEqual(resumed["pending_human_question"], {})
            self.assertEqual(len(llm.calls), 1)
            resumed_protocol = resumed["consultation_state"]["finalize_protocol"]
            self.assertEqual(
                resumed_protocol["terminal_error_code"],
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self.assertEqual(resumed_protocol["terminal_owner"], "controller_storage")
            self.assertNotIn(
                "TAMPERED HANDOFF",
                str(resumed["messages"][0].content),
            )

    def test_raw_user_prohibition_is_source_addressable_and_compiled(self):
        llm = _ScriptedLLM([
            lambda messages: json.dumps(_valid_finish(messages), ensure_ascii=False),
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            task_id = "raw-prohibition-coverage"
            state, _, consultation, audit = self._inputs(task_id)
            task_spec = create_task_spec(
                "Annotate variants. Do not guess a public species database.",
                task_id=task_id,
            )
            result = self._run(agent, state, task_spec, consultation, audit)

            context, _ = _context_from_messages(llm.calls[0])
            prohibition_gaps = [
                row
                for row in context["coverage_gaps"]
                if str(row.get("semantic_key") or "").startswith(
                    "forbidden_behavior.user_directive_"
                )
            ]
            self.assertEqual(len(prohibition_gaps), 1)
            self.assertEqual(prohibition_gaps[0]["operator"], "forbid")
            self.assertIn(
                "Do not guess a public species database",
                prohibition_gaps[0]["normalized_value"],
            )
            directives = {
                row["directive_id"]: row
                for row in context["effective_task_contract"][
                    "source_addressable_directives"
                ]
            }
            self.assertEqual(
                directives[prohibition_gaps[0]["directive_id"]]["normalized_text"],
                prohibition_gaps[0]["normalized_value"],
            )
            self.assertEqual(result["next_step"], "planner")
            final_constraints = result["consultation_state"]["final_contract"][
                "constraints"
            ]
            self.assertTrue(any(
                str(row.get("semantic_key") or "").startswith(
                    "forbidden_behavior.user_directive_"
                )
                and row.get("value") == prohibition_gaps[0]["normalized_value"]
                for row in final_constraints
            ))

    def test_needs_user_replay_ignores_stale_top_level_question_and_checks_nested_binding(self):
        def ask_for_registered_ambiguity(messages):
            context, _ = _context_from_messages(messages)
            return json.dumps({
                "action_type": "ask",
                "finalize_context_id": context["finalize_context_id"],
                "base_contract_hash": context["base_contract_hash"],
                "ambiguity_ids": ["database-choice"],
                "question": "Which database should be used?",
            }, ensure_ascii=False)

        llm = _ScriptedLLM([ask_for_registered_ambiguity])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, base_spec, consultation, audit = self._inputs(
                "needs-user-binding"
            )
            source_id = base_spec["source_refs"][0]
            registered = register_validated_semantic_ambiguity(
                base_spec,
                {
                    "constraint_id": "database-choice",
                    "semantic_key": "required_behavior.database_choice",
                    "operator": "eq",
                    "value": ["database-a", "database-b"],
                    "strength": "ambiguous",
                    "confidence": 1.0,
                    "needs_clarification": True,
                    "unresolved_reason": "two source-backed values remain possible",
                    "alternatives": ["database-a", "database-b"],
                    "provenance": [{
                        "source_type": "user_explicit",
                        "source_id": source_id,
                    }],
                },
                provenance_catalog=[{
                    "source_id": source_id,
                    "source_type": "user_explicit",
                    "label": "initial request",
                }],
            )
            first = self._run(agent, state, registered, consultation, audit)
            self.assertEqual(first["next_step"], "ask_user")

            stale_top_level = {
                "ambiguity_id": "wrong-id",
                "question": "stale question",
                "finalize_context_id": "wrong-context",
                "base_contract_hash": "wrong-hash",
                "task_spec_revision": 999,
            }
            replay = self._run(
                agent,
                {**state, **first, "pending_human_question": stale_top_level},
                first["task_spec"],
                first["consultation_state"],
                audit,
            )
            self.assertEqual(replay["next_step"], "ask_user")
            self.assertEqual(
                replay["pending_human_question"]["ambiguity_id"],
                "database-choice",
            )
            self.assertEqual(len(llm.calls), 1)

            corrupted = deepcopy(first["consultation_state"])
            corrupted["pending_human_question"]["base_contract_hash"] = "wrong-hash"
            blocked = self._run(
                agent,
                {**state, "pending_human_question": first["pending_human_question"]},
                first["task_spec"],
                corrupted,
                audit,
            )
            self.assertEqual(blocked["next_step"], "finish")
            self.assertEqual(blocked["pending_human_question"], {})
            self.assertEqual(
                blocked["consultation_state"]["finalize_protocol"][
                    "terminal_error_code"
                ],
                "FINALIZE_CHECKPOINT_ARTIFACT_MISSING",
            )
            self.assertEqual(len(llm.calls), 1)

    def test_session_1786243169_artifacts_replay_through_isolated_wire_repair(self):
        """Keep the incident fixture as an executable regression boundary."""
        run_root = (
            Path(__file__).resolve().parents[2]
            / "benchmarks"
            / "biomind_agent_benchmark"
            / "runs"
            / "task_outputs"
            / "session_1786243169"
        )
        trace_root = run_root / "context_trace"
        call_ids = [
            "consultant_finalize-0003-6b29e079909f",
            "consultant_finalize-0004-5b3b8ff96280",
            "consultant_finalize-0005-db804a5e2a18",
        ]
        self.assertTrue(trace_root.is_dir(), "required incident replay fixture is missing")

        incident_responses = [
            json.loads((trace_root / call_id / "response.json").read_text(encoding="utf-8"))[
                "content"
            ]
            for call_id in call_ids
        ]
        historical_roles = [
            [
                row["role"]
                for row in json.loads(
                    (trace_root / call_id / "request.json").read_text(encoding="utf-8")
                )["messages"]
            ]
            for call_id in call_ids
        ]
        self.assertEqual(
            historical_roles,
            [
                ["system", "human", "assistant", "system"],
                ["system", "human", "assistant", "system", "system"],
                ["system", "human", "assistant", "system", "system"],
            ],
        )
        self.assertEqual(len(set(incident_responses)), 1)

        metrics = json.loads((run_root / "run_metrics.json").read_text(encoding="utf-8"))
        command_events = [
            row for row in metrics.get("events", []) if row.get("type") == "command"
        ]
        self.assertEqual(len(command_events), 1)
        self.assertTrue(command_events[0]["success"])
        self.assertEqual(command_events[0]["returncode"], 0)

        llm = _ScriptedLLM([
            incident_responses[0],
            lambda messages: json.dumps(_valid_finish(messages), ensure_ascii=False),
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "session-1786243169-replay"
            )
            first = self._run(agent, state, task_spec, consultation, audit)
            second = self._run(
                agent,
                {**state, **first},
                first["task_spec"],
                first["consultation_state"],
                audit,
            )

            context, _ = _context_from_messages(llm.calls[0])
            incident_results = [
                parse_finalize_response(raw, finalize_context=context)
                for raw in incident_responses
            ]
            self.assertTrue(all(not row["valid"] for row in incident_results))
            self.assertEqual(
                {row["error_code"] for row in incident_results},
                {"FINALIZE_RESPONSE_NOT_JSON"},
            )
            self.assertTrue(all(row["provider_status"] == "succeeded" for row in incident_results))

            self.assertEqual(first["next_step"], "consultant")
            self.assertEqual(second["next_step"], "planner")
            self.assertEqual(second["pending_human_question"], {})
            self.assertEqual(second["selected_skills"], [SKILL_ID])
            self.assertEqual(len(llm.calls), 2)
            self.assertTrue(all(
                [message.type for message in call] == ["system", "human"]
                for call in llm.calls
            ))
            repair_text = "\n".join(str(message.content) for message in llm.calls[1])
            self.assertNotIn("LOAD_SKILL_DOC", repair_text)
            self.assertNotIn('"action_type": "execute"', repair_text.lower())
            self.assertNotIn("supported action format", repair_text.lower())

    def test_answered_ambiguity_replaces_context_and_resets_repair_state(self):
        def ask_for_registered_ambiguity(messages):
            context, _ = _context_from_messages(messages)
            return json.dumps({
                "action_type": "ask",
                "finalize_context_id": context["finalize_context_id"],
                "base_contract_hash": context["base_contract_hash"],
                "ambiguity_ids": ["database-choice"],
                "question": "Which database should be used?",
            }, ensure_ascii=False)

        llm = _ScriptedLLM([
            ask_for_registered_ambiguity,
            lambda messages: json.dumps(_valid_finish(messages), ensure_ascii=False),
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, base_spec, consultation, audit = self._inputs("ambiguity-answer")
            source_id = base_spec["source_refs"][0]
            registered = register_validated_semantic_ambiguity(
                base_spec,
                {
                    "constraint_id": "database-choice",
                    "semantic_key": "required_behavior.database_choice",
                    "operator": "eq",
                    "value": ["database-a", "database-b"],
                    "strength": "ambiguous",
                    "confidence": 1.0,
                    "needs_clarification": True,
                    "unresolved_reason": "two source-backed values remain possible",
                    "alternatives": ["database-a", "database-b"],
                    "provenance": [{
                        "source_type": "user_explicit",
                        "source_id": source_id,
                    }],
                },
                provenance_catalog=[{
                    "source_id": source_id,
                    "source_type": "user_explicit",
                    "label": "initial request",
                }],
            )
            first = self._run(agent, state, registered, consultation, audit)
            self.assertEqual(first["next_step"], "ask_user")
            self.assertEqual(
                first["pending_human_question"]["task_spec_revision"],
                task_spec_revision(registered),
            )
            old_protocol = first["consultation_state"]["finalize_protocol"]

            answered_spec = resolve_registered_semantic_ambiguity(
                first["task_spec"],
                "database-choice",
                "database-a",
                question=first["pending_human_question"]["question"],
                source_message_id="answer-database-choice",
                source_task_id=state["task_id"],
            )
            answered_consultation = commit_finalize_decision(
                first["consultation_state"],
                {
                    "transition": "ambiguity_answer_applied",
                    "task_spec_revision": task_spec_revision(answered_spec),
                    "ambiguity_id": "database-choice",
                },
            )
            second = self._run(
                agent,
                {**state, "pending_human_question": {}},
                answered_spec,
                answered_consultation,
                audit,
            )

            self.assertEqual(second["next_step"], "planner")
            self.assertEqual(second["pending_human_question"], {})
            self.assertEqual(len(llm.calls), 2)
            new_protocol = second["consultation_state"]["finalize_protocol"]
            self.assertNotEqual(
                new_protocol["finalize_context_id"],
                old_protocol["finalize_context_id"],
            )
            self.assertEqual(new_protocol["repair"]["wire_repair_attempt"], 0)
            self.assertEqual(new_protocol["repair"]["field_repair_attempt"], 0)
            self.assertEqual(len(new_protocol["attempts"]), 1)

    def test_competing_worker_waits_for_live_owner_then_reuses_one_response(self):
        async def scenario(root):
            class _BlockingLLM(_ScriptedLLM):
                def __init__(self):
                    super().__init__([])
                    self.started = asyncio.Event()
                    self.release = asyncio.Event()

                async def ainvoke(self, messages):
                    self.calls.append(list(messages))
                    if len(self.calls) > 1:
                        raise AssertionError("competing worker invoked the provider")
                    self.started.set()
                    await self.release.wait()
                    return AIMessage(content=json.dumps(
                        _valid_finish(messages),
                        ensure_ascii=False,
                    ))

            llm = _BlockingLLM()
            agent = self._agent(root, llm)
            state, task_spec, stale_consultation, audit = self._inputs(
                "live-owner-race"
            )
            kwargs = {
                "state": state,
                "task_id": state["task_id"],
                "task_spec": task_spec,
                "consultation_state": stale_consultation,
                "resolved_selected_skill_ids": [SKILL_ID],
                "input_audit": audit,
            }
            owner = asyncio.create_task(agent._run_consultant_finalize_v2(**kwargs))
            await asyncio.wait_for(llm.started.wait(), timeout=2)
            observer = asyncio.create_task(agent._run_consultant_finalize_v2(**kwargs))
            await asyncio.sleep(0.1)

            self.assertEqual(len(llm.calls), 1)
            self.assertEqual(
                list(Path(root).glob(
                    "task_outputs/live-owner-race/context_trace/"
                    "finalize_protocol/attempts/*/reconciliation.json"
                )),
                [],
            )
            llm.release.set()
            return await asyncio.gather(owner, observer), llm

        with tempfile.TemporaryDirectory() as root:
            (owner_result, observer_result), llm = asyncio.run(scenario(root))
            self.assertEqual(owner_result["next_step"], "planner")
            self.assertEqual(observer_result["next_step"], "planner")
            self.assertEqual(len(llm.calls), 1)
            self.assertFalse(
                owner_result["consultation_state"]["final_contract"][
                    "response_reused_from_checkpoint"
                ]
            )
            self.assertTrue(
                observer_result["consultation_state"]["final_contract"][
                    "response_reused_from_checkpoint"
                ]
            )

    def test_deepseek_direct_adapter_emits_verified_max_tokens(self):
        llm = _DirectDeepSeekLLM(
            lambda kwargs: _direct_completion(kwargs, finish_reason="stop")
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "deepseek-wire-payload"
            )
            result = self._run(agent, state, task_spec, consultation, audit)

            self.assertEqual(result["next_step"], "planner")
            self.assertEqual(len(llm.calls), 1)
            wire = llm.calls[0]
            self.assertEqual(wire["max_tokens"], 100_000)
            self.assertNotIn("max_completion_tokens", wire)
            self.assertFalse(wire["stream"])
            self.assertEqual(
                [item["role"] for item in wire["messages"]],
                ["system", "user"],
            )
            store = ContextLineageStore(root, state["task_id"])
            attempts = store.list_finalize_attempts()
            effective = store.lookup_finalize_effective_wire_settings(
                attempt_id=attempts[0]["attempt_id"]
            )
            self.assertEqual(effective["output_token_parameter"], "max_tokens")
            self.assertEqual(effective["output_token_value"], 100_000)
            self.assertFalse(effective["max_completion_tokens_present"])

    def test_normal_length_return_is_typed_and_partial_is_not_parsed(self):
        llm = _DirectDeepSeekLLM(
            lambda kwargs: _direct_completion(
                kwargs,
                finish_reason="length",
                completion_tokens=8_192,
            )
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self._agent(root, llm)
            state, task_spec, consultation, audit = self._inputs(
                "deepseek-length-return"
            )
            result = self._run(agent, state, task_spec, consultation, audit)

            self.assertEqual(result["next_step"], "finish")
            self.assertEqual(result["failure_type"], "FINALIZE_RESPONSE_TRUNCATED")
            protocol = result["consultation_state"]["finalize_protocol"]
            self.assertEqual(protocol["provider_status"], "succeeded")
            self.assertEqual(protocol["protocol_status"], "not_validated")
            self.assertEqual(protocol["completion_integrity"], "truncated")
            attempt = protocol["attempts"][0]
            self.assertEqual(attempt["usage"]["completion_tokens"], 8_192)
            store = ContextLineageStore(root, state["task_id"])
            partial = store.lookup_finalize_partial_response(
                attempt_id=attempt["attempt_id"]
            )
            self.assertEqual(partial["finish_reason"], "length")
            self.assertEqual(partial["usage"]["completion_tokens"], 8_192)
            self.assertIsNone(store.lookup_finalize_response_artifact(
                attempt_id=attempt["attempt_id"],
                call_id=attempt["call_id"],
                canonical_request_hash=attempt["canonical_request_hash"],
            ))


if __name__ == "__main__":
    unittest.main()
