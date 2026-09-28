import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, message_chunk_to_message
from agent_core.workflow.recovery_runtime import (RecoveryResourcePolicy, recovery_resource_begin, recovery_resource_end, recovery_token_occupancy)
from agent_core.context_lineage import ContextLineageStore
from tests.test_recovery_reception import native













def test_v11_8192_candidate_calibration_keeps_large_legal_protocol(tmp_path):
    from agent_core.workflow.recovery_reception import validate_candidate,MAX_CHARS
    from agent_core.workflow.context_compaction import ReferenceTokenEstimator
    from tests.test_recovery_reception import candidate
    value=candidate();value['instruction']='中文 evidence Δ 測試。'*8000
    base=json.dumps(value,ensure_ascii=False)
    # Max accepted wire size is a character boundary, not the output policy.
    excess=len(base)-MAX_CHARS
    value['instruction']=value['instruction'][:-excess] if excess>0 else value['instruction']+'a'*(MAX_CHARS-len(base))
    raw=json.dumps(value,ensure_ascii=False);assert len(raw)==MAX_CHARS
    assert validate_candidate(value).instruction==value['instruction']
    estimate=ReferenceTokenEstimator().estimate(raw)
    assert estimate.tokens>8192
    calibration={'policy':'phase_e_candidate_v2','legal_wire_chars':len(raw),'estimated_output_tokens':estimate.tokens,
        'candidate_cap':8192,'largest_legal_message_fit':False,'parser_unchanged':True,
        'consequence':'remote length -> incomplete -> zero directive/tool admission; needs canary/calibration before activation'}
    (tmp_path/'output_calibration.json').write_text(json.dumps(calibration,indent=2))














