"""Existing execution/permission/storage API behind an explicit Pi domain facade.

No BioAnalystAgent, model calls, provider configuration, dotenv or graph runner.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import time
from datetime import datetime,timezone
from types import SimpleNamespace

from agent_core.command_execution import CommandExecutionService, FormalProtectionError, _PRIVATE_PATTERN
from agent_core.formal_command_observation import FormalCommandObserver
from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.pending_input import mutate
from agent_core.request_state import request_scope
from agent_core import execution_permissions as permissions
from agent_core.current_guidance import file_snapshot, digest, without_times
from agent_core.registered_runtime import registered_runtime_reuse
from agent_core.skill_runtime_metadata import SkillRuntimeRegistry
from agent_core.workspace_files import WorkspaceFileService
from agent_core.work_outputs import file_fingerprint
from memory.core_store import CoreMemoryStore
from memory.wiki_memory import WikiMemoryManager
from memory.patch_validator import redact_secrets, sanitize_value
from .registry import RegistryView, registry_path as resolve_registry_path
from .discovery import list_directory

HOST_METHODS = frozenset({'files.list','files.read_authorize','files.mutation_snapshot','files.mutation_replay','request.begin','request.cancel','approval.decide','settings.update','shutdown',
                         'request.pause','request.resume','request.revise','request.status','exec.probe',
                         'memory.maintenance.prepare','memory.maintenance.apply','archive.append','archive.status',
                         'permissions.status','permissions.revoke','permissions.mode','environments.launch','environments.register_observed'})


class DomainError(ValueError):
    pass


class DomainService:
    def __init__(self,*,cwd,data_root,install_root,session_id,registry_path=None,skills_root=None,
                 preferences_path=None,memory_root=None,wiki_root=None,history_enabled=False,
                 memory_mode='ask',archive_path=None,environment_inventory=None,
                 execution_profile='protected',profile_source='host',session_existing=False,migrate_profile=False):
        self.cwd=Path(cwd).resolve(strict=True)
        self.install_root=Path(install_root).resolve(strict=True)
        self.data_root=Path(data_root).resolve()
        self.data_root.mkdir(parents=True,exist_ok=True)
        self.session_id=str(session_id)
        self.task_id='pi-'+hashlib.sha256(self.session_id.encode()).hexdigest()[:24]
        self.host_token=secrets.token_urlsafe(32)
        self.skills_root=Path(skills_root or self.install_root/'skills').resolve()
        self.preferences_path=Path(preferences_path or os.environ.get('BIOMIND_USER_PREFERENCES') or self.install_root/'user_preferences.md').expanduser()
        if not self.preferences_path.is_absolute(): self.preferences_path=self.install_root/self.preferences_path
        self.preferences_source=os.environ.get('BIOMIND_PI_PREFERENCES_SOURCE') or (
            'config' if preferences_path else 'environment' if os.environ.get('BIOMIND_USER_PREFERENCES') else 'default')
        self.memory_root=Path(memory_root).resolve() if memory_root else None
        self.wiki_root=Path(wiki_root).resolve() if wiki_root else None
        self.history_enabled=bool(history_enabled)
        self.memory_mode=memory_mode
        self.archive_path=Path(archive_path).resolve() if archive_path else None
        self._archive=None;self._public_sources={}
        self._archive_writer=None;self._archive_writer_stats={}
        self.registry=RegistryView(resolve_registry_path(self.install_root,registry_path),environment_inventory)
        self.commands=CommandExecutionService(data_path=self.data_root,project_dir=self.cwd,startup_dir=self.cwd,
            first_yield_ms=50,deny_paths=[self.install_root/'.env.api_keys',self.install_root/'.env'],
            runtime_locations=({Path(sys.prefix).resolve()},'', [str(Path(sys.executable).parent),'/usr/bin','/bin']))
        self.commands.install_dir=self.install_root
        self.commands.runtime_registry=self.registry
        self.formal=FormalCommandObserver(self.commands)
        self.files=WorkspaceFileService(self.commands)
        self._skills=None; self._wiki=None; self._core=None
        self._seen_sources={}; self._dispatch_lock=asyncio.Lock(); self._install_tasks={}
        self._install_cancels={}
        self._closed=False
        from agent_core.execution_profile import profile_value, bind_host_profile
        self._execution_profile=profile_value(execution_profile)
        self.commands._execution_profile=self._execution_profile
        bind_host_profile(self,self._execution_profile,source=profile_source,
                          existing=session_existing,migrate=migrate_profile)

    @property
    def execution_profile(self):
        return self._execution_profile

    def _state(self):
        from agent_core.execution_profile import require_host_profile
        require_host_profile(self)
        state=load_plan_state(self.data_root,self.task_id)
        task=state.get('task_state') or {}
        if task.get('pi_request_id') and 'execution_permissions' not in task:
            raise DomainError('PERMISSION_BOOK_MISSING: restore authoritative storage or explicitly start a new Pi session; chat history cannot restore approvals')
        book=permissions._permissions(state)
        if book['policy_revision']=='execution-permissions-v1':
            permissions._mutate(self.data_root,self.task_id,lambda current:None)
            state=load_plan_state(self.data_root,self.task_id);book=permissions._permissions(state)
        owner=book.get('owner')
        if owner and owner!={'task_id':self.task_id,'session_id':self.session_id,'project':str(self.cwd),'storage':str(self.data_root)}:
            raise DomainError('PERMISSION_STORAGE_OWNER_MISMATCH')
        return state
    def scope(self): return request_scope(self._state())
    def _host(self,token):
        if not isinstance(token,str) or not secrets.compare_digest(token,self.host_token):
            raise DomainError('HOST_CHANNEL_REQUIRED')

    def _ensure_request(self):
        if not (self._state().get('task_state') or {}).get('pi_request_id'):
            raise DomainError('REQUEST_BEGIN_REQUIRED')
        if self._state().get('task_state',{}).get('pi_pause'):
            raise DomainError('REQUEST_PAUSED')

    def _ensure_write_path(self,p):
        self._ensure_request()
        from agent_core.command_execution import check_data_path
        value=p.get('path')
        if not isinstance(value,str) or not value.strip():raise DomainError('FILE_PATH_REQUIRED')
        target=(self.cwd/Path(value).expanduser()).resolve()
        check_data_path(self.commands,self.task_id,target,write=True)
        if not target.is_relative_to(self.cwd):
            permissions.prepare_path_access(self.commands,self.task_id,
                [{'path':str(target),'access':'read_write','target_kind':'file','recursive':False}],
                call_id=self._call_id(p),purpose='创建或编辑此文件',suggest_directory=True)

    def _pause_request(self,reason):
        def update(state):
            task=state.setdefault('task_state',{})
            task['pi_pause']={'reason':str(reason)[:256],'request_id':task.get('pi_request_id')}
            permissions.suspend_automatic(state)
            for row in task.get('execution_permissions',{}).get('requests',{}).values():
                if row.get('request_scope')==request_scope(state) and row['status'] in {'pending','allowed'} and not row.get('claim'):
                    row.update(status='cancelled',reason='request_paused')
            return {'status':'paused','request_id':task.get('pi_request_id'),'reason':str(reason)[:256],
                    'jobs_cancelled':False}
        return mutate(self.data_root,self.task_id,update)

    def _approval(self,row):
        payload=deepcopy(row['payload'])
        # The ledger keeps the full launch projection. Approval displays and
        # model-visible waiting results need only the requested resources;
        # host runtime roots and control/secret masks are never capabilities
        # supplied to the model.
        access=payload.get('access_projection')
        if isinstance(access,dict):
            payload['access_projection']={k:deepcopy(access[k]) for k in
                ('mount_policy','project','cwd','write_roots','policy_revision','directory_snapshots') if k in access}
            directories=[Path(r['root']) for r in access.get('directory_snapshots',[])]
            payload['access_projection']['reads']=[{k:deepcopy(grant[k]) for k in
                ('root','target_kind','recursive','lifetime') if k in grant}
                for grant in access.get('reads',[]) if not any(Path(grant['root']).is_relative_to(root) for root in directories)]
        return {'request_id':row['request_id'],'action_hash':row['action_hash'],
                'request_scope':row['request_scope'],'payload':payload,
                'allowed_decisions':row['allowed_decisions'],'status':row['status']}

    def _waiting(self,row):
        return {'status':'waiting_approval','execution_started':False,'terminal':False,
                'approval':self._approval(row),'approval_id':row['request_id']}

    @staticmethod
    def _run_view(result):
        # Logs and full execution environment stay with the Python receipt.
        keys={'status','terminal','execution_started','run_ref','attempt_id','action_id','task_id',
              'exit_code','returncode','stdout','stderr','stdout_delta','stderr_delta','next_cursor',
              'delta_byte_ranges','unread_log_bytes','log_refs','receipt_ref','error','detail','replayed',
              'cleanup_confirmed','cleanup_reason','owner_available','process_alive','activity',
              'elapsed_seconds','output_idle_seconds','original_deadline_at','remaining_seconds',
              'scientific_validation','cancel_requested','stop_requested','task_cancelled','instruction',
              'wall_timeout_disabled','run_status','run_terminal','truncated','output_projection','observation_only',
              'capability_missing','filesystem_protection','runtime_binding','runtime_diagnostic'}
        view={k:v for k,v in result.items() if k in keys}
        # Copy only known diagnostic scalars, never traceback/environment or a
        # second directory listing. Keep the raw receipt unchanged for replay.
        def summary(value, limit):
            value=redact_secrets(value[:8192])
            value=''.join(c for c in value if c >= ' ' and c != '\x7f')
            return value[:limit] + ('…' if len(value)>limit else '')
        for key in ('status','error','detail','reason','phase','protection_phase','affected_capability','host_boundary',
                    'backend','probe_stderr','instruction','run_ref','receipt_ref'):
            value=result.get(key)
            if isinstance(value,str): view[key]=summary(value,1024 if key in {'detail','probe_stderr','instruction','receipt_ref'} else 256)
            else: view.pop(key,None)
        for key in ('execution_started','exit_code'):
            expected=bool if key=='execution_started' else int
            if key in view and view[key] is not None and type(view[key]) is not expected: view.pop(key)
        if result.get('status') in {'stage_blocked','not_run'}:
            for key in ('stdout','stderr'):
                if isinstance(view.get(key),str): view[key]=summary(view[key],2048)
        for key in ('count','unique_inode_count','errno','probe_exit_code'):
            if key in result and (result[key] is None or type(result[key]) is int): view[key]=result[key]
        def public_path(value):
            return (isinstance(value,str) and not any(_PRIVATE_PATTERN.fullmatch(p.lower())
                    for p in Path(value).parts))
        for key in ('path','current_project_root','requested_workdir'):
            if public_path(result.get(key)): view[key]=summary(result[key],1024)
        if isinstance(result.get('path_samples'),list):
            view['path_samples']=[summary(p,1024) for p in result['path_samples'][:3] if public_path(p)]
        if isinstance(result.get('capability_missing'),dict):
            # Projection-stage failures have no attempt yet. Apply exactly the
            # same disclosure boundary as persisted prelaunch diagnostics.
            view['capability_missing']=DomainService._run_view(result['capability_missing'])
        return view

    @staticmethod
    def _permission_refusal(row):
        cancelled=row['status']=='cancelled'
        return {'status':'not_run','permission_status':row['status'],
                'error':'EXECUTION_PERMISSION_CANCELLED' if cancelled else 'EXECUTION_PERMISSION_DENIED',
                'execution_started':False,'terminal':True,
                'reason':row.get('reason','unknown'), 'source':row.get('source','unknown')}

    def _permission(self,call_id,payload):
        self._ensure_request()
        return permissions.request_permission(self.data_root,self.task_id,str(call_id),payload)

    def _claim_or_result(self,row,call_id):
        if row.get('result'): return None,{**row['result'],'replayed':True}
        if row.get('claim'): return None,{'status':'reconciliation_required','error':'CLAIM_OUTCOME_UNKNOWN','execution_started':None}
        if row['status']=='pending': return None,self._waiting(row)
        if row['status']!='allowed': return None,self._permission_refusal(row)
        return permissions.claim_permission(self.data_root,self.task_id,row,str(call_id)),None

    def skills(self):
        if self._skills is None: self._skills=SkillRuntimeRegistry(self.skills_root)
        return self._skills

    def wiki(self):
        if not self.history_enabled: raise DomainError('HISTORY_DISABLED')
        if self.memory_root is None: raise DomainError('MEMORY_ROOT_NOT_CONFIGURED')
        if self._wiki is None:
            self._wiki=WikiMemoryManager(None,self.memory_root,wiki_dir=self.wiki_root,
                default_project_id=self.project_id,embedding_config={'enabled':False})
            # Reuse current scope resolution with only its storage/context inputs.
            from agent_core.memory_tools import scope_for
            facade=SimpleNamespace(data_path=self.data_root,command_service=self.commands,wiki_memory_manager=self._wiki)
            scope_for(facade,self.task_id)
        return self._wiki

    @property
    def project_id(self):
        existing=(self._state().get('task_state',{}).get('memory_scope') or {}).get('project_id')
        if existing:return existing
        return ('task-'+hashlib.sha256(self.task_id.encode()).hexdigest()[:24] if self.cwd==self.install_root
                else 'project-'+hashlib.sha256(str(self.cwd).encode()).hexdigest()[:24])

    def requested_scope(self,value):
        return self.project_id if value in (None,'','current') else 'default' if value=='unassigned' else str(value)

    def archive(self):
        if self.archive_path is None:raise DomainError('ARCHIVE_PATH_NOT_CONFIGURED')
        if not self.archive_path.is_file():raise DomainError('ARCHIVE_NOT_FOUND')
        if self._archive is None:
            from memory.session_archive import SessionArchive
            self._archive=SessionArchive(self.archive_path)
        return self._archive

    def source_record(self,ref,scope,*,historical=False):
        if ref.startswith('wiki:'):return self.wiki().page_record(ref[5:],scope,include_inactive=historical)
        if ref in self._public_sources:return self._public_sources[ref]
        if ref.startswith('session_archive:'):
            from memory.session_archive import ArchiveHit
            with self.archive()._connect() as conn:
                row=conn.execute('SELECT * FROM session_events WHERE id=? AND project_id=?',(int(ref.split(':')[-1]),scope)).fetchone()
            if row is None:return None
            hit=ArchiveHit.from_row(row)
            return {'source_ref':ref,'task_id':hit.task_id,'project_id':hit.project_id,'content':hit.content,
                    'content_hash':hit.content_hash,'status':'historical','absolute_path':str(self.archive_path)}
        return None

    def core(self):
        if self.memory_root is None: raise DomainError('MEMORY_ROOT_NOT_CONFIGURED')
        if self._core is None: self._core=CoreMemoryStore(self.memory_root,default_project_id=self.project_id)
        return self._core

    def preference(self):
        preferred=self.preferences_path
        selected=preferred
        try: preferred.stat()
        except FileNotFoundError:
            if self.memory_root is not None: selected=self.memory_root/'USER_PROFILE.md'
        except OSError: pass
        snapshot=file_snapshot(selected,limit=12000,source_kind='explicit_preference' if selected==preferred else 'core_fallback',
            selection_reason='preferred_file' if selected==preferred else 'preferred_missing')
        snapshot.update(preferred_path=str(preferred),
            source=self.preferences_source if selected==preferred else 'core_fallback',
            exists=snapshot['status']!='missing', empty=snapshot['status']=='empty')
        return snapshot

    def context_snapshot(self):
        revoked=[]
        if self.history_enabled and self.memory_root is not None:self.wiki()
        if self.history_enabled and self._seen_sources:
            w=self.wiki()
            for ref,row in self._seen_sources.items():
                page=self.source_record(ref,row['scope'])
                if not page or page.get('content_hash')!=row['content_hash'] or ref in w.excluded_sources(row['scope']): revoked.append(ref)
        elif not self.history_enabled: revoked=list(self._seen_sources)
        # These are observational references, never permits or approval inputs.
        # Reuse the permission authority's scope/policy/cancellation/finish check
        # on one loaded state; do not expose stored payloads, claims or tokens.
        state=self._state(); authorization_refs=[]
        requests=((state.get('task_state') or {}).get('execution_permissions') or {}).get('requests') or {}
        for row in requests.values():
            if row.get('source')=='human' and row.get('status')=='allowed' and permissions._active(state,row):
                authorization_refs.append({key:deepcopy(row[key]) for key in
                    ('request_id','action_hash','request_scope','status','source')})
        value={'preferences':self.preference(),'history_enabled':self.history_enabled,
            'approval_mode':deepcopy(state.get('task_state',{}).get('execution_permissions',{}).get('automation',{'mode':'manual'})),
            'request_paused':bool(state.get('task_state',{}).get('pi_pause')),
            'authorization_refs':authorization_refs,
            'archive_enabled':self.archive_path is not None,
            'history_sources':[r for r in self._seen_sources if r not in revoked], 'revoked_sources':revoked,
            'diagnostics':[],'project_id':self.project_id,
            'registry_path':str(self.registry.path),
            'memory_mode':self.memory_mode,'memory_root':str(self.memory_root) if self.memory_root else None,
            'maintenance':{'enabled':bool(self.history_enabled and self.memory_root and self.memory_mode!='disabled'),
                           'mode':self.memory_mode,'optional':True,'model_owner':'Pi','python_model_calls':False}}
        value['revision']=digest(without_times(value))
        return value

    def _sources(self,rows,scope):
        for row in rows:
            ref=row.get('source_ref')
            if ref: self._seen_sources[ref]={'scope':row.get('project_id') or scope,'content_hash':row.get('content_hash','')}
        return [row['source_ref'] for row in rows if row.get('source_ref')]

    def _call_id(self,params):
        value=params.get('call_id')
        if not isinstance(value,str) or not value or len(value)>500: raise DomainError('CALL_ID_REQUIRED')
        return value

    def _registry_sources(self):
        return {'environment_registry':str(self.registry.path),'source_type':'software_registry'}

    def _binding_diagnostic(self,binding_ref):
        state=self._state();row=(state.get('task_state',{}).get('runtime_invalidations') or {}).get(binding_ref)
        if row and row.get('request_scope')==request_scope(state):return deepcopy(row)
        return None

    def select_environment(self,requirements,skill_id='',env_name='',*,loaded_skills=None,runtime_scope=''):
        self._ensure_request()
        if isinstance(requirements,list) and all(isinstance(r,str) for r in requirements):requirements={'packages':requirements}
        if not isinstance(requirements,dict):raise DomainError('ENV_REQUIREMENTS_MUST_BE_OBJECT_OR_PACKAGE_LIST')
        from agent_core.runtime_evidence import registered_execution_binding
        if not skill_id:
            try:
                bound=registered_execution_binding(self._state(),runtime_scope=runtime_scope,
                    requirements={'env':env_name} if env_name else {})
            except ValueError as exc:
                if not requirements and not env_name:
                    return {'availability':'UNKNOWN','reason_code':str(exc),'install_required':False,
                            'registration_sources':self._registry_sources()}
                bound=None
            if bound and (not requirements or requirements==bound.get('requirements')):
                return {'availability':'AVAILABLE','decision':'binding_reused','binding_ref':bound['binding_ref'],
                        'binding_scope':bound['scope'],'invocation':bound['launch'],'install_required':False,
                        'healthcheck_status':'not_run','registration_sources':self._registry_sources(),
                        'runtime_diagnostic':self._binding_diagnostic(bound['binding_ref'])}
        self.registry.reload()
        identity=self.skills().resolve(skill_id,loaded_skills=loaded_skills) if skill_id else None
        if identity and identity['status']=='ambiguous':
            return {'decision':'registry_unresolved','availability':'UNKNOWN','selected_candidate':{},
                    'candidates':identity.get('candidates',[]),'install_required':False,
                    'reason_code':identity['reason_code'],'skill_resolution':identity,
                    'registration_sources':self._registry_sources()}
        record=(identity.get('record') or {}) if identity else {}
        selected=registered_runtime_reuse(self.registry,requirements,skill_record=record,target_env=env_name,
            trusted_backend='local',trusted_host=os.uname().nodename)
        if identity:selected['skill_resolution']={key:value for key,value in identity.items() if key!='record'}
        selected['registration_sources']={**self._registry_sources(),
            **({'skill_registry':str(self.skills_root/'.skill_runtime_registry.json')} if record else {})}
        if selected.get('availability')!='AVAILABLE':return selected
        candidate=selected['selected_candidate'];audit=deepcopy(candidate['audit'])
        launch=deepcopy(candidate['launch_description'])
        selected['invocation']={**launch,'healthcheck':'not_run','vantage':'registry'}
        sid=runtime_scope or (identity.get('skill_id') if identity else None) or skill_id or 'registered-environment:'+audit['env']
        audit['skill_id']=sid;candidate['audit']=audit
        invalid=(self._state().get('task_state',{}).get('runtime_invalidations') or {}).get(sid)
        if invalid and invalid.get('prefix')==candidate.get('prefix'):
            selected['historical_execution_error']=deepcopy(invalid)
        from agent_core.runtime_evidence import make_runtime_evidence,merge_runtime_evidence,runtime_evidence_state,digest as evidence_digest
        contract=evidence_digest({'requirements':requirements,'registration':record})
        scope={'skill_id':sid,'skill_document_digest':evidence_digest(record),'skill_contract_digest':contract,
               'environment_id':audit['env'],'environment_fingerprint':evidence_digest(candidate['source_ref']),
               'host_id':os.uname().nodename,'inspection_contract_digest':contract}
        evidence=make_runtime_evidence(audit,task_id=self.task_id,skill_scope=scope,
            observed_at=datetime.now(timezone.utc).isoformat(),producer='registered_runtime',
            source_ref=candidate['source_ref'],coverage=candidate['coverage'],inspection_status='not_run')
        request=self.scope()
        def update(state):
            if request_scope(state)!=request:raise DomainError('RUNTIME_REQUEST_CHANGED')
            task=state.setdefault('task_state',{});old=task.get('runtime_evidence') or {}
            existing=next((r for r in old.get('records',[]) if r.get('source_ref')==evidence['source_ref']
                and r.get('scope')==scope and r.get('acceptance',{}).get('request_scope')==request),None)
            if existing:return existing['evidence_id']
            records=merge_runtime_evidence(old.get('records',[]),[evidence],task_id=self.task_id,
                request_scope=request,launch_description=launch,requirements=requirements)
            task['runtime_evidence']=runtime_evidence_state(records,prior_revision=old.get('revision',0))
            return evidence['evidence_id']
        selected['binding_ref']=mutate(self.data_root,self.task_id,update)
        selected['binding_scope']={'request_scope':request,'runtime_scope':sid}
        diagnostic=self._binding_diagnostic(selected['binding_ref'])
        if diagnostic:selected['runtime_diagnostic']=diagnostic
        return selected

    def _prepare_binding(self,arguments,*,shell='/bin/sh',base_path=None,explicit_only=False):
        """Resolve request evidence, then interpret its launch once for both entries."""
        from agent_core.runtime_evidence import registered_execution_binding
        from agent_core.runtime_launch import prepare_runtime_launch
        original=deepcopy(arguments)
        ref=original.pop('binding_ref','') or '';scope=original.pop('runtime_scope','') or ''
        requirements=original.pop('requirements',{}) or {};env_name=original.pop('env_name','') or ''
        if not isinstance(requirements,dict):raise DomainError('ENV_REQUIREMENTS_MUST_BE_OBJECT')
        if env_name and requirements.get('env') not in (None,'',env_name):raise DomainError('REGISTERED_RUNTIME_TARGET_CONFLICT')
        if env_name:requirements={**requirements,'env':env_name}
        if explicit_only and not ref:raise DomainError('RUNTIME_BINDING_REF_REQUIRED')
        if not ref and env_name:
            choice=self.select_environment(requirements,env_name=env_name,runtime_scope=scope)
            if choice.get('availability')!='AVAILABLE':
                return original,None,choice
            ref=choice['binding_ref']
        bound=registered_execution_binding(self._state(),binding_ref=ref,runtime_scope=scope,requirements=requirements)
        if bound is None and requirements and not explicit_only:
            choice=self.select_environment(requirements,runtime_scope=scope)
            if choice.get('availability')!='AVAILABLE':return original,None,choice
            bound=registered_execution_binding(self._state(),binding_ref=choice['binding_ref'],requirements=requirements)
        if bound is None:return original,None,None
        command=original.get('argv') or original.get('cmd')
        launch=prepare_runtime_launch(bound['launch'],command,
            base_env={'PATH':base_path or os.environ.get('PATH','/usr/bin:/bin'),'LC_ALL':'C.UTF-8'},
            extra_env=original.get('env') or {},shell=shell)
        original.pop('cmd',None);original.pop('argv',None)
        original['argv' if isinstance(launch['command'],list) else 'cmd']=launch['command']
        original['env']=launch['env']
        original['_runtime_binding']={'binding_ref':bound['binding_ref'],'scope':bound['scope'],'launch':launch['launch']}
        return original,original['_runtime_binding'],None

    def launch_environment(self,p):
        self._ensure_request()
        if self.execution_profile!='native':raise DomainError('NATIVE_BASH_REQUIRES_NATIVE_PROFILE')
        if set(p)-{'binding_ref','command','shell','base_path'}:raise DomainError('ENVIRONMENT_LAUNCH_FIELDS_INVALID')
        import shlex
        prepared,binding,unavailable=self._prepare_binding({'cmd':p.get('command'),'binding_ref':p.get('binding_ref')},
            shell=p.get('shell') or '/bin/bash',base_path=p.get('base_path'),explicit_only=True)
        command=prepared.get('argv') or prepared.get('cmd')
        return {'command':shlex.join(command) if isinstance(command,list) else command,'command_argv':command if isinstance(command,list) else None,
                'env':prepared.get('env',{}),'launch':binding,'healthcheck_status':'not_run',
                'registration_sources':self._registry_sources()}

    def register_environment(self,p,*,observation=None):
        """Metadata only; model declarations cannot mint execution evidence."""
        self._ensure_request()
        allowed={'call_id','env_name','prefix','tools','packages','capabilities','entries','env_runner','runner_argv',
                 'runtime_env','source_note','evidence_refs','language','versions'}
        if set(p)-allowed:raise DomainError('ENVIRONMENT_REGISTRATION_FIELDS_INVALID')
        name=p.get('env_name');prefix=p.get('prefix')
        if not isinstance(name,str) or not name or not isinstance(prefix,str) or not Path(prefix).is_absolute():
            raise DomainError('REGISTRATION_IDENTITY_REQUIRED')
        from package_manager import PackageManager,RegistryError
        source=self._registry_sources();call=self._call_id(p)
        if self.execution_profile=='protected':
            permissions.prepare_path_access(self.commands,self.task_id,
                [{'path':str(self.registry.path),'access':'read_write','target_kind':'file','recursive':False}],
                call_id='registration:'+call,purpose='补登记现有软件的元数据，不安装软件')
        tools=p.get('tools') or []
        if not isinstance(tools,list) or any(not isinstance(t,str) or not t or Path(t).name!=t or t in {'.','..'} for t in tools):
            raise DomainError('REGISTRATION_TOOLS_INVALID')
        prefix=str(Path(prefix).resolve())
        entries={tool:str(Path(prefix)/'bin'/tool) for tool in tools};entries.update(p.get('entries') or {})
        if any(not isinstance(v,str) or not Path(v).is_absolute() for v in entries.values()):raise DomainError('REGISTRATION_ENTRY_INVALID')
        evidence={'source':'registration_declaration','note':str(p.get('source_note') or ''),
                  'evidence_refs':p.get('evidence_refs') or [],'strength':'declared_not_verified'}
        record={k:deepcopy(p[k]) for k in ('packages','capabilities','runtime_env','env_runner','runner_argv','language','versions') if k in p}
        record.update(prefix=prefix,tools=tools,entries=entries,status='registered',backend='local',host=os.uname().nodename,
                      evidence=evidence)
        if observation is not None:
            required={'source_ref','observed_at','session_id','request_id','checks'}
            if not isinstance(observation,dict) or not required<=set(observation):raise DomainError('HOST_OBSERVATION_INCOMPLETE')
            # The host alone supplies bounded, previously checked receipts. This
            # timestamp is evidence time, never upgraded to the registration time.
            datetime.fromisoformat(observation['observed_at'].replace('Z','+00:00'))
            record['evidence']={**deepcopy(observation),'source':'historical_execution_receipt','strength':'historically_observed'}
            record['last_verified_at']=observation['observed_at']
            record['capability_evidence']={key:{'status':'historically_observed','source_ref':observation['source_ref'],
                'observed_at':observation['observed_at']} for key in observation['checks']}
        try:
            pm=PackageManager.for_registry(self.registry.path)
            before=pm.get_environment(name)
            pm.register_environment(name,record=record)
            main=deepcopy(pm.last_registry_receipt);derived=[]
            for tool in tools:
                try:
                    pm.register_existing_tool(tool,name,prefix=prefix,generate_shim=False,
                        record={'source_executable':entries[tool]},evidence=record['evidence'])
                    derived.append({'tool':tool,**deepcopy(pm.last_registry_receipt)})
                except (OSError,ValueError) as exc:
                    derived.append({'tool':tool,'committed':False,'error':str(exc)})
            self.registry.reload()
            changed=main.get('changed') or any(d.get('changed') for d in derived)
            return {'status':'partial' if any(not d.get('committed') for d in derived) else 'registered',
                    'operation':'created' if not before else 'updated' if changed else 'unchanged','env_name':name,
                    'registration_sources':source,'registration_receipt':main,'tool_mappings':derived,
                    'skill_mapping':{'status':'not_requested'},'shim':{'status':'not_required'},'healthcheck_status':'not_run',
                    'installation_status':'not_run','record':deepcopy(self.registry.registry['environments'][name])}
        except (RegistryError,OSError,ValueError) as exc:
            return {'status':'registration_failed','error':str(exc),'registration_sources':source,
                    'installation_status':'not_run','healthcheck_status':'not_run'}

    async def locate_environment(self,p):
        self._ensure_request()
        if set(p)-{'requirements'}:raise DomainError('ENVIRONMENT_LOOKUP_FIELDS_INVALID')
        requirements=p.get('requirements') or {};key=digest(requirements);scope=self.scope()
        previous=self._state().get('task_state',{}).get('runtime_lookup_attempts',{}).get(key)
        if previous and previous.get('request_scope')==scope:return {**previous['result'],'replayed':True}
        started=time.monotonic()
        try:
            from .registry import bounded_location_lookup
            result=await bounded_location_lookup(self,requirements)
        except asyncio.TimeoutError:
            result={'status':'timed_out','cleanup':'cleanup_unknown','reason':'metadata lookup did not return a cleanup receipt'}
        except (OSError,ValueError) as exc:result={'status':'lookup_failed','error':str(exc)}
        result={**result,'elapsed_seconds':round(time.monotonic()-started,4),'budget':{'max_candidates':3,'item_seconds':5,'total_seconds':15},
                'registration_sources':self._registry_sources(),'install_required':False,'healthcheck_status':'not_run'}
        if len(json.dumps(result).encode())>8192:
            result={'status':'incomplete','reason':'LOOKUP_SUMMARY_LIMIT','max_bytes':8192,'registration_sources':self._registry_sources(),'install_required':False}
        mutate(self.data_root,self.task_id,lambda state:state.setdefault('task_state',{}).setdefault('runtime_lookup_attempts',{}).update({key:{'request_scope':scope,'result':result}}))
        return result

    def execution_record(self,p):
        """Explicit bounded projection of this request's existing execution record.

        No accepted/Commit dependency, directory listing, new record or replay.
        The observer remains the authority for run identity and request scope.
        """
        kind=p.get('record')
        if kind not in {'command','script'}:raise DomainError('RUN_RECORD_KIND_INVALID')
        offset,limit=p.get('offset',0),p.get('max_chars',6000)
        index=p.get('script_index',0)
        if (type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=20000
                or type(index) is not int or index<0):raise DomainError('RUN_RECORD_RANGE_INVALID')
        owner,saved=self.formal._lookup(p['run_ref'],self.task_id,self.scope())
        result=owner.result if owner else saved
        if not result or not result.get('terminal'):
            return {'status':'unavailable','reason':'EXECUTION_RECORD_NOT_TERMINAL','kind':kind}
        if not result.get('cmd'):
            return {'status':'missing','reason':'EXECUTION_COMMAND_NOT_RECORDED','kind':kind}
        files=result.get('frozen_files') or []
        metadata={'kind':kind,'run_ref':p['run_ref'],'status':'available',
            'execution_status':result.get('status'),'execution_started':result.get('execution_started'),
            'exit_code':result.get('exit_code'),'scientific_validation':'not_assessed'}
        if kind=='command':
            # Do not return env, approval objects, host tokens or unrelated state.
            body=json.dumps({'cmd':result['cmd'],'original_cmd':result.get('original_cmd'),
                'argv':result.get('argv'),'cwd':result.get('cwd'),
                'environment_prefix':(result.get('env') or {}).get('ENV_PREFIX') or (result.get('env') or {}).get('CONDA_PREFIX'),
                'scripts':[{'index':i,'original_path':row.get('original_path'),'sha256':row.get('sha256')}
                           for i,row in enumerate(files)]},ensure_ascii=False)
            metadata['source_ref']=result.get('receipt_ref')
        else:
            if index>=len(files):return {**metadata,'status':'missing','reason':'FROZEN_SCRIPT_NOT_RECORDED'}
            row=files[index]
            directory=self.formal._task_dir(self.task_id)/p['run_ref']
            try:
                relative=Path(row['path']).relative_to(directory)
                if '..' in relative.parts:raise ValueError('RUN_SCRIPT_PATH_INVALID')
                # Walk only the recorded frozen file, refusing parent symlinks;
                # ordinary file tools keep their protected-directory boundary.
                fds=[os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)]
                try:
                    for part in relative.parts[:-1]:
                        fds.append(os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fds[-1]))
                    fd=os.open(relative.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fds[-1]);fds.append(fd)
                    info=os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('RUN_SCRIPT_IDENTITY_INVALID')
                    if info.st_size>1024*1024:return {**metadata,'status':'unavailable','reason':'RUN_SCRIPT_EXCEEDS_READ_BOUND','bytes':info.st_size}
                    with os.fdopen(os.dup(fd),'rb') as stream:data=stream.read(1024*1024+1)
                    if len(data)>1024*1024 or hashlib.sha256(data).hexdigest()!=row.get('sha256'):
                        raise ValueError('RUN_SCRIPT_CHANGED')
                finally:
                    for fd in reversed(fds):os.close(fd)
                body=data.decode('utf-8')
            except (OSError,ValueError,KeyError,UnicodeError) as error:
                return {**metadata,'status':'unavailable','reason':str(error) if isinstance(error,ValueError) else type(error).__name__}
            metadata.update(source_ref=row['path'],original_path=row.get('original_path'),source_sha256=row['sha256'],script_index=index)
        # Sanitize string values before JSON encoding; regex redaction over
        # serialized JSON can consume escapes and corrupt command records.
        shown=json.dumps(sanitize_value(json.loads(body)),ensure_ascii=False) if kind=='command' else redact_secrets(body)
        if offset>len(shown):raise DomainError('RUN_RECORD_OFFSET_OUT_OF_RANGE')
        end=min(len(shown),offset+limit)
        return {**metadata,'content':shown[offset:end],'offset':offset,'next_offset':end if end<len(shown) else None,
                'total_chars':len(shown),'truncated':end<len(shown),'redacted':shown!=body}

    def _reserve_diagnostic_budget(self,call_id,arguments):
        """Small request admission budget; process ownership stays with observer.

        Reserve worst-case time so concurrent calls/reconnects cannot overrun the
        total. A short/failed check does not silently authorize more discovery.
        Explicit user checks use the ordinary probe path, outside this budget.
        """
        budget=arguments['timeout_ms'];signature=digest(arguments)
        def update(state):
            task=state['task_state'];identity=task['pi_request_id']
            book=task.get('pi_diagnostic_budget') or {}
            if book.get('request_id')!=identity:book={'request_id':identity,'calls':{}}
            prior=book['calls'].get(call_id)
            if prior:
                if prior['signature']!=signature:raise DomainError('DIAGNOSTIC_CALL_CONFLICT')
                return
            reserved=sum(row['timeout_ms'] for row in book['calls'].values())
            if reserved+budget>30000:raise DomainError('ENVIRONMENT_DIAGNOSTIC_BUDGET_EXHAUSTED')
            book['calls'][call_id]={'signature':signature,'timeout_ms':budget}
            task['pi_diagnostic_budget']=book
        mutate(self.data_root,self.task_id,update)

    async def _exec_start(self,p,*,probe=False):
        self._ensure_request(); call_id=self._call_id(p)
        original=deepcopy(p.get('arguments') or {})
        for field in ('approved','source','permit','human_required','host_token','access_projection','read_access','grants','mounts','_biomind_probe','profile','execution_profile','disable_sandbox','grant','session','session_id','roots','policy_revision','trusted_roots','_runtime_binding'):
            if field in original or field in p: raise DomainError('MODEL_AUTHORITY_FIELD_FORBIDDEN')
        # Pi's tool description is presentation metadata, not an executable
        # argument supported by the existing strict formal action contract.
        original.pop('description',None)
        diagnostic=original.pop('environment_diagnostic',False)
        if type(diagnostic) is not bool or (diagnostic and not probe):
            raise DomainError('ENVIRONMENT_DIAGNOSTIC_REQUIRES_PROBE')
        try:
            original,binding,unavailable=self._prepare_binding(original)
        except ValueError as exc:
            return {'status':'blocked','error':str(exc),'execution_started':False,'registration_sources':self._registry_sources()}
        if unavailable:
            return {'status':'blocked','error':'REGISTERED_ENVIRONMENT_UNAVAILABLE','selection':unavailable,'execution_started':False}
        if probe:
            budget=original.get('timeout_ms',15000)
            if type(budget) is not int or not 1<=budget<=300000:raise DomainError('PROBE_TIMEOUT_INVALID')
            original.update(_biomind_probe=True,timeout_ms=budget,max_output_chars=8000)
            if diagnostic and budget>15000:raise DomainError('ENVIRONMENT_DIAGNOSTIC_SINGLE_BUDGET_EXCEEDED')
        arguments=original
        if self.execution_profile == 'native':
            # Select before any legacy permission payload, projection or output ACL.
            async with self._dispatch_lock:
                if diagnostic:self._reserve_diagnostic_budget(call_id,arguments)
                result=self._run_view(await self.formal.start(arguments,task_id=self.task_id,stage='pi_native_probe' if probe else 'pi_native',request_scope=self.scope(),call_id=call_id,yield_time_ms=50))
                return {**result,'vantage':'host','profile':'native','execution_profile':'native'}
        command_id=permissions.derive_permission_call_id(call_id,'command')
        row=self._permission(command_id,permissions.command_payload(self.commands,arguments,self.task_id,call_id=command_id))
        if row['status']=='pending': return self._waiting(row)
        if row['status']!='allowed': return self._permission_refusal(row)
        # Owner reservation itself is short. Long work lives in the real observer.
        async with self._dispatch_lock:
            if diagnostic:self._reserve_diagnostic_budget(call_id,arguments)
            result=self._run_view(await self.formal.start(arguments,task_id=self.task_id,stage='pi_probe' if probe else 'pi_main',request_scope=self.scope(),call_id=call_id,yield_time_ms=50))
            return {**result,'vantage':'sandbox','profile':'consult-probe-ro-v1' if probe else 'formal-input-ro-v1'}

    async def call(self,method,p,*,host_token=None):
        # All access requests reuse the original approval object and host UI.
        # Never turn a pending read into a cached stat or a new business request.
        try:
            return await self._call(method,p,host_token=host_token)
        except permissions.PermissionPending as exc:
            return self._waiting(exc.request)
        except FormalProtectionError as exc:
            return self._run_view({**exc.diagnostic,'status':'stage_blocked',
                    'execution_started':False,'terminal':True,'error':exc.code,
                    'capability_missing':exc.diagnostic})

    async def _call(self,method,p,*,host_token=None):
        if not isinstance(p,dict): raise DomainError('PARAMS_MUST_BE_OBJECT')
        if any(key in p for key in ('execution_profile','disable_sandbox')):
            raise DomainError('MODEL_AUTHORITY_FIELD_FORBIDDEN')
        if method in HOST_METHODS: self._host(host_token)
        if method!='hello':self._state()  # Validate storage ownership before any host or model mutation.
        active_identity=p.get('active_request_id')
        if method=='archive.append' and not active_identity:raise DomainError('ACTIVE_REQUEST_ID_REQUIRED')
        if method not in {'hello','request.begin'} and active_identity is not None:
            current=self._state().get('task_state',{}).get('pi_request_id')
            if active_identity!=current:raise DomainError('STALE_ACTIVE_REQUEST')
        # Transport correlation is supplied by the host, never part of action hashes.
        p={k:v for k,v in p.items() if k!='active_request_id'}
        if method=='hello':
            return {'protocol':'biomind-domain-v1','session_id':self.session_id,'task_id':self.task_id,
                    'execution_profile':self.execution_profile,
                    'host_token':self.host_token,'methods':['request.begin','context.snapshot','environments.select',
                    'environments.locate','environments.register','environments.launch','environments.register_observed',
                    'exec.start','exec.status','exec.wait','exec.read_log','exec.cancel','approval.decide','files.read','files.write','files.stat',
                    'memory.search','memory.read','memory.write','install.propose','install.execute','settings.update',
                    'archive.append','archive.status','permissions.status','permissions.propose','permissions.mode','permissions.revoke']}
        if method=='request.begin':
            identity=str(p.get('request_id') or '')
            if not identity: raise DomainError('REQUEST_ID_REQUIRED')
            if p.get('role','main') not in {'main','consult','generate'}: raise DomainError('LEGACY_ROLE_INVALID')
            def update(state):
                task=state.setdefault('task_state',{})
                if 'boundary_id' in p and task.get('pi_request_id')!=identity:
                    raise DomainError('CONTEXT_REQUEST_OWNER_CHANGED')
                if 'boundary_id' in p and p.get('user_revision',1)!=task.get('pi_user_revision',1):
                    raise DomainError('CONTEXT_USER_REVISION_CHANGED')
                if task.get('pi_request_id')!=identity:
                    if self.active_jobs() or self.formal.active_runs(self.task_id,None): raise DomainError('ACTIVE_JOB_PREVENTS_NEW_REQUEST')
                    state['authoritative_user_turn_revision']=int(state.get('authoritative_user_turn_revision') or 0)+1
                    task['current_request_key']=identity;task['pi_request_id']=identity
                    task.pop('request_finish',None);task['status']='running'
                    task.pop('pi_pause',None)
                    task.pop('pi_profile_requires_new_request',None)
                    mode=task.get('execution_permissions',{}).get('automation',{})
                    if mode.get('lifetime')!='session':mode.update(mode='manual')
                    task['pi_user_revision']=1
                elif task.get('pi_pause'):
                    raise DomainError('REQUEST_PAUSED_EXPLICIT_RESUME_REQUIRED')
                task['intent_admission']={'request_kind':'work',
                    'execution_scope':'simple','review_before_execution':False,'source':'pi_host_request'}
                if 'boundary_id' in p:
                    if not isinstance(p['boundary_id'],str) or not p['boundary_id']:raise DomainError('BOUNDARY_ID_REQUIRED')
                    task['pi_context_boundary']={'id':p['boundary_id'],'kind':'task',
                        'user_revision':p.get('user_revision',1)}
                task['pi_session_id']=self.session_id
                permissions._permissions(state).setdefault('owner',{'task_id':self.task_id,
                    'session_id':self.session_id,'project':str(self.cwd),'storage':str(self.data_root)})
                return {'status':'ready','request_scope':request_scope(state),'task_id':self.task_id}
            return mutate(self.data_root,self.task_id,update)
        if method=='request.status':
            task=self._state().get('task_state',{})
            return {'request_id':task.get('pi_request_id'),'paused':task.get('pi_pause'),
                    'execution_profile':self.execution_profile,'requires_new_request':bool(task.get('pi_profile_requires_new_request')),
                    'revision':task.get('pi_user_revision',1),'agent':'main',
                    'request_scope':self.scope(),'approval_mode':task.get('execution_permissions',{}).get('automation',{'mode':'manual'})}
        if method=='request.pause':return self._pause_request(p.get('reason') or 'host_pause')
        if method in {'request.resume','request.revise'}:
            if not self._state().get('task_state',{}).get('pi_request_id'):raise DomainError('REQUEST_BEGIN_REQUIRED')
            def update(state):
                task=state.setdefault('task_state',{})
                if method=='request.resume':
                    if task.get('pi_profile_requires_new_request'):
                        raise DomainError('PROFILE_MIGRATION_REQUIRES_NEW_REQUEST')
                    task.pop('pi_pause',None)
                    task['pi_resume_revision']=int(task.get('pi_resume_revision',0))+1
                    # A resume never restores cancelled permits or automatic mode.
                    task.get('execution_permissions',{}).get('automation',{}).update(mode='manual')
                else:
                    task['pi_user_revision']=int(task.get('pi_user_revision',1))+1
                return {'status':'ready','request_id':task['pi_request_id'],'revision':task.get('pi_user_revision',1)}
            return mutate(self.data_root,self.task_id,update)
        if method=='approval.decide':
            row=permissions.permission_state(self.data_root,self.task_id,str(p['request_id']))
            if p.get('action_hash')!=row['action_hash'] or p.get('request_scope')!=row['request_scope'] or self.scope()!=row['request_scope']:
                raise DomainError('STALE_PERMISSION_REPLY')
            choice=p.get('choice')
            if choice=='cancel':
                updated=permissions.cancel_permission(self.data_root,self.task_id,row['request_id'],
                    source='human',reason='host_cancel_decision')
                self._pause_request('approval_cancelled')
                return {'status':updated['status'],'approval':self._approval(updated),'request_paused':True}
            if choice not in {'y','n','edit','scope','session','read_only','read_session','directory_session'}: return {'status':'pending','approved':False,'approval':self._approval(row)}
            replacement=p.get('replacement') or ''
            if isinstance(replacement,dict):replacement=replacement.get('cmd') or ''
            if not isinstance(replacement,str):raise DomainError('EDIT_REPLACEMENT_MUST_BE_COMMAND_TEXT')
            decision=permissions._Decision(row['request_id'],row['action_hash'],choice,permissions._SEAL,replacement,p.get('selections'))
            updated=permissions.record_human_decision(self.data_root,self.task_id,decision)
            if choice=='n':self._pause_request('approval_denied')
            return {'status':'waiting_approval' if updated['status']=='pending' else updated['status'],
                    'approval':self._approval(updated),'request_paused':choice=='n'}
        if method=='context.snapshot': return self.context_snapshot()
        if method=='permissions.status':
            state=self._state()
            if p.get('approval_id'):
                row=permissions.permission_state(self.data_root,self.task_id,str(p['approval_id']))
                if row['request_scope']!=self.scope():raise DomainError('STALE_PERMISSION_REPLY')
                return {'status':row['status'],'approval':self._approval(row)}
            return {**permissions.read_permission_status(self.commands,self.task_id),
                    'paused':state.get('task_state',{}).get('pi_pause'),
                    'running_jobs':[o.run_ref for o in self.formal._owners.values() if o.task is not None and not o.task.done()]}
        if method=='permissions.propose':
            self._ensure_request()
            if set(p)-{'paths','purpose','lifetime','call_id'}:raise DomainError('PATH_PROPOSAL_FIELDS_INVALID')
            payload=permissions.path_access_payload(self.commands,self.task_id,p.get('paths'),
                purpose=p.get('purpose',''),lifetime=p.get('lifetime','request'))
            row=self._permission(permissions.derive_permission_call_id(self._call_id(p),'path_access'),payload)
            if row['status']=='pending':return self._waiting(row)
            if row['status']!='allowed':return self._permission_refusal(row)
            status=permissions.read_permission_status(self.commands,self.task_id)
            return {'status':'completed','grants':[g for g in status['grants'] if g['grant_id'] in row.get('granted_ids',[])],
                    'project_root':str(self.cwd),'approval_mode_unchanged':True,'execution_started':False}
        if method=='permissions.mode':
            if not self._state().get('task_state',{}).get('pi_request_id'):raise DomainError('REQUEST_BEGIN_REQUIRED')
            if p.get('mode')=='auto' and self._state().get('task_state',{}).get('pi_pause'):raise DomainError('REQUEST_PAUSED')
            if set(p)-{'mode','lifetime'}:raise DomainError('APPROVAL_MODE_FIELDS_INVALID')
            return permissions.set_automatic_mode(self.commands,self.task_id,p.get('mode'),lifetime=p.get('lifetime','request'))
        if method=='permissions.revoke':
            if not self._state().get('task_state',{}).get('pi_request_id'):raise DomainError('REQUEST_BEGIN_REQUIRED')
            if (not isinstance(p.get('grant_id'),str) or not p['grant_id'] or
                    not isinstance(p.get('request_scope'),dict) or
                    not isinstance(p.get('policy_revision'),str) or not p['policy_revision']):
                raise DomainError('REVOKE_IDENTITY_AND_CAS_REQUIRED')
            return permissions.revoke_read_grant(self.commands,self.task_id,p['grant_id'],
                expected_scope=p.get('request_scope'),expected_policy_revision=p.get('policy_revision'))
        if method in {'archive.append','archive.status'}:
            from .archive_observation import append,status
            return append(self,p) if method=='archive.append' else status(self)
        if method=='session.lifecycle':
            runs=[o['run_ref'] for o in self.formal.active_runs(self.task_id,None)]
            installs=[key for key,task in self._install_tasks.items() if not task.done()]
            return {'active_jobs':bool(runs or installs),'count':len(runs)+len(installs),'run_refs':runs,
                    'installation_ids':installs,'policy':'disconnect preserves workers; switching a live owner is blocked'}
        if method=='settings.update':
            if 'history_enabled' in p: self.history_enabled=bool(p['history_enabled'])
            if p.get('memory_mode') in {'ask','auto','manual','disabled'}: self.memory_mode=p['memory_mode']
            return self.context_snapshot()
        if method.startswith('memory.maintenance.'):
            from .maintenance import prepare,apply
            return prepare(self,p) if method=='memory.maintenance.prepare' else apply(self,p) if method=='memory.maintenance.apply' else {'status':'unsupported'}
        if method=='environments.select':
            if 'loaded_skills' in p:self._host(host_token)
            if set(p)-{'requirements','skill_id','env_name','loaded_skills','runtime_scope'}:raise DomainError('ENVIRONMENT_QUERY_FIELDS_INVALID')
            return self.select_environment(p.get('requirements') or {},p.get('skill_id') or '',p.get('env_name') or '',
                loaded_skills=p.get('loaded_skills'),runtime_scope=p.get('runtime_scope') or '')
        if method=='environments.launch':return self.launch_environment(p)
        if method=='environments.register':return self.register_environment(p)
        if method=='environments.register_observed':
            values=dict(p);observation=values.pop('observation',None)
            return self.register_environment(values,observation=observation)
        if method=='environments.locate':return await self.locate_environment(p)
        if method=='exec.start': return await self._exec_start(p)
        if method=='exec.probe': return await self._exec_start(p,probe=True)
        if method in {'exec.status','exec.wait','exec.read_log'}:
            args={k:p[k] for k in ('run_ref','after_cursor','max_wait_ms') if k in p}
            if method!='exec.wait': args['max_wait_ms']=0
            result=self._run_view(await self.formal.wait_command(args,task_id=self.task_id,request_scope=self.scope()))
            if method=='exec.status' and p.get('record') is not None:result['execution_record']=self.execution_record(p)
            return result
        if method=='exec.cancel':
            if str(p.get('run_ref','')).startswith('install:'):
                key=p['run_ref'][8:]
                if key not in self._install_cancels:raise DomainError('INSTALL_RUN_UNKNOWN')
                self._install_cancels[key].set()
                return {'status':'cancel_requested','run_ref':p['run_ref']}
            return self._run_view(await self.formal.stop_command({k:p[k] for k in ('run_ref','reason') if k in p},task_id=self.task_id,request_scope=self.scope()))
        if method=='request.cancel':
            for event in self._install_cancels.values():event.set()
            result=self.formal.cancel_request(self.task_id,self.scope(),str(p.get('reason') or 'user_cancel'))
            mutate(self.data_root,self.task_id,lambda s:s.setdefault('task_state',{}).update(status='cancelled'))
            permissions._mutate(self.data_root,self.task_id,permissions.suspend_automatic)
            return result
        if method=='files.read':
            self._ensure_request()
            if 'trusted_skill_dirs' in p:raise DomainError('HOST_SOURCE_FIELD_FORBIDDEN')
            args=dict(p)
            if 'offset' in args:args['offset_chars']=args.pop('offset')
            return self.files.read(args,task_id=self.task_id,call_id=p.get('call_id'))
        if method=='files.stat':
            self._ensure_request()
            if 'trusted_skill_dirs' in p:raise DomainError('HOST_SOURCE_FIELD_FORBIDDEN')
            return self.files.stat(p,task_id=self.task_id,call_id=p.get('call_id'))
        if method=='files.list':
            self._host(host_token)
            self._ensure_request()
            return list_directory(self, p)
        if method=='files.read_authorize':
            self._host(host_token)
            self._ensure_request()
            return self.files.stat(p,task_id=self.task_id,call_id=p.get('call_id'),
                trusted_skill_dirs=p.get('trusted_skill_dirs',()))
        if method=='files.mutation_snapshot':
            self._ensure_write_path(p)
            return self.files.mutation_snapshot(p,task_id=self.task_id)
        if method=='files.mutation_replay':
            call_id=self._call_id(p)
            key='permission-'+permissions.digest([self.scope(),call_id])[:32]
            try:
                row=permissions.permission_state(self.data_root,self.task_id,key)
            except KeyError:
                return {'status':'new'}
            arguments=row['payload'].get('arguments') or {}
            if (arguments.get('native_tool')!=p.get('native_tool') or
                    arguments.get('native_input')!=p.get('native_input')):
                raise DomainError('PERMISSION_ACTION_CHANGED')
            if row.get('result'):return {**row['result'],'replayed':True}
            if row.get('claim'):return {'status':'reconciliation_required','error':'FILE_CLAIM_OUTCOME_UNKNOWN'}
            if row['status'] not in {'pending','allowed'}:
                return self._permission_refusal(row)
            return {'status':'new'}
        if method=='files.write':
            self._ensure_write_path(p)
            call_id=self._call_id(p);args={k:v for k,v in p.items() if k!='call_id'}
            args.setdefault('expected_sha256','')
            row=self._permission(call_id,permissions.file_payload(self.files,'write_file',args,self.task_id))
            if row['status']=='pending': return self._waiting(row)
            result=await self.files.dispatch('write_file',args,task_id=self.task_id,call_id=call_id)
            # Internal CAS keeps its legacy interface; Pi recovery hints must
            # point at the actual model-visible native reader.
            if 'next_actions' in result:
                result={**result,'next_actions':['read' if action=='read_file' else action for action in result['next_actions']]}
            if result.get('error')=='FILE_CHANGED: read_file again before replacing existing content':
                result={**result,'error':'FILE_CHANGED: use read again before replacing existing content'}
            return result
        if method in {'memory.search','memory.read','memory.write'}: return await self._memory(method,p)
        if method in {'install.propose','install.execute'}: return await self._install(method,p)
        if method=='shutdown':
            if self.active_jobs(): return {'status':'blocked','error':'ACTIVE_JOB_PREVENTS_SHUTDOWN'}
            self._closed=True;return {'status':'closed'}
        raise DomainError('UNKNOWN_METHOD:'+str(method))

    def active_jobs(self):
        return [o.task for o in self.formal._owners.values() if o.task is not None and not o.task.done()]+[t for t in self._install_tasks.values() if not t.done()]

    async def disconnected(self):
        # EOF is a transport event, not cancellation authorization.
        # Pending decisions must never become permission on a later reconnect.
        if any(row.get('status')=='pending' and row.get('request_scope')==self.scope()
               for row in self._state().get('task_state',{}).get('execution_permissions',{}).get('requests',{}).values()):
            self._pause_request('approval_transport_disconnected')
        jobs=self.active_jobs()
        if jobs: await asyncio.gather(*jobs,return_exceptions=True)
        from .archive_observation import release
        await release(self)
        from agent_core.execution_profile import release_host_profile
        release_host_profile(self)

    async def _memory(self,method,p):
        if not self.history_enabled: return {'status':'disabled','reason':'history policy disabled'}
        w=self.wiki();scope=self.requested_scope(p.get('scope'))
        if method=='memory.search':
            source=p.get('source') or 'wiki';limit=max(1,min(24,int(p.get('limit') or 6)))
            if source=='projects': return {'status':'found','source':'projects',**w.discover_projects(p.get('query') or '',limit=limit,offset=p.get('offset') or 0)}
            if source=='wiki':rows=w.search(str(p.get('query') or ''),scope,top_k=limit)
            elif source=='current':rows=[r for r in self._public_sources.values() if str(p.get('query') or '').casefold() in r['content'].casefold()][:limit]
            elif source=='archive':
                rows=[{'source_ref':'session_archive:'+str(hit.id),'task_id':hit.task_id,'project_id':hit.project_id,
                    'text':redact_secrets(hit.summary or hit.content)[:1600],'content_hash':hit.content_hash,'status':'historical'}
                    for hit in self.archive().search(str(p.get('query') or ''),task_id=p.get('task_id'),project_id=scope,limit=limit)]
            else:raise DomainError('UNKNOWN_MEMORY_SOURCE')
            rows=[r for r in rows if r['source_ref'] not in w.excluded_sources(scope)]
            return {'status':'found' if rows else 'no_match','hits':rows,'sources':self._sources(rows,scope),'scope':scope}
        if method=='memory.read':
            ref=str(p['source_ref']);row=self.source_record(ref,scope,historical=bool(p.get('historical')))
            if not row or (ref in w.excluded_sources(scope) and not p.get('historical')): return {'status':'unavailable','source_ref':ref}
            body=redact_secrets(row.get('content',''))
            offset=max(0,int(p.get('offset') or 0));limit=min(24000,max(1,int(p.get('max_chars') or 6000)))
            excluded=w.excluded_sources(scope)
            return {**{k:v for k,v in row.items() if k not in {'content','text'}},'status':'read','content':body[offset:offset+limit],
                    'record_status':row.get('record_status') or row.get('status') or 'unknown',
                    'record_sources':[redact_secrets(source) for source in (row.get('sources') or []) if isinstance(source,str) and source not in excluded],
                    'sources':self._sources([row],scope),'scope':scope,'has_more':offset+limit<len(body),'next_offset':offset+limit}
        if self.memory_mode=='disabled': return {'status':'disabled'}
        call_id=self._call_id(p);payload={'kind':'memory_write','arguments':deepcopy(p),'scope':scope,'human_required':True}
        row=self._permission('memory:'+call_id,payload);permit,result=self._claim_or_result(row,call_id)
        if result is not None:return result
        if p.get('target')=='preference':
            selected=Path(self.preference()['source_path'])
            operation=p.get('operation') or 'append'
            result=self.core().update_profile(p.get('content',''),operation='append' if operation=='create' else operation,expected_hash=p.get('expected_hash'),path=selected)
        else:
            names={'create':'create_page','append':'append_section','replace':'replace_body','supersede':'update_frontmatter','forget':'update_frontmatter'}
            operation=p.get('operation');name=names.get(operation)
            if not name: raise DomainError('UNKNOWN_MEMORY_OPERATION')
            requested=p.get('source_refs') or []
            known=[ref for ref in requested if ref in self._seen_sources and ref not in self.context_snapshot()['revoked_sources']]
            op={'op':name,'path':p['path'],'title':p.get('title') or Path(p['path']).stem,'section':p.get('section') or 'Notes',
                'content':p.get('content',''),'sources':known,'status':'unverified','operation_id':row['request_id']}
            if p.get('expected_hash'):op['expected_hash']=p['expected_hash']
            if operation in {'forget','supersede'}:op['values']={'status':'forgotten' if operation=='forget' else 'superseded'}
            result=w.apply_patch_result({'operations':[op]},scope)
            result['unavailable_source_refs']=[ref for ref in requested if ref not in known]
        permissions.complete_permission(permit,result)
        return result

    async def _install(self,method,p):
        self._ensure_request()
        from agent_core.runtime_launch import installation_verification_checks, prepare_runtime_launch, RuntimeLaunchError
        call_id=self._call_id(p);args=deepcopy(p.get('arguments') or {})
        for field in ('approved','source','permit','host_token','access_projection','read_access','grants','mounts','execution_profile','profile','disable_sandbox','_biomind_probe'):
            if field in args or field in p:raise DomainError('MODEL_AUTHORITY_FIELD_FORBIDDEN')
        code=args.get('cmd')
        if not isinstance(code,str) or not code.strip():raise DomainError('INSTALL_COMMAND_REQUIRED')
        if not args.get('env_name'):raise DomainError('INSTALL_ENV_NAME_REQUIRED')
        access_args={'cmd':code,'workdir':str(self.cwd)}
        if args.get('prefix'):
            prefix_target=Path(args['prefix']).expanduser()
            if not prefix_target.is_absolute() or (self.execution_profile == 'protected' and not prefix_target.resolve().is_relative_to(self.cwd)):
                return {'status':'blocked','error':'INSTALL_PREFIX_OUTSIDE_AUTHORIZED_PROJECT','execution_started':False}
            access_args['write_paths']=[str(prefix_target)]
        access=permissions.command_access_projection(self.commands,access_args,self.task_id,call_id='install:'+call_id) if self.execution_profile == 'protected' else None
        payload={'kind':'installation','code':code,'language':'Bash','request':args,
                 'cwd':str(self.cwd),'human_required':True}
        if access is not None:payload['access_projection']=access
        row=self._permission('install:'+call_id,payload)
        request=row['payload']['request']
        source={'registry_path':str(self.registry.path),'registry_source':'software_registry'}

        def progress(stage,**details):
            def save(state):
                live=permissions._permissions(state)['requests'][row['request_id']]
                if live.get('action_hash')!=row['action_hash'] or not live.get('claim'):
                    raise DomainError('INSTALLATION_RECEIPT_IDENTITY_CHANGED')
                live['installation_progress']={'stage':stage,**deepcopy(details)}
            permissions._mutate(self.data_root,self.task_id,save)

        def register_verified(saved,*,recovery=False):
            # The recovery record is host-produced after real checks. It lives
            # in the original claim, never in model-supplied retry arguments.
            from package_manager import PackageManager
            final=deepcopy(saved['registration']);result=deepcopy(saved['result'])
            pm=PackageManager.for_registry(self.registry.path)
            current=pm.get_environment(request['env_name']) or {}
            marker=(current.get('evidence') or {}).get('installation_request_id')
            committed=marker==row['request_id'] and all(current.get(k)==v for k,v in final.items() if k in {'prefix','entries','env_runner','runner_argv','launcher','runtime_env'})
            if not committed:
                live=permissions.permission_state(self.data_root,self.task_id,row['request_id'])
                if live.get('status')!='allowed':
                    result.update(status='cancelled',registration_status='not_run',registration_recovery='explicit_retry_required')
                    return result
                try:
                    pm.register_environment(request['env_name'],record=final)
                except Exception as exc:
                    # A post-replace error may still mean the record committed.
                    try:
                        fresh=PackageManager.for_registry(self.registry.path).get_environment(request['env_name']) or {}
                        committed=(fresh.get('evidence') or {}).get('installation_request_id')==row['request_id'] and all(fresh.get(k)==v for k,v in final.items() if k in {'prefix','entries','env_runner','runner_argv','launcher','runtime_env'})
                    except Exception:
                        committed=False
                    if not committed:
                        result.update(status='registration_failed',registration_status='failed',
                                      registration_recovery='retry_registration_only',error=type(exc).__name__+': '+str(exc))
                        progress('verification_completed',registration=final,result=result)
                        return result
            result.pop('error',None)
            result.update(status='registered',registration_status='verified',registration_recovery='completed',
                          registration_receipt=getattr(pm,'last_registry_receipt',{}) or {'committed':True,'reconciled':committed},
                          reconciled=bool(recovery or committed))
            progress('registration_committed',registration=final,result=result)
            self.registry.reload()
            return result

        saved=row.get('installation_progress') or {}
        if saved.get('stage')=='registration_committed':
            # Includes a crash/lost transport receipt after atomic registry commit.
            return {**saved['result'],'replayed':True,'reconciled':True}
        if method=='install.execute' and saved.get('stage')=='verification_completed':
            try:
                result=register_verified(saved,recovery=True)
            except Exception as exc:
                return {**saved['result'],'status':'registration_failed','registration_status':'failed',
                        'registration_recovery':'retry_registration_only','error':type(exc).__name__+': '+str(exc),**source}
            def recovered(state):
                live=permissions._permissions(state)['requests'][row['request_id']]
                if live['action_hash']!=row['action_hash']:raise DomainError('INSTALLATION_RECEIPT_IDENTITY_CHANGED')
                live['result']=deepcopy(result)
            permissions._mutate(self.data_root,self.task_id,recovered)
            return {**result,'replayed':True,'installation_replayed':False}
        if row.get('result'):return {**row['result'],'replayed':True}
        if row['status']=='pending':return self._waiting(row)
        if row['status']!='allowed':return {'status':'not_run','error':'INSTALLATION_PERMISSION_DENIED','execution_started':False,**source}
        if method=='install.propose':return {'status':'approved','approval':self._approval(row),'execution_started':False}
        if row.get('claim'):return {'status':'reconciliation_required','error':'INSTALLATION_OUTCOME_UNKNOWN_RECONCILE',**source}
        try:
            verification=installation_verification_checks(request)
        except RuntimeLaunchError as exc:
            return {'status':'blocked','error':str(exc),'execution_started':False,**source}
        prefix=Path(request.get('prefix') or '')
        # Current install API creates prefix environments. Runner-only metadata
        # can be registered separately; do not publish incomplete target records.
        if not prefix.is_absolute() or (self.execution_profile == 'protected' and not prefix.resolve().is_relative_to(self.cwd)):
            return {'status':'blocked','error':'INSTALL_PREFIX_OUTSIDE_AUTHORIZED_PROJECT','execution_started':False,**source}
        if self.registry.path is None:return {'status':'blocked','error':'REGISTRY_PATH_NOT_CONFIGURED','execution_started':False}
        from agent_core.command_execution import formal_protection_capability
        if self.execution_profile == 'protected':formal_protection_capability(row['payload']['access_projection'])
        permit=permissions.claim_permission(self.data_root,self.task_id,row,'install:'+call_id)
        from execution.executor import run_command
        import threading
        cancel=threading.Event();self._install_cancels[call_id]=cancel
        command=row['payload']['code'];cwd=self.cwd
        logroot=self.data_root/'task_outputs'/self.task_id/'installations'/row['request_id']
        logroot.mkdir(parents=True,exist_ok=True)
        descriptor={k:deepcopy(request[k]) for k in ('env_runner','runner_argv','launcher','runtime_env','entries') if k in request}
        descriptor['prefix']=str(prefix.resolve())

        async def dispatch(actual,*,env=None,**kwargs):
            if self.execution_profile == 'protected':
                with permissions.execution_context(permit,actual,cwd=cwd,env=env):
                    return await run_command(actual,cwd=cwd,env=env,**kwargs)
            from execution.executor import _run_native_command
            from agent_core.execution_profile import require_host_profile
            def check_install_intent():
                require_host_profile(self)
                live=permissions.validate_permit(permit)
                if live['action_hash']!=row['action_hash']:
                    raise permissions.PermissionDenied('INSTALL_EXECUTION_PAYLOAD_CHANGED')
            return await _run_native_command(actual,check_intent=check_install_intent,cwd=cwd,env=env,**kwargs)

        async def execute():
            result={'status':'not_run','run_ref':'install:'+call_id,'registration_status':'not_run',
                    'scientific_validation':'not_assessed','execution_started':False,'verification':[],**source,
                    'log_refs':{'stdout':str(logroot/'stdout.log'),'stderr':str(logroot/'stderr.log')}}
            observation={}
            try:
                progress('installation_started',result=result)
                stdout,stderr,rc=await dispatch(command,profile=False,display_streams=False,cancel_event=cancel,
                    capture_log_paths=result['log_refs'],runtime_observation=observation)
                result.update(status='executed' if rc==0 else 'execution_failed',exit_code=rc,
                              stdout=stdout[:6000],stderr=stderr[:6000],execution_started=True,
                              software_side_effects='possible' if rc else 'installation_completed')
                progress('installation_completed',result=result)
                if rc==0:
                    for index,check in enumerate(verification):
                        launch=prepare_runtime_launch(descriptor,check['argv'],required_tool=check['tool'])
                        logs={'stdout':str(logroot/f'verify-{index}.stdout.log'),'stderr':str(logroot/f'verify-{index}.stderr.log')}
                        observation={}
                        out,err,check_rc=await dispatch(launch['command'],env=launch['env'],profile=False,display_streams=False,
                            cancel_event=cancel,capture_log_paths=logs,runtime_observation=observation)
                        result['verification'].append({**check['evidence'],'argv':launch['command'],
                            'launch':launch['launch'],'exit_code':check_rc,'stdout':out[:1000],'stderr':err[:1000],
                            'log_refs':logs,'observed_at':datetime.now(timezone.utc).isoformat()})
                        marker=check.get('expected_stdout_line')
                        if check_rc!=0 or (marker and marker not in out.splitlines()):
                            result.update(status='verification_failed',registration_status='not_run')
                            if check_rc==0:result['error']='INSTALL_VERIFICATION_OUTPUT_MISSING'
                            break
                    if result['status']=='executed':
                        if cancel.is_set():raise asyncio.CancelledError()
                        observed=datetime.now(timezone.utc).isoformat()
                        capability_evidence={}
                        for checked in result['verification']:
                            keys=['tool:'+checked['tool']]
                            keys.extend('package:'+name for name in checked.get('packages',[]))
                            keys.extend('python_module:'+name for name in checked.get('modules',[]))
                            for key in keys:
                                capability_evidence[key]={'status':'verified','source':'installation_execution',
                                    'observed_at':checked['observed_at'],'installation_request_id':row['request_id'],
                                    'log_refs':checked['log_refs']}
                        final={**descriptor,'status':'verified','packages':request.get('packages') or [],
                               'purpose':request.get('description') or 'Pi-approved installation',
                               'last_verified_at':observed,'tools':sorted({c['tool'] for c in verification}),
                               'capability_evidence':capability_evidence,
                               'evidence':{'source':'installation_execution','installation_request_id':row['request_id'],
                                           'observed_at':observed,'checks':deepcopy(result['verification']),
                                           'declared_packages':request.get('packages') or [],
                                           'checked_packages':sorted({name for c in verification for name in c['evidence'].get('packages',[])}),
                                           'checked_python_modules':sorted({name for c in verification for name in c['evidence'].get('modules',[])})}}
                        # No invented package versions; executable-only checks
                        # evidence that tool, not every declared library/package.
                        saved={'registration':final,'result':deepcopy(result)}
                        progress('verification_completed',**saved)
                        result=register_verified(saved)
            except asyncio.CancelledError:
                result.update(status='cancelled',registration_status='not_run',software_side_effects='possible',
                              execution_started=bool(result['execution_started'] or observation.get('process_started_at')),
                              cleanup_confirmed=bool(observation.get('cleanup_confirmed')),
                              process_returncode=observation.get('process_returncode'))
                if not observation.get('cleanup_confirmed'):
                    result.update(status='reconciliation_required',error='INSTALLATION_CANCEL_CLEANUP_UNKNOWN')
            except (FormalProtectionError,permissions.PermissionDenied) as exc:
                result.update(status='verification_blocked' if result['execution_started'] else 'stage_blocked',
                              error=str(exc),registration_status='not_run')
                if isinstance(exc,FormalProtectionError):result['capability_missing']=exc.diagnostic
            except (RuntimeLaunchError,OSError,ValueError) as exc:
                saved=permissions.permission_state(self.data_root,self.task_id,row['request_id']).get('installation_progress') or {}
                if saved.get('stage')=='verification_completed':
                    result.update(status='registration_failed',registration_status='failed',registration_recovery='retry_registration_only')
                else:
                    result.update(status='verification_failed' if result['execution_started'] else 'execution_failed',registration_status='not_run')
                result['error']=type(exc).__name__+': '+str(exc)
            permissions.complete_permission(permit,result)
            return result
        task=asyncio.create_task(execute(),name='pi-install:'+call_id);self._install_tasks[call_id]=task
        return await asyncio.shield(task)
