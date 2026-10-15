"""Execute generated fixed CLI entries; verify user-facing dispatch compatibility."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from agent_core import pi_plugin_adoption as adoption

class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.release = self.root / 'release'
        (self.release / 'agent_core').mkdir(parents=True)
        (self.release / 'integrations/pi_runtime/dist').mkdir(parents=True)
        (self.release / 'agent_core/pi_plugin_guard.py').write_text('# legacy-adopted')
        (self.release / 'RELEASE.json').write_text(json.dumps({'releaseId': 'fixed', 'python': {'path': sys.executable}, 'node': {'path': '/fixed/node'}}))
        (self.release / 'integrations/pi_runtime/dist/build-manifest.json').write_text('{"buildId":"fixed"}')
        (self.release / 'bio_agents.py').write_text('import json,os,sys\nprint(json.dumps({"args":sys.argv[1:],"node":os.getenv("BIOMIND_PI_NODE"),"python":os.getenv("BIOMIND_PI_PYTHON")}))\nsys.exit(int(os.getenv("TEST_EXIT","0")))\n')
        self.agent = self.root / 'agent'; self.agent.mkdir()
        self.config = self.root / 'runtime.json'; self.config.write_text(json.dumps({'agentDir': str(self.agent), 'installRoot': str(self.release)}))
        self.model = self.root / 'cli-model-selection.json'; self.model.write_text('{}')
        self.entry = self.root / 'biomind'
        data, self.bindings = adoption.replacement({'replacement': {'release': str(self.release), 'runtimeConfig': str(self.config), 'modelRecord': str(self.model)}}, {'agentDir': str(self.agent)})
        self.entry.write_bytes(data); self.entry.chmod(0o700)
    def invoke(self, args, **env):
        environment = {k:v for k,v in os.environ.items() if k != 'BIOMIND_PI_MODEL_RECORD'}; environment.update(env)
        return subprocess.run([str(self.entry), *args], env=environment, text=True, capture_output=True)
    def test_daily_model_and_mutable_selection_are_preserved(self):
        for selection in ['{}', '{"modelId":"other"}']:
            self.model.write_text(selection)
            result = json.loads(self.invoke(['--prompt','literal $() `quotes`']).stdout)
            self.assertEqual(result['args'], ['--prompt','literal $() `quotes`','--pi-config',str(self.config),'--pi-node','/fixed/node','--pi-model-record',str(self.model)])
            self.assertEqual(result['node'], '/fixed/node'); self.assertEqual(result['python'], sys.executable)
        self.assertNotIn(str(self.model), [p['path'] for p in self.bindings])
    def test_custom_config_and_interpreter_forwarded_once_both_forms(self):
        for args in [['--pi-config','/custom.json','--pi-node','/custom/node'], ['--pi-config=/custom.json','--pi-node=/custom/node']]:
            self.assertEqual(json.loads(self.invoke(args).stdout)['args'], args)
    def test_package_and_integration_do_not_inject_daily_model(self):
        for prefix in [[], ['--engine','pi'], ['--engine=pi']]:
            for command in ['plugins','install','remove','uninstall','list','config','update','doctor','integrate']:
                args = prefix+[command,'--json']; observed=json.loads(self.invoke(args).stdout)['args']
                self.assertEqual(observed[:len(args)],args)
                self.assertNotIn('--pi-model-record',observed)
                self.assertEqual('--pi-config' in observed, command not in ['doctor','integrate'])
    def test_explicit_and_environment_model_selection_win(self):
        for args, env in [(['--pi-model-record','/explicit.json'],{}), (['--pi-model-record=/explicit.json'],{}), ([],{'BIOMIND_PI_MODEL_RECORD':'/env.json'})]:
            observed=json.loads(self.invoke(args,**env).stdout)['args'];self.assertNotIn(str(self.model),observed)
    def test_nonzero_exit_and_release_identity_fail_closed(self):
        self.assertEqual(self.invoke([],TEST_EXIT='23').returncode,23)
        (self.release/'integrations/pi_runtime/dist/build-manifest.json').write_text('{"buildId":"changed"}')
        result=self.invoke([]);self.assertEqual(result.returncode,2);self.assertEqual(result.stdout,'')
    def test_missing_daily_model_keeps_original_optional_behavior(self):
        self.model.unlink();self.assertNotIn('--pi-model-record',json.loads(self.invoke([]).stdout)['args'])

if __name__ == '__main__': unittest.main()
