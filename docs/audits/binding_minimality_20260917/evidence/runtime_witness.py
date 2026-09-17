"""Read-only production-function witness; no model, runner, installation or scientific work."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
import yaml
from agent_core.action_runtime import (
    ActionRuntimeContractError, build_runtime_binding_registry,
    build_exact_action_environment, resolve_action_runtime_bindings,
    project_action_runtime_for_provider, validate_runtime_paths_prelaunch,
)
from agent_core.runtime_evidence import project_runtime_audits, evaluate_runtime_evidence

RUN = Path('/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789628497')
SID = 'genomics/wgs-short-read-mapping'
SKILL = ROOT / 'skills' / SID / 'SKILL.md'
meta = yaml.safe_load(SKILL.read_text().split('---', 2)[1])
contract = next(c for c in meta['generate']['execution_capabilities'] if c['operation'] == 'prepare_reference')
capid = contract['capability_id']
prefix = Path('/public/home/liusiqi/anaconda3/envs/bio_wgs')
state = json.loads((RUN / 'plan_state.json').read_text())
task = state['task_state']
wgs_record = next(r for r in task['runtime_evidence']['records'] if r['scope']['skill_id'] == SID)
scope = wgs_record['scope']
# Historical projection is evaluated at the actual attempt time, not audit wall time.
historical_time = '2026-09-17T07:10:39+00:00'
audits = project_runtime_audits(task, task_id=state['task_id'], skill_scope=scope, now=historical_time)
registry = build_runtime_binding_registry(audits)
def resolve(c, reg, optional=()):
    return resolve_action_runtime_bindings(selected_skill_id=SID,
        execution_capability_id=c['capability_id'], approved_capability_contract_hash='',
        capability_registry={c['capability_id']: c}, ready_runtime_binding_registry=reg,
        current_action_identity={'action_id': 'isolated-audit', 'plan_id': 'audit', 'step_id': 'prepare_reference'},
        requested_optional_ids=optional)

current = resolve(contract, registry)
assert meta['runtime']['default_env'] == 'bio_wgs'
assert list(registry) == ['runtime.environment.prefix']
assert current['effective_runtime_bindings'] == {}
assert current['runtime_binding_manifest']['bindings'] == []
assert project_action_runtime_for_provider(current)['current_action_runtime_bindings'] == []
# Explicit synthetic ready record isolates transport semantics. It is NOT an accepted runtime inspection.
synthetic_registry = build_runtime_binding_registry([{'skill_id': SID, 'status': 'ready',
    'runtime_audit_id': 'isolated-synthetic-runtime-record', 'checked_env': 'bio_wgs',
    'resolved_runtime_paths': {'PICARD_BIN': str(prefix / 'bin/picard')}}])
optional_contract = deepcopy(contract)
optional_contract['optional_runtime_binding_ids'] = ['runtime.picard.bin']
ignored = resolve(optional_contract, synthetic_registry)
selected = resolve(optional_contract, synthetic_registry, ['runtime.picard.bin'])
assert ignored['effective_runtime_bindings'] == {}
assert selected['effective_runtime_bindings']['PICARD_BIN'] == str(prefix / 'bin/picard')
validate_runtime_paths_prelaunch(selected)
base = {'PATH': '/isolated/ambient/bin', 'JAVA_HOME': '/isolated/ambient/java'}
base_copy = dict(base)
exact_without = build_exact_action_environment(base)
exact_with = build_exact_action_environment(base, action_environment={'PICARD_BIN': str(prefix / 'bin/picard')})
assert 'PICARD_BIN' not in exact_without
assert exact_without == base_copy and base == base_copy
assert exact_with['PICARD_BIN'] == str(prefix / 'bin/picard')
assert exact_with['JAVA_HOME'] == base['JAVA_HOME']
required_contract = deepcopy(contract)
required_contract['required_runtime_binding_ids'] = ['runtime.picard.bin']
errors = {}
for name, reg in [('no_entry', {}), ('unavailable', {'runtime.picard.bin': {'status': 'unavailable'}}),
                  ('conflict', {'runtime.picard.bin': {'status': 'ambiguous'}})]:
    try:
        resolve(required_contract, reg)
    except ActionRuntimeContractError as exc:
        errors[name] = exc.to_diagnostic()
    else:
        raise AssertionError(name)
assert errors['no_entry']['availability'] == 'UNKNOWN'
assert errors['unavailable']['availability'] == 'UNAVAILABLE'
assert errors['conflict']['availability'] == 'AMBIGUOUS'
expired = evaluate_runtime_evidence(task, task_id=state['task_id'], skill_scope=scope,
    required_binding_ids=['runtime.environment.prefix'], now='2026-09-17T09:10:39+00:00')
assert expired['runtime.environment.prefix']['availability'] == 'UNKNOWN'
picard = prefix / 'bin/picard'
wrapper = picard.resolve()
raw = wrapper.read_text()
assert 'java="$ENV_PREFIX/bin/java"' in raw and 'java="$JAVA_HOME/bin/java"' in raw
report = {
    'scope': 'No science, subprocess, API or config imports. Current production action_runtime/runtime_evidence functions, real Skill frontmatter and archived facts; synthetic optional contracts only in memory.',
    'python': sys.executable,
    'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in [SKILL, ROOT/'agent_core/action_runtime.py', ROOT/'agent_core/runtime_evidence.py']},
    'historical_projection_time': historical_time,
    'historical_wgs_evidence': {'scope': scope, 'observed_at': wgs_record['observed_at'], 'expires_at': wgs_record['expires_at'], 'facts': wgs_record['facts'], 'resolved_runtime_paths': wgs_record['resolved_runtime_paths']},
    'historical_ready_registry_ids': list(registry),
    'current_real_contract': contract,
    'current_resolution': current,
    'synthetic_optional_default_resolution': ignored['effective_runtime_bindings'],
    'synthetic_optional_explicit_resolution': selected['effective_runtime_bindings'],
    'exact_environment_no_action_env': exact_without,
    'exact_environment_explicit_action_env': exact_with,
    'required_missing_diagnostics': errors,
    'expired_evidence': expired,
    'current_picard': {'entry': str(picard), 'resolved_wrapper': str(wrapper),
        'wrapper_sha256': hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        'default_java': str(prefix/'bin/java'), 'jar': str(wrapper.parent/'picard.jar'),
        'java_exists': (prefix/'bin/java').is_file(), 'jar_exists': (wrapper.parent/'picard.jar').is_file(),
        'valid_JAVA_HOME_overrides_default': True},
    'result': 'PASS: assertions distinguish current observed deficiency and valid empty helper runtime; no fix asserted.'}
print(json.dumps(report, ensure_ascii=False, indent=2))
