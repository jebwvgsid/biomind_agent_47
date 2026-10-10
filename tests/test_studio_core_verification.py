import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('verify_core', Path(__file__).resolve().parents[1] / 'scripts/verify-studio-core.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class IdentityVerification(unittest.TestCase):
    def setUp(self):
        self.args = [{'releaseId': 'new', 'sourceCommit': 'new-commit'},
                     {'serverId': 'candidate', 'environment': {'BIOMIND_ENTRYPOINT': '/release/new/bio_agents.py'}},
                     {'agentDir': '/external/agent'}, {'status': 'ready', 'instanceId': 'instance'},
                     {'status': 'ready', 'instanceId': 'instance', 'serverId': 'candidate'},
                     {'profile': 'biomind', 'serverId': 'candidate'},
                     {'schema': 'BioMindPluginsV1', 'status': 'ok', 'serverId': 'candidate',
                      'context': {'buildId': 'new', 'sourceCommit': 'new-commit', 'releaseRoot': '/release/new',
                                  'piVersion': '0.87.1', 'agentDir': '/external/agent'}}]

    def test_matching_real_protocol_context(self):
        reply = verify.validate(*self.args)
        self.assertEqual(reply['result'], 'PASS')
        self.assertEqual(reply['ownerMainExecution'], 'NOT_RUN')

    def test_old_service_old_core_missing_context_or_wrong_agent_never_passes(self):
        for change in ({'buildId': 'old'}, {'sourceCommit': 'old'}, {'releaseRoot': '/S2'}, {'piVersion': '0.84.2'}, {'agentDir': '/other'}):
            args = copy.deepcopy(self.args); args[-1]['context'].update(change)
            self.assertEqual(verify.validate(*args)['result'], 'FAIL')
        self.args[-1].pop('context')
        self.assertEqual(verify.validate(*self.args)['result'], 'FAIL')

    def test_slow_start_or_replaced_instance_never_passes(self):
        for change in ({'status': 'starting'}, {'instanceId': 'other'}, {'serverId': 'old-service'}):
            args = copy.deepcopy(self.args); args[4].update(change)
            self.assertEqual(verify.validate(*args)['result'], 'FAIL')


if __name__ == '__main__': unittest.main()
