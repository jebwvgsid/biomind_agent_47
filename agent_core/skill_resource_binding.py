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
        except (SkillAccessError, OSError, UnicodeError) as exc:
            row.update(availability="UNRESOLVED", error_code=str(getattr(exc, "code", type(exc).__name__)),
                       detail=str(exc), evidence={"source": "selected_skill_registry", "snapshot_id": declared["snapshot_id"]})
        rows.append(row)
    return rows


def bind_action_skill_resources(action, service, access, *, step_id, cwd, namespace="local"):
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
    return result


def validate_action_skill_resources(action, service, access, *, step_id, cwd, namespace="local"):
    if not action.get("skill_resource_refs") and not action.get("formal_skill_resources"):
        return
    rebound = bind_action_skill_resources(action, service, access, step_id=step_id, cwd=cwd, namespace=namespace)
    if rebound.get("formal_skill_resources") != action.get("formal_skill_resources") or rebound.get("env") != action.get("env"):
        raise SkillAccessError("SKILL_BINDING_DRIFT", "Binding or execution environment changed after approval")
