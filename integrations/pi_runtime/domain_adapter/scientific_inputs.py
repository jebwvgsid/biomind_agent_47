"""GWAS input admission on the existing request ledger and command observer.

Only the host-selected, versioned checker creates evidence. Arbitrary stdout,
old output files and model-supplied decisions are never control inputs.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys
import time

from agent_core.pending_input import mutate
from agent_core.request_state import request_scope


def current(service):
    state = service._state()
    row = state.get('task_state', {}).get('scientific_input') or {}
    return deepcopy(row) if row.get('scope') == request_scope(state) else {}


def save(service, row):
    def update(state):
        if request_scope(state) != row['scope']:
            raise ValueError('SCIENTIFIC_REQUEST_OWNER_CHANGED')
        state.setdefault('task_state', {})['scientific_input'] = deepcopy(row)
    mutate(service.data_root, service.task_id, update)


def declarations(service, text):
    # Select a known workflow from the user's request, never from a benchmark
    # directory name, expected answer, tool output or an arbitrary shell string.
    if not re.search(r'\bGWAS\b|popgen-gwas|gwas-emmax|population-genetics|\bEMMAX\b', text, re.I):
        return None
    if re.search(r'只讨论|仅讨论|只读审计|只审计|不要实施|discussion only|do not (?:run|execute)', text, re.I):
        return None
    absolute = re.findall(r'/[^\s\"\x27`<>，。；、)]+', text)
    relative = re.findall(r'(?<![/\w.-])[\w.-]+\.(?:vcf(?:\.gz)?|Q|table|tsv|tfam|json|gff3)\b', text, re.I)
    paths = set()
    for raw in absolute + relative:
        path = Path(raw.rstrip(':;,，。；')).expanduser()
        if not path.is_absolute():
            # An absolute reference with the same basename is unambiguous;
            # don't create an extra (usually nonexistent) cwd-relative input.
            if any(Path(a).name == path.name for a in absolute):
                continue
            path = service.cwd / path
        paths.add(path)
    roles = {'vcf': [], 'phenotype': [], 'q_matrix': [], 'tfam': [], 'row_map': [], 'gff3': []}
    for path in sorted(paths):
        name = path.name.lower()
        key = ('vcf' if name.endswith(('.vcf', '.vcf.gz')) else
               'q_matrix' if name.endswith('.q') else
               'phenotype' if name.endswith('.table') or (name.endswith(('.tsv', '.txt')) and re.search('trait|phenotype', name)) else
               'tfam' if name.endswith('.tfam') else
               'row_map' if name.endswith('.json') and re.search('map|mapping', name) else
               'gff3' if name.endswith('.gff3') else None)
        if key:
            roles[key].append(str(path))
    # Software/version-only requests are not scientific analysis admission.
    if not roles['vcf']:
        return None
    ambiguous = [key for key, values in roles.items() if len(values) > 1]
    missing = [key for key in ('vcf', 'phenotype') if not roles[key]]
    if ambiguous or missing:
        raise ValueError('GWAS_INPUT_SELECTION_REQUIRED:' + ','.join(ambiguous + missing))
    inputs = {key: values[0] for key, values in roles.items() if values}
    outputs = [p for p in paths if p.name == 'output']
    output = outputs[0] if len(outputs) == 1 else service.cwd / 'output'
    return inputs, output


def identity(paths):
    result = {}
    for key, value in paths.items():
        p = Path(value)
        st = p.stat()
        result[key] = {'path': str(p.resolve()), 'bytes': st.st_size, 'mtime_ns': st.st_mtime_ns,
                       'ctime_ns': st.st_ctime_ns, 'inode': st.st_ino}
    return result


def checker_identity(directory):
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ('inspect_inputs.py', 'input_contract.py')}


def public(row):
    return {key: deepcopy(value) for key, value in row.items() if key not in {'user_text', 'stat_identity'}}


async def prepare(service, text=None):
    async with service._scientific_lock:
        previous = current(service)
        text = text if text is not None else previous.get('user_text', '')
        resolved = declarations(service, text)
        if resolved is None:
            # A continuation cannot erase an already selected scientific stage.
            return public(previous) if previous else {'status': 'not_applicable'}
        inputs, output = resolved
        from agent_core.command_execution import check_data_path
        for path in inputs.values():
            check_data_path(service.commands, service.task_id, Path(path), write=False)
        check_data_path(service.commands, service.task_id, output, write=True)
        scripts = service.install_root / 'skills/population-genetics/gwas-vcf-prep-qc/scripts'
        version = checker_identity(scripts)
        stats = identity(inputs)
        if (previous.get('inputs') == inputs and previous.get('stat_identity') == stats
                and previous.get('checker') == version and previous.get('status') in {'validated', 'blocked'}):
            return public(previous)
        scope = service.scope()
        row = {'scope': scope, 'request_id': service._state()['task_state']['pi_request_id'],
               'stage': 'gwas_input_identity', 'status': 'checking', 'user_text': text,
               'inputs': inputs, 'output_dir': str(output), 'checker': version,
               'stat_identity': stats, 'started_at': time.time()}
        save(service, row)
        argv = [sys.executable, str(scripts / 'inspect_inputs.py')]
        flags = {'vcf': '--vcf', 'phenotype': '--phenotype', 'q_matrix': '--q-matrix',
                 'tfam': '--tfam', 'row_map': '--q-row-map', 'gff3': '--gff3'}
        for key, value in inputs.items():
            argv += [flags[key], value]
        call_id = 'scientific-input-' + hashlib.sha256(json.dumps([scope, stats, version], sort_keys=True).encode()).hexdigest()[:24]
        row.update(checking_call_id=call_id, checker_argv=argv)
        save(service, row)
        run = await service.formal.start({'argv': argv, 'workdir': str(service.cwd), 'timeout_ms': 15000,
                                         'read_paths': list(inputs.values()) + [str(scripts)],
                                         'max_output_chars': 64000, '_biomind_probe': True},
                    task_id=service.task_id, request_scope=scope, stage='scientific_input_check', call_id=call_id, yield_time_ms=50)
        while not run.get('terminal') and run.get('run_ref'):
            run = await service.formal.wait_command({'run_ref': run['run_ref'], 'max_wait_ms': 1000},
                                                   task_id=service.task_id, request_scope=scope)
        row.update(run_ref=run.get('run_ref'), receipt_ref=run.get('receipt_ref'),
                   check_completed_at=time.time(), checker_argv=argv,
                   execution={k: run.get(k) for k in ('status', 'exit_code', 'execution_started', 'terminal', 'error', 'cleanup_confirmed', 'elapsed_ms')})
        try:
            if not run.get('execution_started') or run.get('exit_code') not in (0, 2):
                raise ValueError('SCIENTIFIC_CHECK_EXECUTION_FAILED: ' + json.dumps(row['execution']))
            if identity(inputs) != stats or checker_identity(scripts) != version:
                raise ValueError('SCIENTIFIC_INPUT_CHANGED_DURING_CHECK')
            value = json.loads(run.get('stdout') if run.get('exit_code') == 0 else run.get('stderr'))
            if run['exit_code'] == 0:
                if value.get('producer') != 'gwas_input_structure_v1' or value.get('schema_version') != 1:
                    raise ValueError('SCIENTIFIC_CHECK_SCHEMA_INVALID')
                blocked = value.get('blocked_operations', [])
                reason = (value.get('q_mapping') or {}).get('reason_code') or 'SAMPLE_IDENTITY_MISMATCH'
            else:
                reason = (value.get('error') or {}).get('code')
                if not reason or reason == 'INPUT_OBSERVATION_FAILED':
                    raise ValueError('SCIENTIFIC_CHECK_EXECUTION_FAILED')
                blocked = ['input_preparation', 'association']
            row.update(observation=value, blocked_operations=blocked,
                       status='blocked' if blocked else 'validated',
                       reason_code=reason if blocked else 'INPUT_IDENTITY_VALIDATED', accepted_at=time.time())
            save(service, row)
            if blocked:
                return await close_block(service, row)
            def release(state):
                task = state['task_state']
                if (task.get('request_finish') or {}).get('source') == 'scientific_input_check':
                    task.pop('request_finish', None)
                    task['status'] = 'running'
            mutate(service.data_root, service.task_id, release)
            return public(row)
        except (ValueError, OSError) as exc:
            row.update(status='error', reason_code=str(exc), error_at=time.time())
            save(service, row)
            raise


async def close_block(service, row):
    # Commit the block before awaiting any cleanup. All dispatch paths consult
    # this same ledger, including other calls in an already queued tool group.
    row['stopped_at'] = time.time()
    save(service, row)
    runs = service.formal.active_runs(service.task_id, row['scope'])
    cleanup = []
    for run in runs:
        cleanup.append(await service.formal.stop_command({'run_ref': run['run_ref'], 'reason': row['reason_code']},
                          task_id=service.task_id, request_scope=row['scope']))
    row['cleanup'] = {'status': 'not_started' if not runs else 'confirmed' if all(
        r.get('terminal') and r.get('cleanup_confirmed') for r in cleanup) else 'cleanup_unknown', 'runs': cleanup}
    out = Path(row['output_dir'])
    artifacts = {}
    try:
        evidence = out / '00.input_check' / ('input-identity-' + row['run_ref'] + '.json')
        observation = row['observation']
        text = ('# Scientific stage blocked\n\n'
                f"Request: {row['request_id']}\n\nReason: **{row['reason_code']}**\n\n"
                'The host ran the bounded GWAS input checker against the explicitly supplied inputs. '
                'A successful inspection exit code is not an association success.\n\n'
                'The Q rows need trustworthy sample identities: provide a labeled Q, or a row map tied '
                'to the exact Q and target TFAM hashes and its generating/ordering provenance. '
                'Equal sample counts or the current VCF order do not establish Q correspondence.\n\n'
                'No dependent conversion, association, plotting, installation or environment search is permitted '
                'after this receipt. Software availability is not the cause of this input block.\n\n'
                f"Inspected inputs (no other tasks searched):\n```json\n{json.dumps(row['inputs'], ensure_ascii=False, indent=2)}\n```\n\n"
                f"Observation: {evidence}\n\nExecution receipt: {row['receipt_ref']}\n\n"
                f"Cleanup: {row['cleanup']['status']}\n\nScientific result: blocked; no GWAS success claimed.\n")
        script = ('#!/usr/bin/env bash\nset -euo pipefail\n'
                  '# Reproduce only the bounded input inspection; never run association implicitly.\n'
                  'tmp=$(mktemp)\ntrap \'rm -f "$tmp"\' EXIT\n'
                  + shlex.join(row['checker_argv']) + ' > "$tmp"\ncat "$tmp"\n'
                  + shlex.quote(sys.executable) + ' - "$tmp" <<\'PY\'\n'
                  'import json,sys\nr=json.load(open(sys.argv[1]))\n'
                  'if r.get("blocked_operations"):\n print("Scientific input blocked: provide verified mapping", file=sys.stderr)\n sys.exit(20)\n'
                  'print("Input identity validated; downstream computation requires an explicit new action.")\nPY\n')
        values = {evidence: json.dumps(public(row), ensure_ascii=False, indent=2) + '\n',
                  out / 'report.md': text,
                  out / 'stage_status.tsv': 'stage\tstatus\treason\trequest_id\n'
                    + f"input_check\tcompleted\t{row['reason_code']}\t{row['request_id']}\n"
                    + ''.join(f"{stage}\tblocked\t{row['reason_code']}\t{row['request_id']}\n"
                              for stage in ['conversion', 'association', 'plots']),
                  out / 'reproduce.sh': script}
        for path, content in values.items():
            if any(p.is_symlink() for p in [path, *path.parents]):
                raise ValueError('SCIENTIFIC_DELIVERY_SYMLINK')
            if any(path.resolve() == Path(p).resolve() for p in row['inputs'].values()):
                raise ValueError('SCIENTIFIC_DELIVERY_INPUT_CONFLICT')
            # Reuse the existing descriptor-based, no-symlink, CAS writer and
            # its real edit receipt. Protected mode still needs its permission.
            args = {'path': str(path), 'content': content, 'expected_sha256':
                    hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ''}
            if service.execution_profile != 'native':
                from agent_core import execution_permissions as permissions
                allowed = service._permission('scientific-delivery-' + row['run_ref'] + '-' + path.name,
                    permissions.file_payload(service.files, 'write_file', args, service.task_id))
                if allowed['status'] != 'allowed':
                    raise ValueError('SCIENTIFIC_DELIVERY_PERMISSION_REQUIRED')
            written = service.files.write(args, task_id=service.task_id)
            artifacts[path.name] = {'path': str(path), 'sha256': written['sha256'], 'receipt_ref': written['receipt_ref']}
        row.update(artifacts=artifacts, delivery_status='complete', final_response=text)
    except (OSError, ValueError) as exc:
        row.update(artifacts=artifacts, delivery_status='delivery_error', delivery_error=str(exc))
    row['closed_at'] = time.time()
    save(service, row)
    def finish(state):
        state['task_state'].update(status='blocked', request_finish={
            'scope': row['scope'], 'status': 'blocked', 'reason_code': row['reason_code'],
            'source': 'scientific_input_check', 'scientific_success': False,
            'artifacts': artifacts, 'delivery_status': row['delivery_status'], 'finished_at': row['closed_at']})
    mutate(service.data_root, service.task_id, finish)
    return public(row)


async def admit(service, tool):
    row = current(service)
    if not row:
        return {'status': 'allowed'}
    if row.get('status') == 'validated':
        row = await prepare(service)
    if row.get('status') != 'validated':
        def record(state):
            audit = state['task_state'].setdefault('scientific_dispatch_rejections', [])
            if len(audit) < 64:
                audit.append({'scope': row['scope'], 'tool': tool, 'at': time.time(), 'reason_code': row.get('reason_code')})
        mutate(service.data_root, service.task_id, record)
        return {'status': 'blocked', 'execution_started': False, 'scientific_block': public(row)}
    return {'status': 'allowed'}
