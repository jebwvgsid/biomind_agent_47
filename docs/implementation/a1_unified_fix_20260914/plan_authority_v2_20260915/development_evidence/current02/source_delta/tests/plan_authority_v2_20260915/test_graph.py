"""Plan-authority V2 graph witnesses; run only in the existing guarded export.

The fixed model leaf, newly created scientific executable, TaskRouter intent
precondition and Consultant external preflight remain explicit substitutes.
Plan/parser/approval/helper/process/evidence/attester/gate/Commit are real. The
new public candidates contain resource_uses directly; the driver never fills
legacy inputs or resource-side associations before handing them to production.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import shlex
import sys
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tests.a1_unified_fix.test_f6_graph import F6Graph, OPS, PYTHON, SID, digest


GENERIC = 'framework.generic.execute'
DOMAIN = 'genomics.snpeff_annotation.'
CONTEXT_SKILLS = [SID, 'genomics/wgs-gatk-snp-indel',
    'genomics/wgs-bcftools-snp-indel', 'genomics/wgs-short-read-mapping']


class AuthorityGraph(F6Graph):
    def __init__(self, scenario):
        super().__init__('v2_' + scenario)
        self.scenario = scenario
        self.lookups = []
        self.step_generations = {}
        self.plan_review_inputs = []
        self.declared_implementations = {}
        self.raw_plans = []
        self.validator_calls = []
        self.expected_planner_calls = 1
        self.path_resolution_receipts = []

    async def setup(self):
        await super().setup()
        import bio_agents
        # Restore the production PlanReview reader owner, supplying only its
        # explicit test input. No approval state is fabricated by this fixture.
        del self.agent._prompt_user
        def approve(*args, **kwargs):
            self.plan_review_inputs.append({'args': args, 'kwargs': kwargs, 'reply': 'y'})
            return 'y'
        self.input_patch = patch.object(bio_agents, 'safe_input', approve)
        self.input_patch.start()
        review = self.agent.middleware.review_code
        def observed_review(*args, **kwargs):
            before = len(self.approvals)
            result = review(*args, **kwargs)
            if len(self.approvals) > before:
                self.approvals[-1]['step_id'] = self.current_step()[1]['step_id']
            return result
        self.agent.middleware.review_code = observed_review
        if self.scenario != 'renamed_repeated':
            self.outputs['preflight_log'] = next(iter(self.outputs.values())).parent / 'preflight-log.txt'
        self.goal += (' Planner may organize these same synthetic obligations into independent or combined steps. '
            'Input presence/readability and contig compatibility must actually be checked; the newly created '
            'synthetic runtime must be checked, preparation/build must establish the actual isolated database, '
            'and annotation, ANN summary and all requested reports must be physically produced. '
            'A failed dependency must block downstream success. No real scientific result is asserted.')
        if 'preflight_log' in self.outputs:
            self.goal += ' Also write the checked synthetic input names to preflight_log=' + str(self.outputs['preflight_log'])
        if self.scenario == 'advisory_context':
            self.goal += (' Use these four public Skills as knowledge context for the independent generic input check only: '
                + ', '.join(CONTEXT_SKILLS) + '. They do not authorize running additional scientific tools.')
        actual = self.agent._planner_observed_call
        def observed(tid, observation, phase, operation, *args, **kwargs):
            row = {'phase': phase, 'args': deepcopy(args), 'kwargs': deepcopy(kwargs)}
            self.validator_calls.append(row)
            try:
                result = actual(tid, observation, phase, operation, *args, **kwargs)
            except Exception as error:
                row['error'] = {'type': type(error).__name__, 'message': str(error)}
                raise
            row['result'] = deepcopy(result)
            return result
        self.agent._planner_observed_call = observed

    def current_step(self, *, approved=False):
        from agent_core.plan_tracker import load_plan_state, load_approved_plan_snapshot
        durable = load_plan_state(self.root, self.task)
        identity = durable['current_step_id']
        source = load_approved_plan_snapshot(self.root, self.task, state=durable) if approved else durable
        matches = [step for step in source['steps'] if step['step_id'] == identity]
        assert len(matches) == 1, (identity, [step['step_id'] for step in source['steps']])
        step = matches[0]
        self.lookups.append({'step_id': identity, 'operation': step['operation'],
            'execution_capability_id': step['execution_capability_id'], 'approved_snapshot': approved})
        return durable, step

    def current_operation(self):
        return self.current_step()[1]['operation']

    @staticmethod
    def use(reference, access='read', role=None):
        value = {'resource_ref': reference, 'access': access}
        if role is not None:
            value['role'] = role
        return value

    def plan(self):
        registry = self.agent.skill_manager.get_execution_capability_registry([SID])
        workspace = {'local_ref': 'plan.db',
            'semantic_view': {'role': 'snpeff_data_dir', 'format': 'directory', 'description': 'New isolated mutable workspace'},
            'artifact_contract': {'object_kind': 'directory', 'resource_kind': 'mutable_workspace',
                'lifecycle': 'plan_local', 'materialization_policy_id': 'managed_intermediate_v1'}}
        config = {'local_ref': 'plan.config',
            'semantic_view': {'role': 'snpeff_config', 'format': 'text', 'description': 'Explicit cross-step config'},
            'artifact_contract': {'object_kind': 'file', 'lifecycle': 'plan_local',
                'materialization_policy_id': 'managed_intermediate_v1'}}
        uses = {
            'check_contig_compatibility': [self.use('input.' + role) for role in ('variant_vcf', 'reference_fasta', 'annotation_gtf')],
            'prepare_database': [self.use('input.reference_fasta'), self.use('input.annotation_gtf'),
                self.use('plan.db', 'mutate', 'artifact.snpeff_data_dir')],
            'build_database': [self.use('input.reference_fasta'), self.use('input.annotation_gtf'), self.use('plan.config'),
                self.use('plan.db', 'mutate', 'artifact.snpeff_data_dir')],
            'annotate_variants': [self.use('input.variant_vcf'), self.use('plan.config'),
                self.use('plan.db', 'read', 'artifact.snpeff_data_dir')],
            'summarize_ann': [self.use('output.annotated_vcf')],
            'finalize_outputs': [self.use('output.annotated_vcf'), self.use('output.ann_summary')],
        }
        outputs = {'check_contig_compatibility': [], 'prepare_database': ['plan.config'], 'build_database': [],
            'annotate_variants': ['output.annotated_vcf'], 'summarize_ann': ['output.ann_summary'],
            'finalize_outputs': ['output.report', 'output.reproduce_script', 'output.stage_status']}
        steps = []
        self.declared_implementations = {}
        def append(identity, operation, *, implementation=None, reads=None, writes=None, observation=None):
            generic = implementation is not None
            cap = {} if generic else registry[DOMAIN + operation]
            step = {'step_id': identity, 'title': identity + ' explicit synthetic obligation', 'action': 'execute',
                'purpose': 'validation' if not writes and (generic or operation == OPS[0]) else 'analysis',
                'tool': 'bash' if generic else SID, 'skill_ids': [] if generic else [SID],
                'execution_capability_id': GENERIC if generic else cap['capability_id'],
                'operation': 'execute' if generic else operation,
                'resource_uses': deepcopy(reads if generic else uses[operation]),
                'outputs': deepcopy(writes if generic else outputs[operation]),
                'dependencies': [steps[-1]['step_id']] if steps else [], 'batch_id': '',
                'workflow_stage_ids': [], 'risk_level': 'low',
                'success_criteria': ['Perform the explicitly declared synthetic work and emit only actual measured facts.']}
            if generic:
                self.declared_implementations[identity] = implementation
                if self.scenario == 'advisory_context' and identity == 'probe-731':
                    step['skill_ids'] = list(CONTEXT_SKILLS)
                if observation:
                    step['produces_observations'] = [{'identity': observation,
                        'evidence_contract': {'required_fields': ['value', 'details']}}]
            else:
                for field in ('produces_bindings', 'produces_observations', 'explicit_side_effects'):
                    if cap.get(field):
                        step[field] = deepcopy(cap[field])
                if cap.get('workspace_mutations'):
                    step['workspace_mutations'] = [{**deepcopy(m), 'workspace_ref': 'plan.db'}
                        for m in cap['workspace_mutations']]
            steps.append(step)
        if self.scenario != 'renamed_repeated':
            read_uses = [self.use('input.' + x) for x in ('variant_vcf', 'reference_fasta', 'annotation_gtf')]
            append('probe-731', 'execute', implementation='generic_read', reads=read_uses, writes=[], observation='input_probe')
            append('ledger-205', 'execute', implementation='generic_write', reads=read_uses,
                writes=['output.preflight_log'], observation='preflight_log_written')
        instances = [('gate-418', OPS[0]), ('layout-563', OPS[1]), ('compile-927', OPS[2]),
            ('effects-146', OPS[3]), ('aggregate-832', OPS[4]), ('deliver-604', OPS[5])]
        for identity, operation in instances:
            if self.scenario == 'generic_merged' and operation in OPS[4:]:
                continue
            append(identity, operation)
            if operation == OPS[0] and self.scenario in ('renamed_repeated', 'generic_recovery'):
                append('independent-944', operation)
        if self.scenario == 'generic_merged':
            append('publish-386', 'execute', implementation='generic_summary_and_reports',
                reads=[self.use('output.annotated_vcf')],
                writes=['output.ann_summary', 'output.report', 'output.reproduce_script', 'output.stage_status'],
                observation='summary_and_reports_written')
        return {'outcome_type': 'SUBMIT_PLAN_FOR_REVIEW', 'proposed_artifacts': [workspace, config], 'steps': steps,
            'validation_checklist': [{'check_id': 'preserved-obligations', 'description': self.goal}],
            'metadata': {'schema_version': 2, 'planner_contract': 'structured_plan_v2', 'resource_contract_version': 3}}

    async def provider(self, messages, *, stage, **kwargs):
        if stage == 'consultant':
            response = await super().provider(messages, stage=stage, **kwargs)
            if self.scenario == 'advisory_context':
                handoff = json.loads(response.content)
                handoff['selected_skills'] = list(CONTEXT_SKILLS)
                return AIMessage(content=json.dumps(handoff, ensure_ascii=False), response_metadata={'finish_reason': 'stop'})
            return response
        self.counts[stage] += 1
        self.requests.append({'stage': stage, 'ordinal': self.counts[stage], 'messages': [
            {'type': m.type, 'name': getattr(m, 'name', None), 'content': m.content} for m in messages]})
        if stage == 'planner':
            candidate = self.plan()
            assert all('inputs' not in step for step in candidate['steps'])
            assert all('role_associations' not in item for item in candidate['proposed_artifacts'])
            raw = json.dumps(candidate, ensure_ascii=False)
            # A rejection is preserved as a bounded witness, not retried with
            # a silently expanded legacy candidate by this fixed responder.
            if self.counts['planner'] > 1:
                from agent_core.plan_contract import planner_stop_wire_example
                raw = json.dumps(planner_stop_wire_example(), ensure_ascii=False)
            self.raw_plans.append(raw)
            return AIMessage(content=raw, response_metadata={'finish_reason': 'stop'})
        assert stage == 'generate', stage
        _, step = self.current_step(approved=True)
        identity = step['step_id']
        self.requests[-1].update(step_id=identity, operation=step['operation'])
        self.step_generations[identity] = self.step_generations.get(identity, 0) + 1
        action = self.action(step, messages)
        protocol = json.loads(next(m.content for m in messages if getattr(m, 'name', '') == 'current_generate_output_protocol'))
        if step['execution_capability_id'] == GENERIC:
            assert protocol['mode'] == 'script_block_v1', protocol['mode']
            raw = '<shell>\n' + action['argv'][-1] + '</shell>'
        else:
            assert protocol['mode'] == 'native_action_json', protocol['mode']
            raw = json.dumps(action, ensure_ascii=False)
        self.provider_candidates.append({'step_id': identity, 'operation': step['operation'],
            'published_mode': protocol['mode'], 'raw_response': raw})
        return AIMessage(content=raw, response_metadata={'finish_reason': 'stop'})

    def artifact_paths(self, durable, step, template):
        paths = {}
        task_outputs = durable['current_task']['task_spec']['output_contracts']
        for identity in step.get('outputs', []):
            row = durable.get('plan_artifacts', {}).get(identity) or durable.get('artifact_ledger', {}).get(identity) or {}
            source = 'plan_artifacts_or_artifact_ledger'
            if not row.get('canonical_path'):
                # User outputs have their authoritative declaration in TaskSpec;
                # match explicit identifiers exactly, never by role substring.
                matches = [entry for entry in task_outputs if identity in
                    (entry.get('artifact_id'), entry.get('output_id'), entry.get('semantic_handle'))]
                assert len(matches) <= 1, (identity, matches)
                row = matches[0] if matches else {}
                source = 'current_task.task_spec.output_contracts'
            if row.get('canonical_path'):
                paths[identity] = row['canonical_path']
                self.path_resolution_receipts.append({'step_id': step['step_id'], 'identity': identity,
                    'source': source, 'canonical_path': row['canonical_path']})
        for declaration in template['declarations']['produces_artifacts']:
            assert declaration['identity'] in paths, (step['step_id'], declaration, paths)
        return paths

    @staticmethod
    def stage_status_body(durable):
        return ('# Durable status snapshot taken before this action execution and Commit.\n'
            'step_id\toperation\tobserved_status\n' + ''.join(
                str(step['step_id']) + '\t' + str(step['operation']) + '\t' + str(step.get('status', 'UNKNOWN')) + '\n'
                for step in durable['steps']))

    def action(self, step, messages):
        if step['execution_capability_id'] == GENERIC:
            return self.generic_action(step, messages)
        return self.domain_action(step['operation'], messages)

    def domain_action(self,operation,messages):
        from agent_core.plan_tracker import load_plan_state,load_approved_plan_snapshot
        from agent_core.workspace_attestation import project_current_action_workspace_bindings
        durable=load_plan_state(self.root,self.task)
        snapshot=load_approved_plan_snapshot(self.root,self.task,state=durable)
        step=next(x for x in snapshot['steps'] if x['step_id']==durable['current_step_id'])
        registry=self.agent.skill_manager.get_execution_capability_registry([SID]);cap=registry[step['execution_capability_id']]
        template=json.loads(next(m.content for m in messages if getattr(m,'name','')=='approved_effect_emission_template'))
        wire=str(messages)
        paths=self.artifact_paths(durable,step,template)
        outdecl=template['declarations']['produces_artifacts']
        for row in outdecl:
            identity=row['identity']
            assert identity in wire and paths[identity] in wire,(step['step_id'],identity,paths)
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
                'stage_status':self.stage_status_body(durable)}
            for row in outdecl:
                identity=row['identity'];path=paths[identity];role=next(k for k,p in self.outputs.items() if str(p)==path)
                body=bodies[role]
                source+='out=Path('+repr(path)+');out.parent.mkdir(parents=True,exist_ok=True);out.write_text('+repr(body)+')\n'
                facts['produces_artifacts'][identity]={'path':path}
            source+='value={"synthetic":True,"physical_reports":3}\n'
            facts['produces_observations']['final_outputs_recorded']={'value':'__ACTUAL_VALUE__'}
        literal=repr(facts).replace("'__ACTUAL_VALUE__'",'value')
        source+='print(emit_effect_evidence('+literal+'))\n'
        if self.scenario == 'generic_blocked' and operation == 'build_database':
            prefix = "printf 'V2_EXPLICIT_SCIENTIFIC_STANDIN_UNAVAILABLE\\n' >&2\nexit 23\n"
        body='set -euo pipefail\n'+prefix+shlex.quote(PYTHON)+" -I -S - <<'PY'\n"+source+'PY\n'
        return {'action_type':'execute','operation':operation,'language':'bash',
            'argv':['/bin/bash','--noprofile','--norc','-c',body],**binding}

    def generic_action(self, step, messages):
        durable, frozen = self.current_step(approved=True)
        assert step['step_id'] == frozen['step_id']
        implementation = self.declared_implementations[step['step_id']]
        template = json.loads(next(m.content for m in messages
            if getattr(m, 'name', '') == 'approved_effect_emission_template'))
        paths = self.artifact_paths(durable, frozen, template)
        wire = str(messages)
        assert all(identity in wire and path in wire for identity, path in paths.items())
        assert all(Path(path).is_relative_to(self.root) for path in paths.values())
        facts = {'produces_artifacts': {}, 'produces_observations': {}}
        source = template['marker'] + '\nfrom pathlib import Path\n'
        if implementation in ('generic_read', 'generic_write'):
            input_paths = [str(path) for path in (self.vcf, self.reference, self.annotation)]
            assert all(path in wire for path in input_paths)
            source += 'inputs = [Path(p) for p in ' + repr(input_paths) + ']\n'
            source += 'sizes = [len(p.read_bytes()) for p in inputs]\nvalue = all(n > 0 for n in sizes)\n'
            source += 'details = {"input_names": [p.name for p in inputs], "byte_counts": sizes}\n'
            if implementation == 'generic_write':
                assert len(paths) == 1
                path = next(iter(paths.values()))
                source += 'out = Path(' + repr(path) + ')\nout.parent.mkdir(parents=True, exist_ok=True)\n'
                source += 'out.write_text("SYNTHETIC PREFLIGHT\\n" + "\\n".join(p.name for p in inputs) + "\\n")\n'
                source += 'value = value and out.stat().st_size > 0\ndetails["written_bytes"] = out.stat().st_size\n'
        elif implementation == 'generic_summary_and_reports':
            annotated = str(self.outputs['annotated_vcf'])
            assert annotated in wire
            source += 'rows = [x.split("\\t") for x in Path(' + repr(annotated) + ').read_text().splitlines() if x and not x.startswith("#")]\n'
            source += 'assert rows and all("ANN=" in row[7] for row in rows)\ncount = sum("ANN=" in row[7] for row in rows)\n'
            for identity, path in paths.items():
                roles = [role for role, declared in self.outputs.items() if str(declared) == path]
                assert len(roles) == 1
                role = roles[0]
                if role == 'ann_summary':
                    expression = '"effect\\tcount\\nsynthetic_effect\\t" + str(count) + "\\n"'
                elif role == 'report':
                    expression = repr('# SYNTHETIC FRAMEWORK MECHANISM\n\nThe same approved action parsed ANN and wrote reports; no real snpEff science was run.\n')
                elif role == 'reproduce_script':
                    expression = repr('#!/bin/bash\n# New synthetic provenance only.\nprintf "synthetic mechanism receipt\\n"\n')
                elif role == 'stage_status':
                    expression = repr(self.stage_status_body(durable))
                else:
                    raise AssertionError(role)
                source += 'out = Path(' + repr(path) + ')\nout.parent.mkdir(parents=True, exist_ok=True)\nout.write_text(' + expression + ')\n'
            source += 'value = all(Path(p).stat().st_size > 0 for p in ' + repr(list(paths.values())) + ')\n'
            source += 'details = {"annotated_records": count, "physical_outputs": ' + repr(len(paths)) + '}\n'
        else:
            raise AssertionError(implementation)
        for identity, path in paths.items():
            facts['produces_artifacts'][identity] = {'path': path}
        for row in template['declarations']['produces_observations']:
            facts['produces_observations'][row['identity']] = {'value': '__ACTUAL_VALUE__', 'details': '__ACTUAL_DETAILS__'}
        if self.scenario == 'generic_recovery' and implementation == 'generic_read' and self.step_generations[step['step_id']] == 1:
            source += 'import sys\nprint("V2_NEW_REAL_RC7", file=sys.stderr)\nsys.exit(7)\n'
        literal = repr(facts).replace("'__ACTUAL_VALUE__'", 'value').replace("'__ACTUAL_DETAILS__'", 'details')
        source += 'print(emit_effect_evidence(' + literal + '))\n'
        body = 'set -euo pipefail\n' + shlex.quote(PYTHON) + " -I -S - <<'PY'\n" + source + 'PY\n'
        return {'action_type': 'execute', 'operation': step['operation'], 'language': 'bash',
            'argv': ['/bin/bash', '--noprofile', '--norc', '-c', body]}

    class Recovery:
        def __init__(self, case):
            self.case = case

        async def ainvoke(self, messages, **kwargs):
            from agent_core.plan_tracker import load_plan_state
            case = self.case
            case.counts['recovery'] += 1
            durable = load_plan_state(case.root, case.task)
            packet = durable['task_state']['causal_failure_packets'][durable['task_state']['active_failure_packet_ref']]['failure_packet']
            refs = [ref for ref in packet['inspection_refs'] if ref.startswith('inspection:')]
            assert refs and all(ref in str(messages) for ref in refs)
            retry = case.scenario == 'generic_recovery' and case.counts['recovery'] == 1
            directive = {'schema_version': 1, 'directive_id': 'v2-causal-' + str(case.counts['recovery']),
                'task_id': case.task, 'action': 'retry_step' if retry else 'blocked',
                'target_step_id': durable['current_step_id'], 'summary': 'Follow the actual new isolated attempt.',
                'reason_code': 'V2_NEW_SYNTHETIC_RC', 'instruction': 'Correct this same approved generic step using its actual causal receipt.',
                'evidence_refs': refs}
            case.requests.append({'stage': 'recovery', 'directive': deepcopy(directive), 'messages': [
                {'type': m.type, 'content': m.content} for m in messages]})
            return AIMessage(content='', tool_calls=[{'name': 'submit_recovery_directive',
                'id': 'submit-v2-' + str(case.counts['recovery']), 'args': directive}])

    async def execute(self, path, *, expected_sha256, lifecycle_callback=None, env=None, **kwargs):
        from execution.executor import run_script_path
        _, step = self.current_step(approved=True)
        path = Path(path)
        approved = path.with_name('approved.sh').read_bytes()
        assert digest(path.read_bytes()) == expected_sha256
        assert digest(approved) == self.approvals[-1]['sha256']
        assert '-lc' not in self.approvals[-1]['code']
        if step['execution_capability_id'] != GENERIC:
            assert '--noprofile' in self.approvals[-1]['code']
        # The script-block body does not contain a native argv declaration.
        # The same executor's profile=False and bounded clean environment below
        # own its non-login process mode, as in the existing F6 adapter.
        clean = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}
        for key in ('GENOME_VERSION', 'SNPEFF_DB'):
            if key in (env or {}):
                clean[key] = env[key]
        assert not {'BASH_ENV', 'ENV', 'HOME', 'PYTHONPATH'} & set(clean)
        def workspace_observation():
            if 'SNPEFF_DB' not in clean:
                return {}
            workspace = Path(clean['SNPEFF_DB'])
            assert workspace.is_relative_to(self.root / 'task_outputs' / self.task / 'plan_artifacts')
            result = {}
            if workspace.exists():
                for item in workspace.rglob('*'):
                    assert not item.is_symlink()
                    if item.is_file():
                        assert item.stat().st_size < 10000
                        result[str(item.relative_to(workspace))] = digest(item.read_bytes())
            return result
        before_workspace = workspace_observation()
        bootstrap = sys.modules['__main__']
        bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
        bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)] = clean
        try:
            out, err, rc = await run_script_path(str(path), expected_sha256=expected_sha256,
                lifecycle_callback=lifecycle_callback, profile=False, env=clean,
                managed_log_paths=kwargs.get('managed_log_paths'))
        finally:
            bootstrap.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path))
            bootstrap.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path), None)
        self.executions.append({'step_id': step['step_id'], 'operation': step['operation'],
            'execution_capability_id': step['execution_capability_id'], 'attempt_id': path.parent.name,
            'rc': rc, 'stdout': out, 'stderr': err, 'approved_sha256': digest(approved),
            'executed_sha256': expected_sha256, 'approved_code': approved.decode(), 'wrapper': path.read_text(),
            'actual_workspace_before': before_workspace, 'actual_workspace_after': workspace_observation()})
        return out, err, rc

    async def run(self):
        import bio_agents
        from agent_core.plan_tracker import load_plan_state, commit_runtime_evidence_state, load_approved_plan_snapshot
        from agent_core.runtime_evidence import make_runtime_evidence
        runner = self.case._runner_state(self.task)
        runner['workflow_routing_schema_version'] = 2
        runner['messages'] = [HumanMessage(content=self.goal)]
        # Explicit external observation of the newly created scientific stand-in;
        # no ready workspace, Plan, approved action or science result is seeded.
        scope = self.agent._runtime_evidence_scope(SID)
        assert all(scope.values())
        evidence = make_runtime_evidence({'skill_id': SID, 'checked_env': scope['environment_id'], 'status': 'ready',
            'resolved_runtime_paths': {'RESOLVED_JAVA_BIN': str(self.java), 'RESOLVED_SNPEFF_JAR': str(self.jar)}},
            task_id=self.task, skill_scope=scope, observed_at=datetime.now(timezone.utc).isoformat(),
            source_ref='v2-explicit-new-synthetic-external-observation')
        commit_runtime_evidence_state(self.root, self.task, [evidence])
        app = self.agent._app_for_task(self.task, runner)
        config = {'configurable': {'thread_id': self.task}, 'recursion_limit': 150}
        async def pulse():
            while True:
                await asyncio.sleep(.001)
        self.service = asyncio.create_task(pulse())
        incoming = runner
        with patch.object(bio_agents, 'run_script_path', self.execute):
            for _ in range(24):
                async for event in app.astream(incoming, config=config, stream_mode='updates'):
                    self.events.append(event)
                    if 'commit_step' in event:
                        self.commits.append(deepcopy(event['commit_step']))
                        self.state_after_commits.append(deepcopy(load_plan_state(self.root, self.task)))
                snapshot = app.get_state(config)
                if not snapshot.next:
                    break
                assert snapshot.tasks and snapshot.tasks[0].interrupts
                incoming = bio_agents.Command(resume='y')
        self.values = app.get_state(config).values
        self.durable = load_plan_state(self.root, self.task)
        self.terminal = await self.agent._finalize_managed_terminal(self.task, app.get_state(config))
        self.durable = load_plan_state(self.root, self.task)
        if self.scenario == 'generic_blocked':
            declared = self.plan()['steps']
            failed_index = next(index for index, step in enumerate(declared) if step['operation'] == 'build_database')
            assert self.values.get('final_audit_outcome', {}).get('type') != 'task_verified'
            assert self.durable['task_state']['status'] in ('blocked', 'failed')
            assert len(self.commits) == failed_index
            assert [row['step_id'] for row in self.executions] == [step['step_id'] for step in declared[:failed_index + 1]]
            assert [row['rc'] for row in self.executions] == [0] * failed_index + [23]
            assert self.counts['recovery'] == 1
            assert not self.outputs['annotated_vcf'].exists() and not self.outputs['ann_summary'].exists()
            assert all(self.outputs[role].is_file() for role in ('report', 'reproduce_script', 'stage_status'))
            diagnostic = self.terminal['terminal_diagnostic']
            assert diagnostic['origin']['attempt_id'] == self.executions[-1]['attempt_id']
            assert diagnostic['origin']['returncode'] == 23
            assert diagnostic['causal_execution']['attempt_id'] == self.executions[-1]['attempt_id']
            return self.values, self.durable
        assert self.values.get('final_audit_outcome', {}).get('type') == 'task_verified', {
            'planner_outcome': self.values.get('planner_outcome'), 'boundary': self.values.get('managed_run_boundary'),
            'terminal': self.terminal, 'counts': self.counts}
        submitted = self.plan()
        expected_ids = [step['step_id'] for step in submitted['steps']]
        expected_ops = [step['operation'] for step in submitted['steps']]
        assert all(identity != operation for identity, operation in zip(expected_ids, expected_ops))
        assert self.counts['planner'] == self.expected_planner_calls
        assert self.plan_review_inputs, 'The real PlanReview input owner must be reached'
        assert self.durable['task_state']['status'] == 'completed'
        assert [step['step_id'] for step in self.durable['steps']] == expected_ids
        frozen = load_approved_plan_snapshot(self.root, self.task, state=self.durable)
        assert [step['step_id'] for step in frozen['steps']] == expected_ids
        assert [step['operation'] for step in frozen['steps']] == expected_ops
        receipts = self.durable['task_state']['typed_step_commit_receipts']
        assert len(receipts) == len(self.commits) == len(expected_ids)
        execution_ids = ([expected_ids[0]] if self.scenario == 'generic_recovery' else []) + expected_ids
        assert [row['step_id'] for row in self.executions] == execution_ids
        assert [row['step_id'] for row in self.approvals] == execution_ids
        assert len({row['attempt_id'] for row in self.executions}) == len(execution_ids)
        assert [row['rc'] for row in self.executions] == ([7] if self.scenario == 'generic_recovery' else []) + [0] * len(expected_ids)
        for execution in self.executions:
            attempt = self.durable['provenance_state']['attempts'][execution['attempt_id']]
            assert attempt['step_id'] == execution['step_id']
            if execution['rc'] == 0:
                assert attempt['effect_evidence']['step_id'] == execution['step_id']
                assert attempt['effect_evidence']['attempt_id'] == execution['attempt_id']
        assert {receipt['outcome']['payload']['completed_step_id'] for receipt in receipts.values()} == set(expected_ids)
        assert all(path.is_file() and path.stat().st_size > 0 for path in self.outputs.values())
        assert len(self.durable['task_state']['accepted_plan_bindings']) == 2
        stage_lines = self.outputs['stage_status'].read_text().splitlines()
        assert [line.split('\t')[0] for line in stage_lines[2:]] == expected_ids
        mutations = [row for attempt in self.durable['provenance_state']['attempts'].values()
            for row in attempt.get('effect_evidence', {}).get('workspace_mutations', [])]
        assert len(mutations) == 2
        assert {row['operation'] for row in mutations} == {'prepare_database', 'build_database'}
        assert len({row['canonical_resource_id'] for row in mutations}) == 1
        assert all(row['attested'] and row['postcondition_satisfied'] for row in mutations)
        if self.scenario == 'generic_recovery':
            assert self.counts['recovery'] == 1
            assert self.executions[0]['step_id'] == self.executions[1]['step_id']
            assert self.executions[0]['approved_sha256'] != self.executions[1]['approved_sha256']
            directive = next(row['directive'] for row in self.requests if row['stage'] == 'recovery')
            assert directive['target_step_id'] == expected_ids[0]
        else:
            assert self.counts['recovery'] == 0
        if self.scenario == 'advisory_context':
            warnings = [issue for issue in self.durable.get('validation_issues', [])
                if issue.get('code') == 'PLAN_STEP_SKILL_LIMIT_EXCEEDED']
            assert warnings and all(issue.get('severity') == 'warning' and issue.get('disposition') == 'NORMALIZED'
                and issue.get('actual_value') == 4 for issue in warnings), warnings
            assert not self.durable['task_state'].get('planning_failure')
            epoch = self.durable['task_state']['planning_epoch']
            assert epoch['planner_calls_total'] == 1 and epoch.get('candidate_repairs', 0) == 0
            assert not epoch.get('rejected_candidate_comparisons')
            assert len(self.plan_review_inputs) == 1
            request = next(row for row in self.requests if row['stage'] == 'generate' and row['step_id'] == 'probe-731')
            message = next(row['content'] for row in request['messages'] if '<current_focus>' in row['content'])
            focus = json.loads(message.split('<current_focus>', 1)[1].split('</current_focus>', 1)[0])
            assert set(focus['current_step']['skill_ids']) == set(CONTEXT_SKILLS)
            assert len(focus['current_step']['skill_ids']) == 4
            assert focus['current_step']['execution_capability_id'] == GENERIC
        # Reload through production readers and replay actual first/last Commit
        # inputs: neither a migrated approval nor fabricated succeeded state.
        generation = self.durable['state_generation']
        receipt_hash = digest(json.dumps(receipts, sort_keys=True, default=str))
        for index in (0, -1):
            replay = await self.real_commit(deepcopy(self.commit_inputs[index]))
            reloaded = load_plan_state(self.root, self.task)
            assert reloaded['state_generation'] == generation
            assert digest(json.dumps(reloaded['task_state']['typed_step_commit_receipts'], sort_keys=True, default=str)) == receipt_hash
            assert replay['commit_step_outcome'] == self.commits[index]['commit_step_outcome']
            self.receptions.append(('v2_actual_commit_replay', deepcopy(replay)))
        return self.values, self.durable

    async def close(self):
        output = Path(os.environ['BIOMIND_GER_ARTIFACTS'])
        body = json.dumps({'scenario': self.scenario, 'raw_plans': self.raw_plans,
            'plan_review_inputs': self.plan_review_inputs, 'validator_calls': self.validator_calls,
            'identity_lookups': self.lookups, 'step_generations': self.step_generations,
            'path_resolution_receipts': self.path_resolution_receipts,
            'declared_implementations': self.declared_implementations,
            'limits': ['fixed model leaf; no real model reliability claim', 'synthetic algorithm; no scientific E2E',
                'inherited TaskRouter intent_admission=test_fixture and Consultant preflight substitutes',
                'runtime readiness is an explicit observation of new fixture files, not host environment verification',
                'trusted tiny scripts plus existing guard; no claim of arbitrary-syscall sandboxing',
                'reload uses production readers in this process; not an independent restarted graph']},
            ensure_ascii=False, indent=2, default=str).encode()
        (output / (self.mode + '-driver.json.gz')).write_bytes(gzip.compress(body, mtime=0))
        try:
            await super().close()
        finally:
            self.input_patch.stop()


@pytest.mark.parametrize('scenario', ['generic_split', 'renamed_repeated', 'generic_merged', 'generic_recovery', 'generic_blocked', 'advisory_context'])
def test_v2_full_plan_graph(scenario):
    async def exercise():
        case = AuthorityGraph(scenario)
        await case.setup()
        try:
            await case.run()
        finally:
            await case.close()
    asyncio.run(exercise())
