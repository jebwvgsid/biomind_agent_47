"""F6: six distinct snpEff capabilities through the actual production graph.

External model responses and a new scientific-algorithm executable are explicit
stand-ins. Plan/approval/parser/helper/process/evidence/attester/Reviewer parser/
gate/Commit/FinalAudit are production implementations; no success-state seeding.
Only run with this change's guarded export_and_run launcher.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

SID = 'genomics/snpeff-annotation'
OPS = ['check_contig_compatibility', 'prepare_database', 'build_database',
       'annotate_variants', 'summarize_ann', 'finalize_outputs']
PYTHON = '/public/home/liusiqi/anaconda3/envs/biomind/bin/python'


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def save(name, value, *, full=False):
    root = Path(os.environ['BIOMIND_GER_ARTIFACTS'])
    body = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode()
    path = root / ('f6-' + name + ('.json.gz' if full else '.json'))
    path.write_bytes(gzip.compress(body, mtime=0) if full else body)
    return {'path': str(path), 'sha256': digest(path.read_bytes()), 'raw_sha256': digest(body)}


class F6Graph:
    def __init__(self, mode='normal', *, variant='中文 两样本'):
        self.mode, self.variant = mode, variant
        self.counts = {k: 0 for k in ['consultant', 'planner', 'generate', 'reviewer', 'recovery']}
        self.requests, self.events, self.approvals, self.executions, self.commits = [], [], [], [], []
        self.state_after_commits, self.provider_candidates, self.receptions = [], [], []
        self.commit_inputs=[]
        self.hook_hits = {'observe':0,'finalize':0,'prepare':0,'summary':0}

    async def setup(self):
        # This fixture has an independent launcher timeout. Retain the fatal
        # handler but cancel the old bootstrap's scheduled all-thread dump:
        # earlier -11 exits occurred near that diagnostic, with cause unknown.
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
        if not faulthandler.is_enabled():faulthandler.enable()
        import bio_agents
        from tests.test_plan_local_preaction_convergence_v2 import PlanLocalRealGraphTests
        self.case = PlanLocalRealGraphTests(methodName='test_graph_e_malformed_script_block_repair_is_visible_to_provider')
        self.case.SKILL_ID = SID
        self.compaction_patch=patch.object(bio_agents,'SCHEMA2_CONTEXT_COMPACTION_ENABLED',True)
        self.compaction_patch.start()
        await self.case.asyncSetUp()
        self.agent, self.root = self.case.agent, self.case.root
        del self.agent._observe_session_memory
        del self.agent._finalize_task_memory
        del self.agent.memory_manager.maybe_compress
        del self.agent._selected_skill_env_runner_violation
        del self.agent._workflow_future_skill_violation
        observe=self.agent._observe_session_memory;finalize=self.agent._finalize_task_memory
        async def observed(*args,**kwargs):
            self.hook_hits['observe']+=1
            return await observe(*args,**kwargs)
        async def finalized(*args,**kwargs):
            self.hook_hits['finalize']+=1
            return await finalize(*args,**kwargs)
        self.agent._observe_session_memory=observed;self.agent._finalize_task_memory=finalized
        from agent_core.workflow.phase_compaction import PhaseContextCompactor
        prepare=PhaseContextCompactor.prepare
        async def prepared(instance,*args,**kwargs):
            self.hook_hits['prepare']+=1
            return await prepare(instance,*args,**kwargs)
        self.prepare_patch=patch.object(PhaseContextCompactor,'prepare',prepared);self.prepare_patch.start()
        summary=self.agent.task_summary_renderer.write_task_summary
        def summarized(*args,**kwargs):
            self.hook_hits['summary']+=1
            return summary(*args,**kwargs)
        self.agent.task_summary_renderer.write_task_summary=summarized
        self.task = 'fix-f6-' + self.mode
        inputs = self.root / self.variant / '输入 新构造'
        inputs.mkdir(parents=True)
        self.reference, self.annotation, self.vcf = [inputs / x for x in ['新参考.fa', '新注释.gtf', '两样本.vcf']]
        self.reference.write_text('>fixtureA\nACGTACGT\n>fixtureB\nACGTACGT\n')
        self.annotation.write_text('fixtureA\tnew-fixture\tgene\t1\t8\t.\t+\t.\tgene_id "geneA";\n')
        contig = 'mismatchZ' if self.mode in ['false_required','report_conflict','false_diagnostic','empty_observation'] else 'fixtureA'
        self.vcf.write_text('##fileformat=VCFv4.2\n##contig=<ID=' + contig + ',length=8>\n'
                            '#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSYN1\tSYN2\n'
                            + contig + '\t2\tnew1\tC\tT\t.\tPASS\t.\tGT\t0/1\t1/1\n')
        out = self.root / self.variant / 'Part2.vairant_annotation' / '新机制结果'
        self.outputs = {key: out / name for key, name in [('annotated_vcf', 'annotated.vcf'), ('ann_summary', 'ann_summary.tsv'),
            ('report', 'report.md'), ('reproduce_script', 'reproduce.sh'), ('stage_status', 'stage_status.tsv')]}
        if self.mode=='false_diagnostic':self.outputs={}
        if self.mode=='report_conflict':
            out.mkdir(parents=True);self.outputs['report'].write_text('NEW SYNTHETIC USER REPORT — MUST REMAIN UNCHANGED\n')
        self.goal = ('Run a six-operation synthetic snpEff framework mechanism: check contigs, prepare an isolated database layout, '
            'build via the explicitly authorized synthetic algorithm executable, annotate synthetic ANN records, parse summaries, '
            'and deliver all required physical outputs. These are synthetic protocol fixtures, never real scientific snpEff results. '
            'Genome version new_fixture_v2; readonly inputs: ' + ', '.join(map(str, [self.vcf, self.reference, self.annotation]))
            + '; required outputs: ' + ', '.join(f'{k}={p}' for k,p in self.outputs.items()))
        if self.mode=='false_diagnostic':
            self.goal='Diagnose and report the actual contig compatibility observation only. A false observation is a valid diagnostic finding; no downstream annotation or physical output is requested. Inputs: '+', '.join(map(str,[self.vcf,self.reference,self.annotation]))
        persist = self.agent._persist_user_submission
        self.agent._persist_user_submission = lambda tid, _text: persist(tid, self.goal)
        # Replace only the external model leaf. Both production dispatch
        # wrappers retain message checks, invocation registration/consumption,
        # transport recording, output policy and retry/staleness handling.
        real_node_dispatch = self.agent._ainvoke_node_llm
        assert real_node_dispatch.__func__ is bio_agents.BioAnalystAgent._ainvoke_node_llm
        assert self.agent._ainvoke_llm.__func__ is bio_agents.BioAnalystAgent._ainvoke_llm
        case = self
        class FixedModelLeaf:
            def __init__(self, stage, node_context, call_kwargs):
                self.stage, self.node_context, self.call_kwargs = stage, node_context, call_kwargs
                self.model_kwargs, self.extra_body, self.kwargs = {}, {}, {}
                self.max_tokens = None
            def model_copy(self, *, update=None, **_kwargs):
                copied = object.__new__(type(self))
                copied.__dict__ = dict(self.__dict__)
                copied.__dict__.update(update or {})
                return copied
            def bind(self, **kwargs):
                return self.model_copy(update={'kwargs': {**self.kwargs, **kwargs}})
            async def ainvoke(self, delivered_messages, config=None, **_kwargs):
                binding = self.node_context.binding
                assert case.agent._node_invocations.binding_for(binding.invocation_id) == binding
                wire = [{'type': m.type, 'name': getattr(m, 'name', None), 'content': m.content}
                        for m in delivered_messages]
                case.receptions.append(('actual_provider_leaf_delivery', {
                    'stage': self.stage, 'binding': binding.to_dict(),
                    'delivered_messages_sha256': digest(json.dumps(wire, ensure_ascii=False, sort_keys=True, default=str)),
                    'config_metadata': deepcopy((config or {}).get('metadata', {})),
                    'config_tags': list((config or {}).get('tags', [])),
                    'bound_parameters': deepcopy(self.kwargs),
                    'request_ordinal': case.counts[self.stage] + 1}))
                return await case.provider(delivered_messages, stage=self.stage,
                    node_context=self.node_context, **self.call_kwargs)
        async def observed_node_dispatch(messages, *, node_context, stage, **kwargs):
            leaf = FixedModelLeaf(stage, node_context, dict(kwargs))
            response = await real_node_dispatch(messages, node_context=node_context, stage=stage,
                                               **{**kwargs, 'llm_override': leaf})
            entry = case.agent._node_invocations._entries[node_context.binding.invocation_id]
            assert entry.status == 'consumed'
            case.receptions.append(('actual_node_dispatch_consumed', {
                'stage': stage, 'binding': node_context.binding.to_dict(), 'status': entry.status,
                'transport_history_count': len(case.agent._provider_attempt_history.get(case.task, []))}))
            return response
        self.agent._ainvoke_node_llm = observed_node_dispatch
        self.agent.supervisor_node.use_llm_review = True
        self.agent.supervisor_node._llm_reviewer.llm = self.Reviewer(self)
        case=self
        class TerminalSummary:
            async def ainvoke(self,messages,**kwargs):
                case.hook_hits['terminal_summary_provider']=case.hook_hits.get('terminal_summary_provider',0)+1
                case.requests.append({'stage':'terminal_summary','messages':[{'type':m.type,'content':m.content} for m in messages]})
                return AIMessage(content=json.dumps({'fact_ids':['origin','stop_reason']}))
        self.agent.terminal_summary_llm=TerminalSummary()
        del self.agent.middleware.review_code
        self.agent.middleware.auto_approve_safe = False
        real_review = self.agent.middleware.review_code
        def review(code, *args, **kwargs):
            result = real_review(code, *args, **kwargs)
            if result[0] == 'approve':
                self.approvals.append({'code': code, 'sha256': digest(code), 'operation': self.current_operation()})
            return result
        self.agent.middleware.review_code = review
        # Keep the exact constructor's Recovery owner, hooks and adapters.
        # Replace only its external model leaf; do not reconstruct a smaller node.
        from agent_core.workflow.recovery_node import Schema2RecoveryNode
        owners=[getattr(cell.cell_contents,'__self__',None)
                for cell in self.agent._schema2_runtime_node_handlers['recovery'].__closure__ or ()]
        recovery=next(owner for owner in owners if isinstance(owner,Schema2RecoveryNode))
        assert recovery.context_compaction_enabled and recovery.compactor.__self__ is self.agent
        assert recovery.runtime_recovery.__self__ is self.agent
        recovery.model=self.Recovery(self)
        actual_event_sink=recovery.event_sink
        def event_sink(event,payload):
            self.receptions.append((event,deepcopy(payload)))
            if self.mode=='recovery_event_failure' and event=='recovery_started':
                raise OSError('explicit synthetic required Recovery event persistence failure')
            return actual_event_sink(event,payload)
        recovery.event_sink=event_sink
        self.recovery_node=recovery
        self.real_commit=self.agent._schema2_node_handlers['commit_step']
        async def observe_commit(state):
            self.commit_inputs.append(deepcopy(state))
            return await self.real_commit(state)
        self.agent._schema2_node_handlers['commit_step']=observe_commit
        self.agent.schema2_app = bio_agents.build_schema2_production_graph(self.agent._schema2_node_handlers).compile(checkpointer=self.agent.checkpointer)
        self.runtime_dir = self.root / '明确科学替身 runtime'
        self.runtime_dir.mkdir()
        self.java, self.jar = self.runtime_dir / 'synthetic_java', self.runtime_dir / 'synthetic_snpeff.jar'
        self.java.write_text('#!' + PYTHON + '\n' + self.science_source())
        self.java.chmod(0o755)
        self.jar.write_text('EXPLICIT SYNTHETIC ALGORITHM TOKEN; NOT A JAVA ARCHIVE\n')
        self.agent.pm.conda_root = str(self.runtime_dir)
        (self.runtime_dir / 'envs' / 'bio_gatk').mkdir(parents=True)

    def science_source(self):
        return '''import sys
from pathlib import Path
assert sys.argv[1] == '-jar'
assert Path(sys.argv[2]).read_text().startswith('EXPLICIT SYNTHETIC')
operation, db, genome = sys.argv[3:6]
db=Path(db)
if operation == 'build':
    assert (db/'data'/genome/'genes.gtf').is_file()
    predictor=db/'data'/genome/'snpEffectPredictor.bin'
    predictor.write_bytes(b'SYNTHETIC PREDICTOR SHAPE; NOT REAL SCIENCE')
elif operation == 'annotate':
    assert (db/'data'/genome/'snpEffectPredictor.bin').read_bytes().startswith(b'SYNTHETIC')
    source, output = map(Path,sys.argv[6:8])
    rows=[]
    for line in source.read_text().splitlines():
        if line.startswith('#CHROM'):
            rows.append('##INFO=<ID=ANN,Number=.,Type=String,Description="Synthetic fixture annotation">')
        if line and not line.startswith('#'):
            cols=line.split('\\t'); cols[7]='ANN='+cols[4]+'|synthetic_effect|MODIFIER|geneA';line='\\t'.join(cols)
        rows.append(line)
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text('\\n'.join(rows)+'\\n')
else: raise ValueError(operation)
print('SYNTHETIC_ALGORITHM_COMPLETED:'+operation)
'''

    def current_operation(self):
        from agent_core.plan_tracker import load_plan_state
        durable = load_plan_state(self.root, self.task)
        current_id = durable.get('current_step_id')
        return str(next(step for step in durable['steps'] if step['step_id'] == current_id)['operation'])

    def plan(self):
        registry = self.agent.skill_manager.get_execution_capability_registry([SID])
        workspace = {'local_ref':'plan.db', 'semantic_view':{'role':'snpeff_data_dir','format':'directory','description':'New isolated mutable workspace'},
            'artifact_contract':{'object_kind':'directory','resource_kind':'mutable_workspace','lifecycle':'plan_local','materialization_policy_id':'managed_intermediate_v1'},
            'role_associations':[{'step_id':op,'capability_id':'genomics.snpeff_annotation.'+op,'role':'artifact.snpeff_data_dir'} for op in OPS[1:4]]}
        config = {'local_ref':'plan.config','semantic_view':{'role':'snpeff_config','format':'text','description':'New isolated config'},
            'artifact_contract':{'object_kind':'file','lifecycle':'plan_local','materialization_policy_id':'managed_intermediate_v1'}}
        inputs = [
            ['input.variant_vcf','input.reference_fasta','input.annotation_gtf'],
            ['input.reference_fasta','input.annotation_gtf'],
            ['input.reference_fasta','input.annotation_gtf','plan.config'],
            ['input.variant_vcf','plan.config','plan.db'],
            ['output.annotated_vcf'],
            ['output.annotated_vcf','output.ann_summary']]
        outputs = [[],['plan.config'],[],['output.annotated_vcf'],['output.ann_summary'],
                   ['output.report','output.reproduce_script','output.stage_status']]
        steps=[]
        for i,op in enumerate(OPS[:1] if self.mode=='false_diagnostic' else OPS):
            cap=registry['genomics.snpeff_annotation.'+op]
            step={'step_id':op,'title':op+' — explicit synthetic mechanism','action':'execute',
                'purpose':'validation' if i==0 else 'analysis','tool':SID,'skill_ids':[SID],
                'execution_capability_id':cap['capability_id'],'operation':op,'inputs':inputs[i],'outputs':outputs[i],
                'dependencies':[] if i==0 else [OPS[i-1]],'batch_id':'','workflow_stage_ids':[],'risk_level':'low',
                'success_criteria':['Establish only actual newly produced synthetic protocol facts for '+op+'.']}
            for field in ['produces_bindings','produces_observations','explicit_side_effects']:
                if cap.get(field):step[field]=deepcopy(cap[field])
            if cap.get('workspace_mutations'):
                step['workspace_mutations']=[{**deepcopy(m),'workspace_ref':'plan.db'} for m in cap['workspace_mutations']]
            steps.append(step)
        return {'outcome_type':'SUBMIT_PLAN_FOR_REVIEW','proposed_artifacts':[] if self.mode=='false_diagnostic' else [workspace,config],'steps':steps,
            'validation_checklist':[{'check_id':'actual-effect-scope','description':
                'One negative diagnostic; no downstream inference.' if self.mode=='false_diagnostic' else
                'Six distinct capabilities; actual receipts; no real scientific claim.'}],
            'metadata':{'schema_version':2,'planner_contract':'structured_plan_v2'}}

    async def provider(self,messages,*,stage,**kwargs):
        self.counts[stage]+=1
        self.requests.append({'stage':stage,'ordinal':self.counts[stage],'messages':[
            {'type':m.type,'name':getattr(m,'name',None),'content':m.content} for m in messages]})
        if stage=='consultant':
            ops=[{'operation':'set_goal','value':self.goal,'supporting_evidence':['U1']}]
            for role,path in [('variant_vcf',self.vcf),('reference_fasta',self.reference),('annotation_gtf',self.annotation)]:
                ops.append({'operation':'upsert_input','input':{'role':role,'path':str(path),'required':True},'supporting_evidence':['U1']})
            for role,path in self.outputs.items():
                ops.append({'operation':'upsert_output_contract','output_contract':{'output_id':role,'role':role,'artifact_kind':'file',
                    'path':str(path),'required':True},'supporting_evidence':['U1']})
            return AIMessage(content=json.dumps({'action_type':'submit_planning_handoff','summary':'Authorized new synthetic mechanism only.',
                'selected_skills':[SID],'task_spec_operations':ops,'unresolved_ambiguities':[],'planner_feedback':None}),response_metadata={'finish_reason':'stop'})
        if stage=='planner':
            return AIMessage(content=json.dumps(self.plan()),response_metadata={'finish_reason':'stop'})
        assert stage=='generate'
        operation=self.current_operation()
        if self.mode=='voluntary_stop' and operation=='prepare_database':
            return AIMessage(content=json.dumps({'type':'blocked','reason':'The fixed model has no new scientific basis to justify proceeding.'}),response_metadata={'finish_reason':'stop'})
        action=self.action(operation,messages)
        if self.mode=='recovery' and operation==OPS[0] and self.counts['generate'] in [2,3,4,5]:
            ordinal=self.counts['generate']
            wire='{"action_type":' if ordinal==2 else json.dumps({'action_json':action}) if ordinal==3 else json.dumps({**action,'env':[]}) if ordinal==4 else json.dumps(action)
            self.provider_candidates.append({'operation':operation,'raw':wire,'scientific_body_sha256':digest(action['argv'][-1])})
            return AIMessage(content=wire,response_metadata={'finish_reason':'stop'})
        self.provider_candidates.append({'operation':operation,'candidate':deepcopy(action)})
        return AIMessage(content=json.dumps(action),response_metadata={'finish_reason':'stop'})

    def action(self,operation,messages):
        from agent_core.plan_tracker import load_plan_state,load_approved_plan_snapshot
        from agent_core.workspace_attestation import project_current_action_workspace_bindings
        durable=load_plan_state(self.root,self.task)
        snapshot=load_approved_plan_snapshot(self.root,self.task,state=durable)
        step=next(x for x in snapshot['steps'] if x['step_id']==durable['current_step_id'])
        registry=self.agent.skill_manager.get_execution_capability_registry([SID]);cap=registry[step['execution_capability_id']]
        template=json.loads(next(m.content for m in messages if getattr(m,'name','')=='approved_effect_emission_template'))
        wire=str(messages)
        paths={}
        current_step=next(x for x in durable['steps'] if x['step_id']==durable['current_step_id'])
        for identity in current_step.get('outputs',[]):
            path=(durable.get('plan_artifacts',{}).get(identity) or durable.get('artifact_ledger',{}).get(identity) or {}).get('canonical_path')
            if not path:
                outputs=((durable.get('current_task') or {}).get('task_spec') or {}).get('output_contracts') or []
                matches=[row for row in outputs if identity in (row.get('artifact_id'),row.get('output_id'),row.get('semantic_handle'))]
                assert len(matches)<=1,(identity,matches)
                path=matches[0].get('canonical_path') if matches else None
            if path:paths[identity]=path
        # Use the actually published declaration identities and public paths.
        outdecl=template['declarations']['produces_artifacts']
        for row in outdecl:
            identity=row['identity']
            assert identity in wire and paths.get(identity) in wire,(operation,identity,paths)
        binding={}
        if operation in OPS[1:4]:
            frozen=step['generate_binding_input_contract'];contract=frozen['binding_contract']
            catalog=project_current_action_workspace_bindings(plan_state=durable,approved_plan_snapshot=snapshot,current_step=step,
                capability_contract=cap,binding_rule=contract['operations'][operation],derived_artifact_layout=contract.get('derived_artifact_layout'))
            assert len(catalog)==1 and catalog[0]['plan_handle'] in wire
            if operation=='prepare_database':
                binding={'produced_bindings':{'genome_version':{'type':'string','value':'new_fixture_v2'},
                    'snpeff_db':{'type':'artifact_ref','value':{'handle':catalog[0]['plan_handle']}}}}
            else:
                binding={'consumed_bindings':{name:{'ref':'plan_binding:'+name} for name in ['genome_version','snpeff_db']}}
                assert str(self.java) in wire and str(self.jar) in wire
        prefix=''
        source=template['marker']+'\nfrom pathlib import Path\nimport os\n'
        if operation in ['build_database','annotate_variants']:
            config=next(row['canonical_path'] for row in durable['plan_artifacts'].values()
                if row.get('semantic_view',{}).get('role')=='snpeff_config')
            assert config in wire
            source+='assert "new_fixture_v2.genome" in Path('+repr(config)+').read_text()\n'
        if operation=='build_database':
            for path in [self.reference,self.annotation]:
                assert str(path) in wire
                source+='assert Path('+repr(str(path))+').read_bytes()\n'
        facts={'produces_artifacts':{},'produces_observations':{}}
        if operation=='check_contig_compatibility':
            for path in [self.vcf,self.reference,self.annotation]:assert str(path) in wire
            source+='vcf=Path('+repr(str(self.vcf))+'); ref=Path('+repr(str(self.reference))+'); gtf=Path('+repr(str(self.annotation))+')\n'
            source+="v={r.split('\\t')[0] for r in vcf.read_text().splitlines() if r and not r.startswith('#')}\n"
            source+="r={x[1:].split()[0] for x in ref.read_text().splitlines() if x.startswith('>')}\n"
            source+="g={x.split('\\t')[0] for x in gtf.read_text().splitlines() if x and not x.startswith('#')}\n"
            source+="value=bool(v) and v.issubset(r) and bool(v & g)\n"
            if self.mode=='empty_observation':source+='value=sorted(v & r)\n'
            facts['produces_observations']['contigs_compatible']={'value':'__ACTUAL_VALUE__'}
        elif operation=='prepare_database':
            source+='db=Path(os.environ["SNPEFF_DB"]); genome=os.environ["GENOME_VERSION"]\n'
            source+='(db/"data"/"genomes").mkdir(parents=True,exist_ok=True); (db/"data"/genome).mkdir(exist_ok=True)\n'
            source+='(db/"data"/"genomes"/(genome+".fa")).write_bytes(Path('+repr(str(self.reference))+').read_bytes())\n'
            source+='(db/"data"/genome/"genes.gtf").write_bytes(Path('+repr(str(self.annotation))+').read_bytes())\n'
            identity=outdecl[0]['identity'];path=paths[identity]
            source+='out=Path('+repr(path)+');out.parent.mkdir(parents=True,exist_ok=True);out.write_text(genome+".genome : synthetic fixture\\n")\n'
            source+='value=out.is_file() and (db/"data"/genome/"genes.gtf").stat().st_size>0\n'
            facts['produces_artifacts'][identity]={'path':path};facts['produces_observations']['database_layout_prepared']={'value':'__ACTUAL_VALUE__'}
        elif operation=='build_database':
            prefix=shlex.quote(PYTHON)+' -I -S '+shlex.quote(str(self.java))+' -jar '+shlex.quote(str(self.jar))+' build "$SNPEFF_DB" "$GENOME_VERSION"\n'
            source+='p=Path(os.environ["SNPEFF_DB"])/"data"/os.environ["GENOME_VERSION"]/"snpEffectPredictor.bin"\n'
            source+='value=p.read_bytes().startswith(b"SYNTHETIC")\n'
            facts['produces_observations']['snpeff_database_ready']={'value':'__ACTUAL_VALUE__'}
        elif operation=='annotate_variants':
            identity=outdecl[0]['identity'];path=paths[identity]
            prefix=shlex.quote(PYTHON)+' -I -S '+shlex.quote(str(self.java))+' -jar '+shlex.quote(str(self.jar))+' annotate "$SNPEFF_DB" "$GENOME_VERSION" '+shlex.quote(str(self.vcf))+' '+shlex.quote(path)+'\n'
            source+='out=Path('+repr(path)+');assert "ANN=" in out.read_text()\n'
            facts['produces_artifacts'][identity]={'path':path}
        elif operation=='summarize_ann':
            source+='rows=[x.split("\\t") for x in Path('+repr(str(self.outputs['annotated_vcf']))+').read_text().splitlines() if x and not x.startswith("#")]\n'
            source+='assert rows and all("ANN=" in row[7] for row in rows)\ncount=sum("ANN=" in row[7] for row in rows)\n'
            identity=outdecl[0]['identity'];path=paths[identity]
            source+='out=Path('+repr(path)+');out.parent.mkdir(parents=True,exist_ok=True);out.write_text("effect\\tcount\\nsynthetic_effect\\t"+str(count)+"\\n")\nvalue={"synthetic":True,"annotated_records":count}\n'
            facts['produces_artifacts'][identity]={'path':path};facts['produces_observations']['ann_summary_completed']={'value':'__ACTUAL_VALUE__'}
        else:
            for path in [self.outputs['annotated_vcf'],self.outputs['ann_summary']]:assert str(path) in wire
            source+='annotated=Path('+repr(str(self.outputs['annotated_vcf']))+'); summary=Path('+repr(str(self.outputs['ann_summary']))+')\n'
            source+='assert "ANN=" in annotated.read_text() and "synthetic_effect" in summary.read_text()\n'
            bodies={'report':'# SYNTHETIC FRAMEWORK MECHANISM\n\nAll outputs are generated synthetic protocol fixtures. No real snpEff science was run.\n',
                'reproduce_script':'#!/bin/bash\n# Synthetic fixture provenance only; does not run real science.\nprintf "synthetic mechanism receipt\\n"\n',
                'stage_status':'stage\tstatus\n'+''.join(op+'\tcompleted_synthetic\n' for op in OPS)}
            for row in outdecl:
                identity=row['identity'];path=paths[identity];role=next(k for k,p in self.outputs.items() if str(p)==path)
                body=bodies[role]
                source+='out=Path('+repr(path)+');out.parent.mkdir(parents=True,exist_ok=True);out.write_text('+repr(body)+')\n'
                facts['produces_artifacts'][identity]={'path':path}
            source+='value={"synthetic":True,"physical_reports":3}\n'
            facts['produces_observations']['final_outputs_recorded']={'value':'__ACTUAL_VALUE__'}
        literal=repr(facts).replace("'__ACTUAL_VALUE__'",'value')
        source+='print(emit_effect_evidence('+literal+'))\n'
        if self.mode in ['recovery','recovery_event_failure'] and operation==OPS[0] and self.counts['generate']==1:
            source=source.replace('print(emit_effect_evidence(', 'import sys\nprint("NEW_SYNTHETIC_RC_FAILURE",file=sys.stderr)\nsys.exit(7)\nprint(emit_effect_evidence(')
        if self.mode=='no_evidence' and operation==OPS[0]:
            source=source[:source.index('print(emit_effect_evidence(')]+'print("ACTUAL_CHECK_RAN_WITHOUT_EFFECT_ENVELOPE")\n'
        if self.mode in ['partial_build','missing_build_dependency'] and operation=='build_database':
            if self.mode=='partial_build':
                prefix=shlex.quote(PYTHON)+" -I -S -c 'import os;from pathlib import Path;p=Path(os.environ[\"SNPEFF_DB\"])/\"partial-write.txt\";p.write_text(\"actual partial write\")'\nprintf 'SYNTHETIC_PARTIAL_BUILD_FAILURE\\n' >&2\nexit 23\n"
            else:prefix="printf 'EXPLICIT_SYNTHETIC_DEPENDENCY_UNAVAILABLE\\n' >&2\nexit 23\n"
        if self.mode=='empty_summary' and operation=='summarize_ann':
            source=source.replace('print(emit_effect_evidence(', 'out.write_text("")\nprint(emit_effect_evidence(')
        body='set -euo pipefail\n'+prefix+shlex.quote(PYTHON)+" -I -S - <<'PY'\n"+source+'PY\n'
        # Native argv-only is declared before approval. No post-approval -lc rewrite.
        value={'type':'execute','operation':operation,'language':'bash',
            'argv':['/bin/bash','--noprofile','--norc','-c',body],**binding}
        if self.mode=='reviewer_correct' and operation==OPS[0]:value['code']=body
        return value

    class Reviewer:
        def __init__(self,case):self.case=case
        async def ainvoke(self,messages,**kwargs):
            c=self.case;c.counts['reviewer']+=1
            seed=next(m.content for m in messages if getattr(m,'name','')=='supervisor_advisory_seed')
            report=json.loads(seed.split('<completion_report>\n',1)[1].split('\n</completion_report>',1)[0])
            value={'report_id':report['report_id'],'assessment':'ACCEPTABLE','disposition':'completion_accepted',
                'issue_dispositions':[{'issue_id':i,'disposition':'completion_accepted','explanation':'Only actual synthetic protocol facts are accepted.'}
                    for i in report['semantic_review_requirements']['allowed_issue_ids']], 'confidence':0.9,'concerns':[],'suggestions':[]}
            if c.mode=='reviewer_veto':
                value.update(assessment='CONCERN',disposition='inspection_required')
                for row in value['issue_dispositions']:row['disposition']='inspection_required'
            c.requests.append({'stage':'reviewer','report':report,'response':deepcopy(value)})
            if c.mode=='reviewer_exhaust' or c.mode=='reviewer_correct' and c.counts['reviewer']<3:
                return AIMessage(content='{"report_id":',response_metadata={'finish_reason':'stop'})
            return AIMessage(content=json.dumps(value),response_metadata={'finish_reason':'stop'})

    class Recovery:
        def __init__(self,case):self.case=case
        async def ainvoke(self,messages,**kwargs):
            from agent_core.plan_tracker import load_plan_state
            c=self.case;c.counts['recovery']+=1;dur=load_plan_state(c.root,c.task)
            packet=dur['task_state']['causal_failure_packets'][dur['task_state']['active_failure_packet_ref']]['failure_packet']
            refs=[r for r in packet['inspection_refs'] if r.startswith('inspection:')]
            assert refs and all(r in str(messages) for r in refs)
            action='retry_step' if c.mode=='recovery' and c.counts['recovery']==1 else 'revalidate' if c.mode=='reviewer_exhaust' else 'blocked'
            directive={'schema_version':1,'directive_id':'f6-recovery-'+str(c.counts['recovery']),'task_id':c.task,'action':action,
                'target_step_id':dur['current_step_id'],'summary':'Explicit synthetic causal follow-up.','reason_code':'F6_SYNTHETIC_CAUSE',
                'instruction':'Preserve actual failed attempt; follow the current native action protocol.','evidence_refs':refs}
            c.requests.append({'stage':'recovery','directive':directive,'messages':[{'type':m.type,'content':m.content} for m in messages]})
            return AIMessage(content='',tool_calls=[{'name':'submit_recovery_directive','id':'submit-'+str(c.counts['recovery']),'args':directive}])

    async def execute(self,path,*,expected_sha256,lifecycle_callback=None,env=None,**kwargs):
        from execution.executor import run_script_path
        path=Path(path);approved=path.with_name('approved.sh').read_bytes()
        assert digest(path.read_bytes())==expected_sha256 and digest(approved)==self.approvals[-1]['sha256']
        assert '-lc' not in self.approvals[-1]['code'] and '--noprofile' in self.approvals[-1]['code']
        clean={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8'}
        for key in ['GENOME_VERSION','SNPEFF_DB']:
            if key in (env or {}):clean[key]=env[key]
        assert not {'BASH_ENV','ENV','HOME','PYTHONPATH'} & set(clean)
        def workspace_observation():
            if 'SNPEFF_DB' not in clean:return {}
            workspace=Path(clean['SNPEFF_DB'])
            assert workspace.is_relative_to(self.root/'task_outputs'/self.task/'plan_artifacts')
            result={}
            if workspace.exists():
                for item in workspace.rglob('*'):
                    assert not item.is_symlink()
                    if item.is_file():
                        assert item.stat().st_size<10000
                        result[str(item.relative_to(workspace))]=digest(item.read_bytes())
            return result
        before_workspace=workspace_observation()
        boot=sys.modules['__main__'];boot.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path));boot.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)]=clean
        try:
            out,err,rc=await run_script_path(str(path),expected_sha256=expected_sha256,lifecycle_callback=lifecycle_callback,
                profile=False,env=clean,managed_log_paths=kwargs.get('managed_log_paths'))
        finally:
            boot.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path));boot.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path),None)
        self.executions.append({'operation':self.current_operation(),'attempt_id':path.parent.name,'rc':rc,'stdout':out,'stderr':err,
            'approved_sha256':digest(approved),'executed_sha256':expected_sha256,'approved_code':approved.decode(),'wrapper':path.read_text(),
            'actual_workspace_before':before_workspace,'actual_workspace_after':workspace_observation()})
        return out,err,rc

    async def run(self):
        import bio_agents
        from agent_core.plan_tracker import load_plan_state,commit_runtime_evidence_state
        from agent_core.runtime_evidence import make_runtime_evidence
        runner=self.case._runner_state(self.task);runner['workflow_routing_schema_version']=2
        runner['messages']=[HumanMessage(content=self.goal)]
        # The scientific capability observation is the explicit external stub.
        # Production evidence identity, storage, evaluation, registry and resolver remain real.
        scope=self.agent._runtime_evidence_scope(SID)
        assert all(scope.values()),scope
        evidence=make_runtime_evidence({'skill_id':SID,'checked_env':scope['environment_id'],'status':'ready',
            'resolved_runtime_paths':{'RESOLVED_JAVA_BIN':str(self.java),'RESOLVED_SNPEFF_JAR':str(self.jar)}},
            task_id=self.task,skill_scope=scope,observed_at=datetime.now(timezone.utc).isoformat(),source_ref='explicit-new-synthetic-science-observation')
        commit_runtime_evidence_state(self.root,self.task,[evidence])
        app=self.agent._app_for_task(self.task,runner);config={'configurable':{'thread_id':self.task},'recursion_limit':90}
        incoming=runner
        async def service_callbacks():
            last=0.0
            while True:
                now=asyncio.get_running_loop().time()
                if now-last>=10:
                    heartbeat=Path(os.environ['BIOMIND_GER_ARTIFACTS'])/('f6-'+self.mode+'-heartbeat.jsonl')
                    with heartbeat.open('a') as out:out.write(json.dumps({'monotonic':now,'mode':self.mode,'calls':self.counts,
                        'executions':len(self.executions),'commits':len(self.commits)})+'\n')
                    last=now
                await asyncio.sleep(.001)
        self.service=asyncio.create_task(service_callbacks())
        with patch.object(bio_agents,'run_script_path',self.execute):
            for _ in range(12):
                async for event in app.astream(incoming,config=config,stream_mode='updates'):
                    self.events.append(event)
                    if 'commit_step' in event:
                        self.commits.append(deepcopy(event['commit_step']))
                        self.state_after_commits.append(deepcopy(load_plan_state(self.root,self.task)))
                snap=app.get_state(config)
                if not snap.next:break
                assert snap.tasks and snap.tasks[0].interrupts
                incoming=bio_agents.Command(resume='y')
        self.values=app.get_state(config).values;self.durable=load_plan_state(self.root,self.task)
        if self.mode in ['normal','recovery','reviewer_correct']:
            assert self.values['final_audit_outcome']['type']=='task_verified',self.values.get('managed_run_boundary')
            assert self.durable['task_state']['status']=='completed'
            receipts=self.durable['task_state']['typed_step_commit_receipts']
            assert len(receipts)==len(self.commits)==6
            expected=[OPS[0],*OPS] if self.mode=='recovery' else OPS
            assert len(self.executions)==len(self.approvals)==len(expected)
            assert [x['operation'] for x in self.executions]==expected
            assert [x['rc'] for x in self.executions]==([7]+[0]*6 if self.mode=='recovery' else [0]*6)
            assert all(p.is_file() and p.stat().st_size for p in self.outputs.values())
            assert len(self.durable['task_state']['accepted_plan_bindings'])==2
            attempts=self.durable['provenance_state']['attempts'].values()
            mutation_rows=[row for attempt in attempts for row in attempt.get('effect_evidence',{}).get('workspace_mutations',[])]
            assert len(mutation_rows)==2
            assert {row['operation'] for row in mutation_rows}=={'prepare_database','build_database'}
            assert len({row['canonical_resource_id'] for row in mutation_rows})==1
            assert all(row['attested'] and row['changed_during_attempt'] and row['attestation_status']=='mutated_and_validated'
                       and row['postcondition_satisfied'] for row in mutation_rows)
            before_build=next(x for x in self.state_after_commits if x['current_step_id']=='build_database')
            after_build=next(x for x in self.state_after_commits if x['current_step_id']=='annotate_variants')
            after_annotate=next(x for x in self.state_after_commits if x['current_step_id']=='summarize_ann')
            assert before_build['task_state']['accepted_plan_bindings']==after_build['task_state']['accepted_plan_bindings']==after_annotate['task_state']['accepted_plan_bindings']
            by_operation={row['operation']:row for row in self.executions}
            prepared=by_operation['prepare_database']['actual_workspace_after']
            built=by_operation['build_database']['actual_workspace_after']
            assert prepared==by_operation['build_database']['actual_workspace_before'] and prepared!=built
            assert built==by_operation['annotate_variants']['actual_workspace_before']==by_operation['annotate_variants']['actual_workspace_after']
            assert self.hook_hits['observe'] and self.hook_hits['prepare'],self.hook_hits
            if self.mode=='reviewer_correct':assert self.counts['reviewer']==8
            if self.mode=='recovery':
                assert self.counts['recovery']==1 and self.counts['generate']==10
                assert len({row['scientific_body_sha256'] for row in self.provider_candidates if 'raw' in row})==1
            generation=self.durable['state_generation']
            receipt_fingerprint=digest(json.dumps(receipts,sort_keys=True,default=str))
            for replay_index in [0,-1]:
                actual_input=self.commit_inputs[replay_index]
                replay=await self.real_commit(deepcopy(actual_input))
                after_replay=load_plan_state(self.root,self.task)
                assert after_replay['state_generation']==generation and after_replay['task_state']['status']=='completed'
                assert digest(json.dumps(after_replay['task_state']['typed_step_commit_receipts'],sort_keys=True,default=str))==receipt_fingerprint
                assert replay['commit_step_outcome']==self.commits[replay_index]['commit_step_outcome'],replay
                self.receptions.append(('actual_commit_replay',deepcopy(replay)))
        elif self.mode=='false_diagnostic':
            assert self.values['final_audit_outcome']['type']=='task_verified'
            assert len(self.commits)==len(self.executions)==1
            assert self.executions[0]['rc']==0 and 'false' in self.executions[0]['stdout']
            assert self.durable['task_state']['status']=='completed'
        else:
            assert self.values.get('final_audit_outcome',{}).get('type')!='task_verified'
            expected_commits={'voluntary_stop':1,'false_required':0,'report_conflict':0,'empty_observation':0,'recovery_event_failure':0,'partial_build':2,'missing_build_dependency':2,
                'empty_summary':4,'no_evidence':0,'reviewer_veto':0,'reviewer_exhaust':0,
                'repeat':0,'aba':0,'env_reorder':0,'env_unrelated':0}[self.mode]
            assert len(self.commits)==expected_commits,(self.mode,self.commits)
            assert self.durable['task_state'].get('status')!='completed'
            if self.mode in ['partial_build','missing_build_dependency']:
                assert self.executions[-1]['rc']==23
                assert 'annotate_variants' not in [row['operation'] for row in self.executions]
            if self.mode=='reviewer_exhaust':
                assert self.counts['reviewer']==3
                exhausted=self.values['verification_outcome']
                assert exhausted['payload']['automatic_review_stopped']
                before=len(self.executions)
                replay=await self.recovery_node.run(self.values)
                assert replay['recovery_outcome']['type']=='revalidation_required'
                checked=await self.agent._schema2_runtime_node_handlers['supervisor']({**self.values,**replay})
                assert self.counts['reviewer']==3 and len(self.executions)==before
                assert checked['verification_outcome']['payload']['automatic_review_stopped']
                self.receptions.append(('formal_revalidate_exhausted_receipt',deepcopy(checked)))
            if self.mode=='reviewer_veto':assert self.counts['reviewer']==1
            if self.mode=='empty_observation':assert '"value":[]' in self.executions[0]['stdout']
            if self.mode=='recovery_event_failure':
                assert self.counts['generate']==1 and self.counts['recovery']==0 and len(self.executions)==1
                assert self.executions[0]['rc']==7
                assert self.values['recovery_outcome']['type']=='internal_protocol_error'
                assert self.values['recovery_outcome']['payload']['error_code']=='RECOVERY_EVENT_PERSISTENCE_FAILED'
            if self.mode=='report_conflict':
                assert self.outputs['report'].read_text()=='NEW SYNTHETIC USER REPORT — MUST REMAIN UNCHANGED\n'
        # Execute the actual outer run owner's terminal hook with the actual
        # graph snapshot. It alone derives the boundary and writes stop reports.
        self.terminal=await self.agent._finalize_managed_terminal(self.task,app.get_state(config))
        self.durable=load_plan_state(self.root,self.task)
        if self.mode not in ['normal','recovery','reviewer_correct','false_diagnostic']:
            assert self.terminal and self.durable['task_state']['status'] in ['failed','blocked'],self.terminal
            for key in ['report','reproduce_script','stage_status']:
                assert self.outputs[key].is_file(),(self.mode,key,self.terminal)
            if self.mode=='report_conflict':
                assert self.outputs['report'].read_text()=='NEW SYNTHETIC USER REPORT — MUST REMAIN UNCHANGED\n'
            diagnostic=self.terminal['terminal_diagnostic']
            if self.mode=='recovery_event_failure':
                assert diagnostic['origin']['returncode']==7
                assert diagnostic['origin']['attempt_id']==self.executions[0]['attempt_id']
                assert diagnostic['causal_execution']['attempt_id']==self.executions[0]['attempt_id']
            assert diagnostic['persistence_status']=='committed' and diagnostic['summary_status']=='COMPLETED'
            assert diagnostic['delivery_status']==('partial' if self.mode=='report_conflict' else 'complete')
            frozen={key:digest(self.outputs[key].read_bytes()) for key in ['report','reproduce_script','stage_status']}
            before_calls=deepcopy(self.counts);before_summary=self.hook_hits['terminal_summary_provider']
            repeated=await self.agent._finalize_managed_terminal(self.task,app.get_state(config))
            assert repeated==self.terminal and self.counts==before_calls
            assert self.hook_hits['terminal_summary_provider']==before_summary==1
            assert frozen=={key:digest(self.outputs[key].read_bytes()) for key in frozen}
            self.receptions.append(('actual_outer_terminal_idempotent',{'report_sha256':frozen,'summary_calls':before_summary}))
        return self.values,self.durable

    async def close(self):
        # Preserve actual newly generated scripts/receipts before fixture teardown.
        output=Path(os.environ['BIOMIND_GER_ARTIFACTS'])/('f6-preserved-'+self.mode)
        taskroot=self.root/'task_outputs'/self.task
        for pattern in ['attempts/*/approved.sh','attempts/*/executed.sh','attempts/*/stdout.log','attempts/*/stderr.log',
                        'attempts/*/exit_status.json','executions/execution_records.jsonl','plan_state.json']:
            for source in taskroot.glob(pattern):
                assert source.is_file() and not source.is_symlink();target=output/source.relative_to(taskroot)
                target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
        for role,path in self.outputs.items():
            if path.is_file():
                target=output/'outputs'/path.name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
        full=save(self.mode+'-full',{'counts':self.counts,'requests':self.requests,'events':self.events,
            'approvals':self.approvals,'executions':self.executions,'commits':self.commits,'state_after_commits':self.state_after_commits,
            'values':getattr(self,'values',{}),'durable':getattr(self,'durable',{}),'provider_candidates':self.provider_candidates,
            'receptions':self.receptions,'hook_hits':self.hook_hits,'terminal':getattr(self,'terminal',{})},full=True)
        save(self.mode+'-summary',{'mode':self.mode,'counts':self.counts,'routes':[next(iter(x)) for x in self.events],
            'executions':[{k:v for k,v in x.items() if k not in ['wrapper','approved_code']} for x in self.executions],
            'commit_outcomes':[x.get('commit_step_outcome') for x in self.commits],
            'final_outcome':getattr(self,'values',{}).get('final_audit_outcome'),'full':full,
            'substitutes':{'model':'fixed business replies','science':'new executable writes explicit synthetic predictor/ANN shapes',
                'runtime_observation':'make_runtime_evidence for actually created synthetic files; not host Java availability',
                'approval':'existing middleware interrupts resumed with explicit y; Plan input via existing _prompt_user',
                'memory':'real archive/finalize/summary restored; schema2 compactor enabled and real prepare spied'},
            'hook_hits':self.hook_hits,
            'terminal':getattr(self,'terminal',{}),
            'test_diagnostic_adapter':'old bootstrap scheduled stack dump cancelled; fatal handler retained; independent launcher timeout retained; light event-loop heartbeat',
            'real':['handoff','Plan','resource association','approval snapshot','Generate/parser/helper','script approval','tiny executor',
                'effect parser','workspace attester','Reviewer parser','CompletionGate','Commit/CAS','FinalAudit'],
            'real_provider_calls':0,'real_scientific_tool_calls':0})
        await self.agent.aclose()
        if hasattr(self,'service'):
            self.service.cancel()
            try:await self.service
            except asyncio.CancelledError:pass
        await self.case.asyncTearDown()
        self.prepare_patch.stop();self.compaction_patch.stop()


def test_IT01_six_distinct_snpeff_capabilities_native_argv():
    async def scenario():
        f=F6Graph();await f.setup()
        try:await f.run()
        finally:await f.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['recovery','reviewer_correct','false_required','false_diagnostic','partial_build','missing_build_dependency',
    'empty_summary','empty_observation','no_evidence','reviewer_veto','reviewer_exhaust','voluntary_stop','report_conflict','recovery_event_failure'])
def test_F6_real_graph_recovery_and_negative_combinations(mode):
    async def scenario():
        f=F6Graph(mode);await f.setup()
        try:await f.run()
        finally:await f.close()
    asyncio.run(scenario())
