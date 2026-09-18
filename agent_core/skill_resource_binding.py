"""Bind explicitly declared Skill script refs through the existing registry.

This is not a shell scanner or an executor. User scripts do not enter this
boundary unless they explicitly claim a selected Skill resource.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from agent_core.skill_access import SkillAccessError, normalize_skill_access_state


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def resource_env(handle):
    return "BIOMIND_SKILL_" + hashlib.sha256(handle.encode()).hexdigest()[:16].upper()


def script_catalog(service, access, *, step_id):
    """Listed references are vocabulary; resolution is an independent fact."""
    rows = []
    if service is None:
        return rows
    for ref in access.get("selected_skill_refs", ()):
        if ref.get("status") != "active" or (ref.get("target_step_id") and ref["target_step_id"] != step_id):
            continue
        sid = ref["skill_id"]
        snapshot = service._verified_snapshot(access, ref["snapshot_id"])
        meta = service.skill_manager.get_skill_meta(sid) or {}
        for relative in meta.get("_scripts", ()):
            handle = sid + "#" + str(relative)
            rows.append({"handle": handle, "skill_id": sid, "relative_path": str(relative),
                "environment_variable": resource_env(handle), "snapshot_id": snapshot["snapshot_id"],
                "availability": "REGISTERED_NOT_YET_BOUND"})
    return rows


def bind_skill_resources(service, access, references, *, step_id, cwd, namespace="local"):
    if not isinstance(references, list) or len(references) > 16 or any(not isinstance(x, str) for x in references):
        raise SkillAccessError("SKILL_BINDING_REFERENCE_INVALID", "skill_resource_refs must be at most 16 string handles")
    if not references:
        return []
    if namespace != "local":
        raise SkillAccessError("SKILL_BINDING_NAMESPACE_UNRESOLVED", namespace)
    if service is None:
        raise SkillAccessError("SKILL_BINDING_REGISTRY_UNAVAILABLE", "No current Skill registry")
    working = normalize_skill_access_state(deepcopy(access))
    allowed = {r["handle"]: r for r in script_catalog(service, working, step_id=step_id)}
    # Keep the historical explicit order (including old complete manifests).
    # Only the selected snapshot's explicit delegates can add resources.
    ordered = list(dict.fromkeys(references))
    resolved, documents = {}, {}

    def resolve(handle):
        if handle in resolved:
            return resolved[handle]
        if handle not in allowed:
            raise SkillAccessError("SKILL_RESOURCE_OUT_OF_SCOPE", handle)
        declared = allowed[handle]
        snapshot_id = declared["snapshot_id"]
        if snapshot_id not in documents:
            snapshot = service._verified_snapshot(working, snapshot_id)
            sid, source, root = service._registered_source(declared["skill_id"])
            # This cache is local to one bind; prelaunch always reads afresh.
            from agent_core.approved_resource_locator import open_approved_resource
            with open_approved_resource(source) as stream:
                text = stream.read(2 * 1024 * 1024 + 1).decode("utf-8")
            if text != snapshot["exact_text"]:
                raise SkillAccessError("SKILL_SNAPSHOT_SOURCE_CHANGED", sid)
            documents[snapshot_id] = (snapshot, sid, root)
        snapshot, sid, root = documents[snapshot_id]
        relative = Path(declared["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise SkillAccessError("SKILL_RESOURCE_OUT_OF_SCOPE", handle)
        try:
            path = (root / relative).resolve(strict=True)
            path.relative_to(root)
        except FileNotFoundError as exc:
            raise SkillAccessError("SKILL_RESOURCE_NOT_FOUND", handle) from exc
        except ValueError as exc:
            raise SkillAccessError("SKILL_RESOURCE_OUT_OF_SCOPE", handle) from exc
        try:
            resource = service._resource_snapshot(working, snapshot, declared["relative_path"])
        except SkillAccessError as exc:
            if isinstance(exc.__cause__, PermissionError):
                raise SkillAccessError("SKILL_RESOURCE_READ_DENIED", handle) from exc
            raise
        receipt = {"schema_version": 1, "source": "selected_skill_registry", "handle": handle,
            "skill_id": sid, "snapshot_id": snapshot["snapshot_id"], "skill_content_hash": snapshot["content_hash"],
            "relative_path": declared["relative_path"], "registered_root": str(root),
            "canonical_path": resource["resource_location"], "content_hash": resource["content_hash"],
            "resource_snapshot_id": resource["resource_snapshot_id"], "environment_variable": resource_env(handle),
            "cwd": str(Path(cwd).resolve()), "namespace": namespace, "step_id": step_id,
            "readable": True, "executable_bit_required": False}
        from agent_core.skill_script_invocation import resolve_script_invocation
        invocation = resolve_script_invocation(snapshot["exact_text"], declared["relative_path"],
                                               resource["resource_location"])
        if invocation is not None:
            receipt["invocation"] = invocation
        receipt["binding_digest"] = _hash(receipt)
        resolved[handle] = receipt
        return receipt

    visiting, visited = [], set()

    def expand(handle):
        if handle in visiting:
            raise SkillAccessError("SKILL_INVOCATION_DEPENDENCY_CYCLE", " -> ".join([*visiting, handle]))
        if handle in visited:
            return
        row = resolve(handle)
        visiting.append(handle)
        for relative in (row.get("invocation") or {}).get("delegates", []):
            child = row["skill_id"] + "#" + relative
            if child not in allowed:
                raise SkillAccessError("SKILL_INVOCATION_DEPENDENCY_UNBOUND",
                    f"{handle} declares unavailable selected Skill resource {child}")
            if allowed[child]["snapshot_id"] != row["snapshot_id"]:
                raise SkillAccessError("SKILL_INVOCATION_DEPENDENCY_UNBOUND",
                    f"{child} does not belong to the root's selected snapshot")
            if child not in ordered:
                if len(ordered) >= 64:
                    raise SkillAccessError("SKILL_INVOCATION_DEPENDENCY_LIMIT", "At most 64 explicitly declared resources")
                ordered.append(child)
            expand(child)
        visiting.pop()
        visited.add(handle)

    for handle in references:
        expand(handle)
    return [resolved[handle] for handle in ordered]


def project_skill_script_catalog(service, access, *, step_id, cwd, namespace="local"):
    """Small model view; complete receipts and interpreter stats stay internal."""
    rows, cache = [], {}
    for declared in script_catalog(service, access, step_id=step_id):
        handle = declared["handle"]
        row = {key: declared[key] for key in ("handle", "environment_variable")}
        try:
            if handle not in cache:
                receipts = bind_skill_resources(service, access, [handle], step_id=step_id, cwd=cwd, namespace=namespace)
                cache.update({receipt["handle"]: receipt for receipt in receipts})
            receipt = cache[handle]
            row.update(availability="AVAILABLE", canonical_path=receipt["canonical_path"],
                evidence={"source": "selected_skill_registry", "snapshot_id": receipt["snapshot_id"],
                          "resource_snapshot_id": receipt["resource_snapshot_id"]})
            invocation = receipt.get("invocation")
            if invocation:
                # One reusable shell prefix also covers direct argv through
                # ordinary tokenization; do not inject repeated stat/prefix views.
                row.update(entrypoint={"shell_prefix": invocation["shell_prefix"]},
                    arguments=deepcopy(invocation["arguments"]), carrier=invocation["carrier"],
                    dependency_handles=[receipt["skill_id"] + "#" + relative for relative in invocation["delegates"]],
                    constraints={"runtime": invocation["runtime"], "scope": invocation["scope"],
                                 "dependency_binding": "automatic explicit-delegate closure before approval"})
                row["structured_call"] = {"type": "skill_script", "handle": handle,
                    "arguments": "Array of declared option/value strings; a late-bound value is {runtime_binding: published variable}."}
        except (SkillAccessError, OSError, UnicodeError) as exc:
            row.update(availability="UNRESOLVED", error_code=str(getattr(exc, "code", type(exc).__name__)),
                       detail=str(exc), evidence={"source": "selected_skill_registry", "snapshot_id": declared["snapshot_id"]})
        rows.append(row)
    return rows


def parse_generate_script_candidate(text, service, access, *, step_id, cwd, namespace="local", strict=True):
    """Decode a script or assemble a registered helper request before approval."""
    from agent_core.controller_audit import ExecutableScriptBlock, ScriptBlockError, parse_executable_script_block
    from agent_core.skill_script_invocation import build_skill_script_shell
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON member: " + key)
                result[key] = value
            return result
        try:
            request = json.loads(text, object_pairs_hook=unique)
        except (ValueError, TypeError):
            request = None
        if not isinstance(request, dict) or request.get("type") != "skill_script":
            return parse_executable_script_block(text, strict=strict)
        if set(request) != {"type", "handle", "arguments"} or not isinstance(request["handle"], str):
            raise SkillAccessError("SKILL_INVOCATION_ARGUMENT_INVALID", "Use exactly type, handle and arguments; public_message belongs to the display projection")
        receipts = bind_skill_resources(service, access, [request["handle"]],
            step_id=step_id, cwd=cwd, namespace=namespace)
        resource = next(row for row in receipts if row["handle"] == request["handle"])
        return ExecutableScriptBlock(language="bash", tag="shell",
            body=build_skill_script_shell(resource, request["arguments"]) + "\n",
            skill_resource_refs=(request["handle"],), resource_selection_explicit=True)
    except SkillAccessError as exc:
        raise ScriptBlockError(str(exc)) from exc


def continue_action_skill_selection(action, durable, service, access, *, step_id, cwd):
    """Continue a declared helper selection, never infer one from script text.

    A typed retry of the same action keeps its resource selection unless the
    candidate explicitly replaces it (an empty selection means ordinary code).
    Only handles survive. Receipts, environment and per-attempt evidence are
    rebuilt by the ordinary binder before approval.
    """
    if "skill_resource_refs" in action or action.get("formal_skill_resources"):
        return action
    decision = str(action.get("recovery_decision_id") or
        (action.get("recovery_execution_envelope") or {}).get("recovery_decision_id") or "")
    receipt = ((durable.get("task_state") or {}).get("typed_recovery_receipts") or {}).get(decision) or {}
    if (receipt.get("directive") or {}).get("action") != "retry_step":
        return action
    scope = (receipt.get("outcome") or {}).get("scope") or {}
    if any(str(scope.get(k) or "") != str(v or "") for k, v in
           (("task_id", durable.get("task_id")), ("plan_id", durable.get("plan_id")),
            ("plan_version", durable.get("plan_version")), ("step_id", step_id))):
        raise SkillAccessError("SKILL_BINDING_CONTINUATION_STALE", decision)
    recorded = ((durable.get("provenance_state") or {}).get("actions") or {}).get(scope.get("action_id")) or {}
    previous = recorded.get("normalized_action") or recorded.get("raw_action") or recorded
    references = previous.get("skill_resource_refs") or []
    if not references or previous.get("execution_capability_id") != action.get("execution_capability_id"):
        return action
    # An explicit new selection may use a new approved snapshot. An implicit
    # continuation must still be the same resource definition, including its
    # declared delegates. Never copy the old environment or temporary paths.
    current = bind_skill_resources(service, access, references, step_id=step_id, cwd=cwd)
    fields = ("handle", "snapshot_id", "content_hash", "resource_snapshot_id", "skill_content_hash")
    old = previous.get("formal_skill_resources") or []
    identity = lambda rows: [{k: row.get(k) for k in fields} for row in rows]
    if not old or identity(old) != identity(current):
        raise SkillAccessError("SKILL_BINDING_CONTINUATION_CHANGED", "Select the current helper explicitly; its recorded snapshot no longer matches")
    return {**action, "skill_resource_refs": list(references)}


def bind_action_skill_resources(action, service, access, *, step_id, cwd, namespace="local",
                                enforce_invocation=True):
    result = deepcopy(action)
    references = result.get("skill_resource_refs", [])
    try:
        receipts = bind_skill_resources(service, access, references, step_id=step_id, cwd=cwd, namespace=namespace)
    except SkillAccessError as exc:
        exc.diagnostic = {
            "validation_source": "agent_core.skill_resource_binding.bind_skill_resources",
            "repair_stage": "skill_resource_binding", "field_path": "skill_resource_refs",
            "actual": deepcopy(references), "expected": "selected registered handles and valid explicit delegate closure",
            "helper_handles": deepcopy(references), "detail": exc.detail,
            "evidence": {"step_id": step_id, "namespace": namespace},
            **getattr(exc, "diagnostic", {}),
        }
        raise
    supplied = result.get("formal_skill_resources")
    if supplied is not None and supplied != receipts:
        raise SkillAccessError("SKILL_BINDING_DRIFT", "Declared resources no longer match their frozen binding")
    env = dict(result.get("env") or {})
    for row in receipts:
        key, value = row["environment_variable"], row["canonical_path"]
        if key in env and env[key] != value:
            raise SkillAccessError("SKILL_BINDING_ENV_COLLISION", key)
        env[key] = value
    if receipts:
        result.update(env=env, formal_skill_resources=receipts)
        from agent_core.skill_script_invocation import validate_skill_script_invocations
        validate_skill_script_invocations(result, receipts, enforce=enforce_invocation)
    return result


def validate_action_skill_resources(action, service, access, *, step_id, cwd, namespace="local",
                                    enforce_invocation=True):
    if not action.get("skill_resource_refs") and not action.get("formal_skill_resources"):
        return
    rebound = bind_action_skill_resources(action, service, access, step_id=step_id, cwd=cwd,
        namespace=namespace, enforce_invocation=enforce_invocation)
    if rebound.get("formal_skill_resources") != action.get("formal_skill_resources") or rebound.get("env") != action.get("env"):
        raise SkillAccessError("SKILL_BINDING_DRIFT", "Binding or execution environment changed after approval")
