"""Fresh G-A/B/C tasks. Fixed external leaves; real graph, approval and writers."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from agent_core.plan_tracker import load_plan_state
from agent_core.workflow.managed_stop import finalize_managed_stop, terminal_boundary
from tests.a1_unified_fix.test_f6_graph import F6Graph, OPS, PYTHON, SID, save, digest
from tests.a1_unified_fix.test_session_6793_chain import IncidentGraph


class CategoryGraph(IncidentGraph):
    def __init__(self, ending):
        super().__init__('class_'+ending)
        self.ending=ending;self.prepare_calls=0;self.before_candidates=[];self.old_reports={};self.effective_envs=[]

    async def setup(self):
        await super().setup()
        if self.ending=='missing':
            actual=self.agent._candidate_admission_rejection_update
            def omit_one_collected_result(**kwargs):
                if kwargs.get('step_id')=='prepare_database' and self.prepare_calls==2:
                    kwargs['check_records']=[]
                    kwargs['validated_representation_targets']=[x for x in kwargs.get('validated_representation_targets',[]) if x!='executable_syntax']
                return actual(**kwargs)
            self.agent._candidate_admission_rejection_update=omit_one_collected_result
        if self.ending in {'stop','recovery','budget','repeat'}:
            for role in ['report','reproduce_script','stage_status']:
                p=self.outputs[role];p.parent.mkdir(parents=True,exist_ok=True)
                p.write_text('NEW ISOLATED OLD OWNER '+role+'\n')
                self.old_reports[str(p)]=(digest(p.read_bytes()),p.stat().st_mtime_ns,p.stat().st_ino)

    async def close(self):
        import os,shutil
        state=load_plan_state(self.root,self.task)
        internal=state['task_state'].get('managed_run_stop',{}).get('internal_diagnostic_delivery',{})
        for name,source in internal.get('paths',{}).items():
            src=Path(source)
            if src.is_file() and not src.is_symlink():
                target=Path(os.environ['BIOMIND_GER_ARTIFACTS'])/('category-'+self.ending+'-diagnostics')/name
                target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,target)
        await super().close()

    async def execute(self, path, **kwargs):
        state=load_plan_state(self.root,self.task)
        pending=deepcopy(state['task_state'].get('pending_action') or {})
        self.effective_envs.append({'step_id':state['current_step_id'],'env':deepcopy(kwargs.get('env') or {}),'pending':pending})
        if state['current_step_id']=='prepare_database':
            assert kwargs['env']['GENOME_VERSION']=='new_fixture_v2'
            assert kwargs['env']['SNPEFF_DB']
            assert pending['binding_env']['GENOME_VERSION']=={'source':'produced_binding','name':'genome_version'}
        return await super().execute(path,**kwargs)

    def plan(self):
        p=super().plan()
        if self.ending=='recovery':return p
        p['steps'].insert(0,{'step_id':'inspect_inputs','title':'Inspect the new synthetic inputs',
            'action':'execute',
            'purpose':'analysis','tool':'bash','skill_ids':[SID],
            'execution_capability_id':'framework.generic.execute','operation':'execute',
            'resource_uses':[{'resource_ref':r,'access':'read'} for r in ['input.variant_vcf','input.reference_fasta','input.annotation_gtf']],
            'produces_observations':[{'identity':'inputs_checked','evidence_contract':{'required_fields':['value']}}],
            'outputs':[],'dependencies':[],'risk_level':'low','success_criteria':['Actual synthetic input bytes are nonempty.']})
        p['steps'][1]['dependencies']=['inspect_inputs']
        return p

    async def provider(self,messages,*,stage,**kwargs):
        response=await super().provider(messages,stage=stage,**kwargs)
        if stage=='generate' and self.current_operation()=='execute':
            # This generic capability publishes script_block_v1. The fixed
            # provider emits that mode directly; no production parser bypass.
            from langchain_core.messages import AIMessage
            candidate=json.loads(response.content)
            body=candidate['argv'][-1]
            wire="<shell>\n"+PYTHON+" -I -S - <<'PY'\n"+body+"PY\n</shell>"
            self.provider_candidates[-1]={'operation':'execute','raw':wire,'mode':'script_block_v1'}
            return AIMessage(content=wire,response_metadata={'finish_reason':'stop'})
        return response

    def action(self,operation,messages):
        if self.ending=='recovery':
            self.scenario='stop'
            return super().action(operation,messages)
        if operation=='execute':
            body='# BIOMIND_EFFECT_HELPER\nfrom pathlib import Path\n'
            body+='paths='+repr(list(map(str,[self.vcf,self.reference,self.annotation])))+'\n'
            body+="value=all(bool(Path(p).read_bytes()) for p in paths)\nprint(emit_effect_evidence({'produces_observations':{'inputs_checked':{'value':value}}}))\n"
            return {'action_type':'execute','operation':'execute','language':'python','argv':[PYTHON,'-I','-S','-c',body]}
        action=F6Graph.action(self,operation,messages)
        if operation!='prepare_database':return action
        self.prepare_calls+=1
        state=load_plan_state(self.root,self.task)
        self.before_candidates.append(deepcopy(state))
        if self.prepare_calls<=2 or self.ending in {'budget','repeat'} or (self.ending=='last' and self.prepare_calls<5):
            action['binding_env']={'GENOME_VERSION':'new_fixture_v2','SNPEFF_DB':action['produced_bindings']['snpeff_db']['value']['handle']}
        if self.prepare_calls>=3 and self.ending in {'budget','last'}:
            action['env']={'SYNTHETIC_EDIT':str(self.prepare_calls)}
        if self.prepare_calls==1:
            action.pop('operation');action['code']=action['argv'][-1]
        if self.prepare_calls==3 and self.ending=='stop':
            return {'action_type':'blocked','reason':'The candidate still violates binding_env; no new basis for correction. No prepare execution or build occurred.'}
        return action


def run_category(ending):
    async def run():
        c=CategoryGraph(ending);await c.setup()
        try:
            values,d=await c.run()
            # Archive actual messages, approvals, execution wrappers and state before assertions.
            save('category-'+ending,{'requests':c.requests,'candidates':c.provider_candidates,'approvals':c.approvals,
                'executions':c.executions,'commits':c.commits,'states_before_candidates':c.before_candidates,
                'durable':d,'terminal':c.terminal,'timeline':c.timeline,'effective_envs':c.effective_envs},full=True)
            if ending!='recovery':
                assert c.prepare_calls==(5 if ending in {'budget','last'} else 3),(c.prepare_calls,c.terminal)
                assert all(len(s['task_state']['typed_step_commit_receipts'])==2 for s in c.before_candidates)
                assert all(not any(a['step_id']=='prepare_database' for a in s['provenance_state']['attempts'].values()) for s in c.before_candidates)
                rows=sorted([x for x in d['task_state']['candidate_repair_state']['events'].values() if x['scope']['step_id']=='prepare_database'],key=lambda x:x['repair_ordinal'])
                assert len(rows)==(5 if ending=='budget' else 4 if ending=='last' else 3 if ending=='repeat' else 2)
                assert rows[1]['progress_assessment']['resolved_targets']==['operation']
                assert rows[1]['progress_assessment']['check_records']['executable_syntax']['check_status']==('NOT_EVALUATED' if ending=='missing' else 'PASS')
                assert rows[1]['policy_receipt']['remaining_before_authorization']==3
                assert rows[1]['status']=='repairable'
                request=[x for x in c.requests if x['stage']=='generate'][4]
                assert 'Normally OMIT binding_env' in str(request) or 'Normally omit binding_env' in str(request)
                if ending=='missing':
                    assert 'NOT_EVALUATED' in str(request) and 'unknown_targets' in str(request)
                if ending in {'last','budget'}:
                    assert all(not r['progress_proven'] and r['status']=='repairable' for r in rows[2:4])
                    assert [r['policy_receipt']['remaining'] for r in rows[:4]]==[3,2,1,0]
                if ending in {'success','last','missing'}:
                    assert d['task_state']['status']=='completed' and len(c.commits)==7
                    prepare=[a for a in d['provenance_state']['attempts'].values() if a['step_id']=='prepare_database']
                    assert len(prepare)==1
                    assert '"GENOME_VERSION"' in str(c.executions)
                else:
                    assert len(c.commits)==2 and len(c.executions)==2
            else:
                assert len(c.commits)==2 and len(c.executions)==3
                build=[a for a in d['provenance_state']['attempts'].values() if a['step_id']=='build_database']
                assert len(build)==1 and build[0]['returncode']==0
                assert any('"inner_returncode": 255' in e['stdout'] for e in c.executions)
                assert len([x for x in c.timeline if x['event']=='log_read'])>=2
            if ending in {'stop','recovery','budget','repeat'}:
                stop=d['task_state']['managed_run_stop']
                assert stop['persistence_status']=='committed'
                assert stop['execution']['attempt_id']==''
                assert stop['scope']['step_id']==('build_database' if ending=='recovery' else 'prepare_database')
                if ending=='budget':
                    assert stop['stop_reason']['error_code']=='GENERATE_REPAIR_BUDGET_EXHAUSTED'
                    assert rows[-1]['policy_receipt']['remaining']==0
                if ending=='repeat':
                    assert rows[-1]['progress_assessment']['repeat_proven']
                    assert stop['stop_reason']['error_code']=='GENERATE_IDENTICAL_CANDIDATE_EXHAUSTED'
                assert stop['required_output_status']=='conflict'
                assert stop['delivery_status']=='failed'
                internal=stop['internal_diagnostic_delivery']
                assert internal['status']=='complete',internal
                for name,p in internal['paths'].items():
                    assert Path(p).is_file() and internal['receipts'][name]['status']=='created'
                assert 'Required output status: conflict' in Path(internal['paths']['report.md']).read_text()
                assert all((digest(Path(p).read_bytes()),Path(p).stat().st_mtime_ns,Path(p).stat().st_ino)==expected for p,expected in c.old_reports.items())
                before={p:(digest(Path(p).read_bytes()),Path(p).stat().st_mtime_ns) for p in internal['paths'].values()}
                repeated=await finalize_managed_stop(c.agent,c.task,values,terminal_boundary(values))
                assert repeated['stop_id']==stop['stop_id'] and repeated['internal_diagnostic_delivery']==internal
                assert before=={p:(digest(Path(p).read_bytes()),Path(p).stat().st_mtime_ns) for p in before}
            return d
        finally:await c.close()
    return asyncio.run(run())


@pytest.mark.parametrize('ending',['success','stop','recovery','last','budget','repeat','missing'])
def test_category_continuous_graph(ending):
    run_category(ending)
