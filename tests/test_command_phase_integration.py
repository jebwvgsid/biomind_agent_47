"""Exercise production phase adapters, with provider I/O replaced by messages."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage


class Service:
    def __init__(self): self.calls = []
    def describe(self, task_id): return {'project_dir': '/portable-project'}
    async def run(self, arguments, **scope):
        self.calls.append((arguments, scope))
        return {'status': 'completed', 'exit_code': 0, 'stdout': 'observed-file', 'receipt_ref': '/receipt.json',
                'truncated': {'stdout': False, 'stderr': False}}


class Model:
    def bind_tools(self, tools):
        self.tools = tools
        return self


def query():
    return AIMessage(content='', tool_calls=[{'id': 'query-1', 'name': 'exec_command', 'args': {'cmd': 'ls'}}])


class PhaseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_consult_observations_reach_planner_after_service_restart_without_requery(self):
        from agent_core.command_execution import CommandExecutionService
        from bio_agents import BioAnalystAgent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'samples.txt').write_text('S1 paired FASTQ\nS2 paired FASTQ\n')
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.command_service = CommandExecutionService(data_path=root / 'records', project_dir=root)
            agent.command_service.install_dir = root
            agent.command_service.prefixes = set()
            agent.command_service.path = ['/usr/bin', '/bin']
            agent.llm = agent.planner_llm = Model()
            calls = []

            async def once(messages, **kwargs):
                calls.append(kwargs['stage'])
                if calls == ['consultant']:
                    return AIMessage(content='', tool_calls=[{
                        'id': 'inspect-samples', 'name': 'exec_command', 'args': {'cmd': 'cat samples.txt'},
                    }])
                if kwargs['stage'] == 'consultant':
                    result = json.loads(messages[-1].content)
                    self.assertEqual(result['exit_code'], 0, result)
                    self.assertEqual(result['stage'], 'consultant')
                    return AIMessage(content='consult-final')
                payload = '\n'.join(str(m.content) for m in messages)
                self.assertIn('S1 paired FASTQ', payload)
                self.assertIn('S2 paired FASTQ', payload)
                self.assertIn('consultant', payload)
                self.assertIn('receipt_ref', payload)
                self.assertIn('historical_not_revalidated', payload)
                return AIMessage(content='plan using the existing sample observations')

            agent._ainvoke_llm_once = once
            await agent._ainvoke_llm([SystemMessage(content='consult protocol')], task_id='shared', stage='consultant')
            # The handoff is durable, not an in-memory cache or graph-message accident.
            agent.command_service = CommandExecutionService(data_path=root / 'records', project_dir=root)
            with patch.object(agent.command_service, 'run', side_effect=AssertionError('no redundant query')):
                response = await agent._ainvoke_llm([SystemMessage(content='plan protocol')], task_id='shared', stage='planner')
            self.assertEqual(calls, ['consultant', 'consultant', 'planner'])
            self.assertIn('existing sample observations', response.content)

    async def test_consult_plan_generate_dispatch_tools_before_returning_final(self):
        from bio_agents import BioAnalystAgent
        for stage in ('consultant', 'planner', 'planner_repair', 'generate'):
            with self.subTest(stage=stage):
                agent = BioAnalystAgent.__new__(BioAnalystAgent)
                agent.command_service = Service()
                agent.llm = agent.planner_llm = Model()
                requests = []
                async def once(messages, **kwargs):
                    requests.append((messages, kwargs))
                    if len(requests) == 1: return query()
                    self.assertIsInstance(messages[-1], ToolMessage)
                    self.assertIn('observed-file', messages[-1].content)
                    return AIMessage(content='phase-final')
                agent._ainvoke_llm_once = once
                response = await agent._ainvoke_llm([SystemMessage(content='phase contract')],
                    task_id='test', stage=stage, trace_metadata={'semantic_invocation_id': 'frozen-binding'})
                self.assertEqual(response.content, 'phase-final')
                self.assertEqual(len(agent.command_service.calls), 1)
                self.assertEqual(requests[1][1]['trace_metadata']['command_tool_round'], 1)
                self.assertEqual(agent.command_service.calls[0][1]['stage'], stage)

    async def test_compaction_does_not_gain_command_tools(self):
        from bio_agents import BioAnalystAgent
        agent = BioAnalystAgent.__new__(BioAnalystAgent)
        agent.command_service = Service()
        async def once(messages, **kwargs): return AIMessage(content='summary')
        agent._ainvoke_llm_once = once
        response = await agent._ainvoke_llm([], stage='context_compaction')
        self.assertEqual(response.content, 'summary')
        self.assertFalse(agent.command_service.calls)

    async def test_supervisor_keeps_snapshot_per_tool_round(self):
        from supervisor import LLMReviewer
        service, requests = Service(), []
        reviewer = LLMReviewer(Model(), command_service=service)
        async def snapshot(model, messages, **kwargs):
            requests.append(kwargs)
            return (query() if len(requests) == 1 else AIMessage(content='review-final'), {'round': len(requests)})
        with patch('supervisor.ainvoke_with_snapshot', side_effect=snapshot):
            response, final_snapshot = await reviewer._invoke_review(reviewer.llm,
                [SystemMessage(content='review contract')], task_id='task', data_path='',
                metadata={'review_request_id': 'frozen-review'})
        self.assertEqual(response.content, 'review-final')
        self.assertEqual(final_snapshot['round'], 2)
        self.assertEqual(requests[1]['metadata']['review_request_id'], 'frozen-review')
        self.assertEqual(service.calls[0][1]['stage'], 'supervisor')

    async def test_recovery_native_loop_dispatcher_accepts_same_tool(self):
        from tests.test_recovery_agent_loop import _seed_fixture
        from agent_core.workflow.phase_archive import PhaseArchiveStore
        from agent_core.workflow.recovery_tools import RecoveryLocalToolDispatcher, build_recovery_inspection_registry
        from agent_core.workflow.recovery_agent import recovery_tool_schemas
        durable, seed = _seed_fixture()
        service = Service()
        with tempfile.TemporaryDirectory() as root:
            store = PhaseArchiveStore(root)
            registry = build_recovery_inspection_registry(seed, durable, phase_store=store)
            dispatcher = RecoveryLocalToolDispatcher(seed=seed, durable=durable, registry=registry,
                phase_store=store, command_service=service)
            result = await dispatcher.dispatch_full('exec.command', {'cmd': 'ls'})
        self.assertEqual(result['stdout'], 'observed-file')
        self.assertEqual(service.calls[0][1]['stage'], 'recovery')
        self.assertIn('exec_command', [item['function']['name'] for item in recovery_tool_schemas()])

    async def test_recovery_model_receives_query_result_before_submitting_directive(self):
        from tests.test_recovery_agent_loop import _seed_fixture, _directive
        from agent_core.workflow.phase_archive import PhaseArchiveStore
        from agent_core.workflow.recovery_tools import RecoveryLocalToolDispatcher, build_recovery_inspection_registry
        from agent_core.workflow.recovery_agent import RecoveryAgentLoop
        durable, seed = _seed_fixture()
        service, requests = Service(), []
        class RecoveryModel(Model):
            async def ainvoke(self, messages, *, max_tokens):
                requests.append(messages)
                if len(requests) == 1:
                    result = query()
                    result.response_metadata = {'finish_reason': 'tool_calls'}
                    return result
                directive = json.loads(_directive())
                directive['evidence_refs'] = ['/receipt.json']
                return AIMessage(content=json.dumps(directive), response_metadata={'finish_reason': 'stop'})
        with tempfile.TemporaryDirectory() as root:
            store = PhaseArchiveStore(root)
            registry = build_recovery_inspection_registry(seed, durable, phase_store=store)
            dispatcher = RecoveryLocalToolDispatcher(seed=seed, durable=durable, registry=registry,
                phase_store=store, command_service=service)
            result = await RecoveryAgentLoop(model=RecoveryModel(), dispatcher=dispatcher).ainvoke(
                seed=seed, durable=durable, inspection_manifest=registry.provider_manifest())
        self.assertFalse(result['terminal_error'], result['terminal_error'])
        self.assertEqual(len(service.calls), 1)
        self.assertIn('/receipt.json', result['audit']['inspected_refs'])
        self.assertTrue(any(isinstance(item, ToolMessage) and 'observed-file' in item.content for item in requests[-1]))

    def test_repeated_command_receipts_are_not_new_recovery_progress(self):
        from agent_core.workflow.recovery_runtime import meaningful_inspection_facts
        def observation(receipt, status='completed'):
            return {'tool': 'exec.command', 'source_ref': receipt, 'result': {
                'status': status, 'cmd': 'ls', 'stdout': 'same-file', 'exit_code': 0,
                'observed_at': receipt, 'age_seconds': len(receipt),
                'receipt_ref': receipt, 'log_refs': {'stdout': receipt + '/stdout.log'}}}
        first = meaningful_inspection_facts([observation('first')])
        self.assertTrue(first)
        self.assertEqual(first, meaningful_inspection_facts([observation('second')]))
        self.assertFalse(meaningful_inspection_facts([observation('third', status='sandbox_error')]))


if __name__ == '__main__':
    unittest.main()
