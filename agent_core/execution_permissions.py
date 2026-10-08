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

_POLICY_REVISION = 'execution-permissions-v2'
_READ_POLICY_REVISION = 'external-input-read-v1'
_PATH_POLICY_REVISION = 'data-path-access-v1'
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
    selections: object = None


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
    book = state.setdefault('task_state', {}).setdefault('execution_permissions',
            {'policy_revision': _POLICY_REVISION, 'requests': {}, 'grants': {}})
    if (not isinstance(book,dict) or book.get('policy_revision') not in {_POLICY_REVISION,'execution-permissions-v1'}
            or not isinstance(book.get('requests'),dict) or not isinstance(book.get('grants'),dict)):
        raise PermissionDenied('PERMISSION_BOOK_INVALID_OR_UNKNOWN_VERSION')
    return book


def permission_owner(service, task_id, state):
    return {'task_id':str(task_id), 'session_id':str(state.get('task_state',{}).get('pi_session_id') or ''),
            'project':str(service.project_for_task(task_id)), 'storage':str(Path(service.data_path).resolve())}


def bind_permission_owner(service, task_id):
    from agent_core.plan_tracker import load_plan_state
    current=load_plan_state(service.data_path,task_id);existing=_permissions(current).get('owner')
    if existing:
        if existing!=permission_owner(service,task_id,current):raise PermissionDenied('PERMISSION_STORAGE_OWNER_MISMATCH')
        return existing
    def update(state):
        owner=permission_owner(service,task_id,state);book=_permissions(state)
        if not owner['session_id']:raise PermissionDenied('REQUEST_BEGIN_REQUIRED')
        if book.get('owner') not in (None,owner):raise PermissionDenied('PERMISSION_STORAGE_OWNER_MISMATCH')
        book['owner']=owner
        return owner
    return _mutate(service.data_path,task_id,update)


def _lifetime_current(state, value):
    if value.get('lifetime','request') in {'request','current_request'}:
        from agent_core.request_state import current_finish
        return (value.get('request_scope')==_scope(state) and not current_finish(state)
                and state.get('task_state',{}).get('status')!='cancelled')
    if value.get('lifetime')!='session':return False
    owner=value.get('owner');task=state.get('task_state',{})
    return bool(owner and owner==_permissions(state).get('owner')
                and owner.get('session_id')==task.get('pi_session_id') and owner.get('task_id')==state.get('task_id'))


def _path_target_valid(target):
    """Pin an existing anchor; walk canonical components without following links."""
    import stat
    try:
        root=Path(target['root']);anchor=Path(target.get('anchor_path',target['root']))
        info=_safe_read_info(anchor)
        if [info.st_dev,info.st_ino]!=target['anchor']:return False
        if root.resolve()!=root.absolute() or not root.is_relative_to(anchor):return False
        current=anchor
        for part in root.relative_to(anchor).parts:
            current=current/part
            try:info=_safe_read_info(current)
            except FileNotFoundError:
                return (target.get('access')=='read_write' and not target.get('existed_at_proposal')
                        and not target.get('target_anchor'))
        if target.get('target_kind')=='directory' and not stat.S_ISDIR(info.st_mode):return False
        if target.get('target_kind')=='file' and not stat.S_ISREG(info.st_mode):return False
        return not target.get('target_anchor') or [info.st_dev,info.st_ino]==target['target_anchor']
    except (OSError,ValueError,KeyError,RuntimeError):return False


def path_target(service, task_id, proposal):
    """Metadata-only proposal; no mkdir, no business reads, no authority fields."""
    import os,stat
    from agent_core.command_execution import check_data_path
    if (not isinstance(proposal,dict) or set(proposal)-{'path','access','target_kind','recursive'}
            or proposal.get('access') not in {'read','read_write'}):
        raise PermissionDenied('PATH_PROPOSAL_FIELDS_INVALID')
    value=proposal.get('path')
    if not isinstance(value,str) or not value.strip() or '\x00' in value:raise PermissionDenied('FILE_PATH_REQUIRED')
    project=Path(service.project_for_task(task_id));requested=Path(os.path.abspath(project/Path(value).expanduser()))
    target=requested.resolve();access=proposal['access']
    check_data_path(service,task_id,requested,write=access=='read_write')
    if target in {Path('/'),Path('/home'),Path('/public'),Path('/public/home'),Path('/tmp'),Path.home().resolve()}:
        raise PermissionDenied('PATH_SCOPE_TOO_BROAD')
    existing=target.exists();kind=proposal.get('target_kind') or ('directory' if target.is_dir() else 'file')
    if kind not in {'file','directory'}:raise PermissionDenied('PATH_TARGET_KIND_INVALID')
    if not existing and access!='read_write':raise FileNotFoundError(str(target))
    info=_safe_read_info(target) if existing else None
    if info and (stat.S_ISDIR(info.st_mode)!=(kind=='directory')):raise PermissionDenied('PATH_TARGET_KIND_CHANGED')
    recursive=proposal.get('recursive',kind=='directory')
    if type(recursive) is not bool or kind=='file' and recursive:raise PermissionDenied('PATH_RECURSION_INVALID')
    anchor=target if existing and kind=='directory' or existing and access=='read' else target.parent
    while not anchor.exists():anchor=anchor.parent
    ai=_safe_read_info(anchor)
    return {'root':str(target),'requested_path':str(requested),'access':access,'target_kind':kind,
            'recursive':recursive,'anchor_path':str(anchor),'anchor':[ai.st_dev,ai.st_ino],
            'target_anchor':[info.st_dev,info.st_ino] if info and kind=='file' else None,
            'existed_at_proposal':existing}


def _path_grant_live(state, grant):
    book=_permissions(state);origin=book['requests'].get(grant.get('approval_id'),{})
    return bool(grant.get('operation')=='path_access' and grant.get('path_policy_revision')==_PATH_POLICY_REVISION
        and not grant.get('revoked') and grant.get('source')=='human' and origin.get('source')=='human'
        and origin.get('decided_at') and grant.get('owner')==book.get('owner')
        and grant.get('owner',{}).get('session_id')==state.get('task_state',{}).get('pi_session_id')
        and grant.get('policy_revision')==book['policy_revision'] and _lifetime_current(state,grant)
        and _path_target_valid(grant))


def _path_covers(grant, path, access='read'):
    target=Path(path);root=Path(grant['root'])
    return ((access=='read' or grant.get('access')=='read_write')
            and (target==root or grant.get('target_kind')=='directory' and grant.get('recursive') is True and target.is_relative_to(root)))


def path_grants(service, task_id, path, access='read', *, lifetime='request'):
    from agent_core.plan_tracker import load_plan_state
    state=load_plan_state(service.data_path,task_id);owner=permission_owner(service,task_id,state);book=_permissions(state)
    if book.get('owner') not in (None,owner):raise PermissionDenied('PERMISSION_STORAGE_OWNER_MISMATCH')
    target=Path(path).resolve()
    payload={'kind':'file_read','path':str(target),'project':owner['project'],'session_id':owner['session_id']}
    return [{**deepcopy(g),'grant_id':key} for key,g in book['grants'].items()
            if _path_grant_live(state,g) and _path_covers(g,target,access)
            and not _read_explicitly_denied(state,payload,after_revision=g.get('resume_revision',0))
            and (lifetime!='session' or g.get('lifetime')=='session')]


def directory_candidates(service, task_id, targets, *, promote_files=False):
    """Offer only the named directory or a file's direct parent, never a guessed ancestor."""
    project=Path(service.project_for_task(task_id));result={}
    for target in targets:
        root=Path(target['root'])
        if target['target_kind']=='file':
            if not promote_files:return []
            root=root.parent
        # A broad ancestor of this project is not an ordinary default suggestion.
        if root!=project and project.is_relative_to(root):return []
        try:
            candidate=path_target(service,task_id,{'path':str(root),'access':target['access'],
                'target_kind':'directory','recursive':True})
        except (PermissionDenied,FileNotFoundError):return []
        key=str(root)
        if key not in result or candidate['access']=='read_write':result[key]=candidate
    return list(result.values())


def path_access_payload(service, task_id, paths, *, purpose='', lifetime='request', suggest_directory=False):
    if lifetime not in {'request','session'} or not isinstance(purpose,str) or len(purpose)>2000:
        raise PermissionDenied('PATH_PROPOSAL_FIELDS_INVALID')
    if not isinstance(paths,list) or not 1<=len(paths)<=16:raise PermissionDenied('PATH_PROPOSAL_COUNT_INVALID')
    owner=bind_permission_owner(service,task_id)
    targets=[path_target(service,task_id,p) for p in paths]
    return {'kind':'path_access','targets':targets,'project':owner['project'],'owner':owner,
            'directory_targets':directory_candidates(service,task_id,targets,promote_files=suggest_directory),
            'purpose':purpose,'suggested_lifetime':lifetime,'human_required':True,
            'path_policy_revision':_PATH_POLICY_REVISION}


def prepare_path_access(service, task_id, paths, *, call_id, purpose='', lifetime='request', suggest_directory=False):
    payload=path_access_payload(service,task_id,paths,purpose=purpose,lifetime=lifetime,suggest_directory=suggest_directory)
    missing=[];grants=[]
    for target in payload['targets']:
        covering=path_grants(service,task_id,target['root'],target['access'],lifetime=lifetime)
        if covering:grants.append(covering[0])
        else:missing.append(target)
    if missing:
        # Keep the full proposed set/hash stable across partial decisions.
        identity=derive_permission_call_id(call_id,'path_access',digest(payload['targets']))
        row=request_permission(service.data_path,task_id,identity,payload,original_call_id=call_id)
        if row['status']=='pending':raise PermissionPending(row)
        if row['status']!='allowed':raise PermissionDenied('PATH_PERMISSION_'+row['status'].upper())
        # A host may have granted read only or a subset. Never silently upgrade it.
        raise PermissionDenied('PATH_PERMISSION_SUBSET_ONLY: requested capability was not granted')
    return grants


def path_grant_refs(grants):
    return [{'grant_id':g['grant_id'],'revision':g.get('revision',1),'root':g['root'],
             'access':g['access'],'anchor_path':g['anchor_path'],'anchor':g['anchor']} for g in grants]


def _path_refs_current(state, refs):
    book=_permissions(state)
    return all((g:=book['grants'].get(r.get('grant_id'),{})) and _path_grant_live(state,g)
               and path_grant_refs([{**g,'grant_id':r['grant_id']}])[0]==r for r in refs)


def _active(state, row):
    from agent_core.request_state import current_finish
    return (not state.get('load_error') and _scope(state) == row['request_scope']
            and (state.get('task_state') or {}).get('status') != 'cancelled'
            and not (state.get('task_state') or {}).get('pi_pause')
            and not current_finish(state)
            and _permissions(state)['policy_revision'] == row['policy_revision'] == _POLICY_REVISION
            and (row.get('payload',{}).get('kind')!='file_read' or
                 row['payload'].get('session_id','') == str((state.get('task_state') or {}).get('pi_session_id') or ''))
            and (not row.get('grant_id') or
                 (_lifetime_current(state,_permissions(state)['grants'].get(row.get('grant_id'),{}))
                  and _grant_covers(_permissions(state)['grants'].get(row.get('grant_id'),{}),row['payload'])))
            and _path_refs_current(state,row.get('payload',{}).get('path_grants',[]))
            and _path_refs_current(state,row.get('payload',{}).get('covered_directory_grants',[]))
            and _path_refs_current(state,row.get('payload',{}).get('access_projection',{}).get('write_grants',[]))
            and (row.get('source')!='rule' or bool(row.get('grant_id')) or
                 row.get('payload',{}).get('kind')=='path_access' and _path_request_covered(state,row))
            and (row.get('source')!='policy' or _automatic_covers(state,row)))


def _automatic_covers(state,row):
    mode=_permissions(state).get('automation',{})
    if (row.get('automation_revision',mode.get('revision',0))!=mode.get('revision',0)
            or (state.get('task_state') or {}).get('pi_pause')):return False
    global_auto=(mode.get('mode')=='auto' and _lifetime_current(state,mode)
                 and mode.get('policy_revision')==_POLICY_REVISION)
    if not global_auto:return _scoped_automatic_covers(state,row)
    payload=row['payload'];project=mode.get('project')
    if payload.get('kind')=='file_write':
        return bool(project and (Path(payload['path']).is_relative_to(Path(project)) or
            payload.get('path_grants') and _path_refs_current(state,payload['path_grants'])))
    if payload.get('kind')=='command':
        projection=payload.get('access_projection',{})
        return (projection.get('project')==project
                and all(Path(p).is_relative_to(Path(project)) or any(
                    _path_covers(_permissions(state)['grants'].get(r['grant_id'],{}),p,'read_write')
                    for r in projection.get('write_grants',[])) for p in projection.get('write_roots',[]))
                and _path_refs_current(state,projection.get('write_grants',[]))
                and projection.get('network_policy')=='isolated'
                and projection.get('request_scope')==_scope(state))
    return False


def scoped_automatic_grants(state):
    book=_permissions(state);revision=book.get('automation',{}).get('revision',0)
    if state.get('task_state',{}).get('pi_pause'):return []
    return [g for g in book['grants'].values() if _path_grant_live(state,g)
            and g.get('ordinary_operations')=={'policy_revision':_POLICY_REVISION,'automation_revision':revision}]


def _scoped_automatic_covers(state,row):
    grants=scoped_automatic_grants(state);payload=row['payload'];project=_permissions(state).get('owner',{}).get('project')
    if not grants or not project:return False
    live={g['grant_id'] for g in grants}
    if any(gid not in live for gid in row.get('automatic_grant_ids',[])):return False
    if row.get('automatic_grant_ids'):grants=[g for g in grants if g['grant_id'] in row['automatic_grant_ids']]
    def covers(path,access):
        return Path(path).is_relative_to(Path(project)) or any(_path_covers(g,path,access) for g in grants)
    if payload.get('kind')=='file_write':
        return covers(payload['path'],'read_write') and _path_refs_current(state,payload.get('path_grants',[]))
    if payload.get('kind')!='command':return False
    projection=payload.get('access_projection',{})
    return (projection.get('project')==project and projection.get('request_scope')==_scope(state)
            and projection.get('network_policy')=='isolated' and covers(projection['cwd'],'read')
            and all(covers(r['root'],'read') for r in projection.get('reads',[]))
            and all(covers(r['root'],'read') for r in projection.get('read_directories',[]))
            and all(covers(p,'read_write') for p in projection.get('write_roots',[]))
            and _path_refs_current(state,projection.get('write_grants',[])))


def _automatic_grant_ids(state,payload):
    mode=_permissions(state).get('automation',{})
    if mode.get('mode')=='auto' and _lifetime_current(state,mode) and mode.get('policy_revision')==_POLICY_REVISION:return []
    grants=scoped_automatic_grants(state)
    if not grants:return []
    project=Path(_permissions(state)['owner']['project']);paths=[]
    if payload['kind']=='file_write':paths=[(payload['path'],'read_write')]
    elif payload['kind']=='command':
        view=payload['access_projection']
        paths=[(view['cwd'],'read')]+[(r['root'],'read') for r in [*view.get('reads',[]),*view.get('read_directories',[])]]+[(p,'read_write') for p in view.get('write_roots',[])]
    selected=set()
    for path,access in paths:
        if Path(path).is_relative_to(project):continue
        covering=sorted((g for g in grants if _path_covers(g,path,access)),key=lambda g:len(Path(g['root']).parts),reverse=True)
        if covering:selected.add(covering[0]['grant_id'])
    # Current-project output permission is explicitly part of each combined
    # decision; bind one source, so unrelated later grants cannot drift it.
    return sorted(selected or {grants[0]['grant_id']})


def suspend_automatic(state):
    """Existing mode revision also invalidates directory-scoped ordinary policies."""
    mode=_permissions(state).setdefault('automation',{})
    mode.update(mode='manual',revision=int(mode.get('revision',0))+1)


def _path_request_covered(state,row):
    targets=row['payload'].get('targets',[]);grants=scoped_automatic_grants(state)
    return bool(targets) and all(any(_path_covers(g,t['root'],t['access']) for g in grants) for t in targets)


def _refresh_pending(state,row):
    if row['status']!='pending' or not _active(state,row) or row.get('requires_fresh_human_decision'):return
    if row['payload']['kind']=='file_read':_apply_read_coverage(state,row)
    elif row['payload']['kind']=='path_access' and _path_request_covered(state,row):
        row.update(status='allowed',source='rule',granted_ids=[g['grant_id'] for g in _permissions(state)['grants'].values()
            if _path_grant_live(state,g) and any(_path_covers(g,t['root'],t['access']) for t in row['payload']['targets'])])
    if row['status']=='pending' and _automatic_covers(state,row):
        row.update(status='allowed',source='policy',automation_revision=_permissions(state).get('automation',{}).get('revision',0),
            automatic_grant_ids=_automatic_grant_ids(state,row['payload']),decided_at=time.time())


def set_automatic_mode(service,task_id,mode, *, lifetime='request'):
    if mode not in {'manual','auto'}:raise PermissionDenied('APPROVAL_MODE_INVALID')
    if lifetime not in {'request','session'}:raise PermissionDenied('APPROVAL_LIFETIME_INVALID')
    owner=bind_permission_owner(service,task_id)
    def update(state):
        book=_permissions(state);task=state.setdefault('task_state',{})
        if not task.get('pi_request_id'):raise PermissionDenied('REQUEST_BEGIN_REQUIRED')
        if mode=='auto' and task.get('pi_pause'):raise PermissionDenied('REQUEST_PAUSED')
        prior=book.get('automation',{})
        book['automation']={'mode':mode,'source':'explicit_host_user','request_scope':_scope(state),
            'lifetime':lifetime,'owner':owner,
            'project':str(service.project_for_task(task_id)),'policy_revision':_POLICY_REVISION,
            'revision':int(prior.get('revision',0))+1,'network':'isolated',
            'scope':'project and explicitly granted data paths; isolated network; no new grants, installs or policy edits'}
        return deepcopy(book['automation'])
    return _mutate(service.data_path,task_id,update)


def _mutate(data_path, task_id, delta):
    # This is the existing narrow CAS retry; immutable event/object files and
    # plan_state.json are committed by the existing StateContractStore path.
    from agent_core.pending_input import mutate
    def update(state):
        book=_permissions(state)
        if book['policy_revision']=='execution-permissions-v1':
            # Upgrade only the current book under its original CAS. Old code's
            # v1 comparison rejects v2 actions; legacy grants remain request-only.
            for row in book['requests'].values():
                if row.get('status') in {'pending','allowed'} and not row.get('result'):
                    row.update(status='cancelled',reason='permission_policy_migrated; propose a new action')
                row['policy_revision']=_POLICY_REVISION
            for grant in book['grants'].values():
                grant['policy_revision']=_POLICY_REVISION
                grant.setdefault('lifetime','request')
            mode=book.get('automation')
            if mode:mode.update(mode='manual',policy_revision=_POLICY_REVISION)
            book.update(policy_revision=_POLICY_REVISION,migrated_from='execution-permissions-v1')
        return delta(state)
    return mutate(data_path, task_id, update)


def command_payload(service, arguments, task_id, call_id=None):
    if call_id is not None: call_id=derive_permission_call_id(call_id,'command')
    from agent_core.plan_tracker import load_plan_state
    from agent_core.action_runtime import freeze_execution_environment, registered_execution_environment
    import os
    state = load_plan_state(service.data_path, task_id)
    project = Path(service.project_for_task(task_id)).resolve()
    cwd = Path(arguments.get('workdir') or project).expanduser()
    cwd = (project / cwd).resolve(strict=True) if not cwd.is_absolute() else cwd.resolve(strict=True)
    if not cwd.is_dir(): raise ValueError('EXECUTION_CWD_NOT_DIRECTORY')
    env = freeze_execution_environment(os.environ, registered_execution_environment(state, arguments.get('env') or {}, runtime_binding=arguments.get('_runtime_binding')))
    # Environment values remain in controlled execution evidence, never UI or
    # reviewer projection. Neither Plan prose nor DB generation is hashed.
    actual = {k:deepcopy(v) for k,v in arguments.items() if k not in {'timeout_ms','max_output_chars','workdir','read_paths'}}
    access_projection=command_access_projection(service,arguments,task_id,call_id=call_id)
    sources={}
    selected=actual.get('script_path') or (actual.get('legacy_action') or {}).get('script_path')
    if selected:
        from agent_core.workspace_files import WorkspaceFileService
        files=WorkspaceFileService(service)
        source=cwd/selected
        sources[str(source)]=_authorized_source_hash(files,source,task_id,call_id)
        for helper in actual.get('helper_paths') or []:
            path=source.parent/helper
            if '..' in Path(helper).parts or not path.is_relative_to(source.parent): raise PermissionDenied('HELPER_OUTSIDE_SCRIPT_LAYOUT')
            sources[str(path)]=_authorized_source_hash(files,path,task_id,call_id)
    return {'kind':'command', 'arguments':actual, 'cwd':str(cwd), 'source_hashes':sources,
            'environment_hash':digest(env), 'human_required':True,
            'access_projection':access_projection}


def request_permission(data_path, task_id, call_id, payload, *, original_call_id=None):
    from agent_core.state_contract_store import StateContractStore
    def update(state):
        proposed_payload=payload
        book = _permissions(state)
        scope = _scope(state)
        key = 'permission-' + digest([scope, call_id])[:32]
        action_hash = digest(payload)
        row = book['requests'].get(key)
        if row:
            if (row['action_hash'] != action_hash and row.get('proposal_source_hash')!=action_hash and not (payload.get('kind')=='installation'
                    and row.get('original_action_hash')==action_hash)):
                raise PermissionDenied('PERMISSION_ACTION_CHANGED')
            _refresh_pending(state,row)
            if not _active(state, row) and row['status'] in {'pending','allowed'}:
                row['status'] = 'cancelled'
            return deepcopy(row)
        if payload.get('kind')=='path_access' and payload.get('directory_targets'):
            covered=[];missing=[]
            for target in payload['directory_targets']:
                current=next((g for g in scoped_automatic_grants(state) if g.get('lifetime')=='session'
                    and _path_covers(g,target['root'],target['access'])),None)
                if current:covered.append(current)
                else:missing.append(target)
            if covered:
                proposed_payload={**deepcopy(payload),'directory_targets':missing,
                    'covered_directory_grants':path_grant_refs({g['grant_id']:g for g in covered}.values())}
        stored_hash=digest(proposed_payload)
        row = {'request_id':key, 'tool_call_id':str(call_id), 'request_scope':scope,
               'action_hash':stored_hash, 'original_action_hash':stored_hash, 'proposal_source_hash':action_hash,'payload':deepcopy(proposed_payload),
               'resume_revision':int(state.get('task_state',{}).get('pi_resume_revision',0)),
               'policy_revision':book['policy_revision'], 'status':'pending',
               'allowed_decisions':['y','n'], 'created_at':time.time()}
        if original_call_id is not None: row['original_tool_call_id']=str(original_call_id)
        if proposed_payload.get('directory_targets'):row['allowed_decisions'].append('directory_session')
        if payload.get('kind')=='path_access':
            row['allowed_decisions'] += ['session']
            if all(t.get('existed_at_proposal') for t in payload.get('targets',[])):
                row['allowed_decisions'] += ['read_only','read_session']
        if payload.get('kind')=='file_write':
            if payload.get('scope_candidate'):row['allowed_decisions'].append('scope')
            for grant_id,grant in book['grants'].items():
                if (grant.get('operation')=='file_write' and _lifetime_current(state,grant) and grant['policy_revision']==book['policy_revision']
                        and _grant_covers(grant,payload)):
                    row.update(status='allowed',source='rule',grant_id=grant_id)
                    break
        if payload.get('kind')=='file_read':
            row['allowed_decisions'].append('session')
            if payload.get('scope_candidate'): row['allowed_decisions'].append('scope')
            _apply_read_coverage(state,row)
        # A new call ID is not a way to erase an explicit denial of this action.
        denials=[r for r in book['requests'].values() if r['action_hash']==action_hash
                 and r['request_scope']==scope and (r['status']=='denied' or
                    r['status']=='cancelled' and r.get('reason')=='host_cancel_decision')]
        reauthorized=(row.get('source')=='rule' and
            book['grants'].get(row.get('grant_id'),{}).get('resume_revision',0)>
            max((r.get('resume_revision',0) for r in denials),default=-1))
        if denials and not reauthorized:
            # Explicit resume permits a fresh HUMAN question, never restores a
            # denied row or silently reuses an old rule/automatic policy.
            row['status']='denied' if any(r.get('resume_revision',0)>=row['resume_revision'] for r in denials) else 'pending'
            row.pop('source',None);row.pop('grant_id',None)
            row['requires_fresh_human_decision']=True
        if payload.get('kind')=='file_read' and _read_explicitly_denied(state,payload):
            row['requires_fresh_human_decision']=True
        _refresh_pending(state,row)
        row['payload_ref'] = StateContractStore(data_path,task_id).put_object('permission_action', proposed_payload)
        if not _active(state,row): row['status']='cancelled'
        book['requests'][key] = row
        return deepcopy(row)
    return _mutate(data_path, task_id, update)


def permission_state(data_path, task_id, request_id):
    from agent_core.plan_tracker import load_plan_state
    state = load_plan_state(data_path,task_id)
    row = deepcopy(_permissions(state)['requests'][request_id])
    if row['status']=='pending':
        # Re-evaluate an older pending tool against a later directory grant.
        return request_permission(data_path,task_id,row['tool_call_id'],row['payload'])
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
        if decision.choice=='directory_session' or row['payload']['kind']=='path_access' and decision.choice in {'y','session','read_only','read_session'}:
            combined=decision.choice=='directory_session'
            targets=row['payload']['directory_targets'] if combined else row['payload']['targets']
            lifetime='session' if combined or decision.choice in {'session','read_session'} else 'request'
            if combined and decision.selections is not None:raise PermissionDenied('COMBINED_DECISION_FIELDS_INVALID')
            selections=decision.selections
            if selections is None:selections=[{'index':i,'access':'read' if decision.choice in {'read_only','read_session'} else target['access'],'lifetime':lifetime}
                                              for i,target in enumerate(targets)]
            if not isinstance(selections,list) or not selections:raise PermissionDenied('PATH_SELECTION_REQUIRED')
            seen=set();grants={}
            for selected in selections:
                if not isinstance(selected,dict) or set(selected)!={'index','access','lifetime'}:raise PermissionDenied('PATH_SELECTION_INVALID')
                i=selected['index']
                if type(i) is not int or i in seen or not 0<=i<len(targets):raise PermissionDenied('PATH_SELECTION_INVALID')
                seen.add(i);target=targets[i]
                if selected['access']=='read' and not target.get('existed_at_proposal'):
                    raise PermissionDenied('READ_TARGET_DOES_NOT_EXIST')
                if (selected['access'] not in {'read','read_write'} or target['access']=='read' and selected['access']!='read'
                        or selected['lifetime'] not in {'request','session'}):raise PermissionDenied('PATH_SELECTION_ESCALATION')
                if not _path_target_valid(target) or Path(target['requested_path']).resolve()!=Path(target['root']):
                    raise PermissionDenied('PATH_TARGET_CHANGED_BEFORE_APPROVAL')
                gid=row['request_id']+':'+str(i)
                grants[gid]={**deepcopy(target),'access':selected['access'],'lifetime':selected['lifetime'],
                    'operation':'path_access','source':'human','owner':deepcopy(_permissions(state)['owner']),
                    'project':row['payload']['project'],'session_id':_permissions(state)['owner']['session_id'],
                    'request_scope':deepcopy(row['request_scope']),'policy_revision':row['policy_revision'],
                    'path_policy_revision':_PATH_POLICY_REVISION,'read_policy_revision':_READ_POLICY_REVISION,
                    'approval_id':row['request_id'],'grant_id':gid,'revision':1,'revoked':False,'created_at':time.time(),
                    'resume_revision':row.get('resume_revision',0)}
                if combined:grants[gid]['ordinary_operations']={'policy_revision':_POLICY_REVISION,
                    'automation_revision':_permissions(state).get('automation',{}).get('revision',0)}
            _permissions(state)['grants'].update(grants);row['granted_ids']=list(grants)
            if combined and row['payload']['kind']=='file_read':row['grant_id']=next(iter(grants))
        elif row['payload']['kind']=='file_read' and decision.choice in {'y','scope','session'}:
            candidate=(row['payload'].get('scope_candidate') if decision.choice=='scope'
                       else row['payload'].get('target_candidate'))
            if not candidate: raise PermissionDenied('SCOPE_NOT_ENFORCEABLE')
            if _read_explicitly_denied(state,row['payload'],after_revision=row.get('resume_revision',0)):
                row.update(status='denied',source='human')
                return deepcopy(row)
            grant={**deepcopy(candidate),'source':'human','request_scope':row['request_scope'],
                   'policy_revision':row['policy_revision'],'read_policy_revision':_READ_POLICY_REVISION,
                   'grant_id':row['request_id'],'created_at':time.time(),'lifetime':'session' if decision.choice=='session' else 'current_request',
                   'owner':deepcopy(_permissions(state).get('owner')),
                   'resume_revision':row.get('resume_revision',0),
                   'revoked':False}
            if not _grant_covers(grant,row['payload']): raise PermissionDenied('FILE_PERMISSION_SCOPE_CHANGED')
            _permissions(state)['grants'][row['request_id']]=grant
            row['grant_id']=row['request_id']
        elif decision.choice=='scope':
            candidate=row['payload'].get('scope_candidate')
            if row['payload']['kind']!='file_write' or not candidate:
                raise PermissionDenied('SCOPE_NOT_ENFORCEABLE')
            grant={**deepcopy(candidate),'source':'human','request_scope':row['request_scope'],
                   'policy_revision':row['policy_revision'],'resume_revision':row.get('resume_revision',0)}
            _permissions(state)['grants'][row['request_id']]=grant
        row.update(status='allowed' if decision.choice in {'y','scope','session','read_only','read_session','directory_session'} else 'denied',
                   source='human', decided_at=time.time())
        for pending in _permissions(state)['requests'].values():_refresh_pending(state,pending)
        return deepcopy(row)
    return _mutate(data_path,task_id,update)


def cancel_permission(data_path,task_id,request_id,*,source=None,reason=None):
    def update(state):
        row = _permissions(state)['requests'][request_id]
        if row['status'] in {'pending','allowed'} and not row.get('claim'):
            row['status']='cancelled'
            row['decided_at']=time.time()
            # New events only. Unspecified origin remains unknown; never infer
            # a key press or retrofit a historical cancellation.
            if source is not None: row['source']=str(source)[:128]
            if reason is not None: row['reason']=str(reason)[:256]
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
        scope_label=('，scope=授权明确父目录及子目录只读（当前分析请求）'
                     if row['payload']['kind']=='file_read' else '，scope=授权上述目录的文件工具写入')
        text = await owner.aread(('y=批准展示的本次操作，n=拒绝'+(scope_label if 'scope' in row['allowed_decisions'] else '')+'；空白/编辑不是批准 > '),timeout=0)
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
        if live['payload'].get('human_required') and live.get('source') not in {'human','policy'}:
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
    if live['payload'].get('human_required') and live.get('source') not in {'human','policy'}:
        raise PermissionDenied('HUMAN_PERMISSION_REQUIRED')
    if live['payload'].get('access_projection'):
        validate_read_grants(permit.data_path,permit.task_id,live['payload']['access_projection'])
    return live


def request_allows_command(state, arguments):
    """Pi's main request owns work; historical non-Pi admission stays compatible.

    This is request admission only. It grants no path, execution permit or
    installation consent and never interprets model text as host authority.
    """
    task=state.get('task_state') or {}
    admission=task.get('intent_admission') or {}
    if task.get('pi_request_id'):
        return bool(admission.get('request_kind')=='work' and admission.get('source')=='pi_host_request'
                    and task.get('current_request_key')==task['pi_request_id'] and not task.get('pi_pause'))
    return bool(admission.get('request_kind')=='execute' or
                arguments.get('_biomind_probe') is True and admission.get('request_kind')=='consult')


async def authorize_command(service,arguments,*,task_id,call_id,cancel_event=None):
    from agent_core.plan_tracker import load_plan_state
    state=load_plan_state(service.data_path,task_id)
    review=(state.get('task_state') or {}).get('result_review') or {}
    if not request_allows_command(state,arguments) or review.get('request_scope')==_scope(state):
        raise PermissionDenied('CURRENT_REQUEST_FORBIDS_EXECUTION')
    call_id=derive_permission_call_id(call_id,'command')
    payload=command_payload(service,arguments,task_id,call_id=call_id)
    row=request_permission(service.data_path,task_id,call_id,payload)
    row=await await_permission(service.data_path,task_id,row,cancel_event=cancel_event)
    # Detect cwd/environment/argument drift during an arbitrarily long wait.
    if digest(command_payload(service,arguments,task_id,call_id=call_id))!=row['action_hash']:
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
    return validate_permit(permit)


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
        if 'result' not in row:
            row['result']=deepcopy(result)
            # A trusted atomic file commit may replace the exact granted inode.
            # Advance only grants consumed by this action, never model-supplied IDs.
            if result.get('status')=='completed' and row['payload'].get('kind')=='file_write':
                for ref in row['payload'].get('path_grants',[]):
                    grant=_permissions(state)['grants'].get(ref['grant_id'],{})
                    if grant.get('revoked'):continue
                    target=Path(grant['root'])
                    if target.exists() and (grant.get('target_kind')=='directory' or str(target)==result.get('path')):
                        info=_safe_read_info(target)
                        changed=({'target_anchor':[info.st_dev,info.st_ino]} if grant['target_kind']=='file'
                                 else {'anchor_path':str(target),'anchor':[info.st_dev,info.st_ino]})
                        if any(grant.get(k)!=v for k,v in changed.items()):
                            grant.update(changed)
                            grant['revision']=int(grant.get('revision',1))+1
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
    actual_project=Path(files.draft_root or files.commands.project_for_task(task_id))
    if not path.is_relative_to(actual_project):
        grants=path_grants(files.commands,task_id,path,'read_write')
        if not grants:raise PermissionDenied('PATH_WRITE_PERMISSION_REQUIRED')
        chosen=grants[0]
        return {'kind':'file_write','tool':name,'path':str(path),'arguments':deepcopy(args),
                'path_grants':path_grant_refs([chosen]),'human_required':False,
                'write_boundary':{'root':chosen['anchor_path'],'anchor':chosen['anchor']}}
    return {'kind':'file_write','tool':name,'path':str(path),'arguments':deepcopy(args),
            'scope_candidate':{'root':str(root),'anchor':[info.st_dev,info.st_ino],
                               'project':str(project),'operation':'file_write'},
            'human_required':False}


def _grant_covers(grant,payload):
    if grant.get('revoked'): return False
    if payload.get('kind')=='file_read': return _read_grant_covers(grant,payload)
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
        for grant in book['grants'].values():
            grant.update(revoked=True,revoked_at=time.time())
        for row in book['requests'].values():
            if (row.get('source')=='rule' or row.get('grant_id')) and row['status']=='allowed' and not row.get('claim'):
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
    boundary=row['payload'].get('write_boundary') or row['payload']['scope_candidate']
    if row.get('source')=='rule':
        from agent_core.plan_tracker import load_plan_state
        boundary=_permissions(load_plan_state(permit.data_path,permit.task_id))['grants'][row['grant_id']]
    token=_file_dispatch.set((Path(boundary['root']),boundary['anchor'],permit))
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


def record_created_directory(path,fd,permit=None):
    """Pin a formerly missing grant root to the descriptor we just created.

    This narrows the existing grant's identity; no permission/lifetime changes.
    Reuses its claimed action and original CAS, including interrupted creation.
    """
    import os
    context=_file_dispatch.get()
    permit=permit or (context[2] if context and len(context)>2 else None)
    if permit is None:return
    if not isinstance(permit,ExecutionPermit) or permit.seal is not _SEAL:raise PermissionDenied('EXECUTION_PERMIT_REQUIRED')
    info=os.fstat(fd);identity=[info.st_dev,info.st_ino]
    def update(state):
        book=_permissions(state);row=book['requests'][permit.request_id]
        if not row.get('claim') or row['action_hash']!=permit.action_hash or not _active(state,row):
            raise PermissionDenied('EXECUTION_PERMIT_REVOKED')
        refs=[*row['payload'].get('path_grants',[]),*row['payload'].get('access_projection',{}).get('write_grants',[])]
        for ref in refs:
            grant=book['grants'][ref['grant_id']]
            if grant['root']==str(path) and not grant.get('existed_at_proposal'):
                if grant.get('target_anchor') not in (None,identity):raise PermissionDenied('PATH_TARGET_CHANGED')
                grant['target_anchor']=identity
    _mutate(permit.data_path,permit.task_id,update)


def may_create_file_directory(path):
    boundary=_file_dispatch.get()
    return boundary is None or (Path(path)!=boundary[0] and Path(path).is_relative_to(boundary[0]))


def derive_permission_call_id(call_id, operation, target=''):
    """Stable domain-owned subidentity; no tool description affects authority."""
    prefix = 'domain-permission:' + operation + ':'
    value = str(call_id or '')
    if value.startswith(prefix):
        return value
    return prefix + digest([value, operation, str(target)])[:40]


def _safe_read_info(path):
    """Metadata only, rejecting symlinks in every component (no business bytes)."""
    import os
    import stat
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise PermissionDenied('INVALID_READ_PERMISSION_PATH')
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise PermissionDenied('SYMLINK_PERMISSION_ROOT')
        if not (stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
            raise PermissionDenied('REGULAR_SINGLE_LINK_FILE_OR_DIRECTORY_REQUIRED')
        return info
    finally:
        os.close(fd)


def file_read_payload(files, value, task_id):
    import stat
    from agent_core.plan_tracker import load_plan_state
    # This shared resolver enforces private/runtime exclusions for proposals too.
    project, path = files.read_target(value, task_id)
    info = _safe_read_info(path)
    state = load_plan_state(files.commands.data_path, task_id)
    if state.get('task_state',{}).get('pi_session_id'):
        bind_permission_owner(files.commands,task_id)
    session_id = str((state.get('task_state') or {}).get('pi_session_id') or '')
    kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file'
    common = {'operation':'file_read', 'project':str(project), 'session_id':session_id,
              'read_policy_revision':_READ_POLICY_REVISION}
    candidate = {**common, 'root':str(path), 'anchor':[info.st_dev,info.st_ino],
                 'target_kind':kind, 'recursive':kind=='directory'}
    payload = {'kind':'file_read', 'path':str(path), 'project':str(project),
               'session_id':session_id, 'target_kind':kind, 'recursive':kind=='directory',
               'target_candidate':candidate, 'read_policy_revision':_READ_POLICY_REVISION,
               'lifetime':'current_request', 'human_required':False}
    # A file approval may explicitly offer its direct parent, never an ancestor.
    if kind == 'file' and path.parent != Path(path.anchor):
        parent = _safe_read_info(path.parent)
        payload['scope_candidate'] = {**common,'root':str(path.parent),
            'anchor':[parent.st_dev,parent.st_ino], 'target_kind':'directory','recursive':True}
    if session_id:
        payload['directory_targets']=directory_candidates(files.commands,task_id,[{**candidate,'access':'read'}],promote_files=True)
    return payload


def _read_grant_covers(grant, payload):
    if grant.get('operation')=='path_access':
        return (not grant.get('revoked') and grant.get('project')==payload.get('project')
                and grant.get('session_id','')==payload.get('session_id','') and _path_target_valid(grant)
                and _path_covers(grant,payload['path']))
    if (grant.get('operation')!='file_read' or grant.get('revoked')
            or grant.get('read_policy_revision')!=_READ_POLICY_REVISION
            or grant.get('project')!=payload.get('project')
            or grant.get('session_id','')!=payload.get('session_id','')):
        return False
    try:
        import stat
        root, path = Path(grant['root']), Path(payload['path'])
        info = _safe_read_info(root)
        if [info.st_dev,info.st_ino] != grant['anchor']:
            return False
        if grant.get('target_kind')=='file':
            return (stat.S_ISREG(info.st_mode) and payload.get('target_kind')=='file' and path==root
                    and payload.get('target_candidate',{}).get('anchor')==grant['anchor'])
        return (grant.get('target_kind')=='directory' and stat.S_ISDIR(info.st_mode)
                and (path==root or (grant.get('recursive') is True and path.is_relative_to(root))))
    except (OSError, ValueError, KeyError):
        return False


def _read_explicitly_denied(state, payload, *, after_revision=0):
    scope = _scope(state)
    for row in _permissions(state)['requests'].values():
        prior = row.get('payload') or {}
        refused=(row.get('status')=='denied' or row.get('status')=='cancelled' and row.get('reason')=='host_cancel_decision')
        if (row.get('request_scope')!=scope or not refused or row.get('resume_revision',0)<after_revision
                or prior.get('kind') not in {'file_read','path_access'} or prior.get('project')!=payload.get('project')
                or (prior.get('session_id') or prior.get('owner',{}).get('session_id',''))!=payload.get('session_id','')):
            continue
        for candidate in prior.get('targets',[]) if prior.get('kind')=='path_access' else [prior]:
            target, denied = Path(payload['path']), Path(candidate.get('root') or candidate['path'])
            if target==denied or (candidate.get('target_kind')=='directory' and candidate.get('recursive',True) and target.is_relative_to(denied)):
                return True
    return False


def _apply_read_coverage(state,row):
    if _read_explicitly_denied(state,row['payload'],after_revision=row.get('resume_revision',0)):
        row.update(status='denied')
        return
    book = _permissions(state)
    for grant_id,grant in sorted(book['grants'].items(),key=lambda item:(not bool(item[1].get('ordinary_operations')),-len(Path(item[1]['root']).parts))):
        if (_lifetime_current(state,grant)
                and (grant.get('operation')!='path_access' or _path_grant_live(state,grant))
                and grant.get('policy_revision')==book['policy_revision']
                and not _read_explicitly_denied(state,row['payload'],after_revision=grant.get('resume_revision',0))
                and _grant_covers(grant,row['payload'])):
            row.update(status='allowed',source='rule',grant_id=grant_id)
            return


def prepare_file_read(files, value, *, task_id, call_id=None):
    """Return the existing row, raising its original wait/denial as appropriate."""
    payload = file_read_payload(files,value,task_id)
    identity = derive_permission_call_id(call_id or 'file-read','file_read',payload['path'])
    row = request_permission(files.commands.data_path,task_id,identity,payload,original_call_id=call_id)
    if row['status']=='pending':
        raise PermissionPending(row)
    if row['status']!='allowed':
        raise PermissionDenied('FILE_READ_PERMISSION_' + row['status'].upper())
    return row


def active_read_grants(data_path, task_id, *, project=None, include_inactive=False):
    """Host query; returns no credentials and never creates a grant."""
    from agent_core.plan_tracker import load_plan_state
    from agent_core.request_state import current_finish
    state = load_plan_state(data_path,task_id)
    book = _permissions(state)
    if project is None:
        binding = Path(data_path)/'task_outputs'/str(task_id)/'command_inspection'/'project_context.json'
        try: project = json.loads(binding.read_text(encoding='utf-8'))['project_dir']
        except (OSError, ValueError, KeyError): project = ''
    rows = []
    for grant_id, value in book['grants'].items():
        if value.get('operation') not in {'file_read','path_access'}: continue
        grant = deepcopy(value)
        payload = {'kind':'file_read','path':grant['root'],'project':str(project),
                   'session_id':str((state.get('task_state') or {}).get('pi_session_id') or ''),
                   'target_kind':grant.get('target_kind'), 'target_candidate':grant}
        origin = book['requests'].get(value.get('approval_id',grant_id),{})
        grant['active'] = bool(origin.get('source')=='human' and _lifetime_current(state,grant)
            and (grant.get('lifetime')=='session' or not current_finish(state) and state.get('task_state',{}).get('status')!='cancelled')
            and grant.get('policy_revision')==book['policy_revision'] and _grant_covers(grant,payload)
            and (value.get('operation')!='path_access' or _path_grant_live(state,value)))
        grant['grant_id'] = grant_id
        if include_inactive:
            grant['ordinary_operations_active']=any(g['grant_id']==grant_id for g in scoped_automatic_grants(state))
        if include_inactive or grant['active']: rows.append(grant)
    return rows


def revoke_read_grant(data_path, task_id, grant_id, *, request_scope=None, policy_revision=None,
                      expected_scope=None, expected_policy_revision=None):
    """Host-only CAS: retain history and invalidate human and derived rows."""
    data_path = getattr(data_path,'data_path',data_path)
    request_scope = expected_scope if expected_scope is not None else request_scope
    policy_revision = expected_policy_revision if expected_policy_revision is not None else policy_revision
    def update(state):
        book = _permissions(state)
        if request_scope is not None and request_scope != _scope(state):
            raise PermissionDenied('STALE_PERMISSION_REPLY')
        if policy_revision is not None and policy_revision != book['policy_revision']:
            raise PermissionDenied('STALE_PERMISSION_REPLY')
        grant = book['grants'].get(str(grant_id))
        if not grant or grant.get('operation') not in {'file_read','path_access','file_write'}:
            raise PermissionDenied('READ_GRANT_NOT_FOUND')
        if not _lifetime_current(state,grant):
            raise PermissionDenied('STALE_PERMISSION_REPLY')
        grant.update(revoked=True,revoked_at=time.time(),revision=int(grant.get('revision',1))+1)
        for row in book['requests'].values():
            refs=row.get('payload',{}).get('path_grants',[])+row.get('payload',{}).get('access_projection',{}).get('write_grants',[])
            if (row.get('grant_id')==str(grant_id) or grant_id in row.get('automatic_grant_ids',[]) or any(r.get('grant_id')==grant_id for r in refs)) and row['status'] in {'pending','allowed'} and not row.get('result'):
                row['status']='cancelled'
                row['revoked_at']=grant['revoked_at']
        return deepcopy(grant)
    return _mutate(data_path,task_id,update)


def _file_read_projection(grant, target):
    """A directory grant authorizes a named file without mounting its siblings."""
    import stat
    target = Path(target)
    if grant.get('operation')=='path_access':
        info=_safe_read_info(target)
        return {**deepcopy(grant),'root':str(target),'anchor':[info.st_dev,info.st_ino],
                'target_kind':'directory' if stat.S_ISDIR(info.st_mode) else 'file',
                'recursive':stat.S_ISDIR(info.st_mode),'source_grant_hash':digest(grant)}
    if grant.get('target_kind') != 'directory':
        return deepcopy(grant)
    info = _safe_read_info(target)
    if stat.S_ISDIR(info.st_mode):
        # Recursive directory mounts still require the separate backend capability.
        return deepcopy(grant)
    payload = {'kind':'file_read', 'path':str(target), 'target_kind':'file',
               'project':grant['project'], 'session_id':grant.get('session_id','')}
    if not _grant_covers(grant, payload):
        raise PermissionDenied('READ_ACCESS_GRANT_NOT_CURRENT')
    return {**deepcopy(grant), 'root':str(target), 'anchor':[info.st_dev,info.st_ino],
            'target_kind':'file', 'recursive':False, 'source_grant_hash':digest(grant)}


def validate_read_grants(data_path, task_id, projection):
    """Validate only grants bound to this action; unrelated grants do not drift it."""
    live = {g['grant_id']:g for g in active_read_grants(data_path,task_id,project=projection.get('project'))}
    from agent_core.plan_tracker import load_plan_state
    state = load_plan_state(data_path,task_id)
    if (projection.get('request_scope')!=_scope(state)
            or projection.get('session_id','')!=str((state.get('task_state') or {}).get('pi_session_id') or '')
            or projection.get('policy_revision')!=_READ_POLICY_REVISION):
        raise PermissionDenied('READ_ACCESS_PROJECTION_STALE')
    binding = Path(data_path)/'task_outputs'/str(task_id)/'command_inspection'/'project_context.json'
    try: project = json.loads(binding.read_text(encoding='utf-8'))['project_dir']
    except (OSError, ValueError, KeyError): project = None
    if project != projection.get('project'):
        raise PermissionDenied('READ_ACCESS_PROJECT_CHANGED')
    for binding in projection.get('path_bindings',[]):
        try: resolved=str(Path(binding['requested_path']).resolve(strict=True))
        except (OSError,RuntimeError) as exc: raise PermissionDenied('READ_PATH_TARGET_CHANGED') from exc
        if resolved!=binding['canonical_path']:raise PermissionDenied('READ_PATH_TARGET_CHANGED')
    for grant in projection.get('reads',[]):
        if grant.get('project_source'):
            info=_safe_read_info(grant['root'])
            if not Path(grant['root']).is_relative_to(Path(project)) or grant['anchor']!=[info.st_dev,info.st_ino]:
                raise PermissionDenied('PROJECT_READ_TARGET_CHANGED')
            continue
        current=live.get(grant.get('grant_id'))
        expected = current
        if current and grant.get('source_grant_hash'):
            expected = _file_read_projection(current, grant['root'])
        if (not current or digest(expected)!=digest(grant) or
                _read_explicitly_denied(state, {'kind':'file_read', 'path':grant['root'],
                    'project':projection['project'], 'session_id':projection.get('session_id','')},
                    after_revision=grant.get('resume_revision',0))):
            raise PermissionDenied('READ_ACCESS_GRANT_REVOKED_OR_CHANGED')
    for row in projection.get('read_directories',[]):
        info=_safe_read_info(row['root'])
        if [info.st_dev,info.st_ino]!=row['anchor']:raise PermissionDenied('READ_DIRECTORY_TARGET_CHANGED')
        if not row.get('project_source'):
            current=live.get(row.get('grant_id'))
            if not current or not _grant_covers(current,{'kind':'file_read','path':row['root'],
                    'project':project,'session_id':projection.get('session_id'),'target_kind':'directory'}):
                raise PermissionDenied('READ_DIRECTORY_GRANT_REVOKED')
    if not _path_refs_current(state,projection.get('write_grants',[])):
        raise PermissionDenied('WRITE_ACCESS_GRANT_REVOKED_OR_CHANGED')
    return projection


def command_access_projection(service, arguments, task_id, call_id=None):
    """Build the current action view from the original live permission book."""
    if call_id is not None and not str(call_id).startswith('install:'):
        call_id=derive_permission_call_id(call_id,'command')
    from agent_core.plan_tracker import load_plan_state
    from agent_core.command_execution import formal_access_projection, snapshot_read_directory, FormalProtectionError, check_data_path
    from agent_core.action_runtime import registered_execution_environment
    from agent_core.workspace_files import WorkspaceFileService
    state=load_plan_state(service.data_path,task_id);project=Path(service.project_for_task(task_id))
    if arguments.get('_biomind_probe') and (arguments.get('write_paths') or arguments.get('delete_paths')):
        raise FormalProtectionError('probe_cannot_declare_writes')
    # The approved snapshot is stable across retries; new files are only visible
    # to a new action. Revalidate live grants and anchors, never trust chat state.
    prior=next((r for r in _permissions(state)['requests'].values()
                if r.get('tool_call_id')==call_id and r.get('request_scope')==_scope(state)
                and r.get('status') in {'pending','allowed'}),None)
    if prior and prior.get('payload',{}).get('access_projection',{}).get('declaration_hash')==digest(arguments):
        snapshot=deepcopy(prior['payload'].get('access_projection'))
        if snapshot:
            validate_read_grants(service.data_path,task_id,snapshot)
            runtime_arguments={**arguments,'env':registered_execution_environment(state,arguments.get('env') or {},runtime_binding=arguments.get('_runtime_binding'))}
            return formal_access_projection(service,runtime_arguments,task_id,read_access=snapshot)
    cwd=(project/Path(arguments.get('workdir') or project).expanduser()).resolve(strict=True)
    if not cwd.is_dir():raise ValueError('EXECUTION_CWD_NOT_DIRECTORY')
    grants=active_read_grants(service.data_path,task_id,project=project)
    files=WorkspaceFileService(service);read_paths=arguments.get('read_paths')
    for key in ('read_paths','write_paths','delete_paths'):
        value=arguments.get(key)
        if value is not None and (not isinstance(value,list) or not all(isinstance(p,str) and p.strip() for p in value)):
            raise PermissionDenied(key.upper()+'_MUST_BE_PATH_LIST')
    write_grants=[];write_targets=[]
    for raw in [*(arguments.get('write_paths') or []),*(arguments.get('delete_paths') or [])]:
        target=(cwd/Path(raw)).resolve();check_data_path(service,task_id,target,write=True)
        if target.is_relative_to(project):continue
        existing=path_grants(service,task_id,target,'read_write')
        kind=('directory' if target.is_dir() else 'file') if target.exists() else next((g['target_kind'] for g in existing if g['root']==str(target)),'directory')
        chosen=prepare_path_access(service,task_id,[{'path':str(target),'access':'read_write','target_kind':kind,'recursive':kind=='directory'}],
            call_id=call_id or 'command',purpose='本次命令声明的输出范围；不修改任务的输出要求',suggest_directory=True)[0]
        write_grants.append(chosen);write_targets.append({'root':str(target),'target_kind':kind,
            'anchor_path':chosen['anchor_path'],'anchor':chosen['anchor'],'exists':target.exists()})
    source=arguments.get('script_path') or (arguments.get('legacy_action') or {}).get('script_path');source_paths=[]
    if source:
        source_path=cwd/str(source);source_paths.append(str(source_path))
        for helper in arguments.get('helper_paths') or []:
            helper_path=source_path.parent/str(helper)
            if '..' in Path(str(helper)).parts or not helper_path.is_relative_to(source_path.parent):raise PermissionDenied('HELPER_OUTSIDE_SCRIPT_LAYOUT')
            source_paths.append(str(helper_path))
    declared=list(read_paths or [])+source_paths
    # cwd access is RO unless explicitly included in the action's write paths.
    if not cwd.is_relative_to(project):
        row=prepare_file_read(files,str(cwd),task_id=task_id,call_id=call_id)
        grants=active_read_grants(service.data_path,task_id,project=project)
        if not any(g['grant_id']==row.get('grant_id') and g.get('target_kind')=='directory' for g in grants):
            raise PermissionDenied('CWD_DIRECTORY_PERMISSION_REQUIRED')
        if not any(cwd==Path(t['root']) or cwd.is_relative_to(Path(t['root'])) for t in write_targets):declared.append(str(cwd))
    selected={};directories={};bindings=[];snapshots=[]
    # Preserve legacy exact-read projection when nothing explicit was declared.
    if read_paths is None:
        for g in grants:
            if g.get('target_kind')=='file' and g.get('operation')=='file_read':selected[(g['grant_id'],g['root'])]=g
    for raw in declared:
        file_part,separator,group=raw.partition('::');requested=project/Path(file_part).expanduser()
        _,target=files.read_target(str(requested),task_id)
        bindings.append({'requested_path':str(requested),'canonical_path':str(target),**({'uri_group':group} if separator else {})})
        chosen=None
        if not target.is_relative_to(project):
            row=prepare_file_read(files,str(target),task_id=task_id,call_id=call_id)
            grants=active_read_grants(service.data_path,task_id,project=project)
            chosen=next((g for g in grants if g['grant_id']==row.get('grant_id')),None)
            if chosen is None:raise PermissionDenied('READ_ACCESS_GRANT_NOT_CURRENT')
            # A read-only directory is the declared dependency view, including
            # companions/indices. Never mount unrelated historic grants, or
            # widen an exact read inside an RW output tree into an RO overlay.
            if (chosen.get('ordinary_operations') and chosen.get('target_kind')=='directory'
                    and chosen.get('access','read')=='read' and chosen.get('recursive')):
                if not target.is_dir():target=target.parent
        if target.is_dir():
            members,dirs,metrics=snapshot_read_directory(service,task_id,target);snapshots.append(metrics)
            for directory in dirs:
                directories[directory['root']]={**directory,**({'grant_id':chosen['grant_id']} if chosen else {'project_source':True})}
        else:members=[target]
        for member in members:
            if chosen:projection=_file_read_projection(chosen,member)
            else:
                info=_safe_read_info(member)
                projection={'root':str(member),'anchor':[info.st_dev,info.st_ino],'target_kind':'file','recursive':False,'project_source':True}
            selected[(projection.get('grant_id','project'),str(member))]=projection
    if len(selected)+len(directories)>512:raise FormalProtectionError('directory_snapshot_mount_limit',mount_limit=512,phase='directory_projection')
    projection={'policy_revision':_READ_POLICY_REVISION,'project':str(project),'declaration_hash':digest(arguments),
        'session_id':str(state.get('task_state',{}).get('pi_session_id') or ''),'request_scope':_scope(state),
        'reads':sorted(selected.values(),key=lambda g:(g.get('grant_id',''),g['root'])),
        'read_directories':sorted(directories.values(),key=lambda r:r['root']),
        'directory_snapshots':snapshots,'path_bindings':bindings,
        'write_grants':path_grant_refs({g['grant_id']:g for g in write_grants}.values()),'write_targets':write_targets}
    runtime_arguments={**arguments,'env':registered_execution_environment(state,arguments.get('env') or {},runtime_binding=arguments.get('_runtime_binding'))}
    return formal_access_projection(service,runtime_arguments,task_id,read_access=projection)


def read_permission_status(service, task_id):
    from agent_core.plan_tracker import load_plan_state
    state=load_plan_state(service.data_path,task_id)
    project=service.context.project_dir
    if not service.context.explicit:
        binding=Path(service.data_path)/'task_outputs'/str(task_id)/'command_inspection'/'project_context.json'
        try: project=Path(json.loads(binding.read_text(encoding='utf-8'))['project_dir'])
        except FileNotFoundError: pass
    mode=deepcopy(_permissions(state).get('automation',{'mode':'manual','lifetime':'request'}))
    if mode.get('mode')=='auto' and not _lifetime_current(state,mode):mode['mode']='manual'
    return {'project_root':str(project),'approval_mode':mode,
            'session_id':str((state.get('task_state') or {}).get('pi_session_id') or ''),
            'request_scope':_scope(state),'policy_revision':_permissions(state)['policy_revision'],
            'grants':active_read_grants(service.data_path,task_id,
                project=project,include_inactive=True),
            'lifetime':'per_grant: request or same persistent session; new/fork do not inherit',
            'revocation_effect':'blocks new reads and process starts; use cancel_run explicitly to stop running jobs'}


@contextmanager
def open_authorized_source(files, path, task_id, call_id=None):
    """Open explicit analysis sources under the same file-read authorization."""
    import os
    import stat
    if call_id is not None: call_id=derive_permission_call_id(call_id,'command')
    root,path,relative,boundary,_=files.resolve_read_path(str(path),task_id=task_id,call_id=call_id)
    with files._read_parent(root,relative,boundary) as (parent,name):
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                raise PermissionDenied('REGULAR_SINGLE_LINK_FILE_REQUIRED')
            files._verify_read_identity(info,boundary)
            yield stream


def _authorized_source_hash(files, path, task_id, call_id):
    """Keep existing script hashes; never open a source outside its authority."""
    with open_authorized_source(files,path,task_id,call_id) as stream:
        hashed=hashlib.sha256()
        for data in iter(lambda:stream.read(1024*1024),b''): hashed.update(data)
        return hashed.hexdigest()
