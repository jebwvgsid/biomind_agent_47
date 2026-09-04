"""Phase-local Recovery agent contracts and provider-safe projections.

The stable System policy is deliberately task-independent.  One immutable
``RecoverySeed`` is projected once per invocation into the minimum semantic
facts needed for a first diagnosis; archives, Skill bodies, logs, and earlier
agent transcripts remain behind typed inspection references.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from agent_core.plan_tracker import canonical_task_spec_for_state
from agent_core.workflow.scoped_context import (
    AppendOnlyProviderMessages,
    FixedProviderMessage,
    build_provider_messages,
    provider_visible_payload,
)
from agent_core.workflow.stage_contracts import (
    RecoveryDirective,
    RecoverySeed,
    StageContractError,
    freeze,
    thaw,
)


RECOVERY_SYSTEM_PROMPT_VERSION = "recovery-agent-system-v1"
RECOVERY_SEED_NORMAL_TARGET_CHARS = 16_000
RECOVERY_SEED_HARD_LIMIT_CHARS = 32_000
RECOVERY_LOCAL_CONTEXT_SOFT_CHARS = 192_000
RECOVERY_LOCAL_CONTEXT_HARD_CHARS = 256_000
RECOVERY_SINGLE_OBSERVATION_CHARS = 24_000
RECOVERY_CUMULATIVE_OBSERVATION_CHARS = 96_000

# Keep this a literal, with no formatting or interpolation.  Under a fixed
# prompt version, tool schema, model, provider, and directive schema these
# bytes are identical for every task, plan, step, and failure.
RECOVERY_SYSTEM_PROMPT = """You are the BioMind Recovery Agent. Diagnose one causal failure from an immutable RecoverySeed and bounded, read-only inspection tools.

Objective:
- Identify the shortest evidence-backed diagnosis and the semantic correction required.
- Never execute a repair, emit executable code, mutate TaskSpec or PlanSpec, or choose a graph route.

Failure classes:
REAL_EXECUTION_FAILURE, MISSING_REQUIRED_INPUT, TOOL_OR_ENV_FAILURE, SCRIPT_BUG, PLAN_ERROR, CONTRACT_DRIFT, VALIDATOR_FALSE_POSITIVE, OPTIONAL_CHECK_FAILURE, SCIENTIFIC_WARNING, SUCCESS_WITH_WARNING, AMBIGUOUS.

Failure owners:
PLAN, GENERATED_ACTION, SCRIPT, RUNTIME, DEPENDENCY, ARTIFACT_CONTRACT, RENDERER, INTERNAL_PROTOCOL, VERIFICATION, INPUT_DATA, DIAGNOSTIC, CONSULT, UNKNOWN.

Evidence rules:
- Treat the causal FailurePacket as authoritative for failure identity and source.
- Inspect only opaque refs declared by the frozen Seed and local tool manifest.
- State evidence_sufficient only when cited machine facts support the diagnosis and correction.
- If evidence is insufficient, inspect a narrower declared ref or list the unresolved evidence.
- Do not infer missing runtime facts, scan unrelated history, or expose hidden reasoning.

Tool rules:
- Available tools are failure.inspect, step.inspect, artifact.inspect, artifact.read, skill.headings, skill.read, skill.search, skill.resource, evidence.read, and evidence.glob.
- Tools are read-only. Never pass a filesystem path where an opaque ref is required.
- Only already-selected Skills may be inspected. If a new Skill is required, choose reconsult.
- Keep requests narrow and do not repeat an identical request after it has been rejected twice.

Safety rules:
- Do not provide shell, Python, R, patches, replacement commands, or other executable payloads.
- Do not emit route, node, goto, next_step, next_route, target_route, dispatch_route, or controller fields.
- Do not copy large tool observations into the final result.

Terminal output:
Return exactly one strict RecoveryDirective JSON object and no surrounding prose. Required fields are schema_version, directive_id, task_id, action, summary, and reason_code. Optional fields are target_step_id, instruction, question, requested_slots, and evidence_refs. Allowed actions are retry_step, revise_plan, reconsult, revalidate, ask_user, and blocked. retry_step and revalidate require target_step_id. retry_step, revise_plan, and reconsult require instruction. ask_user requires question and requested_slots. All fields are semantic and route-blind."""


class RecoveryAgentError(ValueError):
    """Stable fail-closed error for Recovery provider-context construction."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "RECOVERY_AGENT_ERROR")
        self.detail = str(detail or "")
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def recovery_seed_digest(seed: RecoverySeed | Mapping[str, Any]) -> str:
    parsed = seed if isinstance(seed, RecoverySeed) else RecoverySeed.from_mapping(seed)
    return "sha256:" + hashlib.sha256(
        _canonical(parsed.to_dict()).encode("utf-8")
    ).hexdigest()


def _rows(value: Any, fields: Sequence[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in value or ():
        if not isinstance(item, Mapping):
            continue
        row = {
            field: deepcopy(item[field])
            for field in fields
            if item.get(field) not in (None, "", [], {})
        }
        if row:
            result.append(row)
    return result


def _hard_constraints(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    fields = (
        "requirement_id", "semantic_key", "category", "operator", "value",
        "text", "requirement_level", "strength", "required", "scope",
    )
    result: list[dict[str, Any]] = []
    candidates = [
        *list(task.get("requirements") or ()),
        *list(task.get("semantic_constraints") or ()),
    ]
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "active").lower()
        if status in {"superseded", "withdrawn", "inactive"}:
            continue
        level = str(item.get("requirement_level") or "").lower()
        strength = str(item.get("strength") or "").lower()
        if not (
            item.get("required") is True
            or level in {"required", "conditional"}
            or strength in {"hard", "required", "conditional"}
        ):
            continue
        row = {
            field: deepcopy(item[field])
            for field in fields
            if item.get(field) not in (None, "", [], {})
        }
        if row:
            result.append(row)
    for field, polarity in (("must_do", "required"), ("must_not_do", "forbidden")):
        for item in task.get(field) or ():
            text = str(item.get("text") if isinstance(item, Mapping) else item).strip()
            if text:
                result.append({"polarity": polarity, "text": text})
    return result


def _task_semantic_slice(durable: Mapping[str, Any]) -> dict[str, Any]:
    task = canonical_task_spec_for_state(durable)
    result: dict[str, Any] = {"goal": str(task.get("goal") or "").strip()}
    constraints = _hard_constraints(task)
    if constraints:
        result["hard_constraints"] = constraints
    inputs = _rows(
        task.get("inputs"),
        ("input_id", "role", "path", "format", "required", "availability"),
    )
    outputs = _rows(
        task.get("output_contracts"),
        ("output_id", "role", "path", "format", "required", "path_policy", "root_ref"),
    )
    if inputs:
        result["input_roles"] = inputs
    if outputs:
        result["output_roles"] = outputs
    return result


def _selected_skill_manifest(seed: RecoverySeed, durable: Mapping[str, Any]) -> dict[str, Any]:
    access = dict(
        (durable.get("task_state") or {}).get("skill_access_state")
        or durable.get("skill_access_state")
        or {}
    )
    resources = [
        dict(item)
        for item in (access.get("resource_snapshots") or {}).values()
        if isinstance(item, Mapping)
    ]
    skills: list[dict[str, Any]] = []
    for skill in seed.selected_skill_bundle.skills:
        row: dict[str, Any] = {
            "skill_id": skill.skill_id,
            "snapshot_id": skill.snapshot_id,
            "scope": skill.scope,
        }
        if skill.target_step_id:
            row["target_step_id"] = skill.target_step_id
        available = [
            {
                "resource_snapshot_id": str(resource.get("resource_snapshot_id") or ""),
                "resource_path": str(resource.get("resource_path") or ""),
            }
            for resource in resources
            if str(resource.get("skill_id") or "") == skill.skill_id
            and str(resource.get("snapshot_id") or "") == skill.snapshot_id
            and resource.get("resource_path")
        ]
        if available:
            row["available_resources"] = available
        skills.append(row)
    return {
        "bundle_id": seed.selected_skill_bundle.bundle_id,
        "manifest_only": True,
        "skills": skills,
    }


def _failure_projection(seed: RecoverySeed) -> dict[str, Any]:
    packet = seed.failure_packet
    result: dict[str, Any] = {
        "failure_id": packet.failure_id,
        "source": packet.source,
        "source_outcome_ref": packet.source_outcome_ref,
        "failure_type": packet.failure_type,
        "failure_owner": packet.failure_owner,
        "error_code": packet.error_code,
        "summary": packet.summary,
        "retryable": packet.retryable,
    }
    if packet.action_ref:
        result["failed_action_ref"] = packet.action_ref
    if packet.attempt_ref:
        result["failed_attempt_ref"] = packet.attempt_ref
    if packet.diagnostic_facts:
        result["diagnostic_facts"] = _rows(
            packet.diagnostic_facts,
            ("fact_id", "kind", "summary", "evidence_ref", "confidence"),
        )
    if packet.evidence_refs:
        result["evidence_refs"] = list(packet.evidence_refs)
    if packet.inspection_refs:
        result["inspection_refs"] = list(packet.inspection_refs)
    return result


def _inspection_manifest(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    fields = ("ref_id", "kind", "backend", "digest", "size_hint", "scope")
    result = _rows(value, fields)
    # Raw ``sha256:`` values are internal cache/concurrency metadata in the
    # shared provider guard.  A typed ref digest stays useful as bare hex while
    # avoiding accidental promotion of a durable CAS token into model control.
    for row in result:
        digest = str(row.get("digest") or "")
        if digest.startswith("sha256:"):
            row["digest"] = digest.removeprefix("sha256:")
    return result


def project_recovery_seed_for_provider(
    seed: RecoverySeed | Mapping[str, Any],
    durable: Mapping[str, Any],
    *,
    inspection_manifest: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build and capacity-check the one frozen provider-visible RecoverySeed."""

    parsed = seed if isinstance(seed, RecoverySeed) else RecoverySeed.from_mapping(seed)
    current_step = {
        key: deepcopy(parsed.current_step[key])
        for key in (
            "step_id", "title", "goal", "purpose", "action", "inputs",
            "outputs", "success_criteria", "risk_level",
        )
        if parsed.current_step.get(key) not in (None, "", [], {})
    }
    dependencies = _rows(
        parsed.dependency_outcomes,
        ("step_id", "status", "summary", "outcome_ref", "evidence_refs", "artifact_refs", "binding_refs"),
    )
    provider_seed: dict[str, Any] = {
        "schema_version": 1,
        "invocation_scope": {
            "task_id": parsed.failure_packet.task_id,
            "plan_id": parsed.plan_spec_ref.plan_id,
            "step_id": parsed.failure_packet.step_id,
        },
        "task_semantic_slice": _task_semantic_slice(durable),
        "current_step": current_step,
        "dependencies": dependencies,
        "selected_skill_manifest": _selected_skill_manifest(parsed, durable),
        "failure_packet": _failure_projection(parsed),
        "current_failure_lineage": {
            "failure_id": parsed.failure_packet.failure_id,
            "source_outcome_ref": parsed.failure_packet.source_outcome_ref,
        },
    }
    if parsed.prior_directive_refs:
        provider_seed["same_step_prior"] = {
            "directive_ref": parsed.prior_directive_refs[-1]
        }
    refs = _inspection_manifest(inspection_manifest)
    if refs:
        provider_seed["inspection_manifest"] = refs

    # The stage freezer rejects conversational payloads, full Skill bodies,
    # raw logs, executable streams, and recursive route/controller fields.
    frozen = thaw(freeze(provider_seed))
    encoded = _canonical(frozen)
    if len(encoded) > RECOVERY_SEED_HARD_LIMIT_CHARS:
        raise RecoveryAgentError(
            "RECOVERY_SEED_CAPACITY_EXCEEDED",
            f"chars={len(encoded)} limit={RECOVERY_SEED_HARD_LIMIT_CHARS}",
        )
    return frozen


def recovery_seed_capacity(seed: Mapping[str, Any]) -> dict[str, Any]:
    chars = len(_canonical(dict(seed or {})))
    return {
        "chars": chars,
        "normal_target_chars": RECOVERY_SEED_NORMAL_TARGET_CHARS,
        "hard_limit_chars": RECOVERY_SEED_HARD_LIMIT_CHARS,
        "within_normal_target": chars < RECOVERY_SEED_NORMAL_TARGET_CHARS,
        "within_hard_limit": chars <= RECOVERY_SEED_HARD_LIMIT_CHARS,
    }


@dataclass(frozen=True)
class RecoveryAgentLoopConfig:
    """Central deterministic budgets for one private Recovery episode."""

    max_llm_turns: int = 12
    max_total_tool_calls: int = 24
    max_exact_repeated_tool_request: int = 2
    max_consecutive_no_progress_rounds: int = 2
    max_transport_attempts: int = 1
    local_context_soft_chars: int = RECOVERY_LOCAL_CONTEXT_SOFT_CHARS
    local_context_hard_chars: int = RECOVERY_LOCAL_CONTEXT_HARD_CHARS
    single_observation_chars: int = RECOVERY_SINGLE_OBSERVATION_CHARS
    cumulative_observation_chars: int = RECOVERY_CUMULATIVE_OBSERVATION_CHARS
    max_overflow_epochs: int = 2

    def __post_init__(self) -> None:
        integer_fields = (
            "max_llm_turns", "max_total_tool_calls",
            "max_exact_repeated_tool_request",
            "max_consecutive_no_progress_rounds", "max_transport_attempts",
            "local_context_soft_chars", "local_context_hard_chars",
            "single_observation_chars", "cumulative_observation_chars",
            "max_overflow_epochs",
        )
        for field_name in integer_fields:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise RecoveryAgentError(
                    "RECOVERY_LOOP_CONFIG_INVALID", field_name
                )
        if self.local_context_soft_chars >= self.local_context_hard_chars:
            raise RecoveryAgentError(
                "RECOVERY_LOOP_CONFIG_INVALID",
                "local_context_soft_chars must be below local_context_hard_chars",
            )
        if self.single_observation_chars > self.cumulative_observation_chars:
            raise RecoveryAgentError(
                "RECOVERY_LOOP_CONFIG_INVALID",
                "single observation bound exceeds cumulative bound",
            )


def recovery_tool_schemas() -> list[dict[str, Any]]:
    """Return stable provider schemas; wire-safe names map to dotted tools."""

    specs = (
        ("failure_inspect", "Inspect the current causal FailurePacket.", {"ref_id": {"type": "string"}}),
        ("step_inspect", "Inspect a declared current or direct-dependency Step archive section.", {
            "ref_id": {"type": "string"}, "section": {"type": "string", "enum": ["summary", "artifacts", "evidence", "transcript"]},
            "offset": {"type": "integer"}, "limit": {"type": "integer"},
            "event_type": {"type": "string"}, "tool": {"type": "string"},
            "validator": {}, "execution": {}, "query": {"type": "string"},
        }),
        ("artifact_inspect", "Inspect metadata for an approved artifact ref.", {"ref_id": {"type": "string"}}),
        ("artifact_read", "Read a bounded text range from an approved artifact ref.", {
            "ref_id": {"type": "string"}, "start_line": {"type": "integer"},
            "end_line": {"type": "integer"}, "max_bytes": {"type": "integer"},
            "max_chars": {"type": "integer"}, "binary_policy": {"type": "string", "enum": ["deny"]},
        }),
        ("skill_headings", "List headings for an already-selected Skill.", {"skill_id": {"type": "string"}}),
        ("skill_read", "Read a bounded heading or line range from an already-selected Skill.", {
            "skill_id": {"type": "string"}, "heading": {"type": "string"},
            "start_line": {"type": "integer"}, "end_line": {"type": "integer"},
            "full": {"type": "boolean"},
        }),
        ("skill_search", "Search an already-selected Skill snapshot.", {
            "skill_id": {"type": "string"}, "query": {"type": "string"},
        }),
        ("skill_resource", "Read a bounded resource from an already-selected Skill.", {
            "skill_id": {"type": "string"}, "resource_path": {"type": "string"},
            "start_line": {"type": "integer"}, "end_line": {"type": "integer"},
        }),
        ("evidence_read", "Read bounded evidence through an opaque evidence ref.", {
            "evidence_ref": {"type": "string"}, "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        }),
        ("evidence_glob", "List bounded evidence names through an opaque scope ref.", {"scope_ref": {"type": "string"}}),
    )
    result: list[dict[str, Any]] = []
    for name, description, properties in specs:
        required = [
            key for key in properties
            if key in {"ref_id", "skill_id", "query", "resource_path", "evidence_ref", "scope_ref"}
        ]
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": deepcopy(properties),
                    "required": required,
                    "additionalProperties": False,
                },
            },
        })
    return result


class _RecoveryLoopState(TypedDict, total=False):
    system_message: FixedProviderMessage
    seed_message: FixedProviderMessage
    messages: AppendOnlyProviderMessages
    pending_tool_calls: list[dict[str, Any]]
    directive: dict[str, Any]
    terminal_error: dict[str, Any]
    turn_count: int
    tool_call_count: int
    tool_request_counts: dict[str, int]
    progress_signatures: list[str]
    no_progress_rounds: int
    observation_chars: int
    soft_limit_reached: bool
    transport_attempt_count: int


def _tool_calls(message: AIMessage) -> list[dict[str, Any]]:
    raw = list(message.tool_calls or ())
    if not raw and isinstance(message.additional_kwargs, Mapping):
        raw = list(message.additional_kwargs.get("tool_calls") or ())
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise RecoveryAgentError(
                "RECOVERY_TOOL_CALL_INVALID", f"tool_calls[{index}]"
            )
        function = item.get("function") if isinstance(item.get("function"), Mapping) else {}
        name = str(item.get("name") or function.get("name") or "").strip()
        call_id = str(item.get("id") or "").strip()
        arguments = item.get("args", function.get("arguments", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise RecoveryAgentError(
                    "RECOVERY_TOOL_ARGUMENT_INVALID", name
                ) from exc
        if not name or not call_id or not isinstance(arguments, Mapping):
            raise RecoveryAgentError(
                "RECOVERY_TOOL_CALL_INVALID", f"tool_calls[{index}]"
            )
        result.append({"id": call_id, "name": name, "args": dict(arguments)})
    return result


def _message_chars(messages: Sequence[BaseMessage]) -> int:
    return sum(len(_canonical(provider_visible_payload(message))) for message in messages)


def _content_text(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content.strip()
    if isinstance(message.content, (list, tuple)):
        parts = []
        for item in message.content:
            if isinstance(item, Mapping):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item or ""))
        return "".join(parts).strip()
    return str(message.content or "").strip()


def _directive_from_message(message: AIMessage) -> RecoveryDirective:
    content = _content_text(message)
    if not content:
        raise RecoveryAgentError("RECOVERY_DIRECTIVE_MISSING")
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RecoveryAgentError("RECOVERY_DIRECTIVE_JSON_INVALID", str(exc)) from exc
    try:
        return RecoveryDirective.from_mapping(raw)
    except StageContractError as exc:
        raise RecoveryAgentError(exc.code, exc.detail) from exc


def _source_ref(arguments: Mapping[str, Any]) -> str:
    for key in ("ref_id", "evidence_ref", "scope_ref", "skill_id"):
        value = str(arguments.get(key) or "").strip()
        if value:
            return value
    return ""


def _observation_envelope(
    *,
    request_id: str,
    tool: str,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any] | None,
    error: Mapping[str, Any] | None,
    seed: RecoverySeed,
) -> dict[str, Any]:
    payload = deepcopy(dict(result or {}))
    content_digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    cursor = deepcopy(payload.get("page") or payload.get("cursor") or {})
    scanned = payload.get("scanned_count")
    if scanned is None:
        for key in ("transcript", "items", "matches", "results", "artifacts", "evidence"):
            if isinstance(payload.get(key), list):
                scanned = len(payload[key])
                break
    return {
        "schema_version": 1,
        "request_id": request_id,
        "tool": tool,
        "source_ref": _source_ref(arguments),
        "task_id": seed.failure_packet.task_id,
        "plan_id": seed.plan_spec_ref.plan_id,
        "step_id": seed.failure_packet.step_id,
        "cursor": cursor,
        "result": payload,
        "truncated": bool(payload.get("truncated") or payload.get("status") == "REQUIRES_NARROWING"),
        "scanned_count": int(scanned or 0),
        "content_sha256": content_digest,
        "error": deepcopy(dict(error or {})),
    }


class RecoveryAgentLoop:
    """One LangGraph-owned, append-only Recovery invocation."""

    def __init__(
        self,
        *,
        model: Any,
        dispatcher: Any,
        config: RecoveryAgentLoopConfig | None = None,
        bind_tools: bool = True,
    ) -> None:
        self.config = config or RecoveryAgentLoopConfig()
        self.dispatcher = dispatcher
        self.model = model
        if bind_tools and hasattr(model, "bind_tools"):
            self.model = model.bind_tools(recovery_tool_schemas())

    async def _model_invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        call = getattr(self.model, "ainvoke", None)
        if call is None and callable(self.model):
            call = self.model
        if call is None:
            raise RecoveryAgentError("RECOVERY_PROVIDER_INVALID")
        response = call(messages)
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, AIMessage):
            raise RecoveryAgentError(
                "RECOVERY_PROVIDER_RESPONSE_INVALID", type(response).__name__
            )
        return response

    def _graph(self, seed: RecoverySeed):
        config = self.config

        async def call_model(state: _RecoveryLoopState) -> dict[str, Any]:
            turns = int(state.get("turn_count") or 0)
            if turns >= config.max_llm_turns:
                return {"terminal_error": {
                    "code": "RECOVERY_LLM_TURN_LIMIT",
                    "detail": str(config.max_llm_turns),
                }}
            wire = build_provider_messages(state)
            context_chars = _message_chars(wire)
            if context_chars > config.local_context_hard_chars:
                return {"terminal_error": {
                    "code": "RECOVERY_LOCAL_CONTEXT_HARD_LIMIT",
                    "detail": f"chars={context_chars}",
                }}
            attempts = 0
            while True:
                attempts += 1
                try:
                    response = await self._model_invoke(wire)
                    break
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    if attempts >= config.max_transport_attempts:
                        return {
                            "transport_attempt_count": int(state.get("transport_attempt_count") or 0) + attempts,
                            "terminal_error": {
                                "code": "RECOVERY_PROVIDER_TRANSPORT_FAILED",
                                "detail": f"{type(exc).__name__}: {exc}"[:800],
                            },
                        }
            ordinal = turns + 1
            assistant = response.model_copy(
                deep=True,
                update={"id": f"{seed.seed_id}:assistant:{ordinal:03d}"},
            )
            update: dict[str, Any] = {
                "messages": [assistant],
                "turn_count": ordinal,
                "transport_attempt_count": int(state.get("transport_attempt_count") or 0) + attempts,
                "soft_limit_reached": bool(
                    state.get("soft_limit_reached")
                    or context_chars >= config.local_context_soft_chars
                ),
            }
            calls = _tool_calls(assistant)
            if calls:
                update["pending_tool_calls"] = calls
                return update
            try:
                directive = _directive_from_message(assistant)
            except RecoveryAgentError as exc:
                update["terminal_error"] = {"code": exc.code, "detail": exc.detail}
                return update
            update["directive"] = directive.to_dict()
            return update

        async def run_tools(state: _RecoveryLoopState) -> dict[str, Any]:
            calls = list(state.get("pending_tool_calls") or ())
            counts = dict(state.get("tool_request_counts") or {})
            known_progress = set(state.get("progress_signatures") or ())
            total = int(state.get("tool_call_count") or 0)
            observation_chars = int(state.get("observation_chars") or 0)
            no_progress = int(state.get("no_progress_rounds") or 0)
            tool_messages: list[ToolMessage] = []
            terminal_error: dict[str, Any] = {}
            round_progress = False

            for call in calls:
                tool = str(call["name"])
                dotted_tool = tool.replace("_", ".")
                arguments = dict(call["args"])
                request_key = _canonical({"tool": dotted_tool, "arguments": arguments})
                prior_count = int(counts.get(request_key) or 0)
                if total >= config.max_total_tool_calls:
                    terminal_error = {
                        "code": "RECOVERY_TOOL_CALL_LIMIT",
                        "detail": str(config.max_total_tool_calls),
                    }
                    break
                if prior_count >= config.max_exact_repeated_tool_request:
                    terminal_error = {
                        "code": "RECOVERY_TOOL_REQUEST_REPEAT_LIMIT",
                        "detail": dotted_tool,
                    }
                    break
                counts[request_key] = prior_count + 1
                total += 1
                error: dict[str, Any] = {}
                try:
                    raw_result = self.dispatcher.dispatch(dotted_tool, arguments)
                    if inspect.isawaitable(raw_result):
                        raw_result = await raw_result
                    if not isinstance(raw_result, Mapping):
                        raise RecoveryAgentError(
                            "RECOVERY_TOOL_RESULT_INVALID", type(raw_result).__name__
                        )
                    result = dict(raw_result)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    result = {}
                    error = {
                        "code": str(getattr(exc, "code", None) or "RECOVERY_TOOL_FAILED"),
                        "detail": str(getattr(exc, "detail", None) or exc)[:800],
                    }
                envelope = _observation_envelope(
                    request_id=str(call["id"]),
                    tool=dotted_tool,
                    arguments=arguments,
                    result=result,
                    error=error,
                    seed=seed,
                )
                encoded = _canonical(envelope)
                if len(encoded) > config.single_observation_chars:
                    envelope = _observation_envelope(
                        request_id=str(call["id"]),
                        tool=dotted_tool,
                        arguments=arguments,
                        result={
                            "status": "REQUIRES_NARROWING",
                            "observed_chars": len(encoded),
                            "max_chars": config.single_observation_chars,
                        },
                        error={},
                        seed=seed,
                    )
                    encoded = _canonical(envelope)
                if observation_chars + len(encoded) > config.cumulative_observation_chars:
                    envelope = _observation_envelope(
                        request_id=str(call["id"]),
                        tool=dotted_tool,
                        arguments=arguments,
                        result={
                            "status": "RECOVERY_OBSERVATION_BUDGET_EXCEEDED",
                            "max_chars": config.cumulative_observation_chars,
                        },
                        error={},
                        seed=seed,
                    )
                    encoded = _canonical(envelope)
                observation_chars += len(encoded)
                signature = hashlib.sha256(_canonical({
                    "source_ref": envelope["source_ref"],
                    "content_sha256": envelope["content_sha256"],
                }).encode("utf-8")).hexdigest()
                if not envelope["error"] and signature not in known_progress:
                    known_progress.add(signature)
                    round_progress = True
                tool_messages.append(ToolMessage(
                    id=f"{seed.seed_id}:tool:{total:03d}",
                    tool_call_id=str(call["id"]),
                    name=tool,
                    content=encoded,
                ))

            no_progress = 0 if round_progress else no_progress + 1
            if (
                not terminal_error
                and no_progress >= config.max_consecutive_no_progress_rounds
            ):
                terminal_error = {
                    "code": "RECOVERY_NO_PROGRESS_LIMIT",
                    "detail": str(config.max_consecutive_no_progress_rounds),
                }
            update: dict[str, Any] = {
                "messages": tool_messages,
                "pending_tool_calls": [],
                "tool_request_counts": counts,
                "progress_signatures": sorted(known_progress),
                "tool_call_count": total,
                "observation_chars": observation_chars,
                "no_progress_rounds": no_progress,
            }
            if terminal_error:
                update["terminal_error"] = terminal_error
            return update

        async def after_model(state: _RecoveryLoopState) -> str:
            if state.get("directive") or state.get("terminal_error"):
                return "finish"
            return "tools"

        async def after_tools(state: _RecoveryLoopState) -> str:
            return "finish" if state.get("terminal_error") else "model"

        async def finish(state: _RecoveryLoopState) -> dict[str, Any]:
            return {}

        graph = StateGraph(_RecoveryLoopState)
        graph.add_node("model", call_model)
        graph.add_node("tools", run_tools)
        graph.add_node("finish", finish)
        graph.add_edge(START, "model")
        graph.add_conditional_edges("model", after_model, ["tools", "finish"])
        graph.add_conditional_edges("tools", after_tools, ["model", "finish"])
        graph.add_edge("finish", END)
        return graph.compile(checkpointer=False)

    async def ainvoke(
        self,
        *,
        seed: RecoverySeed | Mapping[str, Any],
        durable: Mapping[str, Any],
        inspection_manifest: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Run one fresh episode; no parent/global message history is accepted."""

        parsed = seed if isinstance(seed, RecoverySeed) else RecoverySeed.from_mapping(seed)
        provider_seed = project_recovery_seed_for_provider(
            parsed,
            durable,
            inspection_manifest=inspection_manifest,
        )
        system = SystemMessage(
            id=f"recovery-system:{RECOVERY_SYSTEM_PROMPT_VERSION}",
            content=RECOVERY_SYSTEM_PROMPT,
        )
        seed_message = HumanMessage(
            id=f"{parsed.seed_id}:seed",
            content=(
                '<recovery_seed schema_version="1">\n'
                + _canonical(provider_seed)
                + "\n</recovery_seed>"
            ),
        )
        initial: _RecoveryLoopState = {
            "system_message": system,
            "seed_message": seed_message,
            "messages": [],
            "pending_tool_calls": [],
            "turn_count": 0,
            "tool_call_count": 0,
            "tool_request_counts": {},
            "progress_signatures": [],
            "no_progress_rounds": 0,
            "observation_chars": 0,
            "soft_limit_reached": False,
            "transport_attempt_count": 0,
        }
        result = await self._graph(parsed).ainvoke(
            initial,
            config={"recursion_limit": self.config.max_llm_turns * 2 + 8},
        )
        local_messages = list(result.get("messages") or ())
        return {
            "directive": deepcopy(dict(result.get("directive") or {})),
            "terminal_error": deepcopy(dict(result.get("terminal_error") or {})),
            "audit": {
                "schema_version": 1,
                "prompt_version": RECOVERY_SYSTEM_PROMPT_VERSION,
                "seed_digest": recovery_seed_digest(parsed),
                "provider_seed_chars": recovery_seed_capacity(provider_seed)["chars"],
                "turn_count": int(result.get("turn_count") or 0),
                "tool_call_count": int(result.get("tool_call_count") or 0),
                "observation_chars": int(result.get("observation_chars") or 0),
                "no_progress_rounds": int(result.get("no_progress_rounds") or 0),
                "soft_limit_reached": bool(result.get("soft_limit_reached")),
                "transport_attempt_count": int(result.get("transport_attempt_count") or 0),
                "provider_message_count": 2 + len(local_messages),
                "append_only": True,
            },
            # Ephemeral and consumed by the Recovery archive adapter before a
            # parent-state update.  It is never an allowed graph handoff field.
            "_provider_episode": [
                provider_visible_payload(message)
                for message in (system, seed_message, *local_messages)
            ],
        }


__all__ = [
    "RECOVERY_SEED_HARD_LIMIT_CHARS",
    "RECOVERY_SEED_NORMAL_TARGET_CHARS",
    "RECOVERY_LOCAL_CONTEXT_SOFT_CHARS",
    "RECOVERY_LOCAL_CONTEXT_HARD_CHARS",
    "RECOVERY_SINGLE_OBSERVATION_CHARS",
    "RECOVERY_CUMULATIVE_OBSERVATION_CHARS",
    "RECOVERY_SYSTEM_PROMPT",
    "RECOVERY_SYSTEM_PROMPT_VERSION",
    "RecoveryAgentError",
    "RecoveryAgentLoop",
    "RecoveryAgentLoopConfig",
    "project_recovery_seed_for_provider",
    "recovery_seed_capacity",
    "recovery_seed_digest",
    "recovery_tool_schemas",
]
