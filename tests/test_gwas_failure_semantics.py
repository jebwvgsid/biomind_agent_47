"""S5: real reconciliation -> TypedExecute -> immutable cause -> stop/CLI.

Only the process leaf supplies a deterministic execution record. No scientific
execution, original session access, provider call or report publication.
"""
import asyncio
from copy import deepcopy

import pytest

from agent_core.cli_lifecycle import terminal_text
from agent_core.managed_artifacts import reconcile_runtime_outputs
from agent_core.workflow.inspection_stop import _execution, build_inspection_stop
from agent_core.workflow.recovery_projection import build_causal_failure_record
from agent_core.workflow.stage_contracts import StageContractError
from tests.test_recovery_projection import _durable, _execute_failure






@pytest.mark.parametrize('failure_code,error_code,validation,expected', [
    ('SPECIFIC_OUTPUT_ERROR', 'EXECUTION_FAILED', False, 'SPECIFIC_OUTPUT_ERROR'),
    ('', '', False, 'VALIDATION_FAILED'),
    ('', '', None, 'EXECUTION_FAILED'),
    ('', 'PROCESS_TIMEOUT', None, 'PROCESS_TIMEOUT'),
])
def test_causal_fallback_uses_specific_owner_code_then_explicit_validation(failure_code, error_code, validation, expected):
    source, durable = _execute_failure(), _durable()
    record = source['payload']['execution_record']
    record.update(failure_code=failure_code, error_code=error_code)
    if validation is not None:
        record['validation'] = {'passed': validation}
    result = build_causal_failure_record('execute_outcome', source, durable)
    assert result['failure_packet']['error_code'] == expected


def test_cross_attempt_specific_failure_is_rejected_not_consumed():
    source = _execute_failure()
    source['payload']['execution_record'].update(attempt_id='different-attempt', failure_code='FOREIGN')
    with pytest.raises(StageContractError, match='STAGE_SCOPE_MISMATCH'):
        build_causal_failure_record('execute_outcome', source, _durable())






