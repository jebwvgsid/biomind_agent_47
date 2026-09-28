"""Small, read-only validators for declared work outputs and recorded observations.

No scientific executables, matrix imports, implicit reruns, or directory search.
"""
from pathlib import Path
import csv
import io
import json
import math
from agent_core.work_outputs import WorkOutputError, declared_samples, plan_parameters


def normalized_validation(value):
    """Read the descriptive form without changing a persisted Plan.

    Older candidates used ``custom`` for a description. Accept that one exact
    spelling through the existing assessment path; never discard machine fields
    or reinterpret an unknown executable rule as a model-only conclusion.
    """
    if isinstance(value, str):
        return {'description': value}
    if not isinstance(value, dict):
        return {}
    rule = dict(value)
    if (rule.get('kind') == 'custom' and set(rule) <= {'kind', 'description'}
            and isinstance(rule.get('description'), str) and rule['description'].strip()):
        rule.pop('kind')
    return rule


def read_small(path, limit=4 * 1024 * 1024):
    p = Path(path)
    if p.stat().st_size > limit: raise WorkOutputError('OUTPUT_REVIEW_SIZE_LIMIT', str(path), limit=limit)
    return p.read_text(encoding='utf-8')


def tsv_rows(text, required):
    reader = csv.DictReader(io.StringIO(text), delimiter='\t')
    missing = sorted(set(required) - set(reader.fieldnames or []))
    if missing:
        raise WorkOutputError('TABLE_COLUMNS_MISSING', missing=missing,
            observed_columns=reader.fieldnames or [], not_checked=['row keys', 'row values'])
    rows = list(reader)
    if not rows or any(None in r for r in rows): raise ValueError('empty or malformed table')
    return rows


def expected_inputs(state):
    """Unambiguous scalar projection; biological groups may contain many files.

    Explicit sample/group relations win by evidence, never by spelling. Multiple
    explicit sources for the same scalar relation must agree.
    """
    document = state.get('plan_document') or {}
    bindings = document.get('path_bindings') or {}
    spec = (state.get('current_task') or {}).get('task_spec') or {}
    inputs = {str(r.get('artifact_id') or r.get('input_id') or r.get('name') or ''):str(r.get('canonical_path') or r.get('path') or '') for r in spec.get('inputs') or [] if isinstance(r, dict)}
    grouped = {};identities={}
    for row in explicit_input_rows(state):
        grouped.setdefault(row['sample'],set()).add(row['path'])
        if row['member_id']:identities.setdefault(row['sample'],set()).add((row['role'],row['member_id']))
    from agent_core.input_manifest import manifest_view
    view=manifest_view(state)
    if view:
        for row in view['rows']:
            grouped.setdefault(row['sample'],set()).update(row['files'].values())
            identities.setdefault(row['sample'],set()).update((row['row_id'],role) for role in row['files'])
    result={}
    for sample in declared_samples(state):
        paths=set(grouped.get(sample) or ())
        legacy={str(v) for v in (bindings.get(sample+'_matrix'),inputs.get(sample+'_matrix'),inputs.get(sample)) if v}
        if len(legacy)>1 or (legacy and paths and not legacy<=paths):
            raise WorkOutputError('INPUT_MAPPING_CONFLICT',sample,field_path='$.inputs',value=sorted(paths|legacy),reason='explicit and legacy sample paths disagree')
        paths.update(legacy)
        if len(paths)==1 and len(identities.get(sample) or ())<=1:result[sample]=next(iter(paths))
        elif not paths:result[sample]=''
    return result


def explicit_input_rows(state):
    document=state.get('plan_document') or {}
    spec=(state.get('current_task') or {}).get('task_spec') or {}
    rows=[];seen={};scalars={}
    for source,values in [('$.inputs',document.get('inputs') or []),('$.task_spec.inputs',spec.get('inputs') or [])]:
        if not isinstance(values,list):
            raise WorkOutputError('INPUT_DECLARATIONS_INVALID',source,field_path=source,value=values,reason='expected an array')
        for index,row in enumerate(values):
            if not isinstance(row,dict):continue
            instance=row.get('input_instance') or {}
            sample=row.get('sample') or instance.get('group_id')
            if not sample:continue
            path=row.get('canonical_path') or row.get('resolved_path') or row.get('path')
            if not isinstance(path,str) or not path:
                raise WorkOutputError('INPUT_PATH_REQUIRED',sample,field_path=f'{source}[{index}].path',value=path)
            if not Path(path).is_absolute():
                from agent_core.input_manifest import project_for_state
                path=str(project_for_state(state)/path)
            role=str(row.get('role') or 'input');member=str(instance.get('member_id') or '')
            key=(str(sample),role,member)
            if key in seen and seen[key]!=path:
                raise WorkOutputError('INPUT_MAPPING_CONFLICT',sample,field_path=f'{source}[{index}].path',value=path,reason='same explicit sample/role/member has another path',paths=[seen[key],path])
            seen[key]=path
            if row.get('sample') and not instance:
                scalars.setdefault(str(sample),set()).add(path)
            rows.append({'sample':str(sample),'role':role,'member_id':member,'path':path})
    for sample,paths in scalars.items():
        all_paths={r['path'] for r in rows if r['sample']==sample}
        instances={(r['role'],r['member_id']) for r in rows if r['sample']==sample and r['member_id']}
        if len(paths)>1 or all_paths!=paths or len(instances)>1:
            raise WorkOutputError('INPUT_MAPPING_CONFLICT',sample,field_path='$.inputs',value=sorted(all_paths),reason='scalar sample declaration conflicts with another explicit relation')
    return rows


def json_observations(text):
    found = []
    for line in str(text).splitlines():
        if line.startswith('BIOMIND_EFFECT_EVIDENCE_JSON='): line = line.split('=',1)[1]
        if not line.startswith('{'): continue
        try: value = json.loads(line)
        except ValueError: continue
        if not isinstance(value,dict): continue
        found.append(value)
        found.extend(r.get('value') or {} for r in value.get('produces_observations') or [] if isinstance(r,dict))
    return found


def result_table_facts(state, tables, *, output_paths=False, output_root=None):
    """Aggregate actual status rows; set union cannot erase a failed/conflicting row."""
    from agent_core.input_manifest import manifest_view, project_for_state
    from agent_core.work_outputs import authorized_output, file_fingerprint
    view=manifest_view(state)
    samples=declared_samples(state)
    resolution=plan_parameters(state).get('resolution_bp')
    wanted={r['row_id'] for r in view['rows']} if view else set(samples)
    identity=view['row_identity'] if view else ['sample']
    found={};sources=[];files=[]
    for source,data in tables:
        for row in data:
            if row.get('row_id'):
                key=row['row_id']
                if view and all(k in row for k in identity):
                    explicit=json.dumps([row[k] for k in identity],ensure_ascii=False,separators=(',',':'))
                    if explicit!=key:raise ValueError('result row identity columns conflict with row_id')
            else:
                if any(not row.get(k) for k in identity):raise ValueError('result identity columns missing: '+','.join(identity))
                key=json.dumps([row[k] for k in identity],ensure_ascii=False,separators=(',',':')) if view else row['sample']
            if key not in wanted:raise ValueError('unexpected result row identity: '+key)
            if row.get('status') in {'reused', 'read_existing'}:
                raise ValueError('existing result was read, not newly executed; verify its original receipt, input version and method before result review: '+key)
            if row.get('status')!='success' or str(row.get('return_code'))!='0':
                raise ValueError('recorded per-sample failure or unknown per-row result despite process rc=0: '+key)
            if resolution is not None and str(row.get('resolution_bp'))!=str(resolution):
                raise ValueError('recorded result resolution mismatch: '+key)
            signature=(row.get('status'),str(row.get('return_code')),row.get('resolution_bp'),row.get('path'))
            if key in found and found[key]!=signature:raise ValueError('conflicting duplicate result row: '+key)
            found[key]=signature
            if output_paths:
                value=row.get('path')
                if not value:raise ValueError('result output path missing: '+key)
                project=project_for_state(state)
                target=Path(value)
                root=Path(output_root or project).absolute()
                if not target.is_absolute():target=root/target
                if target.is_symlink() or not target.absolute().is_relative_to(root) or not target.resolve().is_relative_to(root.resolve()):
                    raise ValueError('result member outside declared output root or symlink: '+str(target))
                target=authorized_output(state,target,project)
                fingerprint=file_fingerprint(target)
                if fingerprint.get('kind')=='directory' or not fingerprint.get('size'):
                    raise ValueError('result member must be a nonempty regular file: '+str(target))
                files.append({**fingerprint,'declared_path':str(target)})
        sources.append(source)
    if set(found)!=wanted or not wanted:raise ValueError('recorded result coverage incomplete')
    fact={'kind':'sample_results','samples':samples,'row_ids':sorted(found) if view else [],
        'resolution_bp':resolution,'all_succeeded':True,'source_attempt_ids':sorted(set(sources))}
    if view:fact['input_manifest_version']=view['version']
    if output_paths:fact['output_files']=list({f['path']:f for f in files}.values())
    return fact


def resolution_inputs(state):
    from agent_core.input_manifest import manifest_view
    view=manifest_view(state)
    if not view:
        expected=expected_inputs(state);samples=declared_samples(state)
        if not samples or any(not expected.get(s) for s in samples):raise ValueError('declared exact resolution inputs unavailable')
        return {(s,expected[s],'resolution') for s in samples}
    counts={s:sum(r['sample']==s for r in view['rows']) for s in view['samples']}
    targets=set()
    for row in view['rows']:
        if len(row['files'])!=1:raise ValueError('resolution requires one explicit matrix file per manifest row')
        handle=row['sample'] if counts[row['sample']]==1 else row['row_id']
        targets.add((handle,next(iter(row['files'].values())),'resolution'))
    return targets


def validate_output(state, row, path):
    """Select declared validators by format/role, never infer authority from a basename."""
    validation = row.get('validation') if isinstance(row.get('validation'),dict) else {}
    if validation.get('kind') and validation['kind'] not in {
            'matrix_sample_sheet','tad_sample_sheet','result_manifest','tad_preflight',
            'matrix_balance_report','matrix_balance_preflight','balance','tad_run_summary',
            'run_summary','stage_status','tad_domains','tad_boundaries','tad_summary',
            'samples','sample_sheet','samples_tsv'}:
        raise WorkOutputError('OUTPUT_VALIDATION_KIND_UNSUPPORTED', str(path),
            kind=validation['kind'], rule_source='output.validation.kind')
    role = validation.get('kind') or row.get('role') or row.get('artifact_id') or row.get('original_reference')
    fmt = row.get('format') or ''
    facts = {}
    samples = declared_samples(state)
    resolution = plan_parameters(state).get('resolution_bp')
    if role in {'matrix_sample_sheet','tad_sample_sheet'}:
        data = tsv_rows(read_small(path), ['sample','matrix'])
        actual = [r['sample'] for r in data]
        if len(actual) != len(set(actual)) or (samples and set(actual) != set(samples)):
            raise ValueError('sample set mismatch or duplicate sample')
        expected = expected_inputs(state)
        for r in data:
            if not Path(r['matrix']).is_absolute() or not expected.get(r['sample']) or r['matrix'] != expected[r['sample']]:
                raise ValueError('matrix does not match declared formal input')
        facts = {'kind':'samples', 'samples':actual, 'inputs':{r['sample']:r['matrix'] for r in data}}
    elif role == 'result_manifest':
        delimiter=',' if fmt=='csv' else '\t'
        reader=csv.DictReader(io.StringIO(read_small(path)),delimiter=delimiter)
        data=list(reader)
        if any(None in r for r in data):raise ValueError('malformed result manifest')
        root=Path(validation.get('output_root') or Path(path).parent)
        if not root.is_absolute():
            from agent_core.input_manifest import project_for_state
            root=project_for_state(state)/root
        from agent_core.work_outputs import authorized_output
        from agent_core.input_manifest import project_for_state
        authorized_output(state,root,project_for_state(state))
        facts=result_table_facts(state,[('output:'+str(path),data)],output_paths=True,output_root=root)
    elif role == 'tad_preflight':
        # call_tads --preflight-only writes this table, not the separate balance
        # helper's schema and not observation JSON on stdout.
        data = tsv_rows(read_small(path), ['sample','matrix','resolution_bp','balanced','status','message'])
        if not samples or len(data) != len(samples) or {r['sample'] for r in data} != set(samples):
            raise ValueError('preflight sample coverage missing, duplicated or unexpected')
        expected = expected_inputs(state)
        if resolution is None:
            raise ValueError('preflight working resolution is not declared')
        for row in data:
            source = expected.get(row['sample']) or ''
            if '::' in source:
                group = source.split('::', 1)[1]
                if group.startswith('/resolutions/') and group != '/resolutions/' + str(resolution):
                    raise ValueError('preflight explicit input URI resolution conflicts with the declared working resolution')
            # Match call_tads.build_matrix_uri: retain an explicit URI, or select
            # the declared resolution group in a declared .mcool path. This does
            # not discover a file or invent a sample/path relationship.
            uri = source if '::' in source or not source.lower().endswith('.mcool') else source + '::/resolutions/' + str(resolution)
            if not source or row['matrix'] != uri:
                raise ValueError('preflight input does not match exact declared sample path')
            if str(row['resolution_bp']) != str(resolution):
                raise ValueError('preflight resolution does not match the declared working resolution')
            if row['status'] != 'success' or not isinstance(row['balanced'], str) or row['balanced'].lower() != 'yes':
                raise ValueError('preflight records a failed or unusable sample')
        facts = {'kind':'balance', 'resolution_bp':resolution, 'samples':samples, 'usable':True,
                 'scope':'recorded preflight weight availability, not independent balance quality'}
    elif role in {'matrix_balance_report','matrix_balance_preflight','balance'}:
        data = tsv_rows(read_small(path), ['sample','matrix','resolution_bp','usable_for_3d_genome_analysis'])
        selected = [r for r in data if str(r['resolution_bp']) == str(resolution)]
        if len(selected) != len(samples) or {r['sample'] for r in selected} != set(samples):
            raise ValueError('working resolution or sample row missing/duplicated')
        expected = expected_inputs(state)
        for r in selected:
            if r['matrix'] != expected.get(r['sample']): raise ValueError('balance input mismatch')
            if r['usable_for_3d_genome_analysis'].lower() != 'yes': raise ValueError('working resolution is not usable')
        facts = {'kind':'balance', 'resolution_bp':resolution, 'samples':samples, 'usable':True}
    elif role in {'tad_run_summary','run_summary'}:
        data = tsv_rows(read_small(path), ['sample','resolution_bp','status','return_code'])
        if len(data) != len(samples) or {r['sample'] for r in data} != set(samples): raise ValueError('sample result missing/duplicated')
        for r in data:
            if r['status'] in {'reused', 'read_existing'}:
                raise ValueError('existing result was read, not newly executed; original provenance and result review are required')
            if str(r['resolution_bp']) != str(resolution) or r['status'] != 'success' or str(r['return_code']) != '0':
                raise ValueError('individual sample failed or resolution mismatch')
        facts = {'kind':'sample_results', 'samples':samples, 'resolution_bp':resolution, 'all_succeeded':True}
    elif validation.get('kind') == 'stage_status':
        # Only an explicitly selected internal interface has framework keys.
        # Gate supplies a prior verified snapshot when consuming unchanged bytes.
        data = tsv_rows(read_small(path), ['step_id','status'])
        states = {s['step_id']:s['status'] for s in state.get('steps') or []}
        positions = {}; problems = []
        for number, record in enumerate(data, 2):
            positions.setdefault(record['step_id'], []).append(number)
            expected = states.get(record['step_id'])
            if expected is None or record['status'] != expected:
                problems.append({'code':'STEP_STATE_MISMATCH', 'field':'status', 'row':number,
                    'key':record['step_id'], 'expected':expected, 'actual':record['status']})
        problems[:0] = [{'code':'DUPLICATE_STEP_KEY', 'field':'step_id', 'key':key, 'rows':lines}
                       for key, lines in positions.items() if len(lines) > 1]
        if problems:
            raise WorkOutputError('INTERNAL_STEP_TABLE_INVALID', object_type='internal_step_snapshot',
                rule_source='output.validation.kind=stage_status', problems=problems[:32],
                problem_count=len(problems), truncated=len(problems)>32)
        facts = {'kind':'stage_status','rows':len(data), 'step_states':states,
                 'scope':'explicit internal interface at the verified snapshot, not scientific stage states'}
    elif fmt == 'bed' or role in {'tad_domains','tad_boundaries'}:
        count = 0
        for line in read_small(path).splitlines():
            if not line or line.startswith(('#','track','browser')): continue
            cols = line.split('\t')
            if len(cols) < 3 or not cols[0] or int(cols[1]) < 0 or int(cols[2]) <= int(cols[1]): raise ValueError('invalid BED interval')
            count += 1
        if not count and validation.get('allow_empty') is not True: raise ValueError('no BED intervals')
        facts = {'kind':'intervals', 'sample':row.get('sample'), 'interval_count':count}
    elif fmt in {'tsv','csv'} or role in {'stage_status','tad_summary','samples','sample_sheet','samples_tsv'}:
        reader=csv.DictReader(io.StringIO(read_small(path)),delimiter=',' if fmt=='csv' else '\t')
        data=list(reader)
        if (not data and validation.get('allow_empty') is not True) or any(None in r or None in r.values() for r in data):raise ValueError('empty or malformed table')
        if role == 'tad_summary' and (len(data) != len(samples) or {r['sample'] for r in data} != set(samples)): raise ValueError('summary sample set mismatch')
        facts = {'kind':'table', 'rows':len(data)}
    return facts


def validate_step_facts(state, step, receipts, dependency_facts, requirement=None):
    raw_rule = requirement or step.get('validation') or {}
    rule = normalized_validation(raw_rule)
    kind = rule.get('kind')
    if 'kind' in rule and kind not in VALIDATION_KINDS:
        raise ValueError('VALIDATION_KIND_UNSUPPORTED: no execution verifier for '+str(kind))
    samples = declared_samples(state)
    resolution = plan_parameters(state).get('resolution_bp')
    if kind in {'resolution','observation'}:
        from agent_core.plan_contract import plan_admission_policy_enabled
        # Runtime must still understand actual observation targets and mappings.
        # The suspended admission field whitelist must not reject a stored Plan
        # again here; extra declarations do not constitute checked machine facts.
        errors, _ = validation_diagnostics(
            [{'validation':rule}], check_field_policy=plan_admission_policy_enabled())
        if errors: raise ValueError(json.dumps(errors))
        expected = expected_inputs(state)
        if kind == 'resolution':
            wanted = resolution_inputs(state)
        else:
            wanted = {(c['resource_handle'],c['path'],c['predicate']) for c in rule['checks']}
            # Checks may refer to explicit step paths even when no samples exist.
            bindings = (state.get('plan_document') or {}).get('path_bindings') or {}
            declared = {**bindings, **expected}
            for handle,path,_ in wanted:
                if declared.get(handle) != path: raise ValueError('observation input identity/path is not an exact declared binding')
        parameters = {**plan_parameters(state), **(step.get('parameters') if isinstance(step.get('parameters'),dict) else {})}
        if any(k in parameters and parameters[k] != v for k,v in (rule.get('parameters') or {}).items()):
            raise ValueError('observation parameters conflict with the formal task/step')
        covered, sources = set(), []
        for actual in receipts:
            if any((actual.get('env') or {}).get(k) != v for k,v in (rule.get('execution_env') or {}).items()):
                raise ValueError('observation execution environment mismatch')
            for value in json_observations(actual.get('stdout','')):
                if kind == 'resolution':
                    if 'resolution_bp' not in value or 'inputs' not in value: continue
                    if str(value.get('resolution_bp')) != str(resolution): raise ValueError('resolution parameter mismatch')
                    rows = value.get('inputs') or []
                    for r in rows:
                        key = (r.get('resource_handle'),r.get('path'),'resolution')
                        if key not in wanted: raise ValueError('resolution input identity/path mismatch')
                        if value.get('ok') is not True or r.get('present') is not True or r.get('target_group') not in {'/resolutions/'+str(resolution),'/'}:
                            raise ValueError('negative or unconfirmed resolution observation')
                        if r.get('target_group') == '/' and str(r.get('root_resolution')) != str(resolution): raise ValueError('root resolution unconfirmed')
                        covered.add(key)
                else:
                    if value.get('kind') != 'observation': continue
                    if (value.get('parameters') or {}) != (rule.get('parameters') or {}): raise ValueError('observation parameters mismatch')
                    for r in value.get('checks') or []:
                        key = (r.get('resource_handle'),r.get('path'),r.get('predicate'))
                        if key not in wanted: raise ValueError('observation input/scope mismatch')
                        if r.get('value') is not True: raise ValueError('negative or unconfirmed observation')
                        covered.add(key)
                sources.append(actual.get('attempt_id'))
        if covered != wanted: raise ValueError('recorded observation coverage incomplete')
        return [{'kind':kind, 'resolution_bp':resolution if kind=='resolution' else None,
            'samples':samples, 'present':True, 'checks':[list(k) for k in sorted(covered)],
            'source_attempt_ids':sorted(set(sources)), 'parameters':rule.get('parameters') or {}}]
    if kind == 'gate':
        good_resolution = any(f.get('kind')=='resolution' and f.get('present') and str(f.get('resolution_bp'))==str(resolution) and set(f.get('samples') or [])==set(samples) for f in dependency_facts)
        good_balance = any(f.get('kind')=='balance' and f.get('usable') and str(f.get('resolution_bp'))==str(resolution) and set(f.get('samples') or [])==set(samples) for f in dependency_facts)
        if not good_resolution or not good_balance: raise ValueError('preflight gate lacks accepted resolution/balance facts')
        return [{'kind':'gate','passed':True,'resolution_bp':resolution,'samples':samples}]
    if kind == 'sample_results':
        tables=[]
        # Parse the actual recorded TSV segment, not the outer shell exit code.
        for actual in receipts:
            lines = str(actual.get('stdout','')).splitlines()
            for i,line in enumerate(lines):
                header=line.split('\t')
                if not {'status','return_code'}<=set(header) or not ({'sample','row_id'}&set(header)):continue
                body = [line]
                for following in lines[i+1:]:
                    if not following.strip() or '\t' not in following: break
                    body.append(following)
                data=tsv_rows('\n'.join(body), ['status','return_code'])
                tables.append((actual.get('attempt_id') or '',data))
        if tables:
            return [result_table_facts(state,tables)]
        raise ValueError('recorded per-sample results unavailable')
    if not step.get('outputs'):
        raise ValueError('a no-output step requires registered read evidence and assessment, or explicit observation/gate validation')
    return []


VALIDATION_KINDS = ('resolution', 'gate', 'sample_results', 'observation')
VALIDATION_GUIDANCE = '机器检查显式写 validation.kind: resolution（精确输入路径与分辨率），gate（已提交的 resolution/balance），sample_results（逐样本结果），或 observation（checks 数组，每项 resource_handle、绝对 path、predicate=exists|readable|nonempty）。观察记录使用相同 checks 加 value=true/false；参数在 parameters 中精确对应；环境约束用 execution_env。观察必须来自本任务/当前 Plan/步骤的终态 receipt。自然语言核验要求由原 Generate 引用已登记 evidence_id/attempt_id 并提交 assessment；模型结论不等于独立机器通过。outputs=[] 可用真实读取证据与明确 assessment，或适用的 observation/gate。'

def validation_diagnostics(steps, *, check_field_policy=True):
    errors, notes = [], []
    for index, step in enumerate(steps):
        value = step.get('validation')
        rule = normalized_validation(value)
        path = '$.steps[%d].validation' % index
        if not value: continue
        if 'kind' not in rule:
            notes.append({'field_path':path,'status':'not_evaluated','description':value}); continue
        if rule['kind'] not in VALIDATION_KINDS:
            errors.append({'code':'VALIDATION_KIND_UNSUPPORTED','field_path':path+'.kind','supported':list(VALIDATION_KINDS)});continue
        allowed = {'kind','description','execution_env'} | ({'checks','parameters'} if rule['kind']=='observation' else set())
        for field in sorted(set(rule)-allowed) if check_field_policy else []:
            errors.append({'code':'VALIDATION_FIELD_UNSUPPORTED','field_path':path+'.'+field,
                'value':rule[field],'reason':'field is not supported for this explicit validation kind','supported_fields':sorted(allowed)})
        if rule['kind'] == 'observation':
            checks = rule.get('checks')
            if not isinstance(checks,list) or not checks:
                errors.append({'code':'OBSERVATION_CHECKS_REQUIRED','field_path':path+'.checks'})
            else:
                for j,c in enumerate(checks):
                    if not isinstance(c,dict) or not isinstance(c.get('resource_handle'),str) or not c['resource_handle'] or not isinstance(c.get('path'),str) or not Path(c['path']).is_absolute() or not isinstance(c.get('predicate'),str) or c['predicate'] not in {'exists','readable','nonempty'}:
                        errors.append({'code':'OBSERVATION_CHECK_INVALID','field_path':path+'.checks[%d]'%j})
        for name in ('parameters','execution_env'):
            if name in rule and not isinstance(rule[name],dict): errors.append({'code':'VALIDATION_MAPPING_REQUIRED','field_path':path+'.'+name})
    return errors, notes
