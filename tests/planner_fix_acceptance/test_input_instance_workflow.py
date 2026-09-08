"""I01: public Consultant operations, durable inputs, Planner and Generate wire.

Only external model/runtime-inspection IO is substituted. Consultant mutation,
CAS, reload, Planner graph, approval, managed binder and Generate remain real.
The recorded scientific handoff and 21-step candidate are reused; this does not
claim a new full Consultant FINALIZE run or scientific multi-sample execution.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile

from langchain_core.messages import HumanMessage
from langgraph.graph.message import add_messages

from agent_core.plan_tracker import canonical_task_spec_for_state, load_plan_state, load_approved_plan_snapshot
from agent_core.managed_artifacts import build_managed_artifact_bindings
from agent_core.approved_resource_locator import approved_resource_locator, open_approved_resource
from agent_core.task_spec import task_semantic_hash
from tests.planner_fix_acceptance.test_planner_recorded_workflow import (
    F, Harness, RecordedModel, SKILLS, evidence, wire_objects,
)


ORIGINAL_RESPONSE_SOURCE = '/tmp/biomind-planner-audit-20260908/evidence/context_trace/consultant-0004-9ddb70315c0f/response.json'
ORIGINAL_RESPONSE_SHA256 = '9b5ec6f93256b13749bf34694ae27483a8c5b7ce05199f2f48015ed12b8a8485'
# Exact two public input operations copied from the recorded response. They
# remain lists; no evaluation, filename-based inference or path access occurs.
ORIGINAL_OPERATIONS = [
    {'operation': 'upsert_input', 'input': {'role': 'paired_fastq', 'path': [
        '/public/home/liusiqi/03.毕设/benchmarks/01.测试/04.重测序/Part1.variant_calling/data/S1_1.fq.gz',
        '/public/home/liusiqi/03.毕设/benchmarks/01.测试/04.重测序/Part1.variant_calling/data/S1_2.fq.gz',
    ], 'required': True}, 'supporting_evidence': ['U1']},
    {'operation': 'upsert_input', 'input': {'role': 'paired_fastq', 'path': [
        '/public/home/liusiqi/03.毕设/benchmarks/01.测试/04.重测序/Part1.variant_calling/data/S2_1.fq.gz',
        '/public/home/liusiqi/03.毕设/benchmarks/01.测试/04.重测序/Part1.variant_calling/data/S2_2.fq.gz',
    ], 'required': True}, 'supporting_evidence': ['U1']},
]


def public_payload(operations, *, handoff_summary=''):
    return json.dumps({
        'action_type': 'submit_planning_handoff' if handoff_summary else 'update_taskspec',
        'summary': handoff_summary or 'Apply the explicitly supplied input instances.',
        **({'selected_skills': SKILLS} if handoff_summary else {}),
        'task_spec_operations': operations,
        'unresolved_ambiguities': [],
    }, ensure_ascii=False)


def member_operation(group, member, path, evidence_handle='U1'):
    return {
        'operation': 'upsert_input',
        'input': {'role': 'paired_fastq', 'path': str(path), 'required': True,
                  'input_instance': {'schema_version': 1, 'group_id': group, 'member_id': member}},
        'supporting_evidence': [evidence_handle],
    }


def members(spec):
    rows = [r for r in spec.get('inputs', []) if r.get('role') == 'paired_fastq']
    return {(r['input_instance']['group_id'], r['input_instance']['member_id']): r for r in rows}


def test_public_input_instances_to_durable_planner_and_generate_request():
    async def scenario():
        with tempfile.TemporaryDirectory(prefix='input-instance-workflow-') as tmp:
            root = Path(tmp)
            task = 'offline-input-instance-workflow'
            model = RecordedModel([])
            harness = Harness(root, model)
            agent = harness.agent
            # External environment inspection is unavailable in this offline
            # fixture; this does not report any environment as available.
            agent._consultant_runtime_seed_evidence = lambda *a, **k: ([], {}, '')
            agent._consultant_runtime_preflight = lambda *a, **k: ([], '')
            updates = []
            snapshots = []
            try:
                state = harness.bootstrap(task)

                async def consultant(operations, *, handoff_summary=''):
                    nonlocal state
                    model.responses.append(public_payload(operations, handoff_summary=handoff_summary))
                    before_count = len(model.calls)
                    update = await agent._graph_node_handlers['consultant'](state)
                    updates.append(deepcopy(update))
                    loaded = load_plan_state(root, task)
                    snapshots.append(deepcopy(loaded))
                    evidence('I01-progress', {'updates': updates, 'durable': snapshots,
                             'provider_calls': model.calls, 'source': __file__})
                    assert len(model.calls) == before_count + 1, update
                    state = {**state, **{k: v for k, v in update.items() if k != 'messages'}}
                    state['messages'] = add_messages(state['messages'], update.get('messages', []))
                    return update, loaded

                before = canonical_task_spec_for_state(load_plan_state(root, task))
                rejected, after_reject = await consultant(deepcopy(ORIGINAL_OPERATIONS))
                assert canonical_task_spec_for_state(after_reject) == before
                feedback = after_reject['task_state']['consultation_state']['last_feedback'][-1]
                assert feedback['code'] == 'INPUT_BINDING_PATH_TYPE_INVALID'
                issue = feedback['validation_issues'][0]
                assert issue['field_path'] == '$.task_spec_operations[0].input.path'
                assert issue['actual'] == {'type': 'list', 'value': ORIGINAL_OPERATIONS[0]['input']['path']}
                assert issue['expected']

                # Explicitly retire only the corrupted singleton in this new
                # isolated copy; no existing instance is guessed or migrated.
                _, removed = await consultant([{
                    'operation': 'remove_input', 'role': 'paired_fastq',
                    'supporting_evidence': ['U1'],
                }])
                assert not [r for r in canonical_task_spec_for_state(removed)['inputs']
                            if r['role'] == 'paired_fastq']
                next_objects = list(wire_objects(model.calls[1]))
                assert issue in next_objects, 'Complete structured input error missing from next real Consultant request'
                first_request = '\n'.join(str(m.get('content', '')) for m in model.calls[0])
                for term in ('input_instance', 'group_id', 'member_id', 'scalar'):
                    assert term in first_request, term

                paths = {}
                for group in ('S1', 'S2'):
                    for member in ('R1', 'R2'):
                        path = root / ('explicit-' + group + '-' + member + '.fastq')
                        path.write_text('@isolated\nACGT\n+\n!!!!\n')
                        paths[group, member] = path
                declared = [member_operation(group, member, path)
                            for (group, member), path in paths.items()]
                _, accepted = await consultant(declared)
                first_members = members(canonical_task_spec_for_state(accepted))
                assert set(first_members) == set(paths)
                assert len({r['semantic_handle'] for r in first_members.values()}) == 4

                corrected = root / 'explicit-new-S2-R2.fastq'
                corrected.write_text('@isolated-corrected\nTGCA\n+\n!!!!\n')
                correction_text = 'Explicit correction: S2 R2 is ' + str(corrected) + '; retain S1 R1/R2 and S2 R1.'
                agent._persist_user_submission(task, correction_text)
                state['messages'] = add_messages(state['messages'], [HumanMessage(content=correction_text)])
                correction = [member_operation('S2', 'R2', corrected, evidence_handle='U2')]
                _, changed = await consultant(correction)
                changed_spec = canonical_task_spec_for_state(changed)
                changed_members = members(changed_spec)
                assert len(changed_members) == 4
                assert changed_members['S2', 'R2']['canonical_path'] == str(corrected)
                for identity in (('S1', 'R1'), ('S1', 'R2'), ('S2', 'R1')):
                    assert changed_members[identity] == first_members[identity]
                assert changed_members['S2', 'R2']['semantic_handle'] == first_members['S2', 'R2']['semantic_handle']
                _, repeated = await consultant(correction)
                final_spec = canonical_task_spec_for_state(repeated)
                assert members(final_spec) == changed_members
                assert task_semantic_hash(final_spec) == task_semantic_hash(changed_spec)
                assert final_spec['revision'] == changed_spec['revision']
                # A real sixth Consultant model response explicitly submits
                # the existing scientific method with the new input contract.
                # The production handoff branch validates and commits it; no
                # manually manufactured ready state bypasses that decision.
                handoff, handed_off = await consultant([], handoff_summary=
                    repeated['task_state']['accepted_consultant_handoff']['summary'])
                assert handoff['next_step'] == 'planner', handoff
                assert handoff['canonical_commit']['succeeded'] is True
                assert members(canonical_task_spec_for_state(handed_off)) == changed_members

                # The candidate is still the recorded 21-step scientific scope.
                # Only its declared aggregate input is expanded to the four
                # explicit member handles just committed by Consultant.
                candidate = json.loads((F / 'corrected_candidate.json').read_text())
                handles = [changed_members[group, member]['semantic_handle']
                           for group in ('S1', 'S2') for member in ('R1', 'R2')]
                for step in candidate['steps']:
                    step['inputs'] = [member for handle in step.get('inputs', [])
                                      for member in (handles if handle == 'input.paired_fastq' else [handle])]
                model.responses.extend([json.dumps(candidate, ensure_ascii=False),
                                        asyncio.CancelledError('stop after actual Generate input consumption')])
                consultant_call_count = len(model.calls)
                harness.human_answers = ['y']
                try:
                    result = await harness.run(harness.input(task))
                except asyncio.CancelledError:
                    pass
                else:
                    evidence('I01-consumer-unreached', {'result': result, 'provider_calls': model.calls,
                             'durable': load_plan_state(root, task), 'source': __file__})
                    raise AssertionError('The actual Generate provider request was not reached')
                final_durable = load_plan_state(root, task)
                evidence('I01-consumer-wire', {'provider_calls': model.calls,
                         'consultant_call_count': consultant_call_count,
                         'durable': final_durable, 'source': __file__})
                assert final_durable['approval_status'] == 'approved'
                assert len(model.calls) == consultant_call_count + 2
                planner_request = model.calls[consultant_call_count]
                generate_request = model.calls[consultant_call_count + 1]
                for label, request in (('Planner', planner_request), ('Generate', generate_request)):
                    objects = list(wire_objects(request))
                    visible = [r for r in objects if isinstance(r, dict)
                               and r.get('input_instance') and r.get('role') == 'paired_fastq']
                    visible_members = {(r['input_instance']['group_id'], r['input_instance']['member_id'])
                                       for r in visible}
                    assert visible_members == set(paths), (label, visible)
                    text = '\n'.join(str(m.get('content', '')) for m in request)
                    for identity, record in changed_members.items():
                        assert record['semantic_handle'] in text, (label, identity)
                        semantic_rows = [r for r in visible if
                                         (r.get('handle') or r.get('semantic_handle')) == record['semantic_handle']]
                        assert any(r['input_instance'] == record['input_instance'] for r in semantic_rows)
                        if label == 'Planner':
                            # Planner chooses semantic handles; concrete paths
                            # belong to Generate's approved artifact manifest.
                            assert any(r.get('basename') == Path(record['canonical_path']).name
                                       for r in semantic_rows)
                        else:
                            concrete = [r for r in objects if isinstance(r, dict)
                                        and r.get('semantic_handle') == record['semantic_handle']
                                        and r.get('path') == record['canonical_path']
                                        and r.get('input_instance') == record['input_instance']]
                            assert concrete, (label, identity)
                assert len(final_durable['steps']) == 21
                assert not final_durable.get('execution_attempts')
                assert not list(root.rglob('execution_result.json'))
                # Auxiliary shared reader: visibility comes from the actual
                # approved step and fresh TaskSpec, never an arbitrary path.
                reloaded = load_plan_state(root, task)
                approved = load_approved_plan_snapshot(root, task, state=reloaded)
                bindings = build_managed_artifact_bindings(
                    canonical_task_spec_for_state(reloaded), plan_state=reloaded,
                    approved_plan_snapshot=approved,
                    current_step_id=reloaded['current_step_id'],
                )
                reads = []
                for identity, record in changed_members.items():
                    binding = next(b for b in bindings.values()
                                   if b.semantic_handle == record['semantic_handle'])
                    assert binding.metadata['input_instance'] == record['input_instance']
                    locator = approved_resource_locator({
                        'canonical_path': binding.canonical_path,
                        'artifact_id': binding.artifact_id,
                    }, trusted_root=str(root))
                    with open_approved_resource(locator['path']) as reader:
                        content = reader.read(512).decode('utf-8')
                    assert content.startswith('@isolated')
                    reads.append({'semantic_handle': binding.semantic_handle,
                                  'input_instance': binding.metadata['input_instance'],
                                  'locator': locator, 'content': content})
                assert len(reads) == 4
                assert not any(r['locator']['path'] == str(paths['S2', 'R2']) for r in reads)
                evidence('I01-complete', {
                    'original_response_source': ORIGINAL_RESPONSE_SOURCE,
                    'original_response_sha256': ORIGINAL_RESPONSE_SHA256,
                    'original_operations': ORIGINAL_OPERATIONS,
                    'updates': updates, 'durable_mutation_snapshots': snapshots,
                    'provider_calls': model.calls, 'consultant_call_count': consultant_call_count,
                    'planner_request': planner_request, 'generate_request': generate_request,
                    'final_durable': final_durable, 'explicit_candidate': candidate,
                    'scoped_shared_reader_results': reads,
                    'source': __file__,
                })
            finally:
                await harness.close()
    asyncio.run(scenario())
