"""Read-only, bounded extraction of the two named sessions; never replay commands."""
from pathlib import Path
from datetime import datetime
from collections import Counter
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
SESSION_DIR = Path('/public/home/liusiqi/.pi/agent/sessions/--public-home-liusiqi-03.毕设-benchmarks-02.测试26.9--')
CASES = {'TAD': '2026-10-01T08-02-03-487Z_01a0f67c-1a5d-779d-8a0b-9542b5a26c76.jsonl',
         'ORA': '2026-10-01T08-19-56-956Z_01a0f68c-7b84-70ca-bfcd-d234aa37b81b.jsonl'}

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def save(name, obj):
    (OUT/name).write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n')

def stamp(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))

def extract():
    cases = {}
    for label, filename in CASES.items():
        path = SESSION_DIR/filename
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        calls = {}
        usage = Counter(); event_counts = Counter(); tokens = []
        for lineno, row in enumerate(rows, 1):
            message = row.get('message', {})
            if row.get('customType') == 'biomind.usage_attempt.v1':
                data = row['data']; event_counts[data['type']] += 1
                if data['type'] == 'provider_result':
                    tokens.append({k:data.get(k) for k in ['providerAttemptId','operation','usage','usageStatus','stopReason','physicalAttemptIds']})
                    for key, value in (data.get('usage') or {}).items():
                        if isinstance(value, (float, int)): usage[key] += value
            if message.get('role') == 'assistant':
                for block in message.get('content', []):
                    if block.get('type') != 'toolCall': continue
                    args = block.get('arguments', {})
                    # Do not archive source data, whole reports, or sensitive requests.
                    calls[block['id']] = {'call_id':block['id'], 'name':block['name'], 'call_line':lineno,
                        'assistant_recorded_at':row['timestamp'], 'command':args.get('command'),
                        'path':args.get('path'), 'timeout':args.get('timeout')}
            if message.get('role') == 'toolResult':
                call = calls[message['toolCallId']]
                text = '\n'.join(b.get('text','') for b in message.get('content',[]) if b.get('type')=='text')
                call.update(result_line=lineno, result_recorded_at=row['timestamp'], isError=message.get('isError'),
                    result_chars=len(text), result_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    pair_elapsed_s=round((stamp(row['timestamp'])-stamp(call['assistant_recorded_at'])).total_seconds(),3))
        task = 'pi-'+hashlib.sha256(rows[0]['id'].encode()).hexdigest()[:24]
        state_path = Path(rows[0]['cwd'])/'.biomind/pi-domain/task_outputs'/task/'plan_state.json'
        state = json.loads(state_path.read_text()); ts=state['task_state']
        final = next(r for r in reversed(rows) if r.get('customType')=='biomind.request')
        cases[label] = {'session_path':str(path),'session_sha256':sha(path),'session_id':rows[0]['id'],
            'project':rows[0]['cwd'],'state_path':str(state_path),'state_sha256':sha(state_path),
            'execution_profile':ts.get('pi_execution_profile'), 'task_id':task,
            'request_id':ts['pi_request_id'], 'request_state':{k:final['data'].get(k) for k in ['role','status','handoffConsumed']},
            'settled_at':final['timestamp'],'runtime_evidence_present':'runtime_evidence' in ts,
            'permission_requests':len(ts.get('execution_permissions',{}).get('requests',{})),
            'state_siblings':sorted(p.name for p in state_path.parent.iterdir()),
            'tool_counts':dict(Counter(c['name'] for c in calls.values())), 'usage_event_counts':dict(event_counts),
            'usage_sum':dict(usage),'usage_attempts':tokens,
            'compaction_entries':sum(r['type']=='compaction' for r in rows), 'calls':list(calls.values()),
            'timing_limit':'pair_elapsed_s includes scheduling/batch persistence; not separately instrumented command CPU or subprocess wall time'}
    save('case_evidence.json', cases)

    registry_paths=[ROOT/'software_registry.json',ROOT.parent/'agent_shim/software_registry.json',ROOT/'skills/.skill_runtime_registry.json']
    regs=[]
    for p in registry_paths:
        obj=json.loads(p.read_text()); item={'path':str(p),'sha256':sha(p),'mtime':p.stat().st_mtime,
            'sections':{k:len(v) for k,v in obj.items() if isinstance(v,dict)}, 'relevant_records':{}}
        for section in ['environments','tools','skills']:
            for name,row in obj.get(section,{}).items():
                if name in ['rna_env','R','Rscript','fgsea','hicexplore','enrichment-analysis','3d-dna/tad-calling'] or row.get('env')=='rna_env':
                    item['relevant_records'][section+':'+name]={k:v for k,v in row.items() if k in
                        ['env','status','prefix','path','packages','capabilities','language','env_runner','registered_at','last_verified_at','skill_ids','bound_tools','versions']}
        item['rna_text_occurrences']=p.read_text().count('rna_env')
        regs.append(item)
    inventory=Path('/public/home/liusiqi/.conda/environments.txt')
    save('registry_evidence.json',{'registries':regs,'inventory':{'path':str(inventory),'sha256':sha(inventory),
        'relevant_prefixes':[s for s in inventory.read_text().splitlines() if Path(s).name in ['rna_env','hicexplore']]},
        'historical_limit':'current bytes/mtime and inventory do not establish task-time registration or task-time actual registry override'})

    relevant=['bin/biomind','bio_agents.py','agent_core/pi_entry.py','agent_core/pi_model_config.py',
        'agent_core/registered_runtime.py','agent_core/skill_runtime_metadata.py','agent_core/action_runtime.py',
        'agent_core/command_execution.py','agent_core/formal_command_observation.py','execution/executor.py','package_manager.py',
        'integrations/pi_runtime/python_service.py','integrations/pi_runtime/domain_adapter/service.py','integrations/pi_runtime/domain_adapter/registry.py',
        'skills/enrichment-analysis/SKILL.md','skills/enrichment-analysis/references/dependencies.md',
        'skills/enrichment-analysis/scripts/run_enrichment.R','skills/3d-dna/tad-calling/scripts/call_tads.py','user_preferences.md']
    for name in ['host','native-workflow','stage-context','execution-profile','domain-client','model-config','native-session','extensions/biomind','product-ui','usage-observer']:
        relevant += ['integrations/pi_runtime/'+name+'.ts','integrations/pi_runtime/dist/'+name+'.js']
    files={name:{'sha256':sha(ROOT/name),'size':(ROOT/name).stat().st_size,'mtime':(ROOT/name).stat().st_mtime} for name in relevant}
    patch=json.loads((ROOT/'integrations/pi_runtime/compat/pi-0.87.1-observer.patch.json').read_text())
    installed=ROOT/'integrations/pi_runtime/node_modules/@earendil-works/pi-coding-agent'
    patched=[]
    for row in patch['files']:
        p=installed/row['path']; actual=sha(p)
        patched.append({'path':row['path'],'sha256':actual,'expected':row.get('patchedSha256'),'matches':actual==row.get('patchedSha256')})
    for rel in ['dist/core/tools/bash.js','dist/utils/shell.js']:
        files['integrations/pi_runtime/node_modules/@earendil-works/pi-coding-agent/'+rel]={'sha256':sha(installed/rel),'size':(installed/rel).stat().st_size}
    initial=Path('/tmp/biomind_audit_v2_initial_status.txt').read_text()
    tracked=[s for s in initial.splitlines() if not s.startswith('??')]
    untracked=Counter('/'.join(s[3:].split('/')[:2]) for s in initial.splitlines() if s.startswith('??'))
    save('baseline.json',{'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'branch':subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip(),
        'initial_status_sha256':hashlib.sha256(initial.encode()).hexdigest(),'initial_entries':len(initial.splitlines()),
        'initial_staged':0,'tracked_dirty':tracked,'untracked_group_counts':dict(untracked),
        'launcher':str(Path('/public/home/liusiqi/.local/bin/biomind').resolve()), 'files':files,
        'pi_version':json.loads((installed/'package.json').read_text())['version'],
        'patch_label':patch['runtimeLabel'],'patch_checks':patched,
        'historical_limit':'disk hashes establish the audit-time runtime, not byte identity of historical running processes'})

if __name__ == '__main__': extract()
