"""View contract only; kernel integration is in test_protection_repair.py."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode=True
RUNTIME=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(RUNTIME),str(RUNTIME.parents[1])]
from domain_adapter.service import DomainService


def samples():
    common={'status':'stage_blocked','execution_started':False,'exit_code':None,
            'error':'FORMAL_READ_PROTECTION_UNAVAILABLE','phase':'protected_spawn_preflight',
            'host_boundary':'before_launch_delegation','run_ref':'attempt-fixture',
            'receipt_ref':'/synthetic/receipts/observation_result.json'}
    rows = [
        {**common,'reason':'hardlink_in_writable_scope','count':8,'unique_inode_count':4},
        {**common,'reason':'bubblewrap_missing_or_platform_unsupported'},
        {**common,'reason':'bubblewrap_namespace_unavailable','backend':'bubblewrap',
         'errno':13,'probe_exit_code':None,'probe_stderr':'PermissionError: [Errno 13]'},
        {'status':'stage_blocked','error':'FORMAL_READ_PROTECTION_UNAVAILABLE'},
        {**common,'reason':'hardlink_in_writable_scope','count':0,'probe_exit_code':0},
        {**common,'reason':'x'*20000,'detail':'token=fixture-private-value '+ 'y'*20000,
         'path':'/synthetic/.env.private','path_samples':['/synthetic/a','/synthetic/.ssh/key','/synthetic/b']*2000,
         'traceback':'PRIVATE_TRACE','env':{'SECRET':'PRIVATE_ENV'},'stderr':'z'*20000},
        {**common,'reason':'writable_workdir_outside_project','phase':'access_projection',
         'current_project_root':'/synthetic/LOOP-020','requested_workdir':'/synthetic/TAD-005',
         'instruction':'在系统终端使用 biomind --project-dir <目标项目目录>；/new 和聊天确认不会切换项目。'},
    ]
    # Optional read-only historical receipt replay; never edit the old session.
    if os.environ.get('BIOMIND_DIAGNOSTIC_RECEIPT'):
        rows.append(json.loads(Path(os.environ['BIOMIND_DIAGNOSTIC_RECEIPT']).read_text()))
    return rows


class ProtectionDiagnostics(unittest.TestCase):
    def test_false_null_missing_zero_and_bounds(self):
        for raw in samples():
            before=copy.deepcopy(raw);view=DomainService._run_view(raw)
            self.assertEqual(raw,before)
            for key in ('execution_started','exit_code','count','probe_exit_code'):
                if key in raw:self.assertEqual(view[key],raw[key])
                else:self.assertNotIn(key,view)
            self.assertNotIn('traceback',view);self.assertNotIn('env',view)
            self.assertNotIn('fixture-private-value',json.dumps(view))
            self.assertLess(len(json.dumps(view)),9000)
        long=DomainService._run_view(samples()[5])
        self.assertNotIn('path',long);self.assertEqual(long['path_samples'],['/synthetic/a','/synthetic/b'])

    def test_projection_failure_uses_the_same_bounded_view(self):
        raw=samples()[5];view=DomainService._run_view({'status':'stage_blocked','capability_missing':raw})
        self.assertNotIn('env',view['capability_missing'])
        self.assertLess(len(json.dumps(view)),9500)
        self.assertNotIn('execution_started',view)

    def test_malformed_types_are_not_launch_facts(self):
        view=DomainService._run_view({'execution_started':'false','exit_code':False,'count':True,'reason':{}})
        for key in ('execution_started','exit_code','count','reason'):self.assertNotIn(key,view)

    def test_project_paths_survive_diagnostics_without_exposing_private_paths(self):
        view=DomainService._run_view(samples()[6])
        for key in ('current_project_root','requested_workdir','instruction'):
            self.assertEqual(view[key],samples()[6][key])
        private=DomainService._run_view({'current_project_root':'/synthetic/.ssh','requested_workdir':'/synthetic/.env.private'})
        self.assertNotIn('current_project_root',private);self.assertNotIn('requested_workdir',private)


if __name__=='__main__':
    if sys.argv[1:]==['--fixture-json']:
        print(json.dumps([{'raw':raw,'view':DomainService._run_view(raw)} for raw in samples()]))
    else:unittest.main()
