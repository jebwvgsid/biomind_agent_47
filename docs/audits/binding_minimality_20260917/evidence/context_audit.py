"""Read-only context measurements; no BioMind startup, model, or executor.

Run with the already installed BioMind Python. Configuration constants alone
are stubbed (documented defaults); all Skill parsing/snapshot/binding functions
are the current production implementations. Writes only beside this file.
"""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
import hashlib
import importlib.util
import json
import re
import types

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
RUNS = ROOT.parent / 'benchmarks/biomind_agent_benchmark/runs/task_outputs'
sys.path.insert(0, str(ROOT))

def guard(event, args):
    if event in {'socket.connect', 'subprocess.Popen', 'os.system'}:
        raise RuntimeError('Audit forbids network/process launch: ' + event)
    if event == 'open' and isinstance(args[0], (str, bytes)):
        if Path(args[0]).name in {'.env', '.env.api_keys'}:
            raise RuntimeError('Audit forbids credential configuration reads')
sys.addaudithook(guard)

cfg = types.ModuleType('config.config')
for k, v in dict(MAX_SKILL_BINDING_PROPOSALS_PER_STEP=3,
                 MAX_SKILL_CONTEXT_ROUNDS_PER_INVOCATION=8,
                 MAX_SKILL_SEARCH_ROUNDS_PER_STEP=5,
                 SKILL_CONTEXT_MAX_REQUESTS_PER_ROUND=6,
                 SKILL_EXCERPT_MAX_CHARS=24000,
                 SKILL_FULL_READ_MAX_CHARS=60000).items():
    setattr(cfg, k, v)
pkg = types.ModuleType('config'); pkg.__path__ = []; pkg.config = cfg
sys.modules['config'] = pkg; sys.modules['config.config'] = cfg
from agent_core.skill_manager import SkillManager
from agent_core.skill_access import SkillAccessService
from agent_core.skill_resource_binding import script_catalog, bind_skill_resources

def dump(name, obj):
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')
def metric(text):
    return {'chars': len(text), 'utf8_bytes': len(text.encode()),
            'lines': len(text.splitlines()), 'sha256': hashlib.sha256(text.encode()).hexdigest()}
def wire(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
def tags(text):
    result = []
    for tag in ['selected_skill_script_bindings','binding_input_requirements',
                'preflight_evidence_binding','action_json_example']:
        for match in re.finditer(r'<'+tag+r'(?: [^>]*?)?>(.*?)</'+tag+'>', text, re.S):
            result.append({'tag':tag, 'text':match[0], **metric(match[0])})
    return result

sid = 'genomics/wgs-short-read-mapping'
manager = SkillManager(str(ROOT / 'skills'))
service = SkillAccessService(manager)
access, snap, created = service.ensure_snapshot({}, sid, status='active')
catalog = script_catalog(service, access, step_id='input_check')
receipts = []
for row in catalog:
    resolved = bind_skill_resources(service, access, [row['handle']], step_id='input_check', cwd=str(ROOT))[0]
    receipts.append(resolved)
    row.update(availability='AVAILABLE', canonical_path=resolved['canonical_path'],
               registered_root=resolved['registered_root'], cwd=resolved['cwd'], namespace=resolved['namespace'])
    if resolved.get('invocation'):
        row['invocation'] = resolved['invocation']
    row.pop('snapshot_id', None)
raw = (ROOT / 'skills' / sid / 'SKILL.md').read_text()
fm = re.match(r'^---\n(.*?)\n---\n', raw, re.S)
draft_catalog = []
for row in catalog:
    iv = row['invocation']
    draft_catalog.append({'handle': row['handle'], 'status': 'available_verified_at_candidate_build',
        'shell_prefix': iv['shell_prefix'], 'arguments': iv['arguments'], 'carrier': iv['carrier'],
        'dependencies': 'program derives declared delegate closure before approval',
        'scope': iv['scope']})
dump('context_current_catalog.json', {'scope':'current production function construction, not a live provider request',
     'python':sys.executable, 'config_stub':'six skill-reader limits only, no environment config imported',
     'catalog':catalog, 'receipts':receipts, 'draft_not_implemented':draft_catalog})
stat = {'scope':'UTF-8 source and decoded message content; no token estimate',
        'python':sys.executable, 'tiktoken_installed':bool(importlib.util.find_spec('tiktoken')),
        'tokenizer_used':None, 'tokenizer_reason':'no encoding initialized or downloaded; byte/character metrics only',
        'skill_file':metric(raw), 'frontmatter_with_delimiters':metric(fm[0]),
        'body':metric(raw[fm.end():]), 'legacy_load_skill_result':metric(manager.load_skill(sid)),
        'schema2_snapshot_exact_text':metric(snap['exact_text']),
        'references':manager.get_skill_reference_index(sid),
        'catalog_current':metric(wire(catalog)), 'catalog_draft':metric(wire(draft_catalog)),
        'interpreter_identity_repetitions':sum('interpreter_identity' in row['invocation'] for row in catalog),
        'catalog_current_leaf_count':None, 'requests':[]}

def leaves(v):
    if isinstance(v, dict): return sum(leaves(x) for x in v.values())
    if isinstance(v, list): return sum(leaves(x) for x in v)
    return 1
stat['catalog_current_leaf_count'] = leaves(catalog)
stat['catalog_draft_leaf_count'] = leaves(draft_catalog)
keywords = ['binding_env','produced_bindings','consumed_bindings','required_runtime_binding_ids',
            'binding_digest','interpreter_identity','invocation','Normally OMIT binding_env',
            'Normally omit binding_env','selected_skill_script_bindings']
trace = RUNS / 'session_1789628497/context_trace'
selected = ['consultant-0005','planner_initial-0008','generate-0009','generate-0016','recovery_turn-0017']
excerpts = []
for pref in selected:
    path = next(trace.glob(pref+'*/request.json'))
    request = json.loads(path.read_text()); combined = '\n'.join(m['content'] for m in request['messages'])
    row = {'source':str(path), 'file_bytes':path.stat().st_size, 'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
           'message_contents':metric(combined), 'keyword_occurrences':{k:combined.count(k) for k in keywords},
           'messages':[]}
    for idx, msg in enumerate(request['messages']):
        content = msg['content']; tag_rows=tags(content)
        row['messages'].append({'index':idx, 'role':msg.get('role'), 'name':msg.get('name',''), **metric(content),
            'binding_related_tag_counts':{t:sum(x['tag']==t for x in tag_rows) for t in sorted({x['tag'] for x in tag_rows})},
            'binding_related_tag_chars':sum(x['chars'] for x in tag_rows)})
        if pref == 'generate-0009' and tag_rows:
            excerpts.append({'source':str(path), 'message_index':idx, 'blocks':tag_rows})
    stat['requests'].append(row)
    if pref == 'generate-0009':
        response = path.with_name('response.json')
        excerpts.append({'source':str(response), 'response':json.loads(response.read_text())})
dump('context_metrics.json', stat)
dump('context_historical_helper_sample.json', excerpts)

# Complete historical candidate, plus a deliberately narrow proposed output
# change: only the explicit root handle; program expands audited delegates.
rp = next(trace.glob('generate-0009*/response.json'))
response = json.loads(rp.read_text())
dump('context_helper_output_draft.json', {'source':str(rp), 'before':response,
    'draft_status':'proposal only; regenerate using current interpreter prefix; never mutate approved historical action',
    'proposal':'Keep the existing payload/script interface. Model declares only the chosen root helper handle; binder derives the verified frontmatter delegates before approval. For this existing historical bad candidate, replace direct source execution by the published interpreter+script prefix in the NEW candidate.',
    'mechanical_handles_before':4, 'mechanical_handles_after':1,
    'internal_receipts_remain':4,
    'business_parameters_preserved':['four FASTQ label/path pairs','reference label/path','observation id','deferred evidence dir'],
    'constraints_preserved':['prefix/header scope only','adverse or incomplete observations','immutable inputs','current attempt evidence leaf']})

# The proposal is concrete and reviewable, but is neither submitted nor run.
import shlex
original_envelope = json.loads(response['content'])
input_root = ROOT.parent / 'benchmarks/01.测试/04.重测序/Part1.variant_calling/data'
root_row = next(x for x in draft_catalog if x['handle'].endswith('/inspect_wgs_inputs.py'))
options = ['--observation-id validation_observation', '--evidence-dir "$BIOMIND_PREFLIGHT_EVIDENCE_DIR"']
for label, filename in [('S1_R1','S1_1.fq.gz'),('S1_R2','S1_2.fq.gz'),('S2_R1','S2_1.fq.gz'),('S2_R2','S2_2.fq.gz')]:
    options.append('--fastq '+label+' '+shlex.quote(str(input_root/filename)))
options.append('--reference genome '+shlex.quote(str(input_root/'genome.fasta')))
payload = '<shell skill_resources="'+root_row['handle']+'">\n'+root_row['shell_prefix']+' \\\n  '+' \\\n  '.join(options)+'\n</shell>'
draft_output = {'response_type':original_envelope['response_type'],
    'public_message':'候选将进行已批准范围内的 FASTQ 前缀与 FASTA 头检查，结果尚未执行或确认。', 'payload':payload}
dump('context_helper_concrete_draft.json', {'status':'NOT IMPLEMENTED / NOT SUBMITTED / NOT EXECUTED',
    'basis':str(rp),'input':{'step_id':'input_check','outputs':[],
       'expected_observation':'validation_observation','helper':root_row,
       'input_paths':{f:str(input_root/f) for f in ['S1_1.fq.gz','S1_2.fq.gz','S2_1.fq.gz','S2_2.fq.gz','genome.fasta']},
       'evidence_directory':'current attempt deferred variable; helper creates leaf',
       'verification_scope':'prefix/header only; preserve adverse/incomplete; no full-file integrity claim'},
    'output':draft_output,
    'required_code_change':'derive frontmatter delegates in bind_action_skill_resources BEFORE approval; current production rejects omitted delegates',
    'unchanged_business_values':'Labels, paths, observation ID and evidence variable come from the same historical candidate',
    'before_model_copied_handles':4, 'after_model_copied_handles':1})

historical_manifest = json.loads(next(trace.glob('generate-0009*/manifest.json')).read_text())
identity = historical_manifest['runtime_identity']
obj_path = trace.parent / 'state_contract/objects' / (identity['object_ref'].split(':')[-1]+'.json')
obj = json.loads(obj_path.read_text())['payload']
versions = {'historical_manifest':str(next(trace.glob('generate-0009*/manifest.json'))),
    'historical_object':str(obj_path), 'runtime_identity':identity,
    'loaded_modules':{k:{kk:vv for kk,vv in v.items() if kk != 'callables'} for k,v in obj['loaded_modules'].items()},
    'source_comparison':[],
    'limitation':'Archived loaded callable identities do not establish a loaded Git revision. skill_script_invocation.py and action_runtime.py are outside that archive source allowlist.'}
for rel, old in obj['source_files'].items():
    p=ROOT/rel
    versions['source_comparison'].append({'file':str(p),'historical_disk_sha256':old.get('sha256'),
        'current_disk_sha256':hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None})
dump('context_versions.json',versions)
lines = raw.splitlines(keepends=True)
stat['source_slices'] = {'helper_machine_contract_lines_12_53':metric(''.join(lines[11:53])),
    'helper_body_lines_211_310':metric(''.join(lines[210:310]))}
dump('context_metrics.json',stat)
print(json.dumps({'skill_file':stat['skill_file'], 'catalog_current':stat['catalog_current'],
                  'catalog_draft':stat['catalog_draft'], 'requests':len(stat['requests'])},ensure_ascii=False))
