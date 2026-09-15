"""New isolated continuous incident regressions; real graph, fixed external leaves.

Never execute historical candidates. Use export_and_run / the existing guard.
The synthetic process is deliberately NOT a snpEff implementation.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import importlib.util
import os
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tests.a1_unified_fix.test_f6_graph import F6Graph, OPS, PYTHON, SID, digest, save


def selected_snpeff_constructor(prefix, *, operation, operation_args=()):
    name = 'incident_selected_snpeff_renderer'
    if name not in sys.modules:
        source = Path(__file__).resolve().parents[2] / 'skills/genomics/snpeff-annotation/scripts/render_snpeff_annotation_scripts.py'
        spec = importlib.util.spec_from_file_location(name, source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name].snpeff_argv(prefix, operation=operation, operation_args=operation_args)


class IncidentGraph(F6Graph):
    def __init__(self, scenario):
        super().__init__('6793_' + scenario)
        self.scenario = scenario
        self.build_calls = 0
        self.timeline = []
        self.preflight_calls = 0

    async def setup(self):
        await super().setup()
        import bio_agents
        del self.agent._prompt_user
        self.input_patch = patch.object(bio_agents, 'safe_input', lambda *a, **k: 'y')
        self.input_patch.start()

    def plan(self):
        plan = super().plan()
        plan['metadata']['resource_contract_version'] = 3
        for artifact in plan['proposed_artifacts']:
            artifact.pop('role_associations', None)
        for step in plan['steps']:
            step['resource_uses'] = [{'resource_ref': ref, 'access': 'read',
                **({'role': 'artifact.snpeff_data_dir'} if ref == 'plan.db' else {})}
                for ref in step.pop('inputs')]
            if step.get('workspace_mutations'):
                step['resource_uses'].append({'resource_ref': 'plan.db', 'access': 'mutate',
                                             'role': 'artifact.snpeff_data_dir'})
        return plan

    def science_source(self):
        return ("import sys\nif '--synthetic-fail' in sys.argv:\n"
                "    print('SYNTHETIC failure fixture; not real snpEff')\n"
                "    for i in range(145): print('synthetic diagnostic line %03d' % i)\n"
                "    print(\"Command: 'ann'\")\n"
                "    print(\"Unknown option '-gtf22'\")\n"
                "    raise SystemExit(255)\n" + super().science_source())

    def action(self, operation, messages):
        action = super().action(operation, messages)
        if self.scenario == 'carrier_missing' and operation == OPS[0]:
            self.preflight_calls += 1
            if self.preflight_calls == 1:
                # New synthetic malformed candidate. It is never executed.
                action.pop('language', None)
                action.pop('argv')
                action['code'] = '# BIOMIND_EFFECT_HELPER\nprint(emit_effect_evidence({}))\n'
            return action
        if self.scenario in {'last_chance', 'exhausted'}:
            if operation != OPS[0]:
                return action
            self.preflight_calls += 1
            number = self.preflight_calls
            if number == 1:
                action['env'] = []
            elif number == 2:
                action['env'] = {'BAD': 1}
            elif number == 3:
                action['argv'][-1] = '# BIOMIND_EFFECT_HELPER\n' + action['argv'][-1]
            elif number == 4:
                action['argv'][-1] += '\nif then\n'
                action.update(code=action['argv'][-1], language='bash')
            elif number == 5 and self.scenario == 'exhausted':
                action.update(code=action['argv'][-1], language='bash', operation='wrong_explicit_operation')
            return action
        if operation != 'build_database':
            return action
        self.build_calls += 1
        call = self.build_calls
        marker = '# BIOMIND_EFFECT_HELPER'
        body = marker + '\nfrom pathlib import Path\nimport os, subprocess, json\n'
        body += 'db=Path(os.environ["SNPEFF_DB"]); genome=os.environ["GENOME_VERSION"]\n'
        from agent_core.plan_tracker import load_plan_state
        durable = load_plan_state(self.root, self.task)
        config = next(row['canonical_path'] for row in durable['plan_artifacts'].values()
                      if row.get('semantic_view', {}).get('role') == 'snpeff_config')
        for path in [str(self.reference), str(self.annotation), config]:
            assert path in str(messages)
            body += 'assert Path(' + repr(path) + ').read_bytes()\n'
        if call >= 3:
            body += 'assert (db/"data"/genome/"genes.gtf").is_file(), "CURRENT_RECOVERY_LAYOUT_CHECK"\n'
        body += 'log=db/"data"/genome/"build.log"\n'
        command = selected_snpeff_constructor([PYTHON, '-I', '-S', str(self.java), '-jar', str(self.jar)], operation='build')
        self.timeline.append({'event': 'selected_constructor', 'operation': 'build', 'argv': command,
            'implementation': 'skills/genomics/snpeff-annotation/scripts/render_snpeff_annotation_scripts.py:snpeff_argv',
            'external_leaf': 'Python synthetic process; no Java/JAR execution'})
        body += 'argv=' + repr(command) + '+[str(db),genome]'
        if call <= 2:
            body += '+["--synthetic-fail"]'
        body += '\nwith log.open("w") as out:\n    result=subprocess.run(argv,stdout=out,stderr=subprocess.STDOUT)\n'
        body += 'predictor=db/"data"/genome/"snpEffectPredictor.bin"\n'
        body += 'ready=result.returncode==0 and predictor.is_file() and predictor.stat().st_size>0\n'
        body += 'print(json.dumps({"inner_returncode":result.returncode,"actual_argv":argv,"build_log":str(log),"predictor_exists":predictor.is_file()}))\n'
        body += 'print(emit_effect_evidence({"produces_observations":{"snpeff_database_ready":{"value":ready}}}))\n'
        if call == 2:
            action.pop('language', None)  # argv alone declares the real Python interpreter.
            action['argv'] = [PYTHON, '-I', '-S', '-c', body]
        else:
            wrapper = shlex.quote(PYTHON) + " -I -S - <<'PY'\n" + body + 'PY\n'
            if call in (1, 3) or (self.scenario == 'repeat' and call == 4):
                wrapper = marker + '\n' + wrapper
            action['argv'] = ['bash', '--noprofile', '--norc', '-c', wrapper]
        if self.scenario == 'repeat' and call == 4:
            action = deepcopy(self.candidate_c)
        if call == 3:
            self.candidate_c = deepcopy(action)
        return action

    async def provider(self, messages, *, stage, **kwargs):
        response = await super().provider(messages, stage=stage, **kwargs)
        if stage == 'generate':
            from agent_core.plan_tracker import load_plan_state
            durable = load_plan_state(self.root, self.task)
            self.timeline.append({'event': 'provider_request', 'operation': self.current_operation(),
                'build_call': self.build_calls, 'ledger': deepcopy(durable['task_state'].get('candidate_repair_state', {})),
                'steps': deepcopy(durable['steps']), 'request': deepcopy(self.requests[-1])})
            if self.scenario == 'stop' and self.current_operation() == 'build_database' and self.build_calls == 4:
                response = AIMessage(content=json.dumps({'action_type': 'blocked',
                    'reason': 'No new basis; synthetic internal process returned 255 and predictor is absent. Preserve prior commits.'}),
                    response_metadata={'finish_reason': 'stop'})
                self.provider_candidates[-1] = {'operation': 'build_database', 'raw': response.content}
        return response

    class Recovery:
        def __init__(self, case): self.case = case

        async def ainvoke(self, messages, **kwargs):
            c = self.case
            c.counts['recovery'] += 1
            c.requests.append({'stage': 'recovery', 'messages': [
                {'type': m.type, 'content': m.content} for m in messages]})
            from agent_core.plan_tracker import load_plan_state
            durable = load_plan_state(c.root, c.task)
            registry = durable['task_state']['inspection_ref_registry']
            logs = [row for row in registry.values() if (row.get('scope') or {}).get('object_kind') == 'failure_log']
            # A pre-fix run records the precise publication gap, then continues
            # to expose the old cross-Recovery comparison and STOP defects.
            if logs:
                assert len(logs) == 1 and logs[0]['scope']['readable']
                ref = logs[0]['ref_id']
                assert ref in str(messages) and 'evidence.read' in str(messages)
                if not hasattr(c, 'log_protection'):
                    from agent_core.workflow.inspection_evidence import InspectionObjectReader, INSPECTION_SCOPE_FIELDS, InspectionEvidenceError
                    entry = logs[0]
                    scope = {k: entry['scope'][k] for k in INSPECTION_SCOPE_FIELDS}
                    reader = InspectionObjectReader(c.root, scope=scope)
                    body = reader.read_body(entry, entry['locator'])
                    assert body['log']['attempt_id'] == scope['attempt_id']
                    assert body['log']['status'] == 'captured'
                    assert 'Unknown option' in ''.join(body['log']['text_lines'])
                    for field, value in [('task_id', 'foreign-task'), ('step_id', 'other-step'), ('attempt_id', 'other-attempt')]:
                        with pytest.raises(InspectionEvidenceError):
                            InspectionObjectReader(c.root, scope={**scope, field: value}).read(entry, entry['locator'])
                    forged = deepcopy(entry)
                    forged['ref_id'] = 'inspection:sha256:' + '0' * 64
                    with pytest.raises(InspectionEvidenceError):
                        reader.read(forged, forged['locator'])
                    c.log_protection = {'scope': scope, 'entry': entry, 'snapshot': body,
                        'cross_task_step_attempt_and_unregistered_ref': 'rejected'}
                    c.timeline.append({'event': 'log_scope_protection', **c.log_protection})
                tool_messages = [json.loads(m.content) for m in messages if m.type == 'tool']
                pages = [item for item in tool_messages if item.get('status') == 'ok' or 'result' in item or 'content' in item]
                if not tool_messages:
                    return AIMessage(content='', tool_calls=[{'name': 'step_inspect', 'id': 'wrong-kind-log',
                        'args': {'ref_id': ref}}])
                if 'INSPECTION_REF_KIND_MISMATCH' in str(tool_messages[-1]):
                    assert 'evidence.read' in str(tool_messages[-1])
                    return AIMessage(content='', tool_calls=[{'name': 'evidence_read', 'id': 'read-log-first',
                        'args': {'evidence_ref': ref, 'start_line': 1, 'end_line': 120}}])
                # Preserve exact tool envelopes before interpreting pagination.
                c.timeline.append({'event': 'log_read', 'tool_messages': tool_messages})
                def find_cursor(value):
                    if isinstance(value, dict):
                        if value.get('next_start_line'): return value
                        for child in value.values():
                            found = find_cursor(child)
                            if found: return found
                    return None
                cursor = find_cursor(tool_messages[-1])
                if cursor:
                    start = cursor['next_start_line']
                    return AIMessage(content='', tool_calls=[{'name': 'evidence_read', 'id': 'read-log-' + str(start),
                        'args': {'evidence_ref': ref, 'start_line': start, 'end_line': start + 119}}])
                assert 'Unknown option' in str(tool_messages), tool_messages
            else:
                c.timeline.append({'event': 'failure_log_not_published'})
            packet = durable['task_state']['causal_failure_packets'][durable['task_state']['active_failure_packet_ref']]['failure_packet']
            directive = {'schema_version': 1, 'directive_id': 'incident-recovery', 'task_id': c.task,
                'action': 'retry_step', 'target_step_id': durable['current_step_id'],
                'summary': 'Synthetic build returned 255; predictor absent.', 'reason_code': 'SYNTHETIC_BUILD_FAILED',
                'instruction': 'CURRENT_RECOVERY_LAYOUT_CHECK: retain the current config/layout check and keep build immediately after launcher/JAR. Preserve actual log and inner returncode; no success without predictor.',
                'evidence_refs': [r for r in packet['inspection_refs'] if r.startswith('inspection:')]}
            return AIMessage(content='', tool_calls=[{'name': 'submit_recovery_directive', 'id': 'submit-retry', 'args': directive}])

    async def execute(self, path, *, expected_sha256, lifecycle_callback=None, env=None, **kwargs):
        # The base fixture asserts a Bash-only native carrier. This case also
        # exercises Python argv-only without rewriting its approved carrier.
        from execution.executor import run_script_path
        path = Path(path)
        approved = path.with_name('approved.sh').read_bytes()
        assert digest(path.read_bytes()) == expected_sha256
        assert digest(approved) == self.approvals[-1]['sha256']
        argv = shlex.split(approved.decode())
        assert argv[0] in {PYTHON, '/bin/bash', 'bash'}
        assert '-lc' not in argv and '--login' not in argv
        clean = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}
        clean.update({k: env[k] for k in ['GENOME_VERSION', 'SNPEFF_DB'] if k in (env or {})})
        def observe():
            if 'SNPEFF_DB' not in clean: return {}
            root = Path(clean['SNPEFF_DB'])
            assert root.is_relative_to(self.root / 'task_outputs' / self.task / 'plan_artifacts')
            result = {}
            for item in root.rglob('*'):
                assert not item.is_symlink()
                if item.is_file():
                    assert item.stat().st_size < 10000
                    result[str(item.relative_to(root))] = digest(item.read_bytes())
            return result
        before = observe()
        boot = sys.modules['__main__']
        boot.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
        boot.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)] = clean
        try:
            out, err, rc = await run_script_path(str(path), expected_sha256=expected_sha256,
                lifecycle_callback=lifecycle_callback, profile=False, env=clean,
                managed_log_paths=kwargs.get('managed_log_paths'))
        finally:
            boot.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path))
            boot.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path), None)
        self.executions.append({'operation': self.current_operation(), 'attempt_id': path.parent.name,
            'rc': rc, 'stdout': out, 'stderr': err, 'approved_sha256': digest(approved),
            'executed_sha256': expected_sha256, 'approved_code': approved.decode(), 'wrapper': path.read_text(),
            'actual_workspace_before': before, 'actual_workspace_after': observe()})
        return out, err, rc

    async def run(self):
        import bio_agents
        from agent_core.plan_tracker import load_plan_state, commit_runtime_evidence_state
        from agent_core.runtime_evidence import make_runtime_evidence
        runner = self.case._runner_state(self.task)
        runner['workflow_routing_schema_version'] = 2
        runner['messages'] = [HumanMessage(content=self.goal)]
        scope = self.agent._runtime_evidence_scope(SID)
        evidence = make_runtime_evidence({'skill_id': SID, 'checked_env': scope['environment_id'], 'status': 'ready',
            'resolved_runtime_paths': {'RESOLVED_JAVA_BIN': str(self.java), 'RESOLVED_SNPEFF_JAR': str(self.jar)}},
            task_id=self.task, skill_scope=scope, observed_at=datetime.now(timezone.utc).isoformat(),
            source_ref='explicit-new-synthetic-science-observation')
        commit_runtime_evidence_state(self.root, self.task, [evidence])
        app = self.agent._app_for_task(self.task, runner)
        config = {'configurable': {'thread_id': self.task}, 'recursion_limit': 100}
        incoming = runner
        async def service_callbacks():
            while True:
                await asyncio.sleep(.001)
        self.service = asyncio.create_task(service_callbacks())
        with patch.object(bio_agents, 'run_script_path', self.execute):
            for _ in range(15):
                async for event in app.astream(incoming, config=config, stream_mode='updates'):
                    self.events.append(event)
                    if 'commit_step' in event:
                        self.commits.append(deepcopy(event['commit_step']))
                        self.state_after_commits.append(deepcopy(load_plan_state(self.root, self.task)))
                snap = app.get_state(config)
                if not snap.next: break
                assert snap.tasks and snap.tasks[0].interrupts
                incoming = bio_agents.Command(resume='y')
        self.values = app.get_state(config).values
        self.durable = load_plan_state(self.root, self.task)
        self.terminal = await self.agent._finalize_managed_terminal(self.task, app.get_state(config))
        self.durable = load_plan_state(self.root, self.task)
        save(self.mode + '-timeline', self.timeline, full=True)
        return self.values, self.durable

    async def close(self):
        self.input_patch.stop()
        await super().close()


@pytest.mark.parametrize('scenario', ['success', 'stop', 'repeat', 'last_chance', 'exhausted', 'carrier_missing'])
def test_continuous_incident(scenario):
    async def run():
        fixture = IncidentGraph(scenario)
        await fixture.setup()
        try:
            values, durable = await fixture.run()
            save('6793-' + scenario + '-witness', {'timeline': fixture.timeline, 'values': values,
                'durable': durable, 'terminal': fixture.terminal}, full=True)
            if scenario in {'last_chance', 'exhausted'}:
                assert fixture.preflight_calls == 5
                rows = sorted(durable['task_state']['candidate_repair_state']['events'].values(), key=lambda r: r['repair_ordinal'])
                assert [r['policy_receipt']['remaining'] for r in rows[:4]] == [3, 2, 1, 0]
                if scenario == 'last_chance':
                    assert len(fixture.commits) == 6 and durable['task_state']['status'] == 'completed'
                else:
                    assert fixture.executions == fixture.commits == []
                    assert values['generate_outcome']['payload']['error_code'] == 'GENERATE_REPAIR_BUDGET_EXHAUSTED'
                    assert fixture.terminal['terminal_diagnostic']['delivery_status'] == 'complete'
                return
            assert fixture.build_calls >= 4, (fixture.build_calls, values.get('generate_outcome'))
            assert not any(row['event'] == 'failure_log_not_published' for row in fixture.timeline)
            attempts = list(durable['provenance_state']['attempts'].values())
            build = [a for a in attempts if a['step_id'] == 'build_database']
            assert build[0]['returncode'] == 0
            failed_execution = [row for row in fixture.executions if row['operation'] == 'build_database'][0]
            assert '"inner_returncode": 255' in failed_execution['stdout']
            assert '"predictor_exists": false' in failed_execution['stdout']
            assert not any(path.endswith('snpEffectPredictor.bin') for path in failed_execution['actual_workspace_after'])
            requests = {row['build_call']: row for row in fixture.timeline
                        if row['event'] == 'provider_request' and row.get('operation') == 'build_database'}
            def repairs(request):
                found = []
                for message in request['request']['messages']:
                    try: value = json.loads(message['content'])
                    except (ValueError, TypeError): continue
                    if isinstance(value, dict) and value.get('kind') == 'generate_validator_feedback':
                        if value.get('feedback', {}).get('action_repair'):
                            found.append(value['feedback']['action_repair'])
                return found
            assert repairs(requests[2])
            assert repairs(requests[3]) == []  # A has no current instruction authority.
            current = repairs(requests[4])[-1]
            assert current['previous_candidate']['parsed_candidate'] == fixture.candidate_c
            diagnostic = current['exact_violations'][0]['actual']
            assert diagnostic['marker_count'] == 2 and diagnostic['template_available'] is True
            assert [p['line'] for p in diagnostic['marker_positions']] == [1, 3]
            assert diagnostic['carrier_source'] == 'argv[4]'
            assert current['correction_policy']['limit'] == 4
            assert current['correction_policy']['remaining_after_this_call'] == 2
            assert 'Preserve the current admitted Recovery directive' in current['repair_instruction']
            assert 'CURRENT_RECOVERY_LAYOUT_CHECK' in str(requests[4]['request']['messages'])
            old = next(row for row in requests[3]['ledger']['events'].values() if row['scope']['step_id'] == 'build_database')
            assert old['resolved_by']['action_id'] == build[0]['action_id']
            assert old['policy_receipt']['remaining'] == 3
            assert len([r for r in fixture.timeline if r['event'] == 'log_read']) >= 2
            if scenario == 'carrier_missing':
                first = [row for row in fixture.timeline if row['event'] == 'provider_request' and row['operation'] == OPS[0]]
                assert len(first) == 2
                actual = repairs(first[1])[-1]['exact_violations'][0]
                assert actual['code'] == 'EFFECT_HELPER_CARRIER_UNDECLARED'
                assert actual['actual']['declared_language'] == 'UNKNOWN'
                assert actual['actual']['marker_count'] == 1 and actual['actual']['template_available']
            assert len(fixture.commits) == (6 if scenario in {'success', 'carrier_missing'} else 2)
            if scenario in {'success', 'carrier_missing'}:
                assert durable['task_state']['status'] == 'completed'
                assert len(build) == 2 and len(fixture.executions) == 7
            else:
                assert len(build) == 1 and len(fixture.executions) == 3
                assert all(a['step_id'] not in OPS[3:] for a in attempts)
                assert all(s['status'] != 'failed' for s in durable['steps'] if s['step_id'] in OPS[3:])
                diagnostic = fixture.terminal['terminal_diagnostic']
                assert diagnostic['persistence_status'] == 'committed'
                assert diagnostic['delivery_status'] == 'complete'
                assert diagnostic['execution']['attempt_id'] == ''
                assert diagnostic['causal_execution']['attempt_id'] == build[0]['attempt_id']
                assert all(fixture.outputs[r].is_file() for r in ['report', 'reproduce_script', 'stage_status'])
                stages = fixture.outputs['stage_status'].read_text()
                assert 'build_database\t' + durable['steps'][2]['status'] + '\tprocess_returned_zero\tFalse' in stages
                assert 'annotate_variants\tpending\tnot_started_or_unknown\tFalse' in stages
                from agent_core.workflow.managed_stop import _matches
                stop = durable['task_state']['managed_run_stop']
                assert _matches(durable, stop)
                for field, value in [('task_id', 'wrong-task'), ('plan_id', 'wrong-plan'), ('plan_version', 'wrong-version'),
                                     ('step_id', 'annotate_variants'), ('attempt_id', build[0]['attempt_id'])]:
                    bad = deepcopy(stop); bad['scope'][field] = value
                    assert not _matches(durable, bad)
                for mutation in ['cancel', 'revision', 'candidate', 'missing_causal_attempt', 'wrong_attempt']:
                    changed = deepcopy(durable)
                    if mutation == 'cancel': changed['task_state']['status'] = 'cancelled'
                    if mutation == 'revision': changed['plan_revision'] += 1
                    if mutation == 'candidate': changed['task_state']['generate_context_episode'] = {'new_candidate': True}
                    if mutation == 'missing_causal_attempt': changed['provenance_state']['attempts'].pop(build[0]['attempt_id'])
                    if mutation == 'wrong_attempt': changed['steps'][2]['verification_attempt_id'] = 'wrong-attempt'
                    assert not _matches(changed, stop), mutation
                frozen = {k: digest(fixture.outputs[k].read_bytes()) for k in ['report', 'reproduce_script', 'stage_status']}
                before_calls = deepcopy(fixture.counts)
                from agent_core.workflow.managed_stop import finalize_managed_stop
                replay = await finalize_managed_stop(fixture.agent, fixture.task, values, stop['boundary'])
                assert replay['stop_id'] == stop['stop_id'] and fixture.counts == before_calls
                assert frozen == {k: digest(fixture.outputs[k].read_bytes()) for k in frozen}
        finally:
            await fixture.close()
    asyncio.run(run())
