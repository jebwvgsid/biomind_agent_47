"""Real startup protocol boundaries on disposable state, with no provider calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNTIME=Path(__file__).resolve().parents[1]
REPO=RUNTIME.parents[1]


class StartupTests(unittest.TestCase):
    def run_service(self,cwd=None,install_root=None,frames=None):
        with tempfile.TemporaryDirectory(prefix='biomind-startup-protocol-') as directory:
            root=Path(directory)
            env={'HOME':str(Path.home()),'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin',
                 'LC_ALL':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
            args=[sys.executable,'-B','-u',str(RUNTIME/'python_service.py'),
                  '--cwd',str(cwd or root),'--data-root',str(root/'state'),
                  '--install-root',str(install_root or REPO),'--session-id','isolated-startup',
                  '--registry-path',str(root/'missing-registry.json'),'--execution-profile','native']
            frames=[{'id':'hello-中文','method':'hello','params':{}}] if frames is None else frames
            process=subprocess.run(args,input=''.join(json.dumps(frame,ensure_ascii=False)+'\n' for frame in frames),
                                   cwd=root,env=env,capture_output=True,text=True,timeout=10)
            return process,[json.loads(line) for line in process.stdout.splitlines()]

    def test_home_rejection_is_correlated_structured_error(self):
        process,frames=self.run_service(cwd=Path.home())
        self.assertEqual(process.returncode,1)
        self.assertEqual(len(frames),1)
        self.assertEqual(frames[0]['id'],'hello-中文')
        self.assertEqual(frames[0]['error']['code'],'PROJECT_DIRECTORY_INVALID')
        self.assertEqual(frames[0]['error']['stage'],'startup')
        self.assertIn('Choose a project directory',frames[0]['error']['message'])

    def test_initial_read_does_not_swallow_pipelined_frames(self):
        process,frames=self.run_service(frames=[{'id':'hello','method':'hello','params':{}},
                                               {'id':'next','method':'session.lifecycle','params':{}}])
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertEqual({frame['id'] for frame in frames},{'hello','next'})
        self.assertTrue(all('result' in frame for frame in frames))

    def test_import_failure_before_service_is_still_structured(self):
        with tempfile.TemporaryDirectory(prefix='biomind-empty-install-') as directory:
            process,frames=self.run_service(install_root=directory)
        self.assertEqual(process.returncode,1)
        self.assertEqual(len(frames),1)
        self.assertEqual(frames[0]['error']['code'],'DOMAIN_INITIALIZATION_FAILED')
        self.assertEqual(frames[0]['error']['stage'],'startup')
        self.assertIn('ModuleNotFoundError',frames[0]['error']['message'])

    def test_input_eof_without_hello_exits_without_initializing(self):
        process,frames=self.run_service(cwd=Path.home(),frames=[])
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertEqual(frames,[])
        self.assertEqual(process.stderr,'')


if __name__=='__main__':unittest.main()
