/bin/bash --noprofile --norc -c 'set -euo pipefail
/public/home/liusiqi/anaconda3/envs/biomind/bin/python -I -S -c '"'"'import os;from pathlib import Path;p=Path(os.environ["SNPEFF_DB"])/"partial-write.txt";p.write_text("actual partial write")'"'"'
printf '"'"'SYNTHETIC_PARTIAL_BUILD_FAILURE\n'"'"' >&2
exit 23
/public/home/liusiqi/anaconda3/envs/biomind/bin/python -I -S - <<'"'"'PY'"'"'
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

_DECLARED_EFFECT_ROWS = json.loads('"'"'{"produces_artifacts": [], "explicit_side_effects": [], "produces_observations": [{"identity": "snpeff_database_ready", "required_fields": []}]}'"'"')

def emit_effect_evidence(actual_facts):
    return _emit_declared_rows(_DECLARED_EFFECT_ROWS, actual_facts, '"'"'BIOMIND_EFFECT_EVIDENCE_JSON='"'"', publication=True)
from pathlib import Path
import os
assert "new_fixture_v2.genome" in Path('"'"'/tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/task_outputs/fix-f6-partial_build/plan_artifacts/plan-artifact-85f0a0d3401ddc39a9151049'"'"').read_text()
assert Path('"'"'/tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/中文 两样本/输入 新构造/新参考.fa'"'"').read_bytes()
assert Path('"'"'/tmp/biomind-6793-fix-20260915/current07/f6negatives/scratch/biomind-plan-local-graph-xus_4zrw/中文 两样本/输入 新构造/新注释.gtf'"'"').read_bytes()
p=Path(os.environ["SNPEFF_DB"])/"data"/os.environ["GENOME_VERSION"]/"snpEffectPredictor.bin"
value=p.read_bytes().startswith(b"SYNTHETIC")
print(emit_effect_evidence({'"'"'produces_artifacts'"'"': {}, '"'"'produces_observations'"'"': {'"'"'snpeff_database_ready'"'"': {'"'"'value'"'"': value}}}))
PY
'