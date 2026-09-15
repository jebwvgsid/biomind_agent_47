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

_DECLARED_EFFECT_ROWS = json.loads('"'"'{"produces_artifacts": [{"identity": "report", "required_fields": []}, {"identity": "reproduce_script", "required_fields": []}, {"identity": "stage_status", "required_fields": []}], "explicit_side_effects": [], "produces_observations": [{"identity": "final_outputs_recorded", "required_fields": []}]}'"'"')

def emit_effect_evidence(actual_facts):
    return _emit_declared_rows(_DECLARED_EFFECT_ROWS, actual_facts, '"'"'BIOMIND_EFFECT_EVIDENCE_JSON='"'"', publication=True)
from pathlib import Path
import os
annotated=Path('"'"'/tmp/biomind-plan-authority-v2-current07/graph_generic_split/scratch/biomind-plan-local-graph-a5eeq91j/中文 两样本/Part2.vairant_annotation/新机制结果/annotated.vcf'"'"'); summary=Path('"'"'/tmp/biomind-plan-authority-v2-current07/graph_generic_split/scratch/biomind-plan-local-graph-a5eeq91j/中文 两样本/Part2.vairant_annotation/新机制结果/ann_summary.tsv'"'"')
assert "ANN=" in annotated.read_text() and "synthetic_effect" in summary.read_text()
out=Path('"'"'/tmp/biomind-plan-authority-v2-current07/graph_generic_split/scratch/biomind-plan-local-graph-a5eeq91j/中文 两样本/Part2.vairant_annotation/新机制结果/report.md'"'"');out.parent.mkdir(parents=True,exist_ok=True);out.write_text('"'"'# SYNTHETIC FRAMEWORK MECHANISM\n\nAll outputs are generated synthetic protocol fixtures. No real snpEff science was run.\n'"'"')
out=Path('"'"'/tmp/biomind-plan-authority-v2-current07/graph_generic_split/scratch/biomind-plan-local-graph-a5eeq91j/中文 两样本/Part2.vairant_annotation/新机制结果/reproduce.sh'"'"');out.parent.mkdir(parents=True,exist_ok=True);out.write_text('"'"'#!/bin/bash\n# Synthetic fixture provenance only; does not run real science.\nprintf "synthetic mechanism receipt\\n"\n'"'"')
out=Path('"'"'/tmp/biomind-plan-authority-v2-current07/graph_generic_split/scratch/biomind-plan-local-graph-a5eeq91j/中文 两样本/Part2.vairant_annotation/新机制结果/stage_status.tsv'"'"');out.parent.mkdir(parents=True,exist_ok=True);out.write_text('"'"'# Durable status snapshot taken before this action execution and Commit.\nstep_id\toperation\tobserved_status\nprobe-731\texecute\tpassed\nledger-205\texecute\tpassed\ngate-418\tcheck_contig_compatibility\tpassed\nlayout-563\tprepare_database\tpassed\ncompile-927\tbuild_database\tpassed\neffects-146\tannotate_variants\tpassed\naggregate-832\tsummarize_ann\tpassed\ndeliver-604\tfinalize_outputs\trunning\n'"'"')
value={"synthetic":True,"physical_reports":3}
print(emit_effect_evidence({'"'"'produces_artifacts'"'"': {'"'"'report'"'"': {'"'"'path'"'"': '"'"'/tmp/biomind-plan-authority-v2-current07/graph_generic_split/scratch/biomind-plan-local-graph-a5eeq91j/中文 两样本/Part2.vairant_annotation/新机制结果/report.md'"'"'}, '"'"'reproduce_script'"'"': {'"'"'path'"'"': '"'"'/tmp/biomind-plan-authority-v2-current07/graph_generic_split/scratch/biomind-plan-local-graph-a5eeq91j/中文 两样本/Part2.vairant_annotation/新机制结果/reproduce.sh'"'"'}, '"'"'stage_status'"'"': {'"'"'path'"'"': '"'"'/tmp/biomind-plan-authority-v2-current07/graph_generic_split/scratch/biomind-plan-local-graph-a5eeq91j/中文 两样本/Part2.vairant_annotation/新机制结果/stage_status.tsv'"'"'}}, '"'"'produces_observations'"'"': {'"'"'final_outputs_recorded'"'"': {'"'"'value'"'"': value}}}))
PY
'