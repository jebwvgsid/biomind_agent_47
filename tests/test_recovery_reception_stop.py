"""Reception failure retains prose through actual graph/outer/reports."""
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage
from agent_core.workflow.managed_stop import current_managed_stop
from tests import test_common_finalization_workflow as shared

@pytest.mark.parametrize('summary_mode',['ok','connection'])
def test_format_exhaustion_preserves_explanation_in_common_stop(tmp_path,summary_mode):
    class ProseModel:
        def __init__(self,*args,**kwargs):self.requests=[]
        async def ainvoke(self,messages,**kwargs):
            self.requests.append(list(messages))
            if len(self.requests)==2:
                assert any('RECOVERY_SUBMISSION_MISSING' in str(m.content) for m in messages)
            assert len(self.requests)<=2
            return AIMessage(content='脚本诊断已产生，但正式修复尚未接纳；输入是否不兼容未经独立核验。')
    async def scenario():
        with patch.object(shared,'RecoveryModel',ProseModel):
            f,a,g,c,r,m,result,generates=await shared.run_execution_case(tmp_path,summary_mode)
        stop=current_managed_stop(f.reload())
        assert len(r.requests)==2 and len(generates)==1 and len(m.requests)==1
        assert stop['origin']['failure_type']=='execution_failed'
        assert stop['stop_reason']['error_code']=='RECOVERY_SUBMISSION_MISSING'
        assert stop['stop_reason']['model_turns']==2 and stop['stop_reason']['tool_calls']==0
        assert stop['model_diagnoses'] and not stop['execution']['committed']
        assert '正式修复尚未接纳' in result.reason
        assert not f.reload()['task_state'].get('typed_step_commit_receipts')
        for name,path in stop['report_paths'].items():
            receipt=stop['report_receipts'][name]
            assert receipt['status']=='created'
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==receipt['sha256']
        assert '正式修复尚未接纳' in Path(stop['report_paths']['report.md']).read_text()
        assert stop['summary_status']==('COMPLETED' if summary_mode=='ok' else 'FAILED')
        await a._finalize_managed_terminal(f.task_id,g.get_state(c))
        assert len(m.requests)==1
    asyncio.run(scenario())
