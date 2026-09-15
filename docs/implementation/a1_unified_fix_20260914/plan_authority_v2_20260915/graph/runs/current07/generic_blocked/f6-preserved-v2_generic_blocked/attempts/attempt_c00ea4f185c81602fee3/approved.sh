
set -euo pipefail
/public/home/liusiqi/anaconda3/envs/biomind/bin/python -I -S - <<'PY'
import json

def _emit_declared_rows(declarations, facts, prefix, *, publication=False):
    """Standalone serializer kernel: no filesystem, inference or execution."""
    def reject(code, path, actual, expected):
        raise ValueError(json.dumps({"code": code, "field_path": path,
                                     "actual": actual, "expected": expected}, sort_keys=True))

    if not isinstance(facts, dict):
        reject("EFFECT_FACTS_REQUIRED", "$.facts", type(facts).__name__, "object")
    unknown = sorted(set(facts) - set(declarations))
    if unknown:
        reject("EFFECT_FIELD_UNDECLARED", "$.facts", unknown, sorted(declarations))
    payload = {"schema_version": 1}
    for field, rows in declarations.items():
        supplied = facts.get(field, {})
        if not isinstance(supplied, dict):
            reject("EFFECT_FACTS_REQUIRED", "$.facts." + field, type(supplied).__name__, "identity-keyed object")
        expected = [row["identity"] for row in rows]
        if (set(supplied) - set(expected)) or (not publication and set(supplied) != set(expected)):
            reject("EFFECT_FACT_IDENTITIES_MISMATCH", "$.facts." + field, sorted(supplied), expected)
        emitted = []
        for row in rows:
            identity = row["identity"]
            if identity not in supplied:
                continue  # No observed fact; never infer absence or success.
            value = supplied[identity]
            path = "$.facts." + field + "." + identity
            if not isinstance(value, dict) or not value:
                reject("EFFECT_FACTS_REQUIRED", path, value, "nonempty actual fact object")
            if any(key in value for key in ("identity", "artifact_id", "effect_id", "kind", "effect_kind")):
                reject("EFFECT_IDENTITY_OVERRIDE_FORBIDDEN", path, sorted(value), "facts without identity overrides")
            missing = [key for key in row["required_fields"] if value.get(key) is None]
            if field == "produces_artifacts" and not (isinstance(value.get("path"), str) and value["path"].strip()):
                missing.append("path")
            if missing:
                reject("EFFECT_FACT_FIELDS_MISSING", path, sorted(value), sorted(set(missing)))
            emitted.append({"identity": identity, **value})
        if emitted:
            payload[field] = emitted
    if len(payload) == 1:
        return ""  # no supported declaration: fabricate nothing
    return prefix + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

_DECLARED_EFFECT_ROWS = json.loads('{"produces_artifacts": [{"identity": "preflight_log", "required_fields": []}], "explicit_side_effects": [], "produces_observations": [{"identity": "preflight_log_written", "required_fields": ["value", "details"]}]}')

def emit_effect_evidence(actual_facts):
    return _emit_declared_rows(_DECLARED_EFFECT_ROWS, actual_facts, 'BIOMIND_EFFECT_EVIDENCE_JSON=', publication=True)
from pathlib import Path
inputs = [Path(p) for p in ['/tmp/biomind-plan-authority-v2-current07/graph_generic_blocked/scratch/biomind-plan-local-graph-43n2uy2w/中文 两样本/输入 新构造/两样本.vcf', '/tmp/biomind-plan-authority-v2-current07/graph_generic_blocked/scratch/biomind-plan-local-graph-43n2uy2w/中文 两样本/输入 新构造/新参考.fa', '/tmp/biomind-plan-authority-v2-current07/graph_generic_blocked/scratch/biomind-plan-local-graph-43n2uy2w/中文 两样本/输入 新构造/新注释.gtf']]
sizes = [len(p.read_bytes()) for p in inputs]
value = all(n > 0 for n in sizes)
details = {"input_names": [p.name for p in inputs], "byte_counts": sizes}
out = Path('/tmp/biomind-plan-authority-v2-current07/graph_generic_blocked/scratch/biomind-plan-local-graph-43n2uy2w/中文 两样本/Part2.vairant_annotation/新机制结果/preflight-log.txt')
out.parent.mkdir(parents=True, exist_ok=True)
temporary = Path('/tmp/biomind-plan-authority-v2-current07/graph_generic_blocked/scratch/biomind-plan-local-graph-43n2uy2w/中文 两样本/Part2.vairant_annotation/新机制结果/preflight-log.txt.v2-partial')
assert temporary.parent == out.parent
temporary.write_text("SYNTHETIC PREFLIGHT\n" + "\n".join(p.name for p in inputs) + "\n")
details["temporary_bytes_before_rename"] = temporary.stat().st_size
temporary.replace(out)
details["temporary_present_after_rename"] = temporary.exists()
value = value and out.stat().st_size > 0
details["written_bytes"] = out.stat().st_size
print(emit_effect_evidence({'produces_artifacts': {'preflight_log': {'path': '/tmp/biomind-plan-authority-v2-current07/graph_generic_blocked/scratch/biomind-plan-local-graph-43n2uy2w/中文 两样本/Part2.vairant_annotation/新机制结果/preflight-log.txt'}}, 'produces_observations': {'preflight_log_written': {'value': value, 'details': details}}}))
PY
