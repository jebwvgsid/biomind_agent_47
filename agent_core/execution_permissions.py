"""Permission transitions inside the existing Runtime/PlanTracker state.

No model-callable decision API, separate database, scheduler or result protocol.
The sealed input/dispatch objects prevent accidental source forgery in the app;
they are NOT a security boundary against arbitrary same-UID Python or shell.
"""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time

_POLICY_REVISION = 'execution-permissions-v1'
_SEAL = object()
_dispatch = ContextVar('biomind_permission_dispatch', default=None)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


from agent_core.human_io import HumanInputUnavailable


class PermissionPending(HumanInputUnavailable):
    def __init__(self, request):
        self.request = request
        super().__init__('AWAITING_EXECUTION_APPROVAL')


class PermissionDenied(ValueError):
    pass


@dataclass(frozen=True)
class _Decision:
    request_id: str
    action_hash: str
    choice: str
    seal: object
    replacement: str = ""


@dataclass(frozen=True)
class ExecutionPermit:
    data_path: str
    task_id: str
    request_id: str
    action_hash: str
    seal: object


def _scope(state):
    from agent_core.request_state import request_scope
    return request_scope(state)


def _permissions(state):
    return state.setdefault('task_state', {}).setdefault('execution_permissions',
            {'policy_revision': _POLICY_REVISION, 'requests': {}, 'grants': {}})


def _active(state, row):
    from agent_core.request_state import current_finish
    return (not state.get('load_error') and _scope(state) == row['request_scope']
            and (state.get('task_state') or {}).get('status') != 'cancelled'
            and not current_finish(state)
            and _permissions(state)['policy_revision'] == row['policy_revision'] == _POLICY_REVISION
            and (row.get('source')!='rule' or
                 _grant_covers(_permissions(state)['grants'].get(row.get('grant_id'),{}),row['payload'])))


def _mutate(data_path, task_id, delta):
    # This is the existing narrow CAS retry; immutable event/object files and
    # plan_state.json are committed by the existing StateContractStore path.
    from agent_core.pending_input import mutate
    return mutate(data_path, task_id, delta)


def command_payload(service, arguments, task_id):
    from agent_core.plan_tracker import load_plan_state
    from agent_core.action_runtime import freeze_execution_environment, registered_execution_environment
    import os
    state = load_plan_state(service.data_path, task_id)
    project = Path(service.project_for_task(task_id)).resolve()
    cwd = Path(arguments.get('workdir') or project).expanduser()
    cwd = (project / cwd).resolve(strict=True) if not cwd.is_absolute() else cwd.resolve(strict=True)
    if not cwd.is_dir(): raise ValueError('EXECUTION_CWD_NOT_DIRECTORY')
    env = freeze_execution_environment(os.environ, registered_execution_environment(state, arguments.get('env') or {}))
    # Environment values remain in controlled execution evidence, never UI or
    # reviewer projection. Neither Plan prose nor DB generation is hashed.
    actual = {k:deepcopy(v) for k,v in arguments.items() if k not in {'timeout_ms','max_output_chars','workdir'}}
    sources={}
    selected=actual.get('script_path') or (actual.get('legacy_action') or {}).get('script_path')
    if selected:
        source=(cwd/selected).resolve(strict=True)
        sources[str(source)]=hashlib.sha256(source.read_bytes()).hexdigest()
        for helper in actual.get('helper_paths') or []:
            path=(source.parent/helper).resolve(strict=True)
            if not path.is_relative_to(source.parent): raise PermissionDenied('HELPER_OUTSIDE_SCRIPT_LAYOUT')
            sources[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
    return {'kind':'command', 'arguments':actual, 'cwd':str(cwd), 'source_hashes':sources,
            'environment_hash':digest(env), 'human_required':True}


def request_permission(data_path, task_id, call_id, payload):
    from agent_core.state_contract_store import StateContractStore
    def update(state):
        book = _permissions(state)
        scope = _scope(state)
        key = 'permission-' + digest([scope, call_id])[:32]
        action_hash = digest(payload)
        row = book['requests'].get(key)
        if row:
            if (row['action_hash'] != action_hash and not (payload.get('kind')=='installation'
                    and row.get('original_action_hash')==action_hash)):
                raise PermissionDenied('PERMISSION_ACTION_CHANGED')
            if not _active(state, row) and row['status'] in {'pending','allowed'}:
                row['status'] = 'cancelled'
            return deepcopy(row)
        row = {'request_id':key, 'tool_call_id':str(call_id), 'request_scope':scope,
               'action_hash':action_hash, 'original_action_hash':action_hash, 'payload':deepcopy(payload),
               'policy_revision':book['policy_revision'], 'status':'pending',
               'allowed_decisions':['y','n'], 'created_at':time.time()}
        if payload.get('kind')=='file_write':
            row['allowed_decisions'].append('scope')
            for grant_id,grant in book['grants'].items():
                if (grant['request_scope']==scope and grant['policy_revision']==book['policy_revision']
                        and _grant_covers(grant,payload)):
                    row.update(status='allowed',source='rule',grant_id=grant_id)
                    break
        # A new call ID is not a way to erase an explicit denial of this action.
        if any(r['action_hash']==action_hash and r['request_scope']==scope and r['status']=='denied'
               for r in book['requests'].values()): row['status']='denied'
        row['payload_ref'] = StateContractStore(data_path,task_id).put_object('permission_action', payload)
        if not _active(state,row): row['status']='cancelled'
        book['requests'][key] = row
        return deepcopy(row)
    return _mutate(data_path, task_id, update)


def permission_state(data_path, task_id, request_id):
    from agent_core.plan_tracker import load_plan_state
    state = load_plan_state(data_path,task_id)
    row = deepcopy(_permissions(state)['requests'][request_id])
    if not _active(state,row) and row['status'] in {'pending','allowed'}:
        row['status']='cancelled'
    return row


def record_human_decision(data_path, task_id, decision):
    if not isinstance(decision,_Decision) or decision.seal is not _SEAL:
        raise PermissionDenied('UNTRUSTED_PERMISSION_DECISION')
    def update(state):
        row = _permissions(state)['requests'][decision.request_id]
        if row['action_hash'] != decision.action_hash: raise PermissionDenied('STALE_PERMISSION_REPLY')
        if not _active(state,row):
            if row['status'] in {'pending','allowed'}: row['status']='cancelled'
            return deepcopy(row)
        if row['status'] != 'pending': return deepcopy(row)
        if decision.choice == 'edit' and row['payload']['kind']=='installation':
            if not isinstance(decision.replacement,str) or not decision.replacement.strip(): return deepcopy(row)
            from agent_core.state_contract_store import StateContractStore
            row['payload']['code']=decision.replacement
            row['action_hash']=digest(row['payload'])
            row['payload_ref']=StateContractStore(data_path,task_id).put_object('permission_action',row['payload'])
            return deepcopy(row)
        if decision.choice not in row['allowed_decisions']: raise PermissionDenied('DECISION_NOT_OFFERED')
        if decision.choice=='scope':
            candidate=row['payload'].get('scope_candidate')
            if row['payload']['kind']!='file_write' or not candidate:
                raise PermissionDenied('SCOPE_NOT_ENFORCEABLE')
            grant={**deepcopy(candidate),'source':'human','request_scope':row['request_scope'],
                   'policy_revision':row['policy_revision']}
            _permissions(state)['grants'][row['request_id']]=grant
        row.update(status='allowed' if decision.choice in {'y','scope'} else 'denied',
                   source='human', decided_at=time.time())
        return deepcopy(row)
    return _mutate(data_path,task_id,update)


def cancel_permission(data_path,task_id,request_id):
    def update(state):
        row = _permissions(state)['requests'][request_id]
        if row['status'] in {'pending','allowed'} and not row.get('claim'):
            row['status']='cancelled'
        return deepcopy(row)
    return _mutate(data_path,task_id,update)


async def read_human_decision(task_id, row):
    from agent_core.cli_display import input_terminal
    from agent_core.cli_tool_activity import clean
    owner = input_terminal()
    if owner is None:
        raise HumanInputUnavailable('EXECUTION_APPROVAL_REQUIRES_HOST_OWNER')
    payload = {'type':'code_review','action_id':row['request_id'],
               'approval_id':row['request_id'], 'action_hash':row['action_hash'],
               'content':clean(json.dumps(row['payload'],ensure_ascii=False)),
               'options':row['allowed_decisions']}
    owner.set_context(task_id,'code_review',payload)
    from agent_core.cli_review import review_display
    owner.prepare_review(review_display('等待执行批准',payload['content']))
    while True:
        text = await owner.aread(('y=批准本次，n=拒绝'+('，scope=授权上述目录的文件工具写入' if 'scope' in row['allowed_decisions'] else '')+'；空白/编辑不是批准 > '),timeout=0)
        if row['payload']['kind']=='installation':
            from agent_core.middleware import HITLMiddleware
            action,value=HITLMiddleware(False)._parse_code_response(text,row['payload']['code'])
            if action=='edit': return _Decision(row['request_id'],row['action_hash'],'edit',_SEAL,value)
            if action=='approve': text='y'
            elif action in {'reject','quit'}: text='n'
            else: continue
        if not isinstance(text,str): continue
        choice=text.strip().lower()
        if choice in row['allowed_decisions']:
            return _Decision(row['request_id'],row['action_hash'],choice,_SEAL)


async def await_permission(data_path,task_id,row,*,cancel_event=None,reviewer=None):
    from agent_core.human_io import HumanInputUnavailable
    from agent_core.cli_tool_activity import runtime_event
    reader = None
    review_task = (asyncio.create_task(reviewer.review(data_path,task_id,deepcopy(row)))
                   if reviewer is not None and reviewer.enabled else None)
    if row['status']=='pending':
        runtime_event('waiting_approval',{'action_id':row['request_id'],'execution_started':False})
    try:
        while row['status']=='pending':
            if cancel_event is not None and cancel_event.is_set():
                cancel_permission(data_path,task_id,row['request_id'])
                raise asyncio.CancelledError()
            if reader is None: reader=asyncio.create_task(read_human_decision(task_id,row))
            # Host observation only. No Generate/reviewer call, tool dispatch,
            # attempt allocation or DB write lock is held during this await.
            done,_=await asyncio.wait({reader},timeout=.1)
            if done:
                try: decision=reader.result()
                except HumanInputUnavailable:
                    if review_task is not None:
                        await review_task
                        row=permission_state(data_path,task_id,row['request_id'])
                        if row['status']!='pending': break
                    from agent_core.human_io import save_waiting_state
                    save_waiting_state(data_path,task_id,stage='code_review',prompt='等待执行批准',
                        payload={'approval_id':row['request_id']},reason='input_unavailable')
                    raise PermissionPending(row) from None
                row=record_human_decision(data_path,task_id,decision)
                reader=None
            else:
                row=permission_state(data_path,task_id,row['request_id'])
        if row['status']=='cancelled': raise asyncio.CancelledError()
        if row['status']!='allowed': raise PermissionDenied('EXECUTION_PERMISSION_DENIED')
        return row
    finally:
        if review_task is not None:
            if not review_task.done(): review_task.cancel()
            await asyncio.wait({review_task},timeout=.1)
            def consume(task):
                if not task.cancelled(): task.exception()
            if review_task.done(): consume(review_task)
            else: review_task.add_done_callback(consume)
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader,return_exceptions=True)


def claim_permission(data_path,task_id,row,claim_id):
    def update(state):
        live=_permissions(state)['requests'][row['request_id']]
        if (not _active(state,live) or live['status']!='allowed'
                or live['action_hash']!=row['action_hash']):
            raise PermissionDenied('PERMISSION_NOT_CURRENT')
        if live['payload'].get('human_required') and live.get('source')!='human':
            raise PermissionDenied('HUMAN_PERMISSION_REQUIRED')
        if live.get('claim'):
            raise PermissionDenied('PERMISSION_ALREADY_CLAIMED_RECONCILE')
        live['claim']=str(claim_id)
        return ExecutionPermit(str(data_path),str(task_id),live['request_id'],live['action_hash'],_SEAL)
    return _mutate(data_path,task_id,update)


def validate_permit(permit):
    if not isinstance(permit,ExecutionPermit) or permit.seal is not _SEAL:
        raise PermissionDenied('EXECUTION_PERMIT_REQUIRED')
    live=permission_state(permit.data_path,permit.task_id,permit.request_id)
    if live['status']!='allowed' or not live.get('claim') or live['action_hash']!=permit.action_hash:
        raise PermissionDenied('EXECUTION_PERMIT_REVOKED')
    if live['payload'].get('human_required') and live.get('source')!='human':
        raise PermissionDenied('HUMAN_PERMISSION_REQUIRED')
    return live


async def authorize_command(service,arguments,*,task_id,call_id,cancel_event=None):
    from agent_core.plan_tracker import load_plan_state
    state=load_plan_state(service.data_path,task_id)
    admission=(state.get('task_state') or {}).get('intent_admission') or {}
    review=(state.get('task_state') or {}).get('result_review') or {}
    if admission.get('request_kind')!='execute' or review.get('request_scope')==_scope(state):
        raise PermissionDenied('CURRENT_REQUEST_FORBIDS_EXECUTION')
    payload=command_payload(service,arguments,task_id)
    row=request_permission(service.data_path,task_id,call_id,payload)
    row=await await_permission(service.data_path,task_id,row,cancel_event=cancel_event)
    # Detect cwd/environment/argument drift during an arbitrarily long wait.
    if digest(command_payload(service,arguments,task_id))!=row['action_hash']:
        raise PermissionDenied('PERMISSION_ACTION_CHANGED')
    return row


@contextmanager
def execution_context(permit,command,*,cwd=None,env=None):
    validate_permit(permit)
    token=_dispatch.set((permit,digest(command),str(cwd) if cwd is not None else None,
                          digest(env) if env is not None else None))
    try: yield
    finally: _dispatch.reset(token)


def require_dispatch(command,*,cwd=None,env=None):
    context=_dispatch.get()
    if context is None: raise PermissionDenied('EXECUTION_PERMIT_REQUIRED')
    permit,expected,expected_cwd,expected_env=context
    if (digest(command)!=expected or (expected_cwd is not None and str(cwd)!=expected_cwd)
            or (expected_env is not None and digest(env)!=expected_env)):
        raise PermissionDenied('FINAL_EXECUTION_PAYLOAD_CHANGED')
    validate_permit(permit)


def pending_result(exc):
    return {'status':'waiting_approval','terminal':False,'execution_started':False,
            'approval_id':exc.request['request_id'],'tool_call_id':exc.request['tool_call_id'],
            'next_actions':['resume_same_request'],'retryable_in_current_scope':False}

@contextmanager
def script_dispatch_context(command):
    context=_dispatch.get()
    if context is None: raise PermissionDenied('EXECUTION_PERMIT_REQUIRED')
    with execution_context(context[0],command): yield


def complete_permission(permit,result):
    # A cancellation after effects must not erase their receipt. This records
    # an already claimed observation; it never authorizes another execution.
    if not isinstance(permit,ExecutionPermit) or permit.seal is not _SEAL:
        raise PermissionDenied('EXECUTION_PERMIT_REQUIRED')
    def update(state):
        row=_permissions(state)['requests'][permit.request_id]
        if not row.get('claim') or row['action_hash']!=permit.action_hash:
            raise PermissionDenied('RECEIPT_PERMISSION_MISMATCH')
        if 'result' not in row: row['result']=deepcopy(result)
        return deepcopy(row['result'])
    return _mutate(permit.data_path,permit.task_id,update)


def file_payload(files,name,args,task_id):
    project,path,relative=files._path(args.get('path'),task_id)
    # _parent later uses dirfd + O_NOFOLLOW; scope anchors additionally prevent
    # a renamed/replaced grant root from acquiring the previous grant.
    root=path.parent
    while not root.exists() and root!=project: root=root.parent
    if root.is_symlink() or project.is_symlink(): raise PermissionDenied('SYMLINK_PERMISSION_ROOT')
    if root.resolve()!=root.absolute(): raise PermissionDenied('SYMLINK_PERMISSION_ROOT')
    info=root.stat()
    return {'kind':'file_write','tool':name,'path':str(path),'arguments':deepcopy(args),
            'scope_candidate':{'root':str(root),'anchor':[info.st_dev,info.st_ino],
                               'project':str(project),'operation':'file_write'},
            'human_required':False}


def _grant_covers(grant,payload):
    if payload.get('kind')!='file_write' or grant.get('operation')!='file_write': return False
    root=Path(grant['root']);path=Path(payload['path'])
    try:
        info=root.stat()
        return (not root.is_symlink() and root.resolve()==root.absolute()
                and [info.st_dev,info.st_ino]==grant['anchor']
                and path.is_relative_to(root) and path!=root
                and grant['project']==payload['scope_candidate']['project'])
    except OSError: return False


async def authorize_file(files,name,args,*,task_id,call_id):
    payload=file_payload(files,name,args,task_id)
    row=request_permission(files.commands.data_path,task_id,call_id,payload)
    row=await await_permission(files.commands.data_path,task_id,row,
        reviewer=getattr(files.commands,'permission_reviewer',None))
    if digest(file_payload(files,name,args,task_id))!=row['action_hash']:
        raise PermissionDenied('FILE_PERMISSION_SCOPE_CHANGED')
    return row


def revoke_grants(data_path,task_id):
    """Host control API; never registered as an LLM tool."""
    def update(state):
        book=_permissions(state)
        book['grants'].clear()
        for row in book['requests'].values():
            if row.get('source')=='rule' and row['status']=='allowed' and not row.get('claim'):
                row['status']='cancelled'
    return _mutate(data_path,task_id,update)


@dataclass
class PermissionReviewer:
    """Retired model-review configuration, retained as an abstaining adapter.

    Pi owns model work; only the existing explicit human/rule permission paths
    may grant execution.  Loading an old reviewer configuration has no effect:
    review never calls a model, records a decision, or changes request state.
    """
    model: object = None
    enabled: bool = False
    delegated_kinds: tuple = ()
    user_authorization: str = ''
    timeout_seconds: float = 10.0

    async def review(self, data_path, task_id, row):
        return None


_file_dispatch = ContextVar('biomind_file_permission', default=None)

@contextmanager
def file_dispatch_context(permit):
    row=validate_permit(permit)
    boundary=row['payload']['scope_candidate']
    if row.get('source')=='rule':
        from agent_core.plan_tracker import load_plan_state
        boundary=_permissions(load_plan_state(permit.data_path,permit.task_id))['grants'][row['grant_id']]
    token=_file_dispatch.set((Path(boundary['root']),boundary['anchor']))
    try: yield
    finally: _file_dispatch.reset(token)


def verify_file_directory(path,fd):
    """Check the OPEN descriptor at the authorized root, closing rename races."""
    import os
    boundary=_file_dispatch.get()
    if boundary and Path(path)==boundary[0]:
        info=os.fstat(fd)
        if [info.st_dev,info.st_ino]!=boundary[1]:
            raise PermissionDenied('FILE_PERMISSION_ROOT_REPLACED')


def may_create_file_directory(path):
    boundary=_file_dispatch.get()
    return boundary is None or (Path(path)!=boundary[0] and Path(path).is_relative_to(boundary[0]))
