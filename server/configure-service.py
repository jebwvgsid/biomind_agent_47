#!/usr/bin/env python3
"""Configure the desktop service from bounded stdin; never open API credentials."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
import uuid

HERE=Path(__file__).resolve().parent


class Refused(Exception):
    pass


def require(condition,code):
    if not condition:
        raise Refused(code)


def absolute(value,kind=None):
    require(isinstance(value,str) and value.startswith('/') and '\x00' not in value and len(value)<=4096,'INVALID_ABSOLUTE_PATH')
    path=Path(value)
    require('..' not in path.parts and path!=Path('/'),'INVALID_ABSOLUTE_PATH')
    path=path.resolve(strict=kind is not None)
    if kind=='file':require(path.is_file(),'REQUIRED_FILE_MISSING')
    if kind=='dir':require(path.is_dir(),'REQUIRED_DIRECTORY_MISSING')
    if kind=='exe':require(path.is_file() and os.access(path,os.X_OK),'REQUIRED_EXECUTABLE_MISSING')
    return path


def private(path,directory=False):
    info=path.lstat()
    require(not stat.S_ISLNK(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode & 0o077,'PRIVATE_DIRECTORY_OR_FILE_REQUIRED')
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),'PRIVATE_DIRECTORY_OR_FILE_REQUIRED')


def atomic(path,value):
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as target:
            target.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
            target.flush();os.fsync(target.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_nonsecret_json(value):
    # The user explicitly selects this non-secret JSON. Never open .env files,
    # credential files, or a symlink whose resolved target has such a name.
    require(isinstance(value,str),'INVALID_RUNTIME_CONFIG')
    requested=Path(value)
    require(not any(part.lower().startswith('.env') for part in requested.parts),'SENSITIVE_RUNTIME_PATH_REFUSED')
    target=absolute(value,'file')
    require(not any(part.lower().startswith('.env') for part in target.parts),'SENSITIVE_RUNTIME_PATH_REFUSED')
    require(target.suffix.lower()=='.json' and requested.suffix.lower()=='.json','RUNTIME_JSON_REQUIRED')
    require(not re.search(r'(?:auth|credential|api[-_]?keys?|secret)',target.name,re.I)
            and not re.search(r'(?:auth|credential|api[-_]?keys?|secret)',requested.name,re.I),'SENSITIVE_RUNTIME_PATH_REFUSED')
    with target.open('rb') as source:
        raw=source.read(65537)
    require(len(raw)<=65536,'RUNTIME_CONFIG_TOO_LARGE')
    return target,json.loads(raw)


def runtime_metadata(value):
    target,value=read_nonsecret_json(value)
    allowed={'python','agentDir','dataRoot','installRoot','provider','model','thinkingLevel','providerFixture',
        'registryPath','preferencesPath','memoryRoot','wikiRoot','historyEnabled','archivePath','memoryMode','sessionMode',
        'session','continueSession','sessionDir','handshakeTimeoutMs','executionProfile'}
    require(isinstance(value,dict) and set(value)<=allowed,'RUNTIME_CONFIG_NOT_NONSECRET_METADATA')
    # Only resolve the directory. Never read session, memory, skill or credential
    # files referenced by the runtime metadata, and never modify any of them.
    if 'sessionDir' in value:
        session_root=absolute(value['sessionDir'],'dir')
        scope='explicit_session_dir'
    else:
        require(isinstance(value.get('agentDir'),str),'RUNTIME_AGENT_DIR_REQUIRED_FOR_NATIVE_SESSIONS')
        agent_dir=absolute(value['agentDir'],'dir')
        session_root=absolute(str(agent_dir/'sessions'),'dir')
        scope='native_project_directories'
    return target,session_root,scope,value


def model_record_metadata(filename,runtime):
    target,value=read_nonsecret_json(filename)
    fields={'schema','provider','model','api','thinking','endpoint_hostname','origin','confirmed_active'}
    require(isinstance(value,dict) and value.get('schema') in ('biomind-active-model-config-v1','biomind-active-model-config-v2'),
            'INVALID_MODEL_RECORD')
    if value['schema'].endswith('-v2'):fields.add('endpoint_base_url')
    require(set(value)==fields and value['confirmed_active'] is True,'MODEL_RECORD_NOT_NONSECRET_METADATA')
    require(all(isinstance(value[key],str) and 0<len(value[key])<=2048 and not any(c in value[key] for c in '\r\n\x00')
                for key in fields-{'confirmed_active'}),'INVALID_MODEL_RECORD')
    require(value['provider']==runtime.get('provider') and value['model']==runtime.get('model') and
            value['thinking']==runtime.get('thinkingLevel'),'RUNTIME_MODEL_RECORD_MISMATCH')
    # Main owns supported provider validation and model registration. Reject URL
    # credentials here before a record can become a launch argument.
    if 'endpoint_base_url' in value:
        from urllib.parse import urlsplit
        url=urlsplit(value['endpoint_base_url'])
        require(url.scheme=='https' and url.hostname==value['endpoint_hostname'] and
                not url.username and not url.password and not url.query and not url.fragment,'INVALID_MODEL_RECORD_ENDPOINT')
    return target


def configure(config_path,setup):
    require(isinstance(setup,dict) and setup.get('schema')=='BioMindStudioConfigureV1','INVALID_SETUP_SCHEMA')
    allowed={'schema','backendRoot','mainRoot','runtimeConfig','sessionRoot','pythonPath','nodePath','projectRoots','dataRoots',
             'uploadsRoot','mode','slurm','workbenchPassword','serviceId','serverId','modelRecordPath'}
    require(set(setup)<=allowed,'UNKNOWN_SETUP_FIELD')
    password=setup.get('workbenchPassword')
    require(isinstance(password,str) and 16<=len(password.encode())<=4096 and not any(c in password for c in '\r\n\x00'),'INVALID_WORKBENCH_PASSWORD')
    supplied=Path(str(config_path))
    require(supplied.is_absolute() and supplied.suffix=='.json' and not supplied.is_symlink(),'INVALID_CONFIG_PATH')
    require(not supplied.parent.is_symlink(),'CONFIG_DIRECTORY_SYMLINK_REFUSED')
    target=absolute(str(supplied))
    parent=target.parent
    parent.mkdir(mode=0o700,parents=True,exist_ok=True);private(parent,True)
    lock_fd=os.open(parent/'configure.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        private(parent/'configure.lock')
        try:fcntl.flock(lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Refused('CONFIGURATION_BUSY')
        return configure_locked(target,setup,password)
    finally:
        os.close(lock_fd)


def configure_locked(target,setup,password):
    existing=None
    if target.exists():
        private(target);existing=json.loads(target.read_text())
        require(existing.get('schema')=='BioMindStudioServiceV1','EXISTING_CONFIGURATION_UNKNOWN')
    backend=absolute(setup.get('backendRoot'),'dir')
    main=absolute(setup.get('mainRoot'),'dir')
    runtime,runtime_session_root,session_scope,runtime_value=runtime_metadata(setup.get('runtimeConfig'))
    session_root=absolute(setup.get('sessionRoot'),'dir')
    require(session_root==runtime_session_root,'RUNTIME_SESSION_ROOT_MISMATCH')
    model_record=model_record_metadata(setup['modelRecordPath'],runtime_value) if setup.get('modelRecordPath') else None
    python=absolute(setup.get('pythonPath'),'exe')
    node=absolute(setup.get('nodePath'),'exe')
    for filename in ('bio_agents.py','integrations/pi_runtime/dist/host.js','integrations/pi_runtime/dist/history-query.js',
                     'integrations/pi_runtime/dist/instructions-query.js','integrations/pi_runtime/testing/posix-pipe-relay.py'):
        require((main/filename).is_file(),'COMPLETE_BIOMIND_MAIN_REQUIRED')
    for filename in ('package.json','lib/biomind-session-adapter.ts','.next-pkg/BUILD_ID',
                     '.next-pkg/server/app/api/agent/profile/route.js','.next-pkg/server/app/api/agent/new/route.js',
                     '.next-pkg/server/app/api/agent/[id]/route.js','.next-pkg/server/app/api/agent/[id]/events/route.js'):
        require((backend/filename).is_file(),'WORKBENCH_BACKEND_BUILD_REQUIRED')
    # Check the delivered adapter source, not arbitrary credential/config data.
    source=(backend/'lib/biomind-session-adapter.ts').read_bytes()
    require(b'liveSnapshot()' in source and b'displayPendingUI' in source,'WORKBENCH_BACKEND_PATCH_REQUIRED')
    require((HERE/'next-service.mjs').is_file() and (HERE/'server-service.py').is_file(),'SERVICE_SCRIPTS_REQUIRED')
    roots={}
    for key in ('projectRoots','dataRoots'):
        values=setup.get(key)
        require(isinstance(values,list) and 0<len(values)<=128,'AUTHORIZED_ROOTS_REQUIRED')
        roots[key]=list(dict.fromkeys(str(absolute(value,'dir')) for value in values))
    uploads=absolute(setup.get('uploadsRoot'),'dir')
    require(any(uploads==Path(root) or Path(root) in uploads.parents for root in roots['projectRoots']+roots['dataRoots']),'UPLOAD_ROOT_OUTSIDE_AUTHORIZED_ROOTS')
    mode=setup.get('mode')
    require(mode in ('local','slurm'),'INVALID_SERVICE_MODE')
    service_id=setup.get('serviceId') or (existing or {}).get('serviceId') or 'biomind-studio'
    server_id=setup.get('serverId') or (existing or {}).get('serverId') or 'biomind-'+uuid.uuid4().hex
    require(isinstance(service_id,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,48}',service_id),'INVALID_SERVICE_ID')
    require(isinstance(server_id,str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',server_id),'INVALID_SERVER_ID')
    if existing:
        require(existing.get('serviceId')==service_id and existing.get('serverId')==server_id,'CONFIGURATION_IDENTITY_CHANGE_REFUSED')
    slurm=setup.get('slurm',{})
    require(isinstance(slurm,dict) and set(slurm)<={'partition','time','cpus','memory','account','qos','constraint'},'INVALID_SLURM_OPTIONS')
    for key in ('partition','account','qos','constraint'):
        require(key not in slurm or isinstance(slurm[key],str) and re.fullmatch(r'[A-Za-z0-9_.,:+-]{1,128}',slurm[key]),'INVALID_SLURM_OPTIONS')
    require('time' not in slurm or isinstance(slurm['time'],str) and re.fullmatch(r'[0-9:-]{1,32}',slurm['time']),'INVALID_SLURM_OPTIONS')
    require('cpus' not in slurm or type(slurm['cpus']) is int and 1<=slurm['cpus']<=512,'INVALID_SLURM_OPTIONS')
    require('memory' not in slurm or re.fullmatch(r'[1-9][0-9]*[KMGTP]?',str(slurm['memory']),re.I),'INVALID_SLURM_OPTIONS')
    state_root=target.parent/'state'
    environment_file=target.parent/'runtime-environment.json'
    if existing:
        require(existing.get('stateRoot')==str(state_root) and existing.get('environmentFile')==str(environment_file),'CONFIGURATION_LAYOUT_CHANGE_REFUSED')
    rt=main/'integrations/pi_runtime'
    config=dict(schema='BioMindStudioServiceV1',serviceId=service_id,serverId=server_id,mode=mode,
        stateRoot=str(state_root),cwd=str(backend),command=[str(node),str(HERE/'next-service.mjs')],
        startupTimeoutSeconds=180,environmentFile=str(environment_file),environment={
            'BIOMIND_STUDIO_ROOT':str(backend),'NODE_ENV':'production','PI_WEB_DIST_DIR':'.next-pkg',
            'BIOMIND_LAUNCHER_PYTHON':str(python),'BIOMIND_ENTRYPOINT':str(main/'bio_agents.py'),
            'BIOMIND_PIPE_LAUNCHER':str(rt/'testing/posix-pipe-relay.py'),'BIOMIND_RUNTIME_CONFIG':str(runtime),
            'BIOMIND_PI_NODE':str(node),'BIOMIND_PI_PYTHON':str(python),
            'BIOMIND_PROJECT_ROOTS_JSON':json.dumps(roots['projectRoots'],ensure_ascii=False),
            'BIOMIND_DATA_ROOTS_JSON':json.dumps(roots['dataRoots'],ensure_ascii=False),'PI_WEB_UPLOADS_DIR':str(uploads),
            'BIOMIND_HISTORY_NODE':str(node),'BIOMIND_HISTORY_QUERY_SCRIPT':str(rt/'dist/history-query.js'),
            'BIOMIND_SESSION_ROOT':str(session_root),'BIOMIND_INSTRUCTIONS_QUERY_SCRIPT':str(rt/'dist/instructions-query.js'),
            'BIOMIND_MAX_OWNERS':'8'},slurm=slurm)
    if model_record:config['environment']['BIOMIND_PI_MODEL_RECORD']=str(model_record)
    secret={'PI_WEB_PASSWORD':password}
    current_secret=None
    if environment_file.exists():
        private(environment_file)
        current_secret=json.loads(environment_file.read_text())
        require(isinstance(current_secret,dict) and set(current_secret)=={'PI_WEB_PASSWORD'},'WORKBENCH_PASSWORD_FILE_REQUIRED')
    if existing==config and current_secret==secret:
        return dict(status='configured',configPath=str(target),serverId=server_id,serviceId=service_id,reused=True,
                    apiCredentialsRead=False,modelStarted=False)
    state_root.mkdir(mode=0o700,exist_ok=True);private(state_root,True)
    service_lock=os.open(state_root/'service.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    lease_lock=os.open(state_root/'backend.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        private(state_root/'service.lock');private(state_root/'backend.lock')
        try:
            fcntl.flock(service_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(lease_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Refused('SERVICE_ACTIVE_CONFIGURATION_LOCKED')
        record_file=state_root/'service.json'
        if record_file.exists():
            private(record_file);record=json.loads(record_file.read_text())
            require(record.get('status') in ('stopped','failed'),'SERVICE_ACTIVE_CONFIGURATION_LOCKED')
        # Keep private rollback copies, never print their contents. Both paths
        # are dedicated workbench files; no API credential files are opened.
        suffix='.backup-'+str(time.time_ns())
        for path in (target,environment_file):
            if path.exists():
                private(path)
                shutil.copyfile(path,path.with_name(path.name+suffix))
                path.with_name(path.name+suffix).chmod(0o600)
        try:
            atomic(environment_file,secret)
            atomic(target,config)
        except BaseException:
            if current_secret is None:environment_file.unlink(missing_ok=True)
            else:atomic(environment_file,current_secret)
            raise
        # Retire only the verified stopped lifecycle record. Native sessions and
        # memory are untouched; ensure can now bind a fresh holder to the config.
        if record_file.exists():
            record_file.replace(record_file.with_name(record_file.name+suffix))
    finally:
        os.close(lease_lock);os.close(service_lock)
    return dict(status='configured',configPath=str(target),serverId=server_id,serviceId=service_id,reused=False,
                apiCredentialsRead=False,modelStarted=False,backendAdapterSha256=hashlib.sha256(source).hexdigest(),
                sessionScope=session_scope,
                checks={'paths':'PASS','runtimeSessionScope':'PASS','modelRecordMetadata':'PASS' if model_record else 'NOT_SET',
                        'model':'NOT_RUN','network':'NOT_RUN','windows':'NOT_RUN'})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    args=parser.parse_args()
    try:
        raw=sys.stdin.buffer.read(65537)
        require(len(raw)<=65536,'SETUP_INPUT_TOO_LARGE')
        result=configure(args.config,json.loads(raw))
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return 0
    except Refused as error:
        print(json.dumps(dict(status='error',code=str(error),apiCredentialsRead=False,modelStarted=False)),flush=True)
        return 2
    except (OSError,ValueError,TypeError,KeyError):
        print(json.dumps(dict(status='error',code='CONFIGURATION_FAILED',apiCredentialsRead=False,modelStarted=False)),flush=True)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
