"""Record already imported runtime bytes; never import missing business modules."""
import hashlib
import inspect
import json
import marshal
import os
from pathlib import Path
import sys


def pytest_sessionfinish(session, exitstatus):
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((root / 'export_manifest.json').read_text())
    modules = {}
    for name, module in list(sys.modules.items()):
        location = getattr(module, '__file__', None)
        if not location:
            continue
        path = Path(location).resolve()
        if path.is_relative_to(root) and path.is_file():
            relative = str(path.relative_to(root))
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            modules[name] = {'path': str(path), 'sha256': sha,
                'matches_export': manifest['files'].get(relative) == sha}
    names = {
        'incident_selected_snpeff_renderer': ['snpeff_argv', 'snpeff_cmd'],
        'bio_agents': ['BioAnalystAgent._persist_bound_action_authority',
            'BioAnalystAgent._candidate_admission_rejection_update',
            'BioAnalystAgent._validated_candidate_repair_projection_state'],
        'agent_core.workflow.candidate_repair': ['resolve_admitted_candidate_repairs',
            'latest_candidate_repair_receipt', 'candidate_progress_assessment', 'claim_candidate_generation'],
        'agent_core.plan_tracker': ['record_preaction_contract_failure', 'save_plan_state'],
        'agent_core.step_effects': ['materialize_effect_action', 'materialize_effect_template'],
        'agent_core.workspace_attestation': ['capture_mutation_failure_logs', 'finalize_mutation_attestation'],
        'agent_core.workflow.inspection_evidence': ['prepare_inspection_evidence', 'InspectionObjectReader.read'],
        'agent_core.workflow.managed_stop': ['build_managed_stop', '_matches', '_save', 'finalize_managed_stop'],
    }
    callables = {}
    for module_name, members in names.items():
        module = sys.modules.get(module_name)
        if module is None:
            continue
        for member in members:
            value = module
            for part in member.split('.'):
                value = getattr(value, part, None)
            if value is None:
                continue
            value = inspect.unwrap(value)
            code = getattr(value, '__code__', None)
            if code:
                callables[module_name + ':' + member] = {'filename': code.co_filename,
                    'firstlineno': code.co_firstlineno,
                    'marshal_v2_sha256': hashlib.sha256(marshal.dumps(code, 2)).hexdigest()}
    result = {'source_signature': manifest['source_signature'], 'exitstatus': exitstatus,
        'imported_modules': modules, 'loaded_callables': callables,
        'limits': 'Imported modules/callables prove loaded bytes, not every function executed; actual graph/request receipts prove exercised paths. Config and provider/science leaves are explicit offline substitutes.'}
    path = Path(os.environ['BIOMIND_GER_ARTIFACTS']) / 'loaded_manifest.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
