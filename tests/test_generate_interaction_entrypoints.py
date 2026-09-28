"""Offline production transitions from executable steps to human interaction.

Providers and the subprocess leaf are stand-ins. Plan compilation, approval,
Generate, typed routing, interruption, user-turn persistence and Commit are real.
"""
import asyncio
from copy import deepcopy
import hashlib
import gzip
import json
import os
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
import pytest










@pytest.mark.parametrize('ordinal', range(4))
def test_archived_runtime_carriers_have_precise_materialization_result(ordinal):
    from agent_core.step_effects import declared_effect_emission_contract, materialize_effect_template, EffectHelperError
    from agent_core.controller_audit import parse_executable_script_block
    rows = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/generate_interaction_1789741049.json.gz').read_bytes()))
    response = json.loads(rows[ordinal]['response']['content'])
    block = parse_executable_script_block(response['payload'])
    step = {'step_id': 'inspect_runtime', 'produces_observations': [
        {'identity': 'runtime_availability', 'evidence_contract': {'required_fields': ['value']}},
    ]}
    # The archived carrier is only materialized. Its script is never approved
    # or executed and does not authorize any access to original environments.
    template = declared_effect_emission_contract({'steps': [step]}, 'inspect_runtime')
    if ordinal in {0, 2}:
        with pytest.raises(EffectHelperError, match='EFFECT_HELPER_PYTHON_EXECUTABLE_NOT_LITERAL') as error:
            materialize_effect_template(block.body, template, language=block.language)
        assert 'single-quoted heredoc is present' in error.value.to_dict()['message']
    else:
        result = materialize_effect_template(block.body, template, language=block.language)
        assert '# BIOMIND_EFFECT_HELPER' not in result






