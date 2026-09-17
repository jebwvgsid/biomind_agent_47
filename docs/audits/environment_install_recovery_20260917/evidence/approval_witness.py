"""Current production approval/runtime checks over synthetic files only.

No model/provider, dotenv, install, original session execution, or science.
Run with an already installed Python 3.12 and PYTHONDONTWRITEBYTECODE=1.
"""
from pathlib import Path
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
os.environ['BIOMIND_DISABLE_DOTENV'] = '1'
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))

def guard(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        p = Path(os.fsdecode(args[0])).absolute()
        if p.name.startswith('.env'):
            raise PermissionError('Audit forbids dotenv reads')
        mode = str(args[1] or '')
        flags = args[2] or 0
        writing = any(c in mode for c in 'wax+') or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if writing and not (p.is_relative_to(OUT) or p.is_relative_to('/tmp')):
            raise PermissionError('Audit write boundary: ' + str(p))
    if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system'}:
        raise PermissionError('No external execution in approval witness')

sys.addaudithook(guard)
from agent_core.action_runtime import (
    ActionRuntimeContractError, build_runtime_binding_registry,
    resolve_action_runtime_bindings, validate_runtime_paths_prelaunch,
    build_action_approval_identity, validate_action_approval_identity,
)

rows = []
with tempfile.TemporaryDirectory(prefix='biomind-approval-audit-') as d:
    tool = Path(d) / 'tool'
    tool.write_text('#!/bin/sh\nexit 0\n')
    tool.chmod(0o755)
    contract = {'capability_id': 'audit.run', 'selected_skill_id': 'audit/skill',
                'required_runtime_binding_ids': ['runtime.tool.bin'],
                'optional_runtime_binding_ids': [], 'required_artifact_binding_ids': []}
    registry = build_runtime_binding_registry([{'status': 'ready', 'skill_id': 'audit/skill',
                'runtime_audit_id': 'synthetic-evidence', 'resolved_runtime_paths': {'TOOL_BIN': str(tool)}}])
    def resolve():
        return resolve_action_runtime_bindings(selected_skill_id='audit/skill',
            execution_capability_id='audit.run', approved_capability_contract_hash='',
            capability_registry={'audit.run': contract}, ready_runtime_binding_registry=registry,
            current_action_identity={'action_id': 'synthetic-action', 'plan_id': 'synthetic-plan', 'step_id': 'synthetic-step'})
    result = resolve()
    kwargs = dict(generated_script_sha256='synthetic-script',
        capability_contract_hash_value=result['capability_contract_hash'],
        runtime_env_manifest_hash=result['runtime_binding_manifest_hash'],
        artifact_env_manifest_hash='synthetic-artifact',
        execution_policy_manifest_hash='synthetic-policy', working_directory=d)
    approved = build_action_approval_identity(**kwargs)
    validate_action_approval_identity(approved, build_action_approval_identity(**kwargs))
    rows.append({'scenario': 'identical action identity', 'observed': 'accepted', 'status': 'PASS'})
    for field in kwargs:
        changed = copy.deepcopy(kwargs)
        changed[field] = d + '/other' if field == 'working_directory' else 'changed'
        try:
            validate_action_approval_identity(approved, build_action_approval_identity(**changed))
        except ActionRuntimeContractError as exc:
            rows.append({'scenario': field + ' changes', 'observed': exc.code, 'status': 'PASS'})
        else:
            raise AssertionError('drift accepted: ' + field)
    before_hash = hashlib.sha256(tool.read_bytes()).hexdigest()
    tool.write_text('#!/bin/sh\nexit 17\n')
    after_hash = hashlib.sha256(tool.read_bytes()).hexdigest()
    assert before_hash != after_hash
    validate_runtime_paths_prelaunch(result)
    assert resolve()['runtime_binding_manifest_hash'] == result['runtime_binding_manifest_hash']
    rows.append({'scenario': 'same executable path with changed bytes',
        'observed': 'path/access prelaunch passes; manifest path-value digest unchanged',
        'status': 'PASS_CURRENT_BEHAVIOR', 'limitation': 'These boundaries alone do not attest installed version or file content; no process executed.'})
    tool.unlink()
    try:
        validate_runtime_paths_prelaunch(result)
    except ActionRuntimeContractError as exc:
        assert exc.code == 'RUNTIME_BINDING_PRELAUNCH_PATH_INVALID'
        rows.append({'scenario': 'registered path removed', 'observed': exc.code, 'status': 'PASS'})
    else:
        raise AssertionError('missing path accepted')

suite = unittest.TestSuite()
loader = unittest.TestLoader()
for name in ['tests.test_action_scoped_runtime_bindings', 'tests.test_plan_contract_approval_scope']:
    suite.addTests(loader.loadTestsFromName(name))
with (OUT / 'approval_existing_tests.txt').open('w') as stream:
    tested = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
loaded_sources = {}
for module in list(sys.modules.values()):
    source_file = getattr(module, '__file__', None)
    if source_file:
        source_path = Path(source_file).resolve()
        if source_path.is_relative_to(ROOT) and source_path.suffix == '.py':
            loaded_sources[str(source_path)] = hashlib.sha256(source_path.read_bytes()).hexdigest()
report = {'type': 'current_behavior_witness_and_existing_regression', 'python': sys.executable,
    'completed_utc': datetime.now(timezone.utc).isoformat(),
    'imported_module_disk_hashes_at_completion': loaded_sources,
    'source_paths': [str(ROOT/'agent_core/action_runtime.py'), str(ROOT/'agent_core/plan_tracker.py')],
    'scenarios': rows, 'existing_tests': {'run': tested.testsRun, 'failures': len(tested.failures),
    'errors': len(tested.errors), 'skipped': len(tested.skipped), 'passed': tested.wasSuccessful()},
    'not_run': ['production repair', 'real model', 'installation', 'scientific analysis', 'original task resume']}
(OUT/'approval_results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
print(json.dumps(report, ensure_ascii=False))
if not tested.wasSuccessful():
    raise SystemExit(1)
