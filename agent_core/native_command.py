"""Local backend for the existing FormalCommandObserver and executor.

No permission grants, sandbox projection, task/output ACL or second scheduler.
Intent/attempt identity, provenance, logs and terminal receipts retain the
original formats so reconnect/cancel/recovery use the original observer.
"""
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import time
from copy import deepcopy

from agent_core.command_inspection_owner import _once
from agent_core.pending_input import mutate
from agent_core.plan_tracker import load_plan_state, record_provenance_event
from agent_core.request_state import request_scope, current_finish


async def execute_native_command(service, arguments, *, task_id, call_id, stage='pi_native',
                                 runtime_observation=None, cancel_event=None, original_deadline=None):
    from agent_core.action_runtime import PrelaunchAdmissionRejected, _record_formal_receipt
    from execution.executor import _run_native_command
    observation = runtime_observation if runtime_observation is not None else {}
    observation.update(launch_delegated=False, preparation_settlement={'safe_to_release': True, 'phase': 'native_admission'})
    scope = request_scope(load_plan_state(service.data_path, task_id))
    intent = hashlib.sha256(json.dumps([scope, call_id], sort_keys=True).encode()).hexdigest()[:24]
    action_id, attempt_id = 'action-' + intent, 'attempt-' + intent
    directory = Path(service.data_path).absolute() / 'task_outputs' / task_id / 'attempts' / attempt_id
    receipt = directory / 'exit_status.json'
    digest = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def check_intent():
        state = load_plan_state(service.data_path, task_id)
        task = state.get('task_state', {})
        if getattr(service, '_execution_profile', 'protected') != 'native' or task.get('pi_execution_profile', {}).get('profile') != 'native':
            raise ValueError('NATIVE_HOST_BINDING_REQUIRED')
        if request_scope(state) != scope or not task.get('pi_request_id') or task.get('pi_pause') or current_finish(state):
            raise ValueError('NATIVE_REQUEST_NOT_ACTIVE')
        if task.get('status') == 'cancelled' or cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError()
        if arguments.get('_runtime_binding'):
            from agent_core.runtime_evidence import registered_execution_binding
            binding=arguments['_runtime_binding']
            registered_execution_binding(state,binding_ref=binding.get('binding_ref',''),runtime_scope=binding.get('scope',''))
        record = json.loads((directory / 'observation_intent.json').read_text())
        if (record.get('task_id') != task_id or record.get('call_id') != call_id or
                record.get('run_ref') != attempt_id or record.get('arguments_hash') != digest):
            raise ValueError('NATIVE_EXECUTION_INTENT_MISMATCH')

    lock = None
    try:
        check_intent()
        allowed = {'cmd','argv','workdir','env','timeout_ms','max_output_chars','read_paths','write_paths','_biomind_probe','_runtime_binding'}
        if set(arguments) - allowed:
            raise ValueError('EXEC_COMMAND_ARGUMENTS_INVALID')
        command = arguments.get('argv') or arguments.get('cmd')
        if bool(arguments.get('argv')) == bool(arguments.get('cmd')):
            raise ValueError('EXECUTION_COMMAND_REQUIRED_OR_AMBIGUOUS')
        if isinstance(command, list):
            if not command or not command[0] or any(not isinstance(s,str) or '\x00' in s for s in command):
                raise ValueError('EXECUTION_ARGV_INVALID')
        elif not isinstance(command, str) or not command.strip() or '\x00' in command:
            raise ValueError('EXECUTION_COMMAND_REQUIRED')
        workdir = arguments.get('workdir') or str(service.project_for_task(task_id))
        if not isinstance(workdir, str) or '\x00' in workdir:
            raise ValueError('EXECUTION_CWD_INVALID')
        cwd = (service.project_for_task(task_id) / Path(workdir).expanduser()).absolute()
        if not cwd.is_dir():
            cwd.stat()  # Preserve ENOENT/EACCES instead of a permission proposal.
            raise NotADirectoryError(str(cwd))
        overlay = arguments.get('env') or {}
        if not isinstance(overlay, dict) or any(not isinstance(k,str) or not k or '=' in k or '\x00' in k
                or not isinstance(v,str) or '\x00' in v for k,v in overlay.items()):
            raise ValueError('EXECUTION_ENV_INVALID')
        env = {**os.environ, **overlay}
        if overlay.get('ENV_PREFIX'):
            target_bin=str(Path(overlay['ENV_PREFIX']) / 'bin')
            env['PATH']=os.pathsep.join([target_bin,*[p for p in env.get('PATH','').split(os.pathsep) if p!=target_bin]])
            env['CONDA_PREFIX']=overlay['ENV_PREFIX']
        lock = (directory / 'intent.lock').open('a+')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status':'reconciliation_unknown','terminal':False,'attempt_id':attempt_id,'replayed':True}
        if receipt.is_file():
            result = json.loads(receipt.read_text())
            if result.get('arguments_hash') != digest: raise ValueError('EXECUTION_INTENT_ARGUMENTS_CHANGED')
            return {**result, 'replayed':True}
        durable = load_plan_state(service.data_path, task_id)
        if attempt_id in durable.get('provenance_state', {}).get('attempts', {}):
            return {'status':'reconciliation_unknown','terminal':False,'attempt_id':attempt_id,'replayed':True}
        display = shlex.join(command) if isinstance(command,list) else command
        action = {'type':'execute','execution_protocol_version':3,'action_id':action_id,
                  'arguments_hash':digest,'arguments':arguments,'request_scope':scope,'tool_call_id':call_id,
                  'step_id':'','plan_version':None,'cmd':display,'original_cmd':display,
                  'argv':command if isinstance(command,list) else None,'cwd':str(cwd),'env':overlay,
                  'execution_carrier':'argv' if isinstance(command,list) else 'shell',
                  'shell':None if isinstance(command,list) else '/bin/sh','shell_login':False,
                  'execution_profile':'native','frozen_files':[]}
        if arguments.get('_runtime_binding'):
            action['runtime_binding']=deepcopy(arguments['_runtime_binding'])
        action['action_hash'] = hashlib.sha256(json.dumps(action,sort_keys=True).encode()).hexdigest()
        def prepare(state):
            if request_scope(state) != scope: raise ValueError('NATIVE_REQUEST_SCOPE_CHANGED')
            record_provenance_event(state,'action_bound',{'action':action})
            record_provenance_event(state,'attempt_created',{'action_id':action_id,'attempt_id':attempt_id,'step_id':'',
                'stdout_path':str(directory/'stdout.log'),'stderr_path':str(directory/'stderr.log'),'exit_status_path':str(receipt)})
            record_provenance_event(state,'attempt_state_changed',{'attempt_id':attempt_id,'status':'STARTING'})
        check_intent()
        mutate(service.data_path,task_id,prepare)
        observation['preparation_settlement']['safe_to_release'] = False

        async def lifecycle(status, details):
            observation.update(details)
            if status == 'HEARTBEAT': status = 'RUNNING'
            if status == 'PROCESS_CANCELLED': status = 'PROCESS_EXITED' if details.get('process_reaped') else 'UNKNOWN_AFTER_CRASH'
            mutate(service.data_path,task_id,lambda state: record_provenance_event(state,'attempt_state_changed',
                {'attempt_id':attempt_id,'status':status,**details}))

        base = {'execution_protocol_version':3,'execution_profile':'native','attempt_id':attempt_id,
                'action_id':action_id,'task_id':task_id,'step_id':'','arguments_hash':digest,'request_scope':scope,
                'receipt_ref':str(receipt),'stdout_path':str(directory/'stdout.log'),'stderr_path':str(directory/'stderr.log'),
                'cmd':display,'original_cmd':display,'argv':action['argv'],'cwd':str(cwd),'env':overlay,
                'scientific_validation':'not_assessed','frozen_files':[]}
        if arguments.get('_runtime_binding'):
            base['runtime_binding']=deepcopy(arguments['_runtime_binding'])
        observation['launch_delegated'] = True
        try:
            stdout,stderr,rc = await _run_native_command(command,check_intent=check_intent,cwd=cwd,env=env,
                runtime_observation=observation,cancel_event=cancel_event,original_deadline=original_deadline,
                timeout=float(arguments['timeout_ms'])/1000 if arguments.get('timeout_ms') else None,
                max_output_bytes=65536 if arguments.get('_biomind_probe') else None,
                short_check=bool(arguments.get('_biomind_probe')),lifecycle_callback=lifecycle,profile=False,
                capture_log_paths={'stdout':str(directory/'stdout.log'),'stderr':str(directory/'stderr.log')},
                display_context={'task_id':task_id,'node':stage,'execution_attempt_id':attempt_id})
        except OSError as exc:
            if observation.get('process_started_at'): raise
            mutate(service.data_path,task_id,lambda state: record_provenance_event(state,'attempt_state_changed',
                {'attempt_id':attempt_id,'status':'FAILED','execution_started':False,'error':str(exc)}))
            observation.update(launch_delegated=False,cleanup_confirmed=True,process_alive=False)
            observation['preparation_settlement']['safe_to_release']=True
            raise
        except asyncio.CancelledError:
            complete = bool(observation.get('cleanup_confirmed'))
            result = {**base,'status':'cancelled' if complete else 'reconciliation_unknown','terminal':complete,
                'exit_code':observation.get('process_returncode'),'returncode':observation.get('process_returncode'),
                'cleanup_confirmed':complete,'execution_started':bool(observation.get('process_started_at')),
                'runtime_metadata':dict(observation)}
            if complete: _once(receipt,result)
            raise
        result = {**base,'status':'exited','terminal':True,'exit_code':rc,'returncode':rc,'stdout':stdout,'stderr':stderr,
                  'execution_started':bool(observation.get('process_started_at')),
                  'cleanup_confirmed':bool(observation.get('cleanup_confirmed',True)),
                  'finished_at':time.time(),'runtime_metadata':dict(observation)}
        _once(receipt,result)
        _record_formal_receipt(service.data_path,task_id,result)
        return result
    except Exception as exc:
        if observation['preparation_settlement']['safe_to_release'] and not observation.get('launch_delegated'):
            raise PrelaunchAdmissionRejected(str(exc),phase='native_admission') from exc
        raise
    finally:
        if lock: lock.close()
