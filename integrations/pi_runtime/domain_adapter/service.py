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
import sys
import time
from datetime import datetime,timezone
from types import SimpleNamespace

from agent_core.command_execution import CommandExecutionService
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
from memory.patch_validator import redact_secrets
from .registry import RegistryView, registry_path as resolve_registry_path
from .discovery import list_directory

HOST_METHODS = frozenset({'files.list','files.read_authorize','files.mutation_snapshot','files.mutation_replay','request.begin','request.cancel','approval.decide','settings.update','shutdown',
                         'memory.maintenance.prepare','memory.maintenance.apply','archive.append','archive.status'})


class DomainError(ValueError):
    pass


class DomainService:
    def __init__(self,*,cwd,data_root,install_root,session_id,registry_path=None,skills_root=None,
                 preferences_path=None,memory_root=None,wiki_root=None,history_enabled=False,
                 memory_mode='ask',archive_path=None,environment_inventory=None):
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
        self.formal=FormalCommandObserver(self.commands)
        self.files=WorkspaceFileService(self.commands)
        self._skills=None; self._wiki=None; self._core=None
        self._seen_sources={}; self._dispatch_lock=asyncio.Lock(); self._install_tasks={}
        self._install_cancels={}
        self._closed=False

    def _state(self): return load_plan_state(self.data_root,self.task_id)
    def scope(self): return request_scope(self._state())
    def _host(self,token):
        if not isinstance(token,str) or not secrets.compare_digest(token,self.host_token):
            raise DomainError('HOST_CHANNEL_REQUIRED')

    def _ensure_request(self):
        if not (self._state().get('task_state') or {}).get('pi_request_id'):
            raise DomainError('REQUEST_BEGIN_REQUIRED')

    def _approval(self,row):
        return {'request_id':row['request_id'],'action_hash':row['action_hash'],
                'request_scope':row['request_scope'],'payload':row['payload'],
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
              'wall_timeout_disabled','run_status','run_terminal','truncated','output_projection','observation_only'}
        return {k:v for k,v in result.items() if k in keys}

    def _permission(self,call_id,payload):
        self._ensure_request()
        return permissions.request_permission(self.data_root,self.task_id,str(call_id),payload)

    def _claim_or_result(self,row,call_id):
        if row.get('result'): return None,{**row['result'],'replayed':True}
        if row.get('claim'): return None,{'status':'reconciliation_required','error':'CLAIM_OUTCOME_UNKNOWN','execution_started':None}
        if row['status']=='pending': return None,self._waiting(row)
        if row['status']!='allowed': return None,{'status':'not_run','error':'EXECUTION_PERMISSION_DENIED','execution_started':False}
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
        return file_snapshot(selected,limit=12000,source_kind='explicit_preference' if selected==preferred else 'core_fallback',
            selection_reason='preferred_file' if selected==preferred else 'preferred_missing')

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

    def select_environment(self,requirements,skill_id='',env_name=''):
        if isinstance(requirements,list) and all(isinstance(r,str) for r in requirements):requirements={'packages':requirements}
        if not isinstance(requirements,dict):raise DomainError('ENV_REQUIREMENTS_MUST_BE_OBJECT_OR_PACKAGE_LIST')
        self.registry.reload()
        record=self.skills().get_runtime_registration(skill_id) if skill_id else {}
        selected=registered_runtime_reuse(self.registry,requirements,skill_record=record,target_env=env_name)
        if selected.get('availability')!='AVAILABLE':return selected
        candidate=selected['selected_candidate'];audit=deepcopy(candidate['audit'])
        sid=skill_id or 'registered-environment:'+audit['env'];audit['skill_id']=sid
        invalid=(self._state().get('task_state',{}).get('runtime_invalidations') or {}).get(sid)
        if invalid and invalid.get('prefix')==candidate['prefix']:
            return {**selected,'availability':'UNKNOWN','decision':'execution_dependency_failed','invalidation':invalid,'install_required':False}
        from agent_core.runtime_evidence import make_runtime_evidence,merge_runtime_evidence,runtime_evidence_state,digest as evidence_digest
        contract=evidence_digest({'requirements':requirements,'registration':record})
        scope={'skill_id':sid,'skill_document_digest':evidence_digest(record),'skill_contract_digest':contract,
               'environment_id':audit['env'],'environment_fingerprint':evidence_digest(candidate['source_ref']),
               'host_id':os.uname().nodename,'inspection_contract_digest':contract}
        evidence=make_runtime_evidence(audit,task_id=self.task_id,skill_scope=scope,
            observed_at=datetime.now(timezone.utc).isoformat(),producer='registered_runtime',
            source_ref=candidate['source_ref'],coverage=candidate['coverage'],inspection_status='not_run')
        def update(state):
            task=state.setdefault('task_state',{});old=task.get('runtime_evidence') or {}
            # Repeated registry selection is metadata reuse, never a fresh healthcheck.
            if any(r.get('source_ref')==evidence['source_ref'] and r.get('scope')==scope for r in old.get('records',[])):return
            records=merge_runtime_evidence(old.get('records',[]),[evidence],task_id=self.task_id)
            task['runtime_evidence']=runtime_evidence_state(records,prior_revision=old.get('revision',0))
        mutate(self.data_root,self.task_id,update)
        return selected

    async def _exec_start(self,p):
        self._ensure_request(); call_id=self._call_id(p)
        original=deepcopy(p.get('arguments') or {})
        for field in ('approved','source','permit','human_required','host_token'):
            if field in original or field in p: raise DomainError('MODEL_AUTHORITY_FIELD_FORBIDDEN')
        # Pi's tool description is presentation metadata, not an executable
        # argument supported by the existing strict formal action contract.
        original.pop('description',None)
        env_name=original.pop('env_name',None)
        if env_name:
            selected=self.select_environment({'env':env_name},env_name=env_name)
            if selected.get('availability')!='AVAILABLE': return {'status':'blocked','error':'REGISTERED_ENVIRONMENT_UNAVAILABLE','selection':selected,'execution_started':False}
            original['env']={**original.get('env',{}),'ENV_PREFIX':selected['selected_candidate']['prefix']}
        arguments=original
        row=self._permission(call_id,permissions.command_payload(self.commands,arguments,self.task_id))
        if row['status']=='pending': return self._waiting(row)
        if row['status']!='allowed': return {'status':'not_run','error':'EXECUTION_PERMISSION_DENIED','execution_started':False,'terminal':True}
        # Owner reservation itself is short. Long work lives in the real observer.
        async with self._dispatch_lock:
            return self._run_view(await self.formal.start(arguments,task_id=self.task_id,stage='pi_generate',request_scope=self.scope(),call_id=call_id,yield_time_ms=50))

    async def call(self,method,p,*,host_token=None):
        if not isinstance(p,dict): raise DomainError('PARAMS_MUST_BE_OBJECT')
        if method in HOST_METHODS: self._host(host_token)
        active_identity=p.get('active_request_id')
        if method=='archive.append' and not active_identity:raise DomainError('ACTIVE_REQUEST_ID_REQUIRED')
        if method not in {'hello','request.begin'} and active_identity is not None:
            current=self._state().get('task_state',{}).get('pi_request_id')
            if active_identity!=current:raise DomainError('STALE_ACTIVE_REQUEST')
        # Transport correlation is supplied by the host, never part of action hashes.
        p={k:v for k,v in p.items() if k!='active_request_id'}
        if method=='hello':
            return {'protocol':'biomind-domain-v1','session_id':self.session_id,'task_id':self.task_id,
                    'host_token':self.host_token,'methods':['request.begin','context.snapshot','environments.select',
                    'exec.start','exec.status','exec.wait','exec.read_log','exec.cancel','approval.decide','files.read','files.write','files.stat',
                    'memory.search','memory.read','memory.write','install.propose','install.execute','settings.update',
                    'archive.append','archive.status']}
        if method=='request.begin':
            identity=str(p.get('request_id') or '')
            if not identity: raise DomainError('REQUEST_ID_REQUIRED')
            if p.get('role','consult') not in {'consult','generate'}: raise DomainError('ROLE_INVALID')
            def update(state):
                task=state.setdefault('task_state',{})
                if task.get('pi_request_id')!=identity:
                    if self.active_jobs(): raise DomainError('ACTIVE_JOB_PREVENTS_NEW_REQUEST')
                    state['authoritative_user_turn_revision']=int(state.get('authoritative_user_turn_revision') or 0)+1
                    task['current_request_key']=identity;task['pi_request_id']=identity
                    task.pop('request_finish',None);task['status']='running'
                task['intent_admission']={'request_kind':'execute' if p.get('role')=='generate' else 'consult',
                    'execution_scope':'simple','review_before_execution':False,'source':'pi_host_request'}
                task['pi_session_id']=self.session_id
                return {'status':'ready','request_scope':request_scope(state),'task_id':self.task_id}
            return mutate(self.data_root,self.task_id,update)
        if method=='approval.decide':
            row=permissions.permission_state(self.data_root,self.task_id,str(p['request_id']))
            if p.get('action_hash')!=row['action_hash'] or p.get('request_scope')!=row['request_scope'] or self.scope()!=row['request_scope']:
                raise DomainError('STALE_PERMISSION_REPLY')
            choice=p.get('choice')
            if choice=='cancel':
                updated=permissions.cancel_permission(self.data_root,self.task_id,row['request_id'])
                return {'status':updated['status'],'approval':self._approval(updated)}
            if choice not in {'y','n','edit','scope'}: return {'status':'pending','approved':False,'approval':self._approval(row)}
            replacement=p.get('replacement') or ''
            if isinstance(replacement,dict):replacement=replacement.get('cmd') or ''
            if not isinstance(replacement,str):raise DomainError('EDIT_REPLACEMENT_MUST_BE_COMMAND_TEXT')
            decision=permissions._Decision(row['request_id'],row['action_hash'],choice,permissions._SEAL,replacement)
            updated=permissions.record_human_decision(self.data_root,self.task_id,decision)
            return {'status':'waiting_approval' if updated['status']=='pending' else updated['status'],
                    'approval':self._approval(updated)}
        if method=='context.snapshot': return self.context_snapshot()
        if method in {'archive.append','archive.status'}:
            from .archive_observation import append,status
            return append(self,p) if method=='archive.append' else status(self)
        if method=='session.lifecycle':
            runs=[o.run_ref for o in self.formal._owners.values() if o.task is not None and not o.task.done()]
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
            return self.select_environment(p.get('requirements') or {},p.get('skill_id') or '',p.get('env_name') or '')
        if method=='exec.start': return await self._exec_start(p)
        if method in {'exec.status','exec.wait','exec.read_log'}:
            args={k:p[k] for k in ('run_ref','after_cursor','max_wait_ms') if k in p}
            if method!='exec.wait': args['max_wait_ms']=0
            return self._run_view(await self.formal.wait_command(args,task_id=self.task_id,request_scope=self.scope()))
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
            return result
        if method=='files.read':
            args=dict(p)
            if 'offset' in args:args['offset_chars']=args.pop('offset')
            return self.files.read(args,task_id=self.task_id)
        if method=='files.stat':
            return self.files.stat(p,task_id=self.task_id)
        if method=='files.list':
            self._host(host_token)
            self._ensure_request()
            return list_directory(self, p)
        if method=='files.read_authorize':
            self._host(host_token)
            return self.files.stat(p,task_id=self.task_id,trusted_skill_dirs=p.get('trusted_skill_dirs',()))
        if method=='files.mutation_snapshot':
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
                return {'status':'not_run','error':'EXECUTION_PERMISSION_DENIED'}
            return {'status':'new'}
        if method=='files.write':
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
        jobs=self.active_jobs()
        if jobs: await asyncio.gather(*jobs,return_exceptions=True)
        from .archive_observation import release
        await release(self)

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
            return {**{k:v for k,v in row.items() if k not in {'content','text'}},'status':'read','content':body[offset:offset+limit],
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
        call_id=self._call_id(p);args=deepcopy(p.get('arguments') or {})
        for field in ('approved','source','permit','host_token'):
            if field in args or field in p:raise DomainError('MODEL_AUTHORITY_FIELD_FORBIDDEN')
        code=args.get('cmd')
        if not isinstance(code,str) or not code.strip():raise DomainError('INSTALL_COMMAND_REQUIRED')
        if not args.get('env_name'):raise DomainError('INSTALL_ENV_NAME_REQUIRED')
        payload={'kind':'installation','code':code,'language':'Bash','request':args,
                 'cwd':str(self.cwd),'human_required':True}
        row=self._permission('install:'+call_id,payload)
        if row.get('result'):return {**row['result'],'replayed':True}
        if row['status']=='pending':return self._waiting(row)
        if row['status']!='allowed':return {'status':'not_run','error':'INSTALLATION_PERMISSION_DENIED','execution_started':False}
        if method=='install.propose':return {'status':'approved','approval':self._approval(row),'execution_started':False}
        if row.get('claim'):return {'status':'reconciliation_required','error':'INSTALLATION_OUTCOME_UNKNOWN_RECONCILE'}
        request=row['payload']['request']
        verification=request.get('verification_commands') or []
        if not isinstance(verification,list) or not verification or any(not isinstance(v,str) or not v.strip() for v in verification):
            return {'status':'blocked','error':'INSTALL_VERIFICATION_COMMANDS_REQUIRED','execution_started':False}
        prefix=Path(request.get('prefix') or '')
        if not prefix.is_absolute() or not prefix.resolve().is_relative_to(self.cwd):
            return {'status':'blocked','error':'INSTALL_PREFIX_OUTSIDE_AUTHORIZED_PROJECT','execution_started':False}
        if self.registry.path is None:return {'status':'blocked','error':'REGISTRY_PATH_NOT_CONFIGURED','execution_started':False}
        permit=permissions.claim_permission(self.data_root,self.task_id,row,'install:'+call_id)
        from execution.executor import run_command
        import threading
        cancel=threading.Event();self._install_cancels[call_id]=cancel
        # The existing formal/installer execution boundary is explicit approval,
        # an in-process dispatch permit and durable IO evidence. Its approved
        # command is host execution; readonly inspection has a separate sandbox.
        command=row['payload']['code'];cwd=self.cwd
        logroot=self.data_root/'task_outputs'/self.task_id/'installations'/row['request_id']
        logroot.mkdir(parents=True,exist_ok=True)
        async def execute():
            try:
                with permissions.execution_context(permit,command,cwd=cwd):
                    stdout,stderr,rc=await run_command(command,cwd=cwd,profile=False,display_streams=False,cancel_event=cancel,
                        capture_log_paths={'stdout':str(logroot/'stdout.log'),'stderr':str(logroot/'stderr.log')})
            except asyncio.CancelledError:
                result={'status':'cancelled','run_ref':'install:'+call_id,'registration_status':'not_run','scientific_validation':'not_assessed'}
                permissions.complete_permission(permit,result)
                return result
            result={'status':'executed' if rc==0 else 'execution_failed','exit_code':rc,'stdout':stdout[:6000],'stderr':stderr[:6000],
                    'run_ref':'install:'+call_id,'registration_status':'verification_required','scientific_validation':'not_assessed',
                    'log_refs':{'stdout':str(logroot/'stdout.log'),'stderr':str(logroot/'stderr.log')},'verification':[]}
            if rc==0:
                for index,code in enumerate(verification):
                    with permissions.execution_context(permit,code,cwd=cwd):
                        out,err,check_rc=await run_command(code,cwd=cwd,profile=False,display_streams=False,cancel_event=cancel,
                            capture_log_paths={'stdout':str(logroot/f'verify-{index}.stdout.log'),'stderr':str(logroot/f'verify-{index}.stderr.log')})
                    result['verification'].append({'exit_code':check_rc,'stdout':out[:1000],'stderr':err[:1000]})
                    if check_rc!=0:result['status']='verification_failed';break
                if result['status']=='executed':
                    if not prefix.is_dir():result.update(status='verification_failed',error='VERIFIED_PREFIX_MISSING')
                    else:
                        # Reuse registration implementation without constructing
                        # its scanning/configuration/shim-repair initializer.
                        from package_manager import PackageManager
                        pm=PackageManager.__new__(PackageManager)
                        self.registry.reload();pm.registry=deepcopy(self.registry.registry);pm.registry_path=self.registry.path
                        pm.register_environment(request['env_name'],packages=request.get('packages') or [],
                            purpose=request.get('description') or 'Pi-approved installation',healthcheck=' && '.join(verification),
                            status='verified',last_verified_at=time.strftime('%Y-%m-%dT%H:%M:%S'))
                        pm.registry['environments'][request['env_name']]['prefix']=str(prefix.resolve());pm._save_registry()
                        self.registry.reload();result['registration_status']='verified';result['status']='registered'
            permissions.complete_permission(permit,result)
            return result
        task=asyncio.create_task(execute(),name='pi-install:'+call_id);self._install_tasks[call_id]=task
        return await asyncio.shield(task)
