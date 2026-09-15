"""BASE/current class witness for the unchanged old whole-dict assertion.

The two fixtures are byte-exact class excerpts, including their decorator,
from known safe source. No BioMind module, provider, science or original state
is loaded. This is a component reproduction, not the complete BASE test graph.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping
import uuid

import pytest


ORIGINAL_ASSERTION = '''assert audit.to_dict() == {
        "prompt_assembly_started": True,
        "prompt_assembly_succeeded": True,
        "provider_call_attempted": True,
        "llm_invoked": True,
    }'''
CLASS_SHA256 = '923ed47d5c90b6beda0a452d10e10113c2d81ec44df49a806ebc3846d54e4b6c'


@pytest.mark.parametrize('version', ['base', 'current'])
def test_original_audit_dictionary_assertion_fails_on_same_class(version):
    source = Path(__file__).with_name('generate_audit_' + version + '.py')
    body = source.read_bytes()
    assert hashlib.sha256(body).hexdigest() == CLASS_SHA256
    assert hashlib.sha256(ORIGINAL_ASSERTION.encode()).hexdigest() == (
        '93e26affcb3c054d3870ddb70ecf090c75d462902010c0965de2de3397021195'
    )
    namespace = dict(__name__=__name__, dataclass=dataclass, field=field,
                     uuid=uuid, Dict=Dict, Any=Any, Mapping=Mapping,
                     deepcopy=deepcopy)
    exec(compile(body, str(source), 'exec', dont_inherit=True), namespace)
    audit = namespace['GenerateInvocationAudit']()
    # The saved full old-suite failure already reached successful assembly.
    # Seed those component inputs; no assembly or provider call is claimed here.
    audit.prompt_assembly_started = True
    audit.prompt_assembly_succeeded = True
    audit.mark_provider_call_attempted()
    actual = audit.to_dict()
    with pytest.raises(AssertionError):
        exec(compile(ORIGINAL_ASSERTION, 'old_assertion_line_166', 'exec'), {'audit': audit})
    four = ('prompt_assembly_started', 'prompt_assembly_succeeded',
            'provider_call_attempted', 'llm_invoked')
    assert all(actual[key] is True for key in four)
    assert set(actual).difference(four) == {'generate_call_id', 'scope', 'source_ref'}
    assert actual['scope'] == {} and actual['source_ref'] == ''
    assert actual['generate_call_id'].startswith('generate-')
    result = {
        'version': version,
        'class_source_sha256': CLASS_SHA256,
        'original_assertion_sha256': hashlib.sha256(ORIGINAL_ASSERTION.encode()).hexdigest(),
        'actual_to_dict': actual,
        'original_assertion_result': 'AssertionError',
        'four_expected_booleans_match': True,
        'extra_keys': sorted(set(actual).difference(four)),
        'full_base_provider_graph_executed': False,
        'model_or_science_executed': False,
        'component_input': 'Successful assembly booleans supplied; real class mark_provider_call_attempted and to_dict run.',
    }
    out = Path(os.environ['BIOMIND_GER_ARTIFACTS']) / ('existing-assertion-' + version + '.json')
    out.write_text(json.dumps(result, indent=2) + '\n')
