"""Offline audit of real helper binding functions; no model/helper execution.

Run with the existing biomind Python. Only explicitly listed source files are
copied to a new /tmp export. Configuration is replaced by six literal limits.
The audit hook rejects network, subprocesses, config and all dot-env reads.
"""
import collections
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types

ROOT = Path('/public/home/liusiqi/03.毕设/Biomind_v1')
OUT = ROOT / 'docs/audits/binding_minimality_20260917/evidence/helper_witness_results.json'
if len(sys.argv) > 1:
    OUT = Path(sys.argv[1]).resolve()
    assert OUT.parent == ROOT / 'docs/audits/binding_minimality_20260917/evidence'
OLD = Path('/tmp/skill-invocation-validation-nm1jgfjk')
MODULES = ['agent_core/__init__.py', 'agent_core/skill_access.py',
           'agent_core/skill_manager.py', 'agent_core/action_runtime.py',
           'agent_core/skill_resource_binding.py', 'agent_core/skill_script_invocation.py',
           'agent_core/approved_resource_locator.py', 'agent_core/managed_artifacts.py',
           'agent_core/step_effects.py']
sys.dont_write_bytecode = True
scratch = Path(tempfile.mkdtemp(prefix='binding-helper-audit-'))
for name in MODULES:
    dst = scratch / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / name, dst)
skill_rel = Path('skills/genomics/wgs-short-read-mapping')
for name in ['SKILL.md', 'scripts/inspect_wgs_inputs.py', 'scripts/inspect_fastq.py',
             'scripts/inspect_fasta_header.py', 'scripts/inspect_fastq_batch.py']:
    dst = scratch / skill_rel / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / skill_rel / name, dst)

def guard(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen',
                 'os.system', 'os.posix_spawn'}:
        raise RuntimeError('OFFLINE_DENIED ' + event)
    if event == 'open' and isinstance(args[0], (str, bytes)):
        p = Path(os.fsdecode(args[0]))
        if p.name.startswith('.env') or (ROOT / 'config') in p.parents:
            raise RuntimeError('PROTECTED_READ_DENIED')
        mode, flags = args[1] or '', args[2] or 0
        if any(x in str(mode) for x in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            if not p.is_relative_to(scratch) and p != OUT:
                raise RuntimeError('WRITE_DENIED ' + str(p))

sys.addaudithook(guard)
sys.path.insert(0, str(scratch))
cfg = types.ModuleType('config'); cfg.__path__ = []
limits = types.ModuleType('config.config')
for key, value in {'MAX_SKILL_BINDING_PROPOSALS_PER_STEP': 4,
                   'MAX_SKILL_CONTEXT_ROUNDS_PER_INVOCATION': 4,
                   'MAX_SKILL_SEARCH_ROUNDS_PER_STEP': 4,
                   'SKILL_CONTEXT_MAX_REQUESTS_PER_ROUND': 4,
                   'SKILL_EXCERPT_MAX_CHARS': 24000,
                   'SKILL_FULL_READ_MAX_CHARS': 200000}.items():
    setattr(limits, key, value)
cfg.config = limits
sys.modules.update({'config': cfg, 'config.config': limits})

from agent_core.skill_access import SkillAccessService, SkillAccessError, new_skill_access_state
from agent_core.skill_manager import SkillManager
from agent_core.skill_resource_binding import bind_action_skill_resources, bind_skill_resources, validate_action_skill_resources, script_catalog
from agent_core.skill_script_invocation import build_skill_script_argv
from agent_core.step_effects import executable_carrier

SID = 'genomics/wgs-short-read-mapping'
H = SID + '#scripts/inspect_fastq.py'
service = SkillAccessService(SkillManager(str(scratch / 'skills')))
access = service.ensure_selected_skill_refs(new_skill_access_state(), [SID], task_revision=1)
row = bind_skill_resources(service, access, [H], step_id='inspect', cwd=scratch)[0]
argv = build_skill_script_argv(row, ['--path', '/synthetic/input.fastq', '--observation-id', 'validation_observation'])
import shlex
command = shlex.join(argv)
results = []
def check(name, action, expected, frozen=False):
    before = json.dumps(action, sort_keys=True)
    try:
        if frozen:
            validate_action_skill_resources(action, service, access, step_id='inspect', cwd=scratch)
        else:
            bound = bind_action_skill_resources(action, service, access, step_id='inspect', cwd=scratch)
            assert bound.get('code') == action.get('code') and bound.get('argv') == action.get('argv')
        actual, detail = 'PASS', ''
    except SkillAccessError as exc:
        actual, detail = exc.code, str(exc)
    assert json.dumps(action, sort_keys=True) == before
    assert actual == expected, (name, actual, expected)
    results.append({'name': name, 'expected': expected, 'actual': actual,
                    'detail': detail, 'action': action, 'selected_carrier': executable_carrier(action)})

def action(body, **extra):
    return {'type': 'execute', 'language': 'bash', 'code': body,
            'skill_resource_refs': [H], **extra}

check('direct_argv', {'type':'execute','argv':argv,'skill_resource_refs':[H]}, 'PASS')
check('code_plus_argv_uses_actual_argv', action('not the executed body', argv=['bash','-lc',command]), 'PASS')
check('quoted_bound_variable', action(shlex.quote(sys.executable) + ' "$' + row['environment_variable'] + '" ' + shlex.join(argv[2:])), 'PASS')
check('two_newline_direct_calls', action(command + '\n' + command), 'PASS')
check('safe_echo_plus_helper_is_unverifiable', action('echo starting\n' + command), 'SKILL_INVOCATION_UNVERIFIABLE')
check('wrapper_is_unverifiable', {'type':'execute','argv':['bash','/synthetic/wrapper.sh'],'skill_resource_refs':[H]}, 'SKILL_INVOCATION_UNVERIFIABLE')
check('local_assignment_is_unverifiable', action('P=' + shlex.quote(row['canonical_path']) + '\n' + shlex.quote(sys.executable) + ' "$P" ' + shlex.join(argv[2:])), 'SKILL_INVOCATION_UNVERIFIABLE')
check('arbitrary_free_script_without_refs', {'type':'execute','language':'bash','code':'P=foo\necho "$P" | cat'}, 'PASS')
check('direct_source_lacks_interpreter', action(shlex.join(argv[1:])), 'SKILL_INVOCATION_INTERPRETER_REQUIRED')
check('unknown_resource', action(command, skill_resource_refs=[SID+'#scripts/no_such.py']), 'SKILL_RESOURCE_OUT_OF_SCOPE')
primary = SID + '#scripts/inspect_wgs_inputs.py'
main = bind_skill_resources(service, access, [primary], step_id='inspect', cwd=scratch)[0]
main_command = main['invocation']['shell_prefix'] + ' --fastq S /synthetic/a.fq --reference R /synthetic/r.fa --observation-id validation_observation --evidence-dir "$BIOMIND_PREFLIGHT_EVIDENCE_DIR"'
check('missing_delegate', action(main_command, skill_resource_refs=[primary]), 'SKILL_INVOCATION_DEPENDENCY_UNBOUND')
all_handles = [primary, *[SID+'#'+p for p in main['invocation']['delegates']]]
check('declared_delegate_closure', action(main_command, skill_resource_refs=all_handles), 'PASS')

count = collections.Counter()
verify0, resource0 = service._verified_snapshot, service._resource_snapshot
def verify(*a, **k): count['verified_snapshot'] += 1; return verify0(*a, **k)
def resource(*a, **k): count['resource_snapshot'] += 1; return resource0(*a, **k)
service._verified_snapshot, service._resource_snapshot = verify, resource
catalog = script_catalog(service, access, step_id='inspect')
for item in catalog:
    bind_skill_resources(service, access, [item['handle']], step_id='inspect', cwd=scratch)
catalog_count = dict(count); count.clear()
bound = bind_action_skill_resources(action(main_command, skill_resource_refs=all_handles), service, access, step_id='inspect', cwd=scratch)
binding_count = dict(count); count.clear()
validate_action_skill_resources(bound, service, access, step_id='inspect', cwd=scratch)
prelaunch_count = dict(count)

# A copied executable used for stat-only identity; this file is never launched.
launcher = scratch / 'synthetic-python'; launcher.write_text('synthetic identity fixture\n'); launcher.chmod(0o755)
original_executable = sys.executable
sys.executable = str(launcher)
identity_row = bind_skill_resources(service, access, [H], step_id='inspect', cwd=scratch)[0]
identity_action = {'type':'execute', 'argv':build_skill_script_argv(identity_row, argv[2:]), 'skill_resource_refs':[H]}
identity_bound = bind_action_skill_resources(identity_action, service, access, step_id='inspect', cwd=scratch)
hash_before = hashlib.sha256(launcher.read_bytes()).hexdigest()
stat = launcher.stat(); os.utime(launcher, ns=(stat.st_atime_ns, stat.st_mtime_ns+1_000_000_000))
check('same_bytes_interpreter_timestamp_change', identity_bound, 'SKILL_BINDING_DRIFT', frozen=True)
assert hashlib.sha256(launcher.read_bytes()).hexdigest() == hash_before
sys.executable = original_executable

receipt = json.loads((ROOT/'docs/fixes/skill_script_invocation_contract_20260917_results.json').read_text())
hashes = []
for f, digest in receipt['source_sha256'].items():
    current = hashlib.sha256((ROOT/f).read_bytes()).hexdigest()
    exported = hashlib.sha256((OLD/'candidate'/f).read_bytes()).hexdigest()
    hashes.append({'path':f,'receipt_sha256':digest,'current_sha256':current,'old_export_sha256':exported,'all_match':digest==current==exported})
old_reports = json.loads((OLD/'integration-results.json').read_text())
old_calls = [x for x in old_reports if x.get('phase') == 'call']
old_scenarios = []
for name in ['compact-missing_interpreter','compact-argument_arity','compact-retry','compact-adverse','compact','off']:
    p = OLD/'artifacts'/('new-mapping-preflight-'+name+'-result.json')
    item = json.loads(p.read_text())
    state = json.loads(p.with_name(p.name.replace('-result.json','-state.json')).read_text())
    rejections = []
    for event in state.get('events', []):
        g = event.get('generate', {})
        if g.get('generate_outcome', {}).get('type') == 'candidate_rejected':
            outcome = g['generate_outcome']; payload0 = outcome['payload']
            context = payload0.get('repair_context') or {}
            rejections.append({'scope':outcome['scope'],
                'candidate_fingerprint':payload0.get('candidate_fingerprint'),
                'feedback':payload0.get('feedback'), 'repairable':payload0.get('repairable'),
                'repair_context':{k:context.get(k) for k in ['repair_stage','validation_source',
                    'repair_domain','repair_ordinal','failure_stage','execution_status',
                    'exact_violations','protocol_error']},
                'previous_candidate':{k:context.get('previous_candidate',{}).get(k) for k in
                    ['type','code','argv','env','skill_resource_refs','binding_env','produced_bindings']},
                'budget':{k:(payload0.get('budget_receipt') or {}).get(k) for k in
                    ['unit','category','correction_authorized','ordinal','remaining']}})
    old_scenarios.append({'source':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
        'counts':item['counts'],'routes':item['routes'],'commits':len(item.get('commits',{})),
        'approval_array_recorded':'approvals' in item,
        'approvals':[{'sha256':a['sha256'],'code':a['code']} for a in item.get('approvals',[])],
        'executions':[{k:e[k] for k in ['returncode','approved_sha256','wrapper_sha256','binding']} for e in item['executions']],
        'duplicate_deliveries':item.get('duplicate_deliveries'),'recovery_calls':item.get('recovery_calls'),
        'rejections':rejections})

assert all((scratch/f).read_bytes() == (ROOT/f).read_bytes() for f in MODULES), 'Source changed during witness'
payload = {'scope':'real binding functions on explicit isolated source export; no subprocess/model/API/science; literal synthetic config; historical graph evidence reused, not rerun',
    'scratch':str(scratch),'python':original_executable,
    'source_sha256':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in MODULES},
    'loaded_modules':{n:str(m.__file__) for n,m in sys.modules.items() if n.startswith('agent_core.') and getattr(m,'__file__',None)},
    'witnesses':results,'same_bytes_interpreter_sha256':hash_before,
    'call_counts':{'generate_catalog_four_resources':catalog_count,'bind_four_resources_once':binding_count,'prelaunch_four_resources_once':prelaunch_count},
    'prior_six_file_hash_verification':hashes,
    'prior_69_tests':{'report_path':str(OLD/'integration-results.json'),'report_sha256':hashlib.sha256((OLD/'integration-results.json').read_bytes()).hexdigest(),
        'call_count':len(old_calls),'outcomes':dict(collections.Counter(x['outcome'] for x in old_calls)),
        'nodeids':[x['nodeid'] for x in old_calls]},
    'prior_graph_scenarios':old_scenarios}
OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'output':str(OUT),'witnesses':len(results),'all_expected_outcomes':True,'six_hashes_match':all(x['all_match'] for x in hashes),'prior_69_calls':len(old_calls),'call_counts':payload['call_counts']},ensure_ascii=False))
