"""Passive registration selection. No subprocesses, imports of science packages or TTL."""
from __future__ import annotations

from pathlib import Path
import re
import shlex
from typing import Mapping

from agent_core.action_runtime import _runtime_binding_identity
from agent_core.runtime_evidence import digest

_INVALID = {"broken", "disabled", "invalid", "failed"}
_SUCCESS = {"success", "verified", "installed"}


def _items(value):
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)] if value else []


def _package_covered(requirement, recorded, versions):
    """An unversioned registration cannot satisfy a newly constrained version."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import Version, InvalidVersion
    try:
        wanted = Requirement(requirement)
        known = [Requirement(item) for item in recorded]
    except Exception:
        return requirement in recorded
    name = canonicalize_name(wanted.name)
    matches = [item for item in known if canonicalize_name(item.name) == name]
    if not matches:
        return False
    if not wanted.specifier:
        return True
    version = next((str(v) for k, v in versions.items() if canonicalize_name(k) == name), "")
    if version:
        try:
            return Version(version) in wanted.specifier
        except InvalidVersion:
            return False
    # Persisted exact versions or the same declared constraint are evidence;
    # do not guess overlap between arbitrary version ranges.
    for item in matches:
        if str(item.specifier) == str(wanted.specifier):
            return True
        exact = list(item.specifier)
        if len(exact) == 1 and exact[0].operator == "==" and "*" not in exact[0].version:
            try:
                if Version(exact[0].version) in wanted.specifier:
                    return True
            except InvalidVersion:
                pass
    return False


def registered_runtime_reuse(pm, requirements: Mapping, *, skill_record: Mapping, target_env=""):
    declared = str(target_env or requirements.get("env") or requirements.get("default_env") or "")
    has_requirements = any(requirements.get(key) for key in
        ("tools", "packages", "capabilities", "required_binding_ids", "inspection_probes"))
    if not declared and not has_requirements:
        return {"decision": "no_runtime_required", "availability": "NOT_REQUIRED",
                "selected_candidate": {}, "candidates": [], "install_required": False,
                "reason_code": "NO_RUNTIME_REQUIREMENTS"}
    environments = (pm.registry or {}).get("environments") or {}
    known = pm._known_environment_prefixes()
    names = list(dict.fromkeys([declared, str(skill_record.get("env") or ""), *environments]))
    candidates = []
    for name in filter(None, names):
        if (target_env and name != target_env) or (declared and not has_requirements and name != declared):
            continue
        environment = dict(environments.get(name) or {})
        skill = dict(skill_record) if skill_record.get("env") == name else {}
        # A specific invalidation always wins over historical success elsewhere.
        if any(str(record.get("status") or "").lower() in _INVALID for record in (environment, skill)):
            candidates.append({"env": name, "availability": "UNKNOWN", "reason_code": "REGISTERED_RUNTIME_INVALIDATED"})
            continue
        records = [r for r in (environment, skill) if str(r.get("status") or "").lower() in _SUCCESS]
        if not records:
            continue
        merged = {**skill, **environment}
        explicit_prefixes = {str(record.get("prefix") or record.get("path") or "") for record in records} - {""}
        prefixes = sorted(explicit_prefixes) if explicit_prefixes else pm._registered_prefixes(name, merged, known)
        row = {"env": name, "availability": "UNKNOWN", "inspection_status": "not_run",
               "reason_code": "REGISTERED_RUNTIME_LOCATION_UNRESOLVED", "prefix_candidates": prefixes}
        candidates.append(row)
        if len(prefixes) != 1 or not Path(prefixes[0]).is_absolute():
            if len(prefixes) > 1:
                row["reason_code"] = "REGISTERED_RUNTIME_LOCATION_AMBIGUOUS"
            continue
        prefix = Path(prefixes[0]).resolve()
        if not prefix.is_dir():
            continue
        packages = sorted({item for record in records for item in _items(record.get("packages"))})
        caps = sorted({item for record in records for item in _items(record.get("capabilities"))})
        tools = {item.casefold() for record in records for key in ("tools", "bound_tools") for item in _items(record.get(key))}
        for tool, info in ((pm.registry or {}).get("tools") or {}).items():
            if (info.get("env") or info.get("env_name")) == name and str(info.get("status") or "").lower() in _SUCCESS:
                tools.add(tool.casefold())
        # A declared tool capability (e.g. hicfindtads) or package of the same
        # name is explicit metadata; never infer tools from an environment name.
        tools.update(re.split(r"[<>=!~\s]", item)[0].casefold() for item in packages)
        tools.update(item.removeprefix("tool:").casefold() for item in caps)
        if any(str(record.get("language") or "").lower() == "python" for record in records):
            tools.update({"python", "python3"})
        versions = {key: value for record in records for key, value in (record.get("versions") or {}).items()}
        missing_packages = [item for item in _items(requirements.get("packages")) if not _package_covered(item, packages, versions)]
        missing_tools = [item for item in _items(requirements.get("tools")) if item.casefold() not in tools]
        missing_caps = sorted(set(_items(requirements.get("capabilities"))) - set(caps))
        row.update(prefix=str(prefix), prefix_present=True, missing_packages=missing_packages,
                   missing_tools=missing_tools, missing_capabilities=missing_caps)
        if missing_packages or missing_tools or missing_caps:
            row["reason_code"] = "REGISTERED_RUNTIME_COVERAGE_GAP"
            continue
        paths = {"RESOLVED_ENV_PREFIX": str(prefix)}
        paths.update({key: value for record in records for key, value in (record.get("resolved_runtime_paths") or {}).items()
                      if isinstance(value, str) and Path(value).is_absolute()})
        for probe in requirements.get("inspection_probes") or []:
            value = str(probe.get("value") or "").replace("{prefix}", str(prefix))
            output = str(probe.get("output") or "")
            # Resolving the declared launch location is path metadata only. No
            # probe checks are executed and no fresh verification is claimed.
            if output and Path(value).is_absolute() and "{" not in value:
                paths[output] = value
        coverage = {_runtime_binding_identity(key)[0]: {
            "availability": "AVAILABLE", "output_name": key,
            "value_kind": _runtime_binding_identity(key)[2], "conclusion_complete": True,
            "reason_code": "REGISTERED_RUNTIME_REUSED"} for key in paths}
        required = set(requirements.get("required_binding_ids") or [])
        if required - set(coverage):
            row.update(reason_code="REGISTERED_RUNTIME_BINDING_UNDECLARED", missing_binding_ids=sorted(required - set(coverage)))
            continue
        source_ref = "registered-runtime:" + digest({"env": name, "prefix": str(prefix), "records": records})
        runner = str(merged.get("env_runner") or requirements.get("env_runner") or "")
        # A name-only launcher must not reintroduce ambiguity after a prefix
        # has been resolved. Keep the registered launch method, pin its target.
        runner = re.sub(r"(\b(?:conda|mamba|micromamba)\s+run\s+)-n\s+" + re.escape(name) + r"(?=\s|$)",
                        lambda match: match.group(1) + "-p " + shlex.quote(str(prefix)), runner)
        audit = {"skill_id": str(requirements.get("skill_id") or skill.get("skill_id") or ""),
                 "env": name, "checked_env": name, "declared_env": declared, "status": "ready",
                 "declared_ok": True, "decision": "registered_runtime_reused", "runtime_source": "registered_runtime",
                 "healthcheck_status": "not_run", "inspection_status": "not_run", "source_ref": source_ref,
                 "resolved_runtime_paths": paths, "coverage": coverage,
                 "env_runner": runner,
                 "last_verified_at": str(merged.get("last_verified_at") or ""),
                 "reason": "Using existing successful registration; no health check performed in this task."}
        row.update(availability="AVAILABLE", reason_code="REGISTERED_RUNTIME_REUSED", coverage=coverage,
                   resolved_runtime_paths=paths, source_ref=source_ref, audit=audit)
        return {"decision": "registry_reused", "availability": "AVAILABLE", "selected_candidate": row,
                "candidates": candidates, "install_required": False, "reason_code": "REGISTERED_RUNTIME_REUSED"}
    return {"decision": "registry_unresolved", "availability": "UNKNOWN", "selected_candidate": {},
            "candidates": candidates, "install_required": False,
            "reason_code": candidates[0]["reason_code"] if candidates else "REGISTERED_RUNTIME_NOT_FOUND"}


def record_execution_invalidation(state, receipt):
    """Retain a concrete dependency failure for this task's selected registration.

    Silence, nonzero status alone, timeout, cancellation and input errors cannot
    invalidate an environment. Registration is not globally rewritten here.
    """
    if (receipt.get('execution_started') is not True or receipt.get('exit_code') in (None, 0)
            or receipt.get('status') != 'exited' or not receipt.get('receipt_ref')):
        return []
    stderr = str(receipt.get('stderr') or '')
    missing = re.search(r"(?:^|\n)ModuleNotFoundError: No module named ['\"]([A-Za-z_][A-Za-z0-9_.]*)['\"](?:\n|$)", stderr)
    if not missing:
        missing = re.search(r"(?:^|\n)[^\n]*:\s*([A-Za-z_][A-Za-z0-9_.+-]*): command not found(?:\n|$)", stderr)
    if not missing:
        return []
    prefix = str((receipt.get('env') or {}).get('ENV_PREFIX') or (receipt.get('env') or {}).get('CONDA_PREFIX') or '')
    if not prefix:
        return []
    from agent_core.runtime_evidence import project_registered_runtime_scopes
    task = state.setdefault('task_state', {})
    step = next((row for row in state.get('steps') or []
                 if row.get('step_id') == (receipt.get('step_id') or state.get('current_step_id'))), {})
    selected = list(step.get('skill_ids') or [])
    selected.extend(str(step[key]) for key in ('skill_id', 'selected_skill_id') if step.get(key))
    audits = project_registered_runtime_scopes(task, task_id=state.get('task_id'), skill_ids=selected)[0] if task.get('runtime_evidence') else []
    changed = []
    for audit in audits:
        paths = audit.get('resolved_runtime_paths') or {}
        if audit.get('runtime_source') != 'registered_runtime' or prefix != (paths.get('RESOLVED_ENV_PREFIX') or paths.get('ENV_PREFIX')):
            continue
        sid = audit['skill_id']
        row = {'environment': audit['env'], 'prefix': prefix, 'missing_dependency': missing.group(1),
               'reason_code': 'REGISTERED_RUNTIME_DEPENDENCY_FAILED', 'receipt_ref': receipt['receipt_ref'],
               'attempt_id': receipt.get('attempt_id'), 'step_id': receipt.get('step_id')}
        task.setdefault('runtime_invalidations', {})[sid] = row
        changed.append(row)
    return changed
