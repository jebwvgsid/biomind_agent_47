"""Create or verify NEW synthetic history for the actual Pi host P16.d test.

This helper never restores a session. It seeds original stores using a tiny real
formal command, then exposes archive read parameters and hashes to the host test.
stdout is one JSON manifest; incidental domain diagnostics go to stderr.
"""
import argparse
import asyncio
from contextlib import redirect_stdout
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import uuid

from test_python_domain import DomainService,REPO
from memory.session_archive import SessionArchive


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def archive_row(db,event_id):
    with sqlite3.connect(db) as conn:
        conn.row_factory=sqlite3.Row
        row=conn.execute('SELECT * FROM session_events WHERE id=?',(event_id,)).fetchone()
        return dict(row) if row else None


def row_digest(row):
    return hashlib.sha256(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def verify(manifest):
    root=Path(manifest['fixture_root']).resolve()
    checks=[]
    for item in manifest['protected_files']:
        path=Path(item['path']).resolve()
        if not path.is_relative_to(root) or path.name.startswith('.env'):
            raise ValueError('fixture verification path escaped fresh root')
        actual=digest(path) if path.is_file() else None
        checks.append({'path':str(path),'expected_sha256':item['sha256'],'actual_sha256':actual,'unchanged':actual==item['sha256']})
    db=Path(manifest['configOverrides']['archivePath']).resolve()
    if not db.is_relative_to(root):raise ValueError('archive path escaped fixture root')
    row=archive_row(db,manifest['protected_archive_row']['id'])
    actual=row_digest(row)
    row_unchanged=actual==manifest['protected_archive_row']['sha256']
    with sqlite3.connect(db) as conn:
        total=conn.execute('SELECT COUNT(*) FROM session_events').fetchone()[0]
        old_task_rows=conn.execute('SELECT COUNT(*) FROM session_events WHERE task_id=?',(manifest['old_task_id'],)).fetchone()[0]
    return {'status':'UNCHANGED' if row_unchanged and all(item['unchanged'] for item in checks) else 'CHANGED',
        'protected_files':checks,'historical_row_unchanged':row_unchanged,
        'historical_row_actual_sha256':actual,'archive_total_rows':total,'historical_task_rows':old_task_rows,
        'new_audit_rows_allowed':True,'launch_count':(root/'seed-execution-count').read_text()}


async def seed(cwd):
    cwd=Path(cwd).resolve()
    if not cwd.is_dir() or not cwd.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError('--cwd must be an existing NEW temporary scientific project under the system temp root')
    root=cwd/'p16d-history-fixture';root.mkdir(exist_ok=False)
    (root/'skills').mkdir()
    (root/'registry.json').write_text('{"environments":{},"tools":{}}')
    (root/'preferences.md').write_text('Synthetic P16.d fixture: read historical evidence without resuming old work.\n')
    config={'dataRoot':str(root/'domain'),'memoryRoot':str(root/'memory'),'wikiRoot':str(root/'wiki'),
        'archivePath':str(root/'session_archive.sqlite3'),'registryPath':str(root/'registry.json'),
        'skillsRoot':str(root/'skills'),'preferencesPath':str(root/'preferences.md'),
        'historyEnabled':True,'memoryMode':'disabled'}
    common={'cwd':cwd,'data_root':config['dataRoot'],'install_root':REPO,
        'memory_root':config['memoryRoot'],'wiki_root':config['wikiRoot'],'archive_path':config['archivePath'],
        'registry_path':config['registryPath'],'skills_root':config['skillsRoot'],
        'preferences_path':config['preferencesPath'],'history_enabled':True,'memory_mode':'disabled'}
    session='synthetic-historical-'+uuid.uuid4().hex
    service=DomainService(session_id=session,**common)
    request=await service.call('request.begin',{'request_id':'synthetic-historical-request','role':'generate'},host_token=service.host_token)
    output=root/'historical-result.tsv';count=root/'seed-execution-count'
    code=('from pathlib import Path;'
        f'p=Path({str(count)!r});p.write_text(p.read_text()+"x" if p.exists() else "x");'
        f'Path({str(output)!r}).write_text("sample\\tvalue\\nfixture\\t17\\n");'
        'print("P16D_HISTORICAL_SENTINEL synthetic physical output; science not assessed")')
    args={'call_id':'synthetic-historical-command','arguments':{'argv':[sys.executable,'-I','-u','-c',code],
        'workdir':str(cwd),'description':'Create only a fresh synthetic historical fixture'}}
    pending=await service.call('exec.start',args)
    assert pending['status']=='waiting_approval',pending
    approval=pending['approval']
    await service.call('approval.decide',{**{k:approval[k] for k in ('request_id','action_hash','request_scope')},
        'choice':'y'},host_token=service.host_token)
    result=await service.call('exec.start',args)
    for _ in range(100):
        if result.get('terminal'):break
        result=await service.call('exec.wait',{'run_ref':result['run_ref'],'max_wait_ms':100})
    assert result.get('terminal') and result.get('exit_code')==0,result
    assert count.read_text()=='x'
    project=service.context_snapshot()['project_id']
    archive=SessionArchive(config['archivePath'])
    content=('P16D_HISTORICAL_SENTINEL: synthetic prior-session fact. The historical stdlib job '
        'wrote sample=fixture, value=17 and exited 0. Scientific validation was not assessed. '
        'This receipt describes the old task only; it is not evidence of a new execution. '
        f'Historical output: {output}. Permission request: {approval["request_id"]}. '
        f'Formal receipt: {result["receipt_ref"]}.')
    event_id=archive.append_event(task_id=service.task_id,session_id=session,project_id=project,
        stage='generate',event_type='tool_result',content=content,summary=content,
        metadata={'synthetic_fixture':True,'run_ref':result['run_ref'],
            'permission_request_id':approval['request_id'],'receipt_ref':result['receipt_ref']},
        paths=[str(output),result['receipt_ref']],returncode=0,
        source_refs=[f'task:{service.task_id}'],tags=['synthetic','P16.d'])
    await service.disconnected()
    task_root=Path(config['dataRoot'])/'task_outputs'/service.task_id
    files=sorted(set([output,count,*[p for p in task_root.rglob('*') if p.is_file()]]))
    assert any(p.name=='plan_state.json' for p in files),'missing original permission state'
    assert any(p.name=='exit_status.json' for p in files),'missing original executor receipt'
    manifest={'status':'READY_FIXTURE','case':'P16.d','fixture_root':str(root),'scientific_cwd':str(cwd),
        'configOverrides':config,'old_task_id':service.task_id,'old_session_id':session,'project_id':project,
        'old_request_scope':request['request_scope'],'old_run_ref':result['run_ref'],
        'old_permission_request_id':approval['request_id'],'historical_output':str(output),
        'protected_files':[{'path':str(p),'sha256':digest(p)} for p in files],
        'protected_archive_row':{'id':event_id,'sha256':row_digest(archive_row(config['archivePath'],event_id)),
            'canonicalization':'UTF-8 JSON full row; ensure_ascii=False, sort_keys=True, separators=(comma,colon)'},
        'search_params':{'query':'P16D_HISTORICAL_SENTINEL','source':'archive','scope':'current'},
        'read_params':{'source_ref':'session_archive:'+str(event_id),'scope':'current'},
        'expected_content':content,'history_policy':'History reads enabled; Wiki writes/maintenance disabled. Explicit archive audit writes remain enabled.',
        'scope_policy':'Current project may read other tasks/sessions in the same scientific cwd; foreign project requires explicit scope.',
        'product_host_assertion':'NOT_RUN by this helper; use actual Pi memory_search and memory_read, then verify protected old state.'}
    # A new synthetic domain identity validates the exact read contract only.
    # The root test must still invoke these through the actual Pi extension.
    reader=DomainService(session_id='fixture-read-probe-'+uuid.uuid4().hex,**common)
    await reader.call('request.begin',{'request_id':'new-read-only-request','role':'consult'},host_token=reader.host_token)
    found=await reader.call('memory.search',manifest['search_params'])
    read=await reader.call('memory.read',manifest['read_params'])
    assert reader.task_id!=service.task_id and found['status']=='found' and read['status']=='read',(found,read)
    assert read['task_id']==service.task_id and 'P16D_HISTORICAL_SENTINEL' in read['content']
    assert not reader.active_jobs()
    await reader.disconnected()
    manifest['new_identity_direct_read_probe']={'current_task_id':reader.task_id,'search':found,'read':read,'current_jobs':0}
    manifest['initial_verification']=verify(manifest)
    assert manifest['initial_verification']['status']=='UNCHANGED'
    manifest['manifest_path']=str(root/'manifest.json')
    Path(manifest['manifest_path']).write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    return manifest


def main():
    parser=argparse.ArgumentParser();group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--cwd');group.add_argument('--verify')
    args=parser.parse_args()
    with redirect_stdout(sys.stderr):
        result=verify(json.loads(Path(args.verify).read_text())) if args.verify else asyncio.run(seed(args.cwd))
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
