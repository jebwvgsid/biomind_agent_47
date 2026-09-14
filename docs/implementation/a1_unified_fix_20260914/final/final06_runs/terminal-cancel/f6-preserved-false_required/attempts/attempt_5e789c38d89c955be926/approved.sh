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

_DECLARED_EFFECT_ROWS = json.loads('"'"'{"produces_artifacts": [], "explicit_side_effects": [], "produces_observations": [{"identity": "contigs_compatible", "required_fields": ["value"]}]}'"'"')

def emit_effect_evidence(actual_facts):
    return _emit_declared_rows(_DECLARED_EFFECT_ROWS, actual_facts, '"'"'BIOMIND_EFFECT_EVIDENCE_JSON='"'"', publication=True)
from pathlib import Path
import os
vcf=Path('"'"'/tmp/biomind-a1-unified-final-06/terminal-cancel/scratch/biomind-plan-local-graph-dahkp_im/终态回调 cancel/输入 新构造/两样本.vcf'"'"'); ref=Path('"'"'/tmp/biomind-a1-unified-final-06/terminal-cancel/scratch/biomind-plan-local-graph-dahkp_im/终态回调 cancel/输入 新构造/新参考.fa'"'"'); gtf=Path('"'"'/tmp/biomind-a1-unified-final-06/terminal-cancel/scratch/biomind-plan-local-graph-dahkp_im/终态回调 cancel/输入 新构造/新注释.gtf'"'"')
v={r.split('"'"'\t'"'"')[0] for r in vcf.read_text().splitlines() if r and not r.startswith('"'"'#'"'"')}
r={x[1:].split()[0] for x in ref.read_text().splitlines() if x.startswith('"'"'>'"'"')}
g={x.split('"'"'\t'"'"')[0] for x in gtf.read_text().splitlines() if x and not x.startswith('"'"'#'"'"')}
value=bool(v) and v.issubset(r) and bool(v & g)
print(emit_effect_evidence({'"'"'produces_artifacts'"'"': {}, '"'"'produces_observations'"'"': {'"'"'contigs_compatible'"'"': {'"'"'value'"'"': value}}}))
PY
'