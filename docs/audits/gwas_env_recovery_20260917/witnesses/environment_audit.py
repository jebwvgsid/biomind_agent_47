"""Frozen-source environment audit; no application startup, API, or installation.

Only AST-selected BioAnalyst methods execute. PackageManager is constructed with
__new__ and a COPY of its registry. Its validation uses persist=False. The real
runner executes an immutable audit-only script and an existing bcftools query.
The original session, registry, inputs and environments are never written.
"""
from __future__ import annotations
import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import types
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO = Path('/public/home/liusiqi/03.毕设/Biomind_v1')
CURRENT = Path('/public/home/liusiqi/03.毕设/biomind_audit_exports/gwas_env_recovery_20260917/current_source')
BASE = Path('/public/home/liusiqi/03.毕设/biomind_audit_exports/environment_binding_unified_20260917/prerequisite_source')
AUDIT = REPO / 'docs/audits/gwas_env_recovery_20260917'
REGISTRY = Path('/public/home/liusiqi/03.毕设/agent_shim/software_registry.json')
SKILLREG = REPO / 'skills/.skill_runtime_registry.json'
CONDA = Path('/public/home/liusiqi/anaconda3')
DATA = Path('/public/home/liusiqi/03.毕设/benchmarks/01.测试/04.重测序/Part6.GWAS/data')
SESSIONS = Path('/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs')
sys.dont_write_bytecode = True
sys.path.insert(0, str(CURRENT))

def guard(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'os.system'}:
        raise PermissionError('AUDIT_NETWORK_OR_OS_SYSTEM_DENIED')
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0]))
        if path.name.startswith('.env'):
            raise PermissionError('AUDIT_DOTENV_DENIED')
        mode, flags = args[1] or '', args[2] or 0
        writing = any(c in str(mode) for c in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
        if writing and not (str(path.absolute()).startswith('/tmp/') or str(path.absolute()).startswith(str(AUDIT)) or str(path) == '/dev/null'):
            raise PermissionError('AUDIT_WRITE_SCOPE_DENIED:' + str(path))
sys.addaudithook(guard)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def file_fact(p):
    p = Path(p)
    return {'path':str(p), 'sha256':sha(p.read_bytes()), 'size':p.stat().st_size}

SOURCES = {}
TREES = {}
def tree(root, filename):
    path = root / filename
    if str(path) in TREES:
        return TREES[str(path)]
    raw = path.read_text()
    SOURCES[str(path)] = file_fact(path)
    TREES[str(path)] = ast.parse(raw), raw
    return TREES[str(path)]

def node(root, filename, name):
    parsed, raw = tree(root, filename)
    found = [n for n in ast.walk(parsed) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name]
    assert len(found) == 1, (filename, name, len(found))
    n = found[0]
    return n, '\n'.join(raw.splitlines()[n.lineno-1:n.end_lineno])

def ast_function(root, filename, name, namespace):
    n, text = node(root, filename, name)
    n = deepcopy(n)
    n.decorator_list = []
    exec(compile(ast.fix_missing_locations(ast.Module(body=[n], type_ignores=[])), str(root/filename), 'exec'), namespace)
    return namespace[name]

def compare_sources():
    results = []
    names = {'bio_agents.py':['_runtime_audit_for_skill','_consultant_runtime_seed_evidence','_consultant_runtime_preflight','_verify_conda_env_presence','_runtime_binding_source','_reuse_registered_runtime_for_install'],
             'package_manager.py':['find_matching_environment'],
             'agent_core/skill_manager.py':['get_runtime_env']}
    for filename, methods in names.items():
        for name in methods:
            old, a = node(BASE, filename, name); new, b = node(CURRENT, filename, name)
            results.append({'file':filename, 'function':name, 'baseline_line':old.lineno, 'current_line':new.lineno,
                            'identical':a == b, 'baseline_method_sha256':sha(a.encode()), 'current_method_sha256':sha(b.encode())})
    for root in (BASE, CURRENT):
        n, text = node(root, 'bio_agents.py', 'install_subagent')
        results.append({'file':str(root/'bio_agents.py'), 'function':'install_subagent', 'line':n.lineno,
                        'contains_find_matching_environment':'find_matching_environment(' in text,
                        'contains_registered_reuse_call':'_reuse_registered_runtime_for_install(' in text,
                        'delegates_to_scoped_impl':'_install_subagent_impl(' in text})
    return results

def branch_witnesses():
    rows=[]
    for root in (BASE, CURRENT):
        ns=dict(globals())
        fn=ast_function(root,'bio_agents.py','_runtime_audit_for_skill',ns)
        for use_cache in (False,True):
            for detail in ('Conda environment `declared` is not available under the configured root.', 'tool entry exists but cannot start', 'version constraint not satisfied'):
                calls=[]
                pm=types.SimpleNamespace(conda_root='/tmp/audit-nonexistent-root',
                    get_environment=lambda env:{'status':'registered'},
                    find_matching_environment=lambda req:(calls.append('find_matching_environment') or {'env':'candidate','record':{'status':'verified'},'match_type':'partial'}),
                    verify_environment=lambda **kw:(calls.append('verify_candidate') or (True,'candidate minimal check passed')))
                owner=types.SimpleNamespace(pm=pm, skill_manager=types.SimpleNamespace(get_runtime_record=lambda sid:{}), runtime_status={})
                owner._skill_runtime_requirements=lambda sid:{'env':'declared','packages':['bcftools'],'healthcheck':'bcftools --version'}
                owner._runtime_healthcheck_for_env=lambda req,env:f'conda run -n {env} bcftools --version'
                owner._registered_runtime_tools=lambda req:[]
                owner._consultant_runtime_code_hint=lambda sid:''
                owner._verify_skill_runtime=lambda *a,**kw:(False,detail,'declared')
                owner._runtime_cache_key=lambda sid,env:f'{sid}:{env}'
                owner._verified_runtime_paths=lambda detail:{}
                owner._cache_runtime_status=lambda *a,**kw:None
                result=fn(owner,'fixture/gwas','audit',use_cache=use_cache)
                assert bool(calls)==use_cache
                rows.append({'snapshot':str(root),'use_cache':use_cache,'declared_failure':detail,'calls':calls,
                             'decision':result['decision'],'status':result['status'],'reason_code':result['reason_code'],
                             'candidate':result['matching_env_candidate'],'substitutions':'declared and candidate healthcheck leaves; real audit/finalization method'})
    return rows

def phase_witness():
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
    from agent_core.workflow.scoped_context import build_provider_messages, fixed_provider_message, provider_visible_payload, provider_visible_fingerprint, ScopedContextError
    ns={**globals(), **locals(), 'MAX_PHASE_SEED_CONTRACT_CHARS':32000,
        'assert_v2_llm_messages_clean':lambda *a,**kw:None}
    n,_=node(CURRENT,'agent_core/workflow/phase_subgraph.py','PhaseProviderContext')
    keep={'__init__','note_provider_visible_materials','queue_provider_event','prepare_messages','split_prompt','prepare_split_prompt'}
    clone=deepcopy(n);clone.body=[m for m in clone.body if isinstance(m,ast.FunctionDef) and m.name in keep]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[clone],type_ignores=[])),str(CURRENT/'agent_core/workflow/phase_subgraph.py'),'exec'),ns)
    cls=ns['PhaseProviderContext']
    ctx=cls(phase_name='consultant',invocation_id='audit',epoch_ordinal=1,provider_seed_contract={},system_message=None,seed_message=None,messages=[])
    first=ctx.prepare_messages('Fixed policy','<controller_feedback>{}</controller_feedback>')
    ctx.system_message=first[0];ctx.seed_message=first[1]
    ctx.messages=[AIMessage(id='audit:e1:assistant:1',content='select_skill'),HumanMessage(id='audit:e1:observation:1',content='Skill selection accepted')]
    new_fact='RUNTIME_HEALTHCHECK_FAILED: candidate bcftools available in bio_gatk; association unverified'
    second=ctx.prepare_messages('Fixed policy','<controller_feedback>'+new_fact+'</controller_feedback>')
    assert all(new_fact not in str(m.content) for m in second)
    ctx.queue_provider_event(event_id='audit-runtime-observation:1',content=new_fact,name='runtime_observation')
    ctx.queue_provider_event(event_id='audit-runtime-observation:1',content=new_fact,name='runtime_observation')
    third=ctx.prepare_messages('Fixed policy','ignored rewritten seed')
    assert sum(new_fact in str(m.content) for m in third)==1
    return {'actual_production_methods':sorted(keep),'prefix_identical':first[1].content==second[1].content,
            'runtime_fact_in_second_request':False,'existing_event_api_delivers_fact_once':True,
            'substitution':'payload-safety check omitted; real LangChain messages and production scoped-context message builders; no provider called',
            'request2':[{'role':m.type,'content':m.content} for m in second],
            'request3':[{'role':m.type,'content':m.content} for m in third]}

def historical_evidence():
    sid=SESSIONS/'session_1789648505'
    requests=[]
    terms=['runtime_checks','RUNTIME_HEALTHCHECK_FAILED','healthcheck_failed','registered_tools','matching_env_candidate','software_registry','popgen_gwas']
    for p in sorted((sid/'context_trace').glob('consultant-*/request.json')):
        d=json.loads(p.read_text());msgs=d.get('messages') or []
        payload='\n'.join(str(m.get('content','')) for m in msgs)
        requests.append({**file_fact(p),'message_count':len(msgs),'terms':{term:payload.count(term) for term in terms},
                         'messages':[{'role':m.get('role') or m.get('type'),'chars':len(str(m.get('content',''))),'content_sha256':sha(str(m.get('content','')).encode()),
                                      'short_observation':m.get('content') if len(str(m.get('content','')))<500 else ''} for m in msgs]})
    attempt=sid/'attempts/attempt_79e2ff04f7dcfa103d11'
    script=(attempt/'approved.sh').read_text()
    d=json.loads((sid/'plan_state.json').read_text())
    records=(d.get('task_state',{}).get('runtime_evidence') or {}).get('records',[])
    return {'consultant_requests':requests,'runtime_records':[{'scope':x.get('scope'),'inspection':x.get('inspection'),'facts':x.get('facts'),'resolved_runtime_paths':x.get('resolved_runtime_paths')} for x in records],
            'attempt':{p.name:file_fact(p) for p in attempt.iterdir() if p.name in {'approved.sh','executed.sh','runtime_env_manifest.json'}},
            'runtime_manifest':json.loads((attempt/'runtime_env_manifest.json').read_text()),
            'script_bcftools_lines':[{'line':i,'text':l} for i,l in enumerate(script.splitlines(),1) if any(w in l for w in ['bcftools','vcf.samples','command -v','PATH='])]}

def import_production():
    pkg=types.ModuleType('execution');pkg.__path__=[str(CURRENT/'execution')];sys.modules['execution']=pkg
    config=types.ModuleType('config'); config.config=types.SimpleNamespace(BIO_SOFTWARE_ROOT='',BIO_SOURCE_INSTALL_DIR='',ANACONDA_ROOT='')
    sys.modules['config']=config
    return importlib.import_module('package_manager').PackageManager

def inventory(pm, registry):
    # Bounded formal index: only registered environment names, configured root,
    # registered tool shim/source paths; no recursive filesystem search.
    tools=['bcftools','plink','plink2','emmax','emmax-intel64','emmax-kin-intel64','Rscript','bedtools','LDBlockShow','ShowLDSVG','java','python']
    rows=[]
    for env,record in registry.get('environments',{}).items():
        prefix=CONDA/'envs'/env
        hits=[]
        for tool in tools:
            p=prefix/'bin'/tool
            if p.exists():hits.append({'tool':tool,'path':str(p),'realpath':str(p.resolve()),'is_file':p.is_file(),'executable':os.access(p,os.X_OK)})
        rows.append({'environment':env,'status':record.get('status'),'prefix':str(prefix),'prefix_exists':prefix.is_dir(),
                     'language':record.get('language'),'packages':record.get('packages'),'skill_ids':record.get('skill_ids'),
                     'last_verified_at':record.get('last_verified_at'),'tool_candidates':hits})
    return {'registry':file_fact(REGISTRY),'skill_registry':file_fact(SKILLREG),'environments':rows,
            'registered_tools':{name:value for name,value in registry.get('tools',{}).items() if name in tools},
            'bounded_scope':'registered environment names × exact bin/tool names; no absence claim outside index',
            'bcftools_match':pm.find_matching_environment({'language':'bash','packages':['bcftools'],'capabilities':[]})}

async def real_process(pm, inv):
    from agent_core.action_runtime import generic_execution_capability, resolve_action_runtime_bindings, build_exact_action_environment, stable_digest, build_action_approval_identity, validate_action_approval_identity
    from execution.executor import run_script_path
    env_name=inv['bcftools_match']['env'];tool=CONDA/'envs'/env_name/'bin/bcftools'
    assert tool.is_file() and os.access(tool,os.X_OK)
    before=REGISTRY.read_bytes()
    ok,detail=pm.verify_environment(env_name=env_name,language='bash',healthcheck=f'{shlex.quote(str(tool))} --version',persist=False)
    assert REGISTRY.read_bytes()==before
    if not ok:return {'verified':False,'environment':env_name,'detail':detail,'farthest':'candidate validation'}
    contract=generic_execution_capability()
    resolution=resolve_action_runtime_bindings(selected_skill_id=contract['selected_skill_id'],execution_capability_id=contract['capability_id'],
        approved_capability_contract_hash='',capability_registry={contract['capability_id']:contract},ready_runtime_binding_registry={},
        current_action_identity={'action_id':'audit-bcftools','plan_id':'audit-only','step_id':'sample-list'})
    assert resolution['runtime_binding_manifest']['bindings']==[]
    with tempfile.TemporaryDirectory(prefix='biomind-gwas-environment-') as tmp:
        process_env=build_exact_action_environment({'PATH':'/usr/bin:/bin','LC_ALL':'C','TMPDIR':tmp},action_environment={})
        vcf=DATA/'Real_snp.vcf.gz'
        code='#!/bin/bash\nset -euo pipefail\ncd '+shlex.quote(tmp)+'\nexec '+shlex.quote(str(tool))+' query -l '+shlex.quote(str(vcf))+'\n'
        script=Path(tmp)/'approved.sh';script.write_text(code); digest=sha(script.read_bytes())
        identity=build_action_approval_identity(generated_script_sha256=digest,capability_contract_hash_value=resolution['capability_contract_hash'],
            runtime_env_manifest_hash=resolution['runtime_binding_manifest_hash'],artifact_env_manifest_hash=stable_digest({'input':str(vcf)}),
            execution_policy_manifest_hash=stable_digest({'env':process_env}),working_directory=tmp)
        validate_action_approval_identity(identity,deepcopy(identity))
        stdout,stderr,rc=await run_script_path(script,expected_sha256=digest,interpreter='/bin/bash',timeout=20,profile=False,env=process_env,max_output_bytes=16384)
        samples=stdout.splitlines();assert rc==0 and len(samples)>0,(rc,stderr)
        mutated=dict(identity,generated_script_sha256='changed')
        try:validate_action_approval_identity(identity,mutated);raise AssertionError('drift accepted')
        except ValueError as exc:drift=str(exc)
        return {'verified':True,'environment':env_name,'tool':file_fact(tool),'version_output':detail,'query_command':code,
                'runtime_binding_manifest':resolution['runtime_binding_manifest'],'approval_identity':identity,
                'approval_scope':'synthetic audit approval identity, not original user approval; no UI/provider/Commit path exercised',
                'actual_runner':'execution.executor.run_script_path','process_env':process_env,'returncode':rc,'stderr':stderr,
                'sample_count':len(samples),'samples':samples,'sample_list_sha256':sha(stdout.encode()),'approval_drift_rejected':drift,
                'registry_unchanged':REGISTRY.read_bytes()==before,'input':file_fact(vcf),
                'boundary':'existing PM candidate+readonly version -> explicit immutable generic action -> approval hash validation -> real runner/query; automatic Consultant discovery remains broken; not complete GWAS'}

async def main():
    report={'captured_utc':datetime.now(timezone.utc).isoformat(),'interpreter':sys.executable,'witness':file_fact(__file__)}
    report['source_comparison']=compare_sources()
    report['audit_branch_witnesses']=branch_witnesses()
    report['phase_wire_witness']=phase_witness()
    report['historical']=historical_evidence()
    PM=import_production();pm=PM.__new__(PM);pm.conda_root=str(CONDA);pm.conda_exe=str(CONDA/'bin/conda')
    registry=json.loads(REGISTRY.read_text());pm.registry=deepcopy(registry)
    report['inventory']=inventory(pm,registry)
    report['real_process']=await real_process(pm,report['inventory'])
    report['source_files']=list(SOURCES.values())
    report['imported_frozen_modules']={n:file_fact(m.__file__) for n,m in list(sys.modules.items()) if getattr(m,'__file__',None) and str(m.__file__).startswith(str(CURRENT))}
    out=AUDIT/'evidence/environment_results.json';out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'result':str(out),'branch_witnesses':len(report['audit_branch_witnesses']),'wire_witness':'PASS_CURRENT_DEFECT_AND_EXISTING_EVENT_API',
                      'real_process':{k:report['real_process'].get(k) for k in ['verified','environment','returncode','sample_count','sample_list_sha256']}}))

if __name__=='__main__':asyncio.run(main())
