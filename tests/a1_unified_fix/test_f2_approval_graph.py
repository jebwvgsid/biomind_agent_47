"""Additional F2 carrier coverage through real approval and execution.

Only the fixed external reply and synthetic diagnostic differ from F6. Legacy
adapters retain their production metadata; their actual approved-file runner is
non-login. No candidate argv is changed by this test after generation/approval.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import shlex
import shutil
import sys
from types import MappingProxyType
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from tests.a1_unified_fix import test_f6_graph as common


class CarrierGraph(common.F6Graph):
    def __init__(self, carrier):
        super().__init__('false_diagnostic')
        self.carrier = carrier

    async def setup(self):
        # Declare the external test-process context before constructing the
        # agent, handoff, Plan or approval. Candidates declare no env bindings.
        self.execution_environment = MappingProxyType({
            'PATH': str(Path(common.PYTHON).parent) + ':/usr/bin:/bin',
            'LC_ALL': 'C.UTF-8', 'PYTHONNOUSERSITE': '1'})
        selected = common.SID if self.carrier == 'native_code_python' else 'genomics/wgs-short-read-mapping'
        self.skill_patch = patch.object(common, 'SID', selected)
        self.skill_patch.start()
        await super().setup()
        (self.runtime_dir / 'envs' / 'bio_wgs').mkdir(exist_ok=True)

    def plan(self):
        if self.carrier == 'native_code_python':
            return super().plan()
        step = {'step_id': common.OPS[0], 'title': 'Observe actual synthetic input compatibility',
            'action': 'execute', 'purpose': 'validation', 'tool': common.SID, 'skill_ids': [common.SID],
            'execution_capability_id': 'genomics.wgs.short.read.mapping.inspect', 'operation': 'inspect',
            'inputs': ['input.variant_vcf', 'input.reference_fasta', 'input.annotation_gtf'], 'outputs': [],
            'dependencies': [], 'batch_id': '', 'workflow_stage_ids': [], 'risk_level': 'low',
            'success_criteria': ['Record the actual negative diagnostic without downstream scientific inference.'],
            'produces_observations': [{'identity': 'validation_observation',
                                      'evidence_contract': {'required_fields': ['value']}}]}
        return {'outcome_type': 'SUBMIT_PLAN_FOR_REVIEW', 'proposed_artifacts': [], 'steps': [step],
            'validation_checklist': [{'check_id': 'actual-diagnostic', 'description': 'Actual negative diagnostic.'}],
            'metadata': {'schema_version': 2, 'planner_contract': 'structured_plan_v2'}}

    async def provider(self, messages, *, stage, **kwargs):
        if stage != 'generate':
            return await super().provider(messages, stage=stage, **kwargs)
        self.counts[stage] += 1
        self.requests.append({'stage': stage, 'ordinal': self.counts[stage], 'messages': [
            {'type': m.type, 'name': getattr(m, 'name', None), 'content': m.content} for m in messages]})
        template = json.loads(next(m.content for m in messages
                                   if getattr(m, 'name', '') == 'approved_effect_emission_template'))
        protocol = next(m.content for m in messages
                        if getattr(m, 'name', '') == 'current_generate_output_protocol')
        expected = 'native_action_json' if self.carrier == 'native_code_python' else 'script_block_v1'
        assert expected in protocol, (self.carrier, protocol)
        identity = template['declarations']['produces_observations'][0]['identity']
        assert all(str(p) in str(messages) for p in [self.vcf, self.reference, self.annotation])
        source = template['marker'] + '\nfrom pathlib import Path\nimport sys\n'
        source += 'v={x.split("\\t")[0] for x in Path(' + repr(str(self.vcf)) + ').read_text().splitlines() if x and not x.startswith("#")}\n'
        source += 'r={x[1:].split()[0] for x in Path(' + repr(str(self.reference)) + ').read_text().splitlines() if x.startswith(">")}\n'
        source += 'g={x.split("\\t")[0] for x in Path(' + repr(str(self.annotation)) + ').read_text().splitlines() if x and not x.startswith("#")}\n'
        source += 'observed=bool(v) and v.issubset(r) and bool(v & g)\nprint("ACTUAL_INTERPRETER="+sys.executable)\n'
        source += "print(emit_effect_evidence({'produces_observations':{" + repr(identity) + ":{'value':observed}}}))\n"
        if self.carrier == 'native_code_python':
            candidate = {'type': 'execute', 'operation': common.OPS[0], 'language': 'python', 'code': source}
            assert 'argv' not in candidate
            response = json.dumps(candidate)
        elif self.carrier == 'legacy_python':
            response = '<python>\n' + source + '</python>'
        else:
            response = '<shell>\nset -euo pipefail\n' + shlex.quote(common.PYTHON) + " -I -S - <<'PY'\n" + source + 'PY\n</shell>'
        self.provider_candidates.append({'carrier': self.carrier, 'public_protocol': protocol,
                                         'raw': response, 'unexpanded_source_sha256': common.digest(source)})
        return AIMessage(content=response, response_metadata={'finish_reason': 'stop'})

    async def execute(self, path, *, expected_sha256, lifecycle_callback=None, env=None, **kwargs):
        from execution.executor import run_script_path
        path = Path(path)
        assert path.is_relative_to(self.root / 'task_outputs' / self.task / 'attempts')
        candidates = [p for p in path.parent.glob('approved.*') if p.suffix in ['.py', '.sh']]
        assert len(candidates) == 1, candidates
        approved_path = candidates[0]
        approved = approved_path.read_bytes()
        wrapper = path.read_text()
        assert common.digest(path.read_bytes()) == expected_sha256
        assert common.digest(approved) == self.approvals[-1]['sha256']
        # The actual executor invokes approved.py with its known interpreter or
        # approved.sh by non-login bash. Inert legacy adapter argv is not run.
        assert str(approved_path) in wrapper and ' -lc ' not in wrapper and ' --login ' not in wrapper
        clean = dict(self.execution_environment)
        assert not {'BASH_ENV', 'ENV', 'HOME', 'PYTHONPATH'} & set(clean)
        boot = sys.modules['__main__']
        boot.OFFLINE_ALLOWED_SCRIPT_PATHS.add(str(path))
        boot.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS[str(path)] = clean
        try:
            out, err, rc = await run_script_path(str(path), expected_sha256=expected_sha256,
                lifecycle_callback=lifecycle_callback, profile=False, env=clean,
                managed_log_paths=kwargs.get('managed_log_paths'))
        finally:
            boot.OFFLINE_ALLOWED_SCRIPT_PATHS.discard(str(path))
            boot.OFFLINE_ALLOWED_SCRIPT_ENVIRONMENTS.pop(str(path), None)
        self.executions.append({'operation': self.current_operation(), 'attempt_id': path.parent.name,
            'rc': rc, 'stdout': out, 'stderr': err, 'approved_sha256': common.digest(approved),
            'executed_sha256': expected_sha256, 'approved_code': approved.decode(), 'wrapper': wrapper,
            'actual_workspace_before': {}, 'actual_workspace_after': {}, 'approved_path': str(approved_path),
            'declared_environment': clean})
        return out, err, rc

    async def run(self):
        result = await super().run()
        assert self.counts['generate'] == self.counts['reviewer'] == 1
        assert len(self.approvals) == 1 and len(self.commits) == 1
        assert 'ACTUAL_INTERPRETER=' + common.PYTHON in self.executions[0]['stdout']
        assert 'def emit_effect_evidence' in self.executions[0]['approved_code']
        common.save('carrier-' + self.carrier, {'carrier': self.carrier, 'counts': self.counts,
            'candidate': self.provider_candidates, 'execution': self.executions,
            'commit': self.commits, 'final_audit': self.values['final_audit_outcome'],
            'test_process_environment_declared_before_graph': dict(self.execution_environment),
            'candidate_environment': 'No provider env property or binding is declared; only the predeclared isolated external process context is supplied.',
            'scope': 'One diagnostic operation with formal handoff/Plan/approval/helper/executor/effect/gate/Commit; no physical report requested.',
            'legacy_execution_policy': 'Production executes approved script file with non-login runner; adapter argv remains unmodified metadata.'})
        return result

    async def close(self):
        import os
        output = Path(os.environ['BIOMIND_GER_ARTIFACTS']) / ('f6-preserved-' + self.mode)
        taskroot = self.root / 'task_outputs' / self.task
        for source in taskroot.glob('attempts/*/approved.py'):
            target = output / source.relative_to(taskroot)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        try:
            await super().close()
        finally:
            self.skill_patch.stop()


@pytest.mark.parametrize('carrier', ['native_code_python', 'legacy_python', 'legacy_bash'])
def test_F2_carrier_formal_approval_execution_commit(carrier):
    async def scenario():
        fixture = CarrierGraph(carrier)
        await fixture.setup()
        try:
            await fixture.run()
        finally:
            await fixture.close()
    asyncio.run(scenario())
