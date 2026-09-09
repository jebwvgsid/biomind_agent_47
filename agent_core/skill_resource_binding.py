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
    if len(set(references)) != len(references):
        raise SkillAccessError("SKILL_BINDING_REFERENCE_DUPLICATE", "skill_resource_refs")
    if not references:
        return []
    if namespace != "local":
        raise SkillAccessError("SKILL_BINDING_NAMESPACE_UNRESOLVED", namespace)
    if service is None:
        raise SkillAccessError("SKILL_BINDING_REGISTRY_UNAVAILABLE", "No current Skill registry")
    working = normalize_skill_access_state(deepcopy(access))
    allowed = {r["handle"]: r for r in script_catalog(service, working, step_id=step_id)}
    result = []
    for handle in references:
        if handle not in allowed:
            raise SkillAccessError("SKILL_RESOURCE_OUT_OF_SCOPE", handle)
        declared = allowed[handle]
        snapshot = service._verified_snapshot(working, declared["snapshot_id"])
        sid, source, root = service._registered_source(declared["skill_id"])
        # Pin selected document revision as well as the script. Relocation is
        # legal before binding; an approval never silently follows relocation.
        from agent_core.approved_resource_locator import open_approved_resource
        with open_approved_resource(source) as stream:
            text = stream.read(2 * 1024 * 1024 + 1).decode("utf-8")
        if text != snapshot["exact_text"]:
            raise SkillAccessError("SKILL_SNAPSHOT_SOURCE_CHANGED", sid)
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
        result.append(receipt)
    return result


def bind_action_skill_resources(action, service, access, *, step_id, cwd, namespace="local"):
    result = deepcopy(action)
    references = result.get("skill_resource_refs", [])
    receipts = bind_skill_resources(service, access, references, step_id=step_id, cwd=cwd, namespace=namespace)
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
