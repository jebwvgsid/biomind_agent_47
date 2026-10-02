"""One-off authorized metadata migration. Default preview; never runs R or science.
--apply writes through Domain; --rollback RECEIPT restores only unchanged owned rows.
"""
import argparse,asyncio,hashlib,importlib.abc,json,os,sys,tempfile
from pathlib import Path
REPO=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(REPO),str(REPO/'integrations/pi_runtime')]
PREFIX=Path('/public/home/liusiqi/anaconda3/envs/rna_env')
SESSION_ID='01a0f68c-7b84-70ca-bfcd-d234aa37b81b'
REQUEST_ID='db4f89eb-08be-4259-ad79-95411ce778f5'
CALL_ID='call_00_m6qtZ57BdGHtcCCP9gOm9784'

def authority():
    # The existing launcher reads only allowlisted non-secret .env assignments;
    # this projection never reads .env.api_keys or mutates os.environ.
    from agent_core.pi_model_config import project_launch_environment
    from domain_adapter.registry import registry_path
    values,source=project_launch_environment(os.environ,repo=REPO,project=REPO.parent)
    return registry_path(REPO,environment={**os.environ,**values}),source

class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='config' or fullname.startswith('config.') or fullname in {'bio_agents','agent_core.installer','agent_core.provider_attempt','dotenv'}:
            raise ImportError('SUPPLEMENT_FORBIDS_RUNTIME_IMPORT:'+fullname)

def observed():
    audit=json.loads((REPO/'docs/audits/pi_unified_environment_20261001/case_evidence.json').read_text())['ORA']
    session=Path(audit['session_path']);data=session.read_bytes()
    if hashlib.sha256(data).hexdigest()!=audit['session_sha256']:raise ValueError('HISTORICAL_SESSION_CHANGED')
    active=None;call=None;receipt=None
    for raw in data.decode().splitlines():
        row=json.loads(raw)
        if row.get('type')=='custom' and row.get('customType')=='biomind.request':
            active=row.get('data',{}).get('requestId')
        msg=row.get('message',{})
        for part in msg.get('content',[]) if isinstance(msg.get('content'),list) else []:
            if part.get('type')=='toolCall' and part.get('id')==CALL_ID:
                if active!=REQUEST_ID:raise ValueError('HISTORICAL_REQUEST_MISMATCH')
                call=part
        if msg.get('role')=='toolResult' and msg.get('toolCallId')==CALL_ID:
            if active!=REQUEST_ID or msg.get('isError'):raise ValueError('HISTORICAL_RECEIPT_MISMATCH')
            receipt=row
    if not call or not receipt:raise ValueError('HISTORICAL_RECEIPT_MISSING')
    command=call['arguments']['command']
    if 'for e in biomind rna_env eggnog_env dmr_r calder_env' not in command or 'p=/public/home/liusiqi/anaconda3/envs/$e' not in command:
        raise ValueError('HISTORICAL_TARGET_UNBOUND')
    text='\n'.join(v.get('text','') for v in receipt['message']['content'])
    section=text.split('== rna_env ==\n',1)[1].split('== ',1)[0]
    if 'Rscript (R) version 4.5.3' not in section or 'fgsea: TRUE' not in section:raise ValueError('HISTORICAL_CAPABILITY_UNBOUND')
    entry=PREFIX/'bin/Rscript'
    if not PREFIX.is_dir() or not entry.is_file() or not os.access(entry,os.X_OK):raise ValueError('EXACT_ENTRY_METADATA_UNAVAILABLE')
    return {'source_ref':f'pi:{SESSION_ID}:{receipt["id"]}','session_id':SESSION_ID,'request_id':REQUEST_ID,
            'observed_at':receipt['timestamp'],'checks':['tool:Rscript','package:R','package:fgsea'],
            'recorded_output':section.strip(),'session_sha256':audit['session_sha256'],
            'strength_boundary':'historical tool output; pipeline exit does not independently prove each command; no current probe'}

def related(raw):return {'environment':raw.get('environments',{}).get('rna_env'),'tool':raw.get('tools',{}).get('Rscript')}

def rollback(receipt):
    from package_manager import PackageManager
    payload=json.loads(Path(receipt).read_text());path,_=authority()
    if str(path)!=payload['authority']:raise ValueError('AUTHORITY_CHANGED')
    pm=PackageManager.for_registry(path)
    def reverse(data):
        if related(data)!=payload['after']:raise ValueError('ROLLBACK_CONFLICT_LATER_REGISTRATION_CHANGE')
        for section,key,field in [('environments','rna_env','environment'),('tools','Rscript','tool')]:
            before=payload['before'][field]
            if before is None:data[section].pop(key,None)
            else:data[section][key]=before
    pm._update_registry(reverse)
    return pm.last_registry_receipt

async def main(args):
    path,source=authority();sys.meta_path.insert(0,Guard())
    if args.rollback:return rollback(args.rollback)
    observation=observed();raw=json.loads(path.read_text());before=related(raw)
    if before['environment'] is not None or before['tool'] is not None:
        raise ValueError('RELATED_RECORD_ALREADY_EXISTS_REVIEW_REQUIRED')
    params={'call_id':'rna-supplement-20261002','env_name':'rna_env','prefix':str(PREFIX),'tools':['Rscript'],
            'entries':{'Rscript':str(PREFIX/'bin/Rscript')},'packages':['R','fgsea'],
            'capabilities':['tool:Rscript','r-package:fgsea'],'language':'R','versions':{'R':'4.5.3'},
            'source_note':'existing software; historical receipt only','observation':observation}
    result={'authority':str(path),'authority_source':source,'before':before,'preview':params,
            'real_software_execution':'NOT_RUN','R_fgsea_recheck':'NOT_RUN','ORA_TAD_reproduce':'NOT_RUN'}
    if not args.apply:return result
    from domain_adapter.service import DomainService
    from agent_core.execution_profile import release_host_profile
    with tempfile.TemporaryDirectory(prefix='biomind-rna-metadata-') as td:
        root=Path(td);prefs=root/'prefs.md';prefs.write_text('metadata migration')
        service=DomainService(cwd=root,data_root=root/'data',install_root=REPO,session_id='rna-metadata-migration-20261002',
            registry_path=path,preferences_path=prefs,skills_root=root/'skills',execution_profile='native')
        await service.call('request.begin',{'request_id':'rna-supplement-metadata','role':'consult'},host_token=service.host_token)
        result['receipt']=await service.call('environments.register_observed',params,host_token=service.host_token)
        release_host_profile(service)
    after_raw=json.loads(path.read_text());result['after']=related(after_raw)
    for section,key in [('environments','rna_env'),('tools','Rscript')]:
        expected={k:v for k,v in raw.get(section,{}).items() if k!=key}
        actual={k:v for k,v in after_raw.get(section,{}).items() if k!=key}
        if expected!=actual:raise ValueError('UNRELATED_RECORDS_CHANGED')
    result['unrelated_records_unchanged']=True
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--rollback');parser.add_argument('--output',type=Path)
    args=parser.parse_args();data=asyncio.run(main(args));body=json.dumps(data,indent=2,ensure_ascii=False)+'\n'
    if args.output:args.output.write_text(body)
    else:print(body)
