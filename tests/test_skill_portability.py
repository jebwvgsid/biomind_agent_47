"""Portable Skills regression: isolated fixtures; no models, installs, or scientific jobs."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class SkillPortabilityTests(unittest.TestCase):
    def test_standard_metadata_resources_and_no_private_loader_contract(self):
        names = []
        for md in sorted((ROOT / 'skills').rglob('SKILL.md')):
            text = md.read_text()
            meta = yaml.safe_load(text.split('---', 2)[1])
            self.assertEqual(meta['name'], md.parent.name)
            self.assertRegex(meta['name'], r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
            self.assertTrue(meta['description'])
            self.assertNotIn('runtime', meta)
            self.assertTrue(all(isinstance(k, str) and isinstance(v, str) for k,v in meta.get('metadata',{}).items()))
            self.assertNotIn('biomind-domain', text)
            self.assertTrue((md.parent / 'references/dependencies.md').is_file())
            for rel in re.findall(r'\$\{SKILL_DIR\}/([^\s"`<>]+)', text):
                self.assertTrue((md.parent / rel).exists(), (md, rel))
            names.append(meta['name'])
        self.assertEqual(len(names),len(set(names)))
        self.assertFalse(list((ROOT/'skills').rglob('biomind-domain.json')))

    def test_referenced_scripts_have_no_private_imports_or_machine_paths(self):
        for p in (ROOT/'skills').rglob('*'):
            if p.suffix not in {'.py','.sh','.R','.md','.yaml'} or not p.is_file(): continue
            text=p.read_text()
            self.assertNotRegex(text,r'/public/home/|/home/liusiqi/')
            self.assertNotRegex(text,r'\b(?:list_skills|read_skill|select_environment|install_tool)\b')
            if p.suffix=='.py':
                tree=ast.parse(text, feature_version=(3, 9))
                for node in ast.walk(tree):
                    if isinstance(node,ast.ImportFrom): self.assertFalse((node.module or '').startswith(('agent_core','execution')),(p,node.module))
                    elif isinstance(node,ast.Import):
                        self.assertFalse(any(x.name.startswith(('agent_core','execution')) for x in node.names),p)

    def test_external_readonly_wgs_and_scrna_wrapper_from_project(self):
        with tempfile.TemporaryDirectory(prefix='技能 包 portable ') as td:
            base=Path(td); package=base/'只读 methods'; project=base/'分析 项目';project.mkdir()
            shutil.copytree(ROOT/'skills',package,ignore=shutil.ignore_patterns('.*','__pycache__','*.pyc'))
            for p in package.rglob('*'):
                p.chmod(0o555 if p.is_dir() else 0o444)
            package.chmod(0o555)
            hashes={str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest() for p in package.rglob('*') if p.is_file()}
            (project/'reads 空格.fq').write_text('@r1\nACGT\n+\nIIII\n')
            (project/'reference.fa').write_text('>chr1\nACGT\n')
            env={'PATH':'/usr/bin:/bin','HOME':str(base/'home'),'PYTHONDONTWRITEBYTECODE':'1'}
            script=package/'genomics/wgs-short-read-mapping/scripts/inspect_wgs_inputs.py'
            args=[sys.executable,'-B',str(script),'--fastq','sample','reads 空格.fq','--reference','reference','reference.fa','--evidence-dir','检查结果']
            result=subprocess.run(args,cwd=project,env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            data=json.loads(result.stdout);self.assertTrue(data['all_checks_passed']);self.assertFalse(data['full_file_validated'])
            self.assertTrue((project/'检查结果/summary.json').is_file())
            # Existing output is refused, never overwritten.
            repeat=subprocess.run(args,cwd=project,env=env,capture_output=True,text=True,timeout=15)
            self.assertNotEqual(repeat.returncode,0)
            # Validate sibling resource resolution with a harmless argv-recording Rscript.
            bindir=base/'bin';bindir.mkdir();fake=bindir/'Rscript'
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$ARG_LOG"\ntest -f "$1"\n');fake.chmod(0o755)
            (project/'manifest.yaml').write_text('output_dir: outputs\n')
            env.update(PATH=str(bindir)+':/usr/bin:/bin',ARG_LOG=str(project/'args.log'))
            wrapper=package/'scrna/workflows/ingest-qc/scripts/run_scrna_ingest_qc.sh'
            result=subprocess.run(['/bin/bash',str(wrapper),'manifest.yaml'],cwd=project,env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            called=(project/'args.log').read_text().splitlines()
            self.assertEqual(sum(x.endswith('.R') for x in called),3)
            self.assertTrue(all(str(package) in x for x in called if x.endswith('.R')))
            self.assertEqual(hashes,{str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest() for p in package.rglob('*') if p.is_file()})
            for p in package.rglob('*'):
                p.chmod(0o755 if p.is_dir() else 0o644)
            package.chmod(0o755)

    def test_registry_alias_reuses_synthetic_record_without_runtime_sidecar(self):
        from agent_core.skill_runtime_metadata import SkillRuntimeRegistry
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'scrna/preprocess/scrna-preprocess-basic').mkdir(parents=True)
            (root/'scrna/preprocess/scrna-preprocess-basic/SKILL.md').write_text('---\nname: scrna-preprocess-basic\ndescription: synthetic\n---\n')
            record={'env_name':'synthetic_existing','prefix':'/synthetic/environment','healthcheck':'MUST_NOT_EXECUTE'}
            (root/'.skill_runtime_registry.json').write_text(json.dumps({'skills':{'scrna/preprocess/basic':record}}))
            value=SkillRuntimeRegistry(root).get_runtime_registration('scrna-preprocess-basic')
            self.assertEqual(value,record)

    def test_actual_domain_select_old_and_native_ids_valid_invalid_unknown(self):
        from unittest.mock import patch
        sys.path.insert(0,str(ROOT/'integrations/pi_runtime'))
        from domain_adapter.service import DomainService
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); skills=root/'skills'; leaf=skills/'scrna/preprocess/scrna-preprocess-basic'
            leaf.mkdir(parents=True); (leaf/'SKILL.md').write_text('---\nname: scrna-preprocess-basic\ndescription: fixture\n---\n')
            prefix=root/'existing'; (prefix/'bin').mkdir(parents=True)
            (prefix/'bin/Rscript').write_text('MUST_NOT_EXECUTE'); (prefix/'bin/Rscript').chmod(0o700)
            registered={'env':'existing','status':'success','packages':['Seurat'],'prefix':str(prefix)}
            skill_registry=skills/'.skill_runtime_registry.json'
            skill_registry.write_text(json.dumps({'skills':{'scrna/preprocess/basic':registered}}))
            registry=root/'software.json'; inventory=root/'environments.txt'; inventory.write_text(str(prefix)+'\n')
            def save(status):
                registry.write_text(json.dumps({'environments':{'existing':{'status':status,'packages':['Seurat'],'prefix':str(prefix)}},'tools':{}}))
            save('success')
            service=DomainService(cwd=root,data_root=root/'data',install_root=ROOT,session_id='portable-registry',
                registry_path=registry,skills_root=skills,environment_inventory=inventory,preferences_path=root/'preferences.md')
            with patch('subprocess.run',side_effect=AssertionError('no healthcheck or installer allowed')):
                for identity in ('scrna/preprocess/basic','scrna-preprocess-basic'):
                    result=service.select_environment({'packages':['Seurat']},skill_id=identity)
                    self.assertEqual(result['availability'],'AVAILABLE',result)
                    self.assertEqual(result['selected_candidate']['prefix'],str(prefix))
                save('broken')
                for identity in ('scrna/preprocess/basic','scrna-preprocess-basic'):
                    self.assertEqual(service.select_environment({'packages':['Seurat']},skill_id=identity)['availability'],'UNKNOWN')
                save('success')
                self.assertEqual(service.select_environment({'packages':['not_registered']},skill_id='unknown-method')['availability'],'UNKNOWN')
            self.assertEqual(json.loads(skill_registry.read_text())['skills']['scrna/preprocess/basic'],registered)
            self.assertFalse(service.active_jobs())

    def test_renderer_preserves_explicit_ploidy_and_refuses_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'samples.tsv').write_text('sample\tinput.bam\n')
            (root/'intervals.tsv').write_text('interval_id\tgatk_interval\nchr1\tchr1\n')
            script=ROOT/'skills/genomics/wgs-variant-calling/scripts/render_gatk_wgs_scripts.py'
            argv=[sys.executable,'-B',str(script),'--sample-bam-list','samples.tsv','--intervals-tsv','intervals.tsv',
                  '--ref','reference.fa','--outdir','generated']
            result=subprocess.run(argv,cwd=root,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse((root/'generated').exists())
            result=subprocess.run(argv+['--sample-ploidy','4','--task-label','synthetic'],cwd=root,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            script_text=(root/'generated/step1.HaplotypeCaller.sh').read_text()
            self.assertIn('HaplotypeCaller --sample-ploidy 4',script_text)
            self.assertFalse((root/'generated/gvcf').exists())
            self.assertEqual(json.loads((root/'generated/manifest.json').read_text())['task_id'],'synthetic')

    def test_methylation_help_without_site_packages(self):
        with tempfile.TemporaryDirectory(prefix='甲基化 帮助 ') as td:
            root=Path(td); script=root/'plot_methylation.py'
            script.write_bytes((ROOT/'skills/epigenomics/methylation/gene-plot/scripts/plot_methylation.py').read_bytes())
            script.chmod(0o444)
            result=subprocess.run([sys.executable,'-S','-B',str(script),'--help'],cwd=root,
                capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('--nbins-body',result.stdout)
            self.assertEqual(list(root.iterdir()),[script])


if __name__=='__main__':unittest.main()
