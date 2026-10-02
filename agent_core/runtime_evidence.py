"""Task-owned immutable runtime observations and compatibility projections.

This module performs no discovery, I/O, installation or routing. Only trusted
inspection adapters may call ``make_runtime_evidence`` and task acceptance must
be persisted by the caller before its projection can authorize Generate. A
content digest detects mutation; it is not a substitute for producer admission.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 1
SCOPE_FIELDS = (
    "skill_id", "skill_document_digest", "skill_contract_digest",
    "environment_id", "environment_fingerprint", "host_id",
    "inspection_contract_digest",
)
AVAILABILITIES = frozenset({"AVAILABLE", "UNKNOWN", "UNAVAILABLE", "AMBIGUOUS"})
INSPECTION_STATUSES = frozenset({
    "not_run", "succeeded", "timeout", "permission_error", "execution_error",
    "output_invalid", "cancelled",
})
PRODUCERS = frozenset({
    "consultant_runtime_inspection", "runtime_inspection", "runtime_cache_adoption",
    "legacy_unverified", "registered_runtime",
})
_SECRET = re.compile(r"(?i)(?:secret|password|credential|authorization|api[_-]?key|(?:^|[/_.-])token(?:$|[/_.-])|(?:^|/)\.env(?:$|[./]))")
_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_BINDING = re.compile(r"^runtime\.[a-z0-9][a-z0-9_.-]{0,126}$")
_CODE = re.compile(r"^[A-Za-z0-9_.:/@+-]{0,240}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_VALUE_KINDS = frozenset({"path", "executable_path", "jar_path", "directory_path"})
_CONTENT_FIELDS = (
    "schema_version", "producer", "source_ref", "scope", "observed_at",
    "expires_at", "inspection", "facts", "resolved_runtime_paths", "supersedes",
    "origin_task_id", "representation",
)
_BINDING_FIELDS = {'request_scope', 'launch_description', 'requirements', 'binding_digest'}
_LAUNCH_FIELDS = {'prefix', 'entries', 'runtime_env', 'runner_argv', 'env_runner', 'launcher',
                 'source_ref', 'env', 'env_name', 'backend', 'host', 'host_id', 'packages',
                 'tools', 'capabilities', 'versions', 'language', 'capability_evidence'}
_REQUIREMENT_FIELDS = {'env', 'env_name', 'default_env', 'prefix', 'backend', 'host', 'host_id',
                      'tools', 'packages', 'capabilities', 'required_binding_ids', 'skill_id',
                      'runtime_scope', 'inspection_probes', 'env_runner', 'language','versions'}


def _binding_context(request_scope, launch_description, requirements, content_digest):
    if not isinstance(request_scope, Mapping) or set(request_scope) != {'task_id','user_turn_revision','request_key'}:
        raise RuntimeEvidenceError('RUNTIME_BINDING_REQUEST_SCOPE_INVALID')
    if not request_scope.get('task_id') or type(request_scope.get('user_turn_revision')) is not int or request_scope['user_turn_revision'] < 0:
        raise RuntimeEvidenceError('RUNTIME_BINDING_REQUEST_SCOPE_INVALID')
    _text(request_scope.get('task_id'), 'request_scope.task_id')
    _text(request_scope.get('request_key'), 'request_scope.request_key')
    for value,allowed,label in ((launch_description,_LAUNCH_FIELDS,'LAUNCH'),(requirements,_REQUIREMENT_FIELDS,'REQUIREMENTS')):
        if not isinstance(value,Mapping) or set(value)-allowed:
            raise RuntimeEvidenceError('RUNTIME_BINDING_'+label+'_INVALID')
        try:
            serialized=json.dumps(value,sort_keys=True,allow_nan=False)
        except (ValueError,TypeError) as exc:
            raise RuntimeEvidenceError('RUNTIME_BINDING_'+label+'_INVALID') from exc
        if len(serialized)>65536 or '\\u0000' in serialized:
            raise RuntimeEvidenceError('RUNTIME_BINDING_'+label+'_INVALID')
    context={'request_scope':deepcopy(dict(request_scope)),
             'launch_description':deepcopy(dict(launch_description)), 'requirements':deepcopy(dict(requirements))}
    context['binding_digest']=digest({'content_digest':content_digest,**context})
    return context


class RuntimeEvidenceError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}" if detail else code)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, limit: int = 2048) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_FIELD_INVALID", field)
    return value.strip()


def _timestamp(value: Any, field: str, *, optional: bool = False) -> str:
    text = _text(value, field, limit=64)
    if not text and optional:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_TIME_INVALID", field) from exc
    # Historical local healthcheck times are accepted as observations only when
    # the producer explicitly supplies the execution host timezone.
    if parsed.tzinfo is None:
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_TIMEZONE_REQUIRED", field)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope(raw: Mapping[str, Any]) -> dict[str, str]:
    scope = {key: _text(raw.get(key), f"scope.{key}") for key in SCOPE_FIELDS}
    for key, value in scope.items():
        if value and (key.endswith("digest") or key == "environment_fingerprint") and not _DIGEST.fullmatch(value):
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_SCOPE_DIGEST_INVALID", key)
        if _SECRET.search(value):
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_SENSITIVE_OR_INVALID_FIELD", key)
    return scope


def _paths(raw: Mapping[str, Any]) -> dict[str, str]:
    paths = {}
    for name, value in raw.items():
        name = _text(name, "output_name", limit=128).upper()
        value = _text(value, "runtime_path", limit=4096)
        if not _NAME.fullmatch(name) or _SECRET.search(name) or _SECRET.search(value):
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_SENSITIVE_OR_INVALID_FIELD", name[:128])
        if not value.startswith("/") or posixpath.normpath(value) != value:
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_PATH_INVALID", name)
        paths[name] = value
    return dict(sorted(paths.items()))


def _content(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(record.get(key)) for key in _CONTENT_FIELDS}


def make_runtime_evidence(
    audit: Mapping[str, Any], *, task_id: str, skill_scope: Mapping[str, Any],
    observed_at: str, producer: str = "consultant_runtime_inspection",
    source_ref: str = "", coverage: Mapping[str, Mapping[str, Any]] | None = None,
    inspection_status: str | None = None, supersedes: Sequence[str] = (),
    expires_at: str = "", evidence_id: str = "", max_age_seconds: float = 3600.0,
) -> dict[str, Any]:
    """Seal an allowlisted observation at a trusted, non-model producer seam.

    ``coverage`` may express explicit negative facts. A nonzero healthcheck or a
    legacy ``missing`` label never establishes a negative fact by itself.
    """
    if producer not in PRODUCERS:
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_PRODUCER_INVALID")
    scope = _scope(skill_scope)
    sid = str(audit.get("skill_id") or audit.get("skill") or "")
    env = str(audit.get("checked_env") or audit.get("env") or audit.get("declared_env") or "")
    if sid and scope["skill_id"] and sid != scope["skill_id"]:
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_SCOPE_MISMATCH", "skill_id")
    if env and scope["environment_id"] and env != scope["environment_id"]:
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_SCOPE_MISMATCH", "environment_id")
    raw_status = str(audit.get("status") or "unknown").lower()
    status = inspection_status or ("succeeded" if raw_status == "ready" else "not_run" if raw_status == "unknown" else "execution_error")
    if status not in INSPECTION_STATUSES:
        raise RuntimeEvidenceError("RUNTIME_INSPECTION_STATUS_INVALID")
    if producer == "registered_runtime" and status != "not_run":
        raise RuntimeEvidenceError("REGISTERED_RUNTIME_IS_NOT_FRESH_INSPECTION")
    paths = _paths(dict(audit.get("resolved_runtime_paths") or {}))
    if coverage is None:
        # Share canonical output mapping with the existing materializer. This
        # does not discover files or add tool-specific rules.
        from agent_core.action_runtime import _runtime_binding_identity
        declared = {
            _runtime_binding_identity(name)[0]: {
                "availability": "AVAILABLE", "output_name": name,
                "value_kind": _runtime_binding_identity(name)[2],
            }
            for name in paths
        }
    else:
        declared = dict(coverage)
    complete_scope = all(scope.values())
    facts = {}
    for binding_id, raw in sorted(declared.items()):
        if not _BINDING.fullmatch(str(binding_id)) or not isinstance(raw, Mapping):
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_COVERAGE_INVALID")
        availability = str(raw.get("availability") or "UNKNOWN").upper()
        if availability not in AVAILABILITIES:
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_AVAILABILITY_INVALID")
        output_name = _text(raw.get("output_name"), "output_name", limit=128).upper()
        if output_name and (not _NAME.fullmatch(output_name) or _SECRET.search(output_name)):
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_COVERAGE_INVALID", binding_id)
        reason = _text(raw.get("reason_code"), "reason_code", limit=240)
        if not _CODE.fullmatch(reason):
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_REASON_INVALID", binding_id)
        verified = bool(complete_scope and ((status == "succeeded" and producer != "legacy_unverified")
                        or (producer == "registered_runtime" and status == "not_run")))
        if not verified:
            availability = "UNKNOWN"
            reason = reason or ("legacy_unverified" if producer == "legacy_unverified" else "inspection_scope_incomplete" if not complete_scope else "inspection_" + status)
        if availability == "AVAILABLE" and (not output_name or output_name not in paths):
            availability, reason = "UNKNOWN", "inspection_contract_incomplete"
        if availability == "UNAVAILABLE" and (raw.get("conclusion_complete") is not True or not reason):
            availability, reason = "UNKNOWN", "negative_evidence_incomplete"
        facts[binding_id] = {
            "availability": availability, "output_name": output_name,
            "reason_code": reason, "value_kind": _text(raw.get("value_kind") or "path", "value_kind", limit=64),
            "conclusion_complete": bool(raw.get("conclusion_complete", availability == "AVAILABLE")),
        }
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or not 0 < max_age_seconds <= 86400:
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_EXPIRY_INVALID")
    normalized_observed_at = _timestamp(observed_at, "observed_at")
    effective_expiry = "" if producer == "registered_runtime" else expires_at or (datetime.fromisoformat(normalized_observed_at) + timedelta(seconds=max_age_seconds)).isoformat()
    observation = {
        "schema_version": SCHEMA_VERSION,
        "producer": producer,
        "source_ref": _text(source_ref, "source_ref"),
        "scope": scope,
        "observed_at": normalized_observed_at,
        "expires_at": _timestamp(effective_expiry, "expires_at", optional=producer == "registered_runtime"),
        "inspection": {"status": status, "method_digest": scope["inspection_contract_digest"], "coverage_complete": all(f["availability"] != "UNKNOWN" for f in facts.values()) and bool(facts)},
        "facts": facts,
        "resolved_runtime_paths": paths,
        "supersedes": sorted(set(_text(item, "supersedes") for item in supersedes)),
        "origin_task_id": _text(task_id, "task_id"),
        "representation": "allowlisted_runtime_observation_v1",
    }
    if not observation["origin_task_id"]:
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_TASK_REQUIRED")
    if observation["expires_at"] and observation["expires_at"] <= observation["observed_at"]:
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_TIME_INVALID", "expiry before observation")
    if not observation["source_ref"]:
        observation["source_ref"] = producer + ":" + digest({key: value for key, value in observation.items() if key != "source_ref"})
    content_digest = digest(observation)
    record = {**observation, "evidence_id": _text(evidence_id, "evidence_id") or "runtime-evidence:" + content_digest, "content_digest": content_digest}
    return validate_runtime_evidence(record)


def validate_runtime_evidence(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate immutable bytes and schema without granting task acceptance."""
    if not isinstance(record, Mapping) or type(record.get("schema_version")) is not int or record.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_SCHEMA_INVALID")
    unknown = set(record) - set(_CONTENT_FIELDS) - {"evidence_id", "content_digest", "acceptance"}
    if unknown:
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_FIELD_UNKNOWN", sorted(unknown)[0])
    value = deepcopy(dict(record))
    if digest(_content(value)) != value.get("content_digest"):
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_DIGEST_MISMATCH")
    if not _text(value.get("evidence_id"), "evidence_id"):
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_ID_REQUIRED")
    if value.get("producer") not in PRODUCERS or not _text(value.get("source_ref"), "source_ref") or _SECRET.search(value["source_ref"]):
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_PRODUCER_INVALID")
    if value.get("representation") != "allowlisted_runtime_observation_v1":
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_REPRESENTATION_INVALID")
    if not isinstance(value.get("scope"), Mapping) or set(value["scope"]) != set(SCOPE_FIELDS) or _scope(value["scope"]) != value["scope"]:
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_SCOPE_INVALID")
    observed = _timestamp(value.get("observed_at"), "observed_at")
    expiry = _timestamp(value.get("expires_at"), "expires_at", optional=value.get("producer") == "registered_runtime")
    if observed != value["observed_at"] or expiry != value["expires_at"] or (expiry and expiry <= observed):
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_TIME_INVALID")
    if not _text(value.get("origin_task_id"), "origin_task_id"):
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_TASK_REQUIRED")
    if not isinstance(value.get("supersedes"), list) or any(not isinstance(ref, str) or not ref for ref in value["supersedes"]):
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_SUPERSESSION_INVALID")
    if _paths(value.get("resolved_runtime_paths") or {}) != value["resolved_runtime_paths"]:
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_PATH_INVALID")
    inspection = value.get("inspection") or {}
    if set(inspection) != {"status", "method_digest", "coverage_complete"} or inspection.get("status") not in INSPECTION_STATUSES or inspection.get("method_digest") != value["scope"]["inspection_contract_digest"]:
        raise RuntimeEvidenceError("RUNTIME_INSPECTION_STATUS_INVALID")
    if value["producer"] == "registered_runtime" and inspection["status"] != "not_run":
        raise RuntimeEvidenceError("REGISTERED_RUNTIME_IS_NOT_FRESH_INSPECTION")
    if not isinstance(value.get("facts"), Mapping):
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_COVERAGE_INVALID")
    for binding_id, fact in value["facts"].items():
        if not _BINDING.fullmatch(binding_id) or not isinstance(fact, Mapping) or set(fact) != {"availability", "output_name", "reason_code", "value_kind", "conclusion_complete"}:
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_COVERAGE_INVALID")
        if not _CODE.fullmatch(_text(fact.get("reason_code"), "reason_code", limit=240)) or type(fact.get("conclusion_complete")) is not bool:
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_COVERAGE_INVALID")
        output_name = _text(fact.get("output_name"), "output_name", limit=128)
        if output_name and (not _NAME.fullmatch(output_name) or _SECRET.search(output_name)):
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_COVERAGE_INVALID")
        if fact.get("value_kind") not in _VALUE_KINDS:
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_VALUE_KIND_INVALID")
        if output_name:
            from agent_core.action_runtime import _runtime_binding_identity
            if _runtime_binding_identity(output_name)[0] != binding_id:
                raise RuntimeEvidenceError("RUNTIME_EVIDENCE_OUTPUT_BINDING_MISMATCH", binding_id)
        availability = fact.get("availability")
        if availability not in AVAILABILITIES:
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_AVAILABILITY_INVALID")
        if availability != "UNKNOWN" and (not all(value["scope"].values()) or value["producer"] == "legacy_unverified" or (inspection["status"] != "succeeded" and not (value["producer"] == "registered_runtime" and inspection["status"] == "not_run"))):
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_UNVERIFIED_FACT", binding_id)
        if availability == "AVAILABLE" and fact.get("output_name") not in value["resolved_runtime_paths"]:
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_COVERAGE_INVALID", binding_id)
        if availability == "UNAVAILABLE" and (fact.get("conclusion_complete") is not True or not fact.get("reason_code")):
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_NEGATIVE_INCOMPLETE", binding_id)
    acceptance = value.get("acceptance")
    if acceptance is not None:
        core={"task_id", "revision", "accepted_at", "content_digest", "scope_digest"}
        if not isinstance(acceptance, Mapping) or set(acceptance) not in (core,core|_BINDING_FIELDS):
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_ACCEPTANCE_INVALID")
        if not acceptance.get("task_id") or isinstance(acceptance.get("revision"), bool) or not isinstance(acceptance.get("revision"), int) or acceptance["revision"] < 1 or acceptance.get("content_digest") != value["content_digest"] or acceptance.get("scope_digest") != digest(value["scope"]):
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_ACCEPTANCE_INVALID")
        _timestamp(acceptance.get("accepted_at"), "accepted_at")
        if 'request_scope' in acceptance:
            binding=_binding_context(acceptance['request_scope'],acceptance['launch_description'],
                                     acceptance['requirements'],value['content_digest'])
            if acceptance['binding_digest']!=binding['binding_digest'] or acceptance['request_scope']['task_id']!=acceptance['task_id']:
                raise RuntimeEvidenceError('RUNTIME_BINDING_DIGEST_MISMATCH')
    return value


def merge_runtime_evidence(existing: Sequence[Mapping[str, Any]], incoming: Sequence[Mapping[str, Any]], *, task_id: str, accepted_at: str | None = None,
                           request_scope=None, launch_description=None, requirements=None) -> list[dict[str, Any]]:
    """Pure immutable admission. Caller persists the returned records atomically."""
    task_id = _text(task_id, "task_id")
    if not task_id:
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_TASK_REQUIRED")
    accepted_at = _timestamp(accepted_at or _now(), "accepted_at")
    records = {}
    revision = 0
    for raw in existing:
        record = validate_runtime_evidence(raw)
        acceptance = record.get("acceptance") or {}
        if acceptance.get("task_id") != task_id:
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_TASK_MISMATCH")
        key = record["evidence_id"]
        if key in records and records[key]["content_digest"] != record["content_digest"]:
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_ID_CONFLICT", key)
        records[key] = record
        revision = max(revision, acceptance["revision"])
    for raw in incoming:
        record = validate_runtime_evidence(raw)
        prior_acceptance = record.get("acceptance") or {}
        if prior_acceptance and prior_acceptance.get("task_id") != task_id:
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_TASK_MISMATCH", "cache adoption must reseal its provenance")
        key = record["evidence_id"]
        if key in records:
            if records[key]["content_digest"] != record["content_digest"]:
                raise RuntimeEvidenceError("RUNTIME_EVIDENCE_ID_CONFLICT", key)
            if request_scope is not None:
                binding=_binding_context(request_scope,launch_description or {},requirements or {},record['content_digest'])
                if any(records[key]['acceptance'].get(k)!=v for k,v in binding.items()):
                    raise RuntimeEvidenceError('RUNTIME_BINDING_ACCEPTANCE_CONFLICT')
            continue
        for previous_id in record["supersedes"]:
            previous = records.get(previous_id)
            if previous is None or previous_id == key or previous["scope"] != record["scope"] or previous["observed_at"] >= record["observed_at"]:
                raise RuntimeEvidenceError("RUNTIME_EVIDENCE_SUPERSESSION_INVALID", previous_id)
        revision += 1
        record["acceptance"] = {"task_id": task_id, "revision": revision, "accepted_at": accepted_at, "content_digest": record["content_digest"], "scope_digest": digest(record["scope"])}
        if request_scope is not None:
            record['acceptance'].update(_binding_context(request_scope,launch_description or {},requirements or {},record['content_digest']))
        records[key] = validate_runtime_evidence(record)
    return list(records.values())


def runtime_evidence_state(records: Sequence[Mapping[str, Any]], *, prior_revision: int = 0) -> dict[str, Any]:
    clean = [validate_runtime_evidence(record) for record in records]
    revision = max([int(prior_revision), *[int((record.get("acceptance") or {}).get("revision") or 0) for record in clean]])
    return {"schema_version": SCHEMA_VERSION, "revision": revision, "records": clean}


def _accepted_records(task_state: Mapping[str, Any], task_id: str | None) -> list[dict[str, Any]]:
    envelope = task_state.get("runtime_evidence")
    if envelope is None:
        return []  # historical audit is unknown until trusted reinspection/adoption
    if not isinstance(envelope, Mapping) or envelope.get("schema_version") != SCHEMA_VERSION or not isinstance(envelope.get("records"), list):
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_STORE_INVALID")
    records = [validate_runtime_evidence(record) for record in envelope["records"]]
    identities = {}
    for record in records:
        previous = identities.get(record["evidence_id"])
        if previous is not None:
            raise RuntimeEvidenceError("RUNTIME_EVIDENCE_ID_CONFLICT", record["evidence_id"])
        identities[record["evidence_id"]] = record
    for record in records:
        for ref in record["supersedes"]:
            previous = identities.get(ref)
            if previous is None or previous["scope"] != record["scope"] or previous["observed_at"] >= record["observed_at"]:
                raise RuntimeEvidenceError("RUNTIME_EVIDENCE_SUPERSESSION_INVALID", ref)
    tasks = {(record.get("acceptance") or {}).get("task_id") for record in records}
    if None in tasks or len(tasks) > 1 or (task_id is not None and tasks and tasks != {task_id}):
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_TASK_MISMATCH")
    if envelope.get("revision", 0) < max([0, *[(record.get("acceptance") or {}).get("revision", 0) for record in records]]):
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_STORE_REVISION_INVALID")
    return records


def _current_records(task_state, task_id):
    records=_accepted_records(task_state,task_id)
    if not task_state.get('pi_request_id'):
        return records
    # Compatibility projections receive task_state only; the authoritative
    # revision lives at the enclosing state level. Pi's durable request key is
    # unique per request and is available here without inventing a new store.
    key=task_state.get('current_request_key') or task_state['pi_request_id']
    return [record for record in records if
            (record.get('acceptance',{}).get('request_scope') or {}).get('request_key')==key and
            (task_id is None or record['acceptance']['request_scope']['task_id']==task_id)]


def registered_execution_binding(state, binding_ref='', runtime_scope='', requirements=None):
    """Resolve accepted references for this request, without registry I/O/probes."""
    task=state.get('task_state') or {};task_id=state.get('task_id')
    from agent_core.request_state import request_scope
    current=request_scope(state)
    records=_accepted_records(task,task_id)
    eligible=[r for r in records if r['producer']=='registered_runtime' and
              r.get('acceptance',{}).get('request_scope')==current]
    if binding_ref:
        chosen=next((r for r in eligible if r['evidence_id']==binding_ref),None)
        if not chosen:
            raise RuntimeEvidenceError('RUNTIME_BINDING_NOT_IN_CURRENT_REQUEST',str(binding_ref))
        eligible=[chosen]
    if runtime_scope:
        eligible=[r for r in eligible if r['scope']['skill_id']==runtime_scope]
        if binding_ref and not eligible:raise RuntimeEvidenceError('RUNTIME_BINDING_SCOPE_CONFLICT')
    req=dict(requirements or {})
    if req.get('env') and req.get('env_name') and req['env']!=req['env_name']:
        raise RuntimeEvidenceError('RUNTIME_BINDING_REQUIREMENT_CONFLICT','env/env_name')
    def matches(record):
        launch=record['acceptance']['launch_description'];previous=record['acceptance']['requirements']
        wanted_env=req.get('env') or req.get('env_name') or req.get('default_env')
        if wanted_env and wanted_env!=record['scope']['environment_id']:return False
        for key in ('prefix','backend','host','host_id','language','env_runner'):
            wanted=req.get(key)
            actual=launch.get(key) or (launch.get('host_id') if key=='host' else launch.get('host') if key=='host_id' else None)
            if wanted and wanted!=actual:return False
        for key in ('tools','capabilities','required_binding_ids'):
            available=set(launch.get(key) or []) | set(previous.get(key) or []) | (set(record['facts']) if key=='required_binding_ids' else set())
            wanted=set(req.get(key) or [])
            if key=='tools':available={v.casefold() for v in available};wanted={v.casefold() for v in wanted}
            if not wanted.issubset(available):return False
        if req.get('packages'):
            from agent_core.registered_runtime import _package_covered
            known=launch.get('packages') or previous.get('packages') or []
            for item in req['packages']:
                comparison=_package_covered(item,known,launch.get('versions') or {})
                if comparison is False:return False
                if comparison is None and not binding_ref and not wanted_env:
                    name=re.split(r'[<>=!~\s]',item)[0]
                    if _package_covered(name,[*known,*(previous.get('packages') or [])],launch.get('versions') or {}) is not True:return False
        if req.get('versions'):
            from agent_core.registered_runtime import _package_covered
            for name,wanted in req['versions'].items():
                condition=str(wanted)
                spec=condition if condition.startswith(('<','>','=','!','~')) else '=='+condition
                if _package_covered(name+spec,launch.get('packages') or [],launch.get('versions') or {}) is False:return False
        return True
    eligible=[r for r in eligible if matches(r)]
    if binding_ref and not eligible:raise RuntimeEvidenceError('RUNTIME_BINDING_REQUIREMENT_CONFLICT')
    if not eligible:return None
    # Identical repeated selection of the same scope/source may deduplicate.
    # Different scopes retain separate meaning even if the prefix is shared.
    unique={digest({'scope':r['scope'],'source':r['source_ref'],'launch':r['acceptance']['launch_description']}):r for r in eligible}
    if len(unique)>1:raise RuntimeEvidenceError('RUNTIME_BINDING_REQUIRED','multiple current runtime scopes; specify binding_ref')
    record=next(iter(unique.values()))
    return {'binding_ref':record['evidence_id'],'scope':record['scope']['skill_id'],
            'launch':deepcopy(record['acceptance']['launch_description']),
            'requirements':deepcopy(record['acceptance']['requirements']),
            'source_ref':record['source_ref'],'record':deepcopy(record)}


def evaluate_runtime_evidence(task_state: Mapping[str, Any], *, task_id: str | None = None, skill_scope: Mapping[str, Any] | None = None, required_binding_ids: Sequence[str] = (), now: str | None = None) -> dict[str, dict[str, Any]]:
    """Project each fact, keeping unknown/error and unresolved conflicts explicit."""
    records = _current_records(task_state, task_id)
    now = _timestamp(now or _now(), "now")
    scope_filter = dict(skill_scope or {})
    if set(scope_filter) - set(SCOPE_FIELDS):
        raise RuntimeEvidenceError("RUNTIME_EVIDENCE_SCOPE_INVALID")
    eligible = [record for record in records if all(record["scope"].get(k) == v for k, v in scope_filter.items())]
    bindings = set(required_binding_ids) | {key for record in eligible for key in record["facts"]}
    result = {}
    for binding_id in sorted(bindings):
        observations = [(record, record["facts"][binding_id]) for record in eligible if binding_id in record["facts"]]
        valid = [(record, fact) for record, fact in observations if record["observed_at"] <= now and (not record["expires_at"] or record["expires_at"] > now)]
        strong = [(record, fact) for record, fact in valid if fact["availability"] != "UNKNOWN"]
        # Supersession is per fact; a new partial observation never erases an
        # old binding that it did not cover, and UNKNOWN cannot erase truth.
        superseded = {ref for record, fact in strong for ref in record["supersedes"]}
        strong = [(record, fact) for record, fact in strong if record["evidence_id"] not in superseded]
        signatures = {(fact["availability"], record["resolved_runtime_paths"].get(fact["output_name"], ""), digest(record["scope"])) for record, fact in strong}
        if len(signatures) > 1 or any(fact["availability"] == "AMBIGUOUS" for _, fact in strong):
            availability, reason = "AMBIGUOUS", "conflicting_runtime_evidence"
        elif strong:
            availability, reason = strong[0][1]["availability"], strong[0][1]["reason_code"]
        else:
            availability = "UNKNOWN"
            reason = "evidence_expired_or_future" if observations and not valid else next((fact["reason_code"] for _, fact in valid if fact["reason_code"]), "runtime_evidence_missing")
        selected = strong if strong else valid
        result[binding_id] = {
            "availability": availability, "reason_code": reason,
            "evidence_ids": sorted(record["evidence_id"] for record, _ in selected),
            "inspection_statuses": sorted({record["inspection"]["status"] for record, _ in selected}),
        }
    return result


def project_runtime_audits(task_state: Mapping[str, Any], *, task_id: str | None = None, skill_scope: Mapping[str, Any] | None = None, now: str | None = None) -> list[dict[str, Any]]:
    """Compatibility view; unaccepted, stale, negative or conflicting facts fail closed."""
    records = _current_records(task_state, task_id)
    evaluations = evaluate_runtime_evidence(task_state, task_id=task_id, skill_scope=skill_scope, now=now)
    projected = []
    for record in records:
        paths = {}
        for binding_id, fact in record["facts"].items():
            evaluation = evaluations.get(binding_id) or {}
            if evaluation.get("availability") == "AVAILABLE" and record["evidence_id"] in evaluation.get("evidence_ids", []):
                paths[fact["output_name"]] = record["resolved_runtime_paths"][fact["output_name"]]
        if paths:
            projected.append({
                "runtime_audit_id": record["evidence_id"],
                "skill_id": record["scope"]["skill_id"], "skill": record["scope"]["skill_id"],
                "checked_env": record["scope"]["environment_id"], "env": record["scope"]["environment_id"],
                "status": "ready", "healthcheck_status": "not_run" if record["producer"] == "registered_runtime" else "passed",
                "runtime_source": record["producer"], "source_ref": record["source_ref"],
                "resolved_runtime_paths": paths,
                "runtime_evidence_digest": record["content_digest"],
                "observed_at": record["observed_at"], "evidence_revision": record["acceptance"]["revision"],
            })
    return projected


def project_registered_runtime_scopes(task_state: Mapping[str, Any], *, task_id=None, skill_ids=()):
    """Keep operation scopes separate while resolving a concrete launch prefix.

    Two registered operations may share the same prefix. Their different
    inspection-contract digests do not make that launch location ambiguous.
    Each operation still evaluates its own negatives and supersession normally.
    """
    selected = set(skill_ids)
    scopes = {digest(row['scope']): row['scope'] for row in _current_records(task_state, task_id)
              if row['producer'] == 'registered_runtime' and (not selected or row['scope']['skill_id'] in selected)}
    audits, evaluations = [], []
    for scope in scopes.values():
        audits.extend(project_runtime_audits(task_state, task_id=task_id, skill_scope=scope))
        evaluations.append(evaluate_runtime_evidence(task_state, task_id=task_id, skill_scope=scope))
    return audits, evaluations
