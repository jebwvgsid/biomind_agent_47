"""IT-03: actual model transport, malformed candidates, END and real reports.

Only the external response changes. The inherited fixture retains real handoff,
Plan freeze/approval, node registration/consumption, parser, correction ledger,
terminal owner and physical report publication. No action becomes executable.
"""
import asyncio
from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage

from tests.a1_unified_fix.test_f6_graph import F6Graph, digest, save


class RejectedCandidateGraph(F6Graph):
    ENVS = {
        'repeat': [[], []],
        'aba': [[], {'BAD': 7}, []],
        'env_reorder': [{'BAD': 7, 'VALID': 'ok'}, {'VALID': 'ok', 'BAD': 7}],
        'env_unrelated': [{'BAD': 7}, {'NEW': 'ok', 'BAD': 7}],
    }

    async def provider(self, messages, *, stage, **kwargs):
        response = await super().provider(messages, stage=stage, **kwargs)
        if stage != 'generate':
            return response
        ordinal = self.counts['generate']
        envs = self.ENVS[self.mode]
        assert ordinal <= len(envs), (self.mode, ordinal)
        candidate = json.loads(response.content)
        candidate['env'] = deepcopy(envs[ordinal - 1])
        raw = json.dumps(candidate, ensure_ascii=False)
        # Replace the fixture's preprepared valid candidate record with exactly
        # the malformed business reply that the real transport will deliver.
        self.provider_candidates[-1] = {
            'operation': self.current_operation(), 'raw': raw,
            'raw_sha256': digest(raw), 'candidate': deepcopy(candidate),
        }
        return AIMessage(content=raw, response_metadata={'finish_reason': 'stop'})

    async def run(self):
        values, durable = await super().run()
        from agent_core.plan_tracker import load_approved_plan_snapshot
        from agent_core.state_contract_store import StateContractStore
        from agent_core.workflow.candidate_repair import candidate_repair_fingerprint

        snapshot = load_approved_plan_snapshot(self.root, self.task, state=durable)
        assert snapshot and len(snapshot['steps']) == 6
        assert durable['approved_plan_hash']
        count = len(self.ENVS[self.mode])
        assert self.counts['generate'] == count
        assert self.counts['recovery'] == self.counts['reviewer'] == 0
        assert self.executions == self.commits == self.approvals == []
        assert not durable['task_state'].get('typed_step_commit_receipts')
        assert durable['current_step_id'] == 'check_contig_compatibility'
        assert values['generate_outcome']['type'] == 'internal_protocol_error'
        rows = sorted(durable['task_state']['candidate_repair_state']['events'].values(),
                      key=lambda item: item['repair_ordinal'])
        assert len(rows) == count
        assert rows[-1]['policy_receipt']['remaining'] == (2 if self.mode == 'aba' else 3)
        assert rows[-1]['progress_assessment']['progress_proven'] is False
        assert rows[-1]['progress_assessment']['resolved_targets'] == []
        if self.mode == 'aba':
            assert rows[1]['progress_assessment']['progress_proven'] is True
            assert rows[1]['progress_assessment']['resolved_targets'] == ['env']
        if self.mode in ('repeat', 'aba', 'env_reorder'):
            assert rows[-1]['progress_assessment']['basis'] == 'recurrent_rejected_candidate'
            assert candidate_repair_fingerprint(self.provider_candidates[0]['candidate']) == \
                   candidate_repair_fingerprint(self.provider_candidates[-1]['candidate'])
        else:
            assert rows[-1]['progress_assessment']['basis'] == 'no_validated_target_progress'
        if self.mode == 'repeat':
            assert self.provider_candidates[0]['raw'] == self.provider_candidates[1]['raw']

        feedback = []
        for request in self.requests:
            if request['stage'] != 'generate' or request['ordinal'] == 1:
                continue
            found = []
            for message in request['messages']:
                try:
                    content = json.loads(message['content'])
                except (ValueError, TypeError):
                    continue
                if isinstance(content, dict) and content.get('kind') == 'generate_validator_feedback':
                    repair = content.get('feedback', {}).get('action_repair')
                    if repair:
                        found.append(repair)
            latest = [item for item in found if item['correction_policy']['current_correction_number'] == request['ordinal'] - 1]
            assert len(latest) == 1, (self.mode, request['ordinal'], found)
            actual = latest[0]
            assert actual['exact_error']['code'] == 'ACTION_SCHEMA_TYPE_ERROR'
            assert actual['progress_assessment']['scientific_success'] == 'not_assessed'
            prior = rows[request['ordinal'] - 2]
            assert actual['correction_policy']['remaining_after_this_call'] == prior['policy_receipt']['remaining']
            for field in ['basis', 'newly_satisfied_targets', 'resolved_targets', 'pending_targets']:
                assert actual['progress_assessment'][field] == prior['progress_assessment'][field]
            feedback.append({'ordinal': request['ordinal'], 'actual_feedback': actual,
                             'all_retained_feedback': found})
        assert len(feedback) == count - 1
        diagnostic = self.terminal['terminal_diagnostic']
        assert diagnostic['origin']['source_stage'] == 'generate'
        assert diagnostic['origin']['source_owner'] == 'GENERATED_ACTION'
        assert diagnostic['origin']['error_code'] == 'ACTION_SCHEMA_TYPE_ERROR'
        assert diagnostic['origin'].get('attempt_id') in ('', None)
        assert diagnostic['causal_execution'] == {'status': 'UNKNOWN'}
        assert diagnostic['delivery_status'] == 'complete'
        assert diagnostic['persistence_status'] == 'committed'
        assert diagnostic['summary_status'] == 'COMPLETED'
        claims = sorted(durable['task_state']['candidate_repair_state']['provider_claims'].values(),
                        key=lambda item: item['corrective_ordinal'])
        assert len(claims) == count
        store = StateContractStore(self.root, self.task)
        cached_responses = []
        for claim, candidate in zip(claims, self.provider_candidates):
            assert claim['status'] == 'completed'
            cached = store.read_object(claim['response_ref'], expected_type='generate_candidate_response')
            assert cached['content'] == candidate['raw']
            cached_responses.append({'claim_id': claim['claim_id'], 'response_ref': claim['response_ref'],
                'actual_cached_content': cached['content'], 'sha256': digest(cached['content'])})
        # Inherited finalization already asserts all 3 physical reports and a
        # second actual terminal call makes no provider/execute/file changes.
        evidence = {
            'mode': self.mode, 'frozen_plan_hash': durable['approved_plan_hash'],
            'counts': self.counts, 'actual_execution_count': 0, 'actual_commit_count': 0,
            'candidate_sequence': self.provider_candidates, 'repair_rows': rows,
            'actual_next_requests_feedback': feedback, 'actual_persisted_responses': cached_responses,
            'terminal': self.terminal,
        }
        self.receptions.append(('actual_rejected_candidate_end', evidence))
        save('it03-' + self.mode, evidence)
        return values, durable


@pytest.mark.parametrize('mode', ['repeat', 'aba', 'env_reorder', 'env_unrelated'])
def test_IT03_rejected_candidate_real_transport_end_reports(mode):
    async def scenario():
        fixture = RejectedCandidateGraph(mode)
        await fixture.setup()
        try:
            await fixture.run()
        finally:
            await fixture.close()
    asyncio.run(scenario())
