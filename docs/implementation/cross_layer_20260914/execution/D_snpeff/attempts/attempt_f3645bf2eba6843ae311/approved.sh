bash -lc 'test "$BIOMIND_SYNTHETIC_RUNTIME_SCOPE" = explicit-approved-scope
test -r '"'"'/tmp/biomind-cross-layer-implementation-20260914/closure-bare-D-results/scratch/biomind-plan-local-graph-qaapdexb/改名 新样本.vcf'"'"'
test -r '"'"'/tmp/biomind-cross-layer-implementation-20260914/closure-bare-D-results/scratch/biomind-plan-local-graph-qaapdexb/新 注释.gtf'"'"'
test -r '"'"'/tmp/biomind-cross-layer-implementation-20260914/closure-bare-D-results/scratch/biomind-plan-local-graph-qaapdexb/参考 改名.fasta'"'"'
/public/home/liusiqi/anaconda3/envs/biomind/bin/python - <<'"'"'PY'"'"'
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

_DECLARED_EFFECT_ROWS = json.loads('"'"'{"produces_artifacts": [], "explicit_side_effects": [], "produces_observations": [{"identity": "contigs_compatible", "required_fields": ["value"]}]}'"'"')

def emit_effect_evidence(actual_facts):
    return _emit_declared_rows(_DECLARED_EFFECT_ROWS, actual_facts, '"'"'BIOMIND_EFFECT_EVIDENCE_JSON='"'"', publication=True)
print(emit_effect_evidence({'"'"'produces_observations'"'"': {'"'"'contigs_compatible'"'"': {'"'"'value'"'"': True}}}))
PY'