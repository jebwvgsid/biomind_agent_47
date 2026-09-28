"""Saved incident replay and production wire capture; no provider is initialized."""
import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
import pytest
from agent_core.workflow.recovery_projection import _task_ref,_skill_bundle,_dependency_outcomes,_current_step
from agent_core.workflow.recovery_tools import build_recovery_inspection_registry
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.context_compaction import ReferenceTokenEstimator
from agent_core.workflow.stage_contracts import FailurePacket,RecoverySeed
from tests.phase_b_1789471955.test_causal_history import replay,HISTORY


def sha(value):return hashlib.sha256(value.encode()).hexdigest()



def test_T16_saved_request_prefix_boundaries(tmp_path):
    base=HISTORY/'context_trace'
    def held(number):
        path=next(base.glob('*-'+number+'-*/request.json'))
        return path,json.loads(path.read_text())
    def compare(left,right,scope):
        a,x=held(left);b,y=held(right)
        count=0
        for one,two in zip(x['messages'],y['messages']):
            if one!=two:break
            count+=1
        xm=x['messages'];ym=y['messages']
        common_chars=0
        if count<min(len(xm),len(ym)):
            for one,two in zip(xm[count]['content'],ym[count]['content']):
                if one!=two:break
                common_chars+=1
        return {'left':str(a),'right':str(b),'scope':scope,
            'left_sha256':sha(a.read_text()),'right_sha256':sha(b.read_text()),
            'exact_leading_messages':count,'next_message_common_content_chars':common_chars,
            'system_content_equal':xm[0]['content']==ym[0]['content'],
            'system_hashes':[sha(xm[0]['content']),sha(ym[0]['content'])],
            'top_level_tools_equal':x.get('tools')==y.get('tools'),
            'tools_in_snapshot':('tools' in x and 'tools' in y)}
    pairs=[('0047','0064','same Recovery episode, compaction fallback'),
        ('0016','0024','different Recovery first calls'),
        ('0024','0029','different Recovery first calls'),('0029','0034','category must be checked'),
        ('0044','0058','same-source map across transactions'),('0058','0076','same-source map across transactions'),
        ('0045','0046','final-plan correction'),('0062','0063','final-plan correction'),
        ('0022','0027','corrective Generate'),('0027','0032','corrective Generate')]
    # Discover the actual first Recovery request for the fourth episode from
    # the saved request names instead of claiming a compactor as a business call.
    recovery=[(p,json.loads(p.read_text())) for p in sorted(base.glob('recovery_turn-*/request.json'))]
    seeds={}
    for p,w in recovery:
        key=sha(w['messages'][1]['content']);seeds.setdefault(key,p.parent.name.split('-')[1])
    starts=list(seeds.values())
    pairs=[p for p in pairs if p[2] not in {'different Recovery first calls','category must be checked'}]
    pairs.extend((a,b,'different Recovery first calls') for a,b in zip(starts,starts[1:]))
    rows=[compare(*p) for p in pairs]
    assert all(row['system_content_equal'] for row in rows if row['scope']=='same-source map across transactions')
    (tmp_path/'saved_prefix_boundaries.json').write_text(json.dumps(rows,indent=2))
