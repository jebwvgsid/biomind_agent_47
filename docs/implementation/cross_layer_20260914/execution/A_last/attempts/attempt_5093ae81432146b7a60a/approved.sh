set -euo pipefail
INDEX_BUNDLE=/tmp/biomind-cross-layer-implementation-20260914/closure-bare-last-veto-results/scratch/biomind-plan-local-graph-j99isg2y/task_outputs/new-cross-layer-A_last/plan_artifacts/plan-artifact-c4eaa14cfcf21ff63fff899e
dictionary_probe() { return 9; } # explicitly approved synthetic capability stub
if dictionary_probe; then exit 99; else echo "STAGE_BLOCKED: synthetic_dictionary unavailable" >&2; fi
/public/home/liusiqi/anaconda3/envs/biomind/bin/python - '/tmp/biomind-cross-layer-implementation-20260914/closure-bare-last-veto-results/scratch/biomind-plan-local-graph-j99isg2y/参考 改名.fasta' synthetic_dictionary <<'PY'
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

_DECLARED_EFFECT_ROWS = json.loads('{"produces_artifacts": [{"identity": "plan-artifact-c4eaa14cfcf21ff63fff899e", "required_fields": []}], "explicit_side_effects": [{"identity": "create_reference_indexes", "required_fields": ["reference_resource_ref", "index_artifacts"]}], "produces_observations": []}')

def emit_effect_evidence(actual_facts):
    return _emit_declared_rows(_DECLARED_EFFECT_ROWS, actual_facts, 'BIOMIND_EFFECT_EVIDENCE_JSON=', publication=True)
import sys
facts={"explicit_side_effects":{"create_reference_indexes":{"reference_resource_ref":sys.argv[1],"index_artifacts":[],"status":"blocked","missing_commands":sys.argv[2:]}}}
print(emit_effect_evidence(facts))
PY
exit 1
