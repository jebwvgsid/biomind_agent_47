#!/usr/bin/env python3
"""Actual benchmark adapter -> shipped host/extension -> Python domain witness."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile


def assert_event_sequence(events):
    """Check structured Pi events; this never drives a model or repairs history."""
    active = None
    calls, executions, results = {}, {}, {}
    assistants = []
    tool_delta_count = 0
    for index, event in enumerate(events):
        kind = event.get('type')
        message = event.get('message', {})
        if kind == 'message_start':
            assert active is None, ('overlapping message_start', index)
            active = {'role': message['role'], 'start': index, 'text': {}, 'arguments': {}}
        elif kind == 'message_update':
            assert active is not None and active['role'] == 'assistant', ('orphan update', index)
            delta = event['assistantMessageEvent']
            part = delta.get('contentIndex')
            if delta['type'] in {'text_delta', 'toolcall_delta'}:
                bucket = 'text' if delta['type'] == 'text_delta' else 'arguments'
                active[bucket][part] = active[bucket].get(part, '') + delta['delta']
                tool_delta_count += int(bucket == 'arguments')
        elif kind == 'message_end':
            assert active is not None and active['role'] == message['role'], ('unpaired message_end', index)
            if message['role'] == 'assistant':
                assert message.get('stopReason') != 'pending', ('unfinished final message', index)
                content = message['content']
                for part, delta_text in active['text'].items():
                    assert content[part]['type'] == 'text' and content[part]['text'] == delta_text, ('text delta/final mismatch', index)
                for part, arguments_text in active['arguments'].items():
                    assert content[part]['type'] == 'toolCall'
                    assert json.loads(arguments_text) == content[part]['arguments'], ('tool delta/final mismatch', index)
                for block in content:
                    if block['type'] == 'toolCall':
                        identity = block['id']
                        assert identity not in calls, ('duplicate finalized tool call', identity)
                        calls[identity] = {'message_end': index, 'name': block['name'], 'args': block['arguments']}
                assistants.append({'start': active['start'], 'end': index, 'stop_reason': message.get('stopReason'),
                    'text': ''.join(block['text'] for block in content if block['type'] == 'text')})
            elif message['role'] == 'toolResult':
                identity = message['toolCallId']
                assert identity in executions and 'end' in executions[identity], ('result before execution end', identity)
                assert identity not in results, ('duplicate tool result', identity)
                assert bool(message.get('isError')) == executions[identity]['is_error']
                assert message['content'] == executions[identity]['content'], ('tool result payload mismatch', identity)
                results[identity] = index
            active = None
        elif kind == 'tool_execution_start':
            identity = event['toolCallId']
            assert identity in calls and calls[identity]['message_end'] < index, ('partial/unfinalized tool executed', identity)
            assert identity not in executions, ('duplicate execution', identity)
            assert event['toolName'] == calls[identity]['name'] and event['args'] == calls[identity]['args']
            executions[identity] = {'start': index}
        elif kind == 'tool_execution_end':
            identity = event['toolCallId']
            assert identity in executions and 'end' not in executions[identity], ('unpaired execution end', identity)
            executions[identity].update({'end': index, 'is_error': bool(event.get('isError')), 'content': event['result']['content']})
    assert active is None, 'message left open'
    assert calls.keys() == executions.keys() == results.keys(), 'unmatched tool call/execution/result'
    pairs = [{'id': identity, 'assistant_message_end': call['message_end'],
        'execution_start': executions[identity]['start'], 'execution_end': executions[identity]['end'],
        'tool_result_message_end': results[identity]} for identity, call in calls.items()]
    assert all(p['assistant_message_end'] < p['execution_start'] < p['execution_end'] < p['tool_result_message_end'] for p in pairs)
    return {'status': 'PASS', 'event_count': len(events), 'assistant_messages': assistants, 'tool_pairs': pairs,
        'tool_argument_delta_count': tool_delta_count, 'method': 'ordered structured-event correlation, exact delta/final/arguments/payload equality'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--no-answer', action='store_true')
    options = parser.parse_args()
    repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo))
    from agent_core.pi_entry import pi_parser, build_pi_command
    from agent_core.pi_benchmark import run_with_pi
    evidence = options.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    isolated = Path(tempfile.mkdtemp(prefix='biomind-benchmark-witness-'))
    project = isolated / '科学 项目'
    project.mkdir()
    manifest = project / '合成 清单.tsv'
    manifest.write_text('sample\tpath\nSYNTHETIC_SAMPLE_A\t/synthetic/a.fastq\n')
    registry = isolated / 'registry.json'
    registry.write_text('{"environments":{},"tools":{}}')
    preferences = isolated / 'preferences.md'
    preferences.write_text('合成当前偏好，不运行真实分析。')
    trace = evidence / 'provider-requests.jsonl'
    def call(identity, name, arguments):
        return {'content': [{'type': 'toolCall', 'id': identity, 'name': name, 'arguments': arguments}]}
    steps = [
        {**call('benchmark-question', 'ask_user', {'question': '请给出合成样本清单路径？'}), 'trace_path': str(trace)},
        call('benchmark-handoff', 'handoff_generate', {'goal': '拒绝安装后读取合成清单，不运行分析', 'manifest': str(manifest)}),
        call('benchmark-install', 'install_tool', {'cmd': 'printf NEVER > SHOULD_NOT_EXIST', 'env_name': 'synthetic',
            'description': '合成提案，benchmark应拒绝'}),
        call('benchmark-read', 'read', {'path': str(manifest)}),
        {'content': [{'type': 'text', 'text': 'BENCHMARK_MODEL_CONCLUSION 仅已读取合成清单；未运行科学分析。'}], 'expect_system_contains': 'Generate'},
    ]
    if options.no_answer:
        steps = steps[:1]
    fixture = isolated / 'fixture.json'
    fixture.write_text(json.dumps(steps, ensure_ascii=False))
    config = isolated / 'runtime.json'
    config.write_text(json.dumps({'python': sys.executable, 'agentDir': str(isolated / 'agent'),
        'dataRoot': str(isolated / 'domain'), 'providerFixture': str(fixture), 'registryPath': str(registry),
        'preferencesPath': str(preferences), 'memoryRoot': str(isolated / 'memory'), 'wikiRoot': str(isolated / 'wiki'),
        'historyEnabled': False}, ensure_ascii=False))
    os.environ['NODE_OPTIONS'] = '--import=' + str(repo / 'integrations/pi_runtime/testing/offline-guard.mjs')
    os.environ['BIOMIND_PI_TEST_ROOT'] = str(isolated)
    os.environ['BIOMIND_PI_GUARD_TRACE'] = str(evidence / 'guard.jsonl')
    args = pi_parser().parse_args(['--project-dir', str(project), '--pi-mode', 'json', '--pi-config', str(config)])
    command, _ = build_pi_command(args)
    answer = None if options.no_answer else {'manifest': str(manifest)}
    try:
        audit = asyncio.run(run_with_pi(command=command, task_id='actual-pi-benchmark-witness',
            prompt={'copy_paste_prompt': '合成benchmark：先询问清单，再进入Generate拒绝安装并读取，不运行科学分析。'},
            structured_source={'fixture': 'synthetic-only'}, case_entry={'case_id': 'B3-SYNTHETIC',
                'oracle_template': {'canonical_user_response': answer, 'expected_requested_slots': ['manifest']}},
            canonical_response=answer, run_output_dir=evidence, audit_output=evidence / 'audit.json',
            project_dir=project, timeout_seconds=40))
        assert audit['host_exit_code'] == 0
        assert audit['question_status'] == ('waiting' if options.no_answer else 'answered')
        assert audit['scientific_success'] is None and audit['evaluator_status'] == 'NOT_RUN'
        assert audit['extension_sha256'] and audit['session_id']
        assert not (project / 'SHOULD_NOT_EXIST').exists()
        requests = [json.loads(line) for line in trace.read_text().splitlines()]
        assert len(requests) == len(steps)
        if options.no_answer:
            assert audit['runtime_status'] == 'needs_input'
            assert audit['canonical_response_hash'] is None and not audit['approvals']
        else:
            assert audit['requested_slot_evidence'][0]['status'] == 'NOT_AVAILABLE'
            assert audit['approvals'][0]['decision'] == 'deny'
            messages = requests[-1]['context']['messages']
            results = {message['toolCallId']: message for message in messages if message['role'] == 'toolResult'}
            assert all(not result.get('isError') for result in results.values())
            assert json.loads(results['benchmark-install']['content'][0]['text'])['status'] == 'denied'
            assert 'SYNTHETIC_SAMPLE_A' in json.dumps(results['benchmark-read'], ensure_ascii=False)
            assert str(manifest) in json.loads(results['benchmark-question']['content'][0]['text'])['answer']
            frames = [json.loads(line) for line in (evidence / 'pi_host_events.jsonl').read_text().splitlines()]
            sequence = assert_event_sequence([frame['event'] for frame in frames if frame.get('type') == 'event'])
            final_messages = [message for message in sequence['assistant_messages'] if message['stop_reason'] == 'stop']
            final_frames = [(index, frame) for index, frame in enumerate(frames) if frame.get('type') == 'result']
            settled = [index for index, frame in enumerate(frames) if frame.get('type') == 'event' and frame['event']['type'] == 'agent_settled']
            assert len(final_messages) == len(final_frames) == len(settled) == 1
            assert settled[0] < final_frames[0][0]
            assert final_frames[0][1]['model_conclusion'] == final_messages[0]['text']
            sequence.update({'layer': 'L5 headless; synthetic provider', 'extension_sha256': audit['extension_sha256'],
                'final_projection_count': 1, 'final_projection_matches_message_end': True,
                'limits': ['Native screen redraws are not deduplicated by this headless assertion',
                           'Split tool argument deltas require the separate built-in provider parser evidence']})
            (evidence / 'event-sequence.json').write_text(json.dumps(sequence, ensure_ascii=False, indent=2) + '\n')
        status = {'status': 'PASS', 'layer': 'L5 with real Python domain; synthetic provider',
            'runtime_status': audit['runtime_status'], 'host_exit': audit['host_exit_code'], 'provider_requests': len(requests),
            'extension_sha256': audit['extension_sha256'], 'session_id': audit['session_id'], 'command': command,
            'answer_supplied': not options.no_answer,
            'project': str(project), 'isolated_root': str(isolated), 'scientific_success': None,
            'limits': ['Shared run_with_pi adapter invoked directly, not the v3 CLI argument parser',
                       'No real provider, HTTP endpoint, scientific job or installation executed']}
        (evidence / 'result.json').write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(status, ensure_ascii=False))
        return 0
    except BaseException as error:
        (evidence / 'result.json').write_text(json.dumps({'status': 'FAIL', 'error': f'{type(error).__name__}: {error}',
            'command': command, 'project': str(project), 'isolated_root': str(isolated)}, ensure_ascii=False, indent=2) + '\n')
        raise


if __name__ == '__main__':
    raise SystemExit(main())
