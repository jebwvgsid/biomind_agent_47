/bin/bash --noprofile --norc -c 'set -euo pipefail
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

_DECLARED_EFFECT_ROWS = json.loads('"'"'{"produces_artifacts": [{"identity": "plan-artifact-52a527461a09fddf4fab710d", "required_fields": []}], "explicit_side_effects": [], "produces_observations": [{"identity": "database_layout_prepared", "required_fields": []}]}'"'"')

def emit_effect_evidence(actual_facts):
    return _emit_declared_rows(_DECLARED_EFFECT_ROWS, actual_facts, '"'"'BIOMIND_EFFECT_EVIDENCE_JSON='"'"', publication=True)
from pathlib import Path
import os
db=Path(os.environ["SNPEFF_DB"]); genome=os.environ["GENOME_VERSION"]
(db/"data"/"genomes").mkdir(parents=True,exist_ok=True); (db/"data"/genome).mkdir(exist_ok=True)
(db/"data"/"genomes"/(genome+".fa")).write_bytes(Path('"'"'/tmp/biomind-plan-authority-v2-current11/graph_generic_recovery/scratch/biomind-plan-local-graph-8w6a51i8/中文 两样本/输入 新构造/新参考.fa'"'"').read_bytes())
(db/"data"/genome/"genes.gtf").write_bytes(Path('"'"'/tmp/biomind-plan-authority-v2-current11/graph_generic_recovery/scratch/biomind-plan-local-graph-8w6a51i8/中文 两样本/输入 新构造/新注释.gtf'"'"').read_bytes())
out=Path('"'"'/tmp/biomind-plan-authority-v2-current11/graph_generic_recovery/scratch/biomind-plan-local-graph-8w6a51i8/task_outputs/fix-f6-v2_generic_recovery/plan_artifacts/plan-artifact-52a527461a09fddf4fab710d'"'"');out.parent.mkdir(parents=True,exist_ok=True);out.write_text(genome+".genome : synthetic fixture\n")
value=out.is_file() and (db/"data"/genome/"genes.gtf").stat().st_size>0
print(emit_effect_evidence({'"'"'produces_artifacts'"'"': {'"'"'plan-artifact-52a527461a09fddf4fab710d'"'"': {'"'"'path'"'"': '"'"'/tmp/biomind-plan-authority-v2-current11/graph_generic_recovery/scratch/biomind-plan-local-graph-8w6a51i8/task_outputs/fix-f6-v2_generic_recovery/plan_artifacts/plan-artifact-52a527461a09fddf4fab710d'"'"'}}, '"'"'produces_observations'"'"': {'"'"'database_layout_prepared'"'"': {'"'"'value'"'"': value}}}))
PY
'