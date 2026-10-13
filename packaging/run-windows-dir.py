#!/usr/bin/env python3
"""Offline electron-builder directory packaging with an explicit cached toolchain."""
import datetime,json,os,pathlib,subprocess,sys
root=pathlib.Path(__file__).resolve().parents[1]
old=root.parent/'BioMind_Studio_S2_S3_Implementation_20261008'
nodebin=old/'candidate/Biomind_v1/integrations/pi_runtime/.runtime/node/bin'
stage=root/'build/windows-input'
evidence=root/'evidence/windows-dir'
evidence.mkdir(parents=True,exist_ok=True)
for name in ['tmp','cache','appdata','userprofile']:(evidence/name).mkdir(exist_ok=True)
for name in ['npm-user.conf','npm-global.conf']:(evidence/name).write_text('')
env={'PATH':str(nodebin)+':/usr/bin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8','TMPDIR':str(evidence/'tmp'),'XDG_CACHE_HOME':str(evidence/'cache'),'APPDATA':str(evidence/'appdata'),'USERPROFILE':str(evidence/'userprofile'),'CSC_IDENTITY_AUTO_DISCOVERY':'false','ELECTRON_BUILDER_CACHE':str(old/'delivery_correction_20261008/assets/builder-cache'),'ELECTRON_CACHE':str(old/'delivery_correction_20261008/assets/electron-43.3.0'),'ELECTRON_BUILDER_7ZIP_PATH':str(old/'delivery_correction_20261008/assets/7zip/bin/7za'),'npm_config_userconfig':str(evidence/'npm-user.conf'),'npm_config_globalconfig':str(evidence/'npm-global.conf'),'npm_config_cache':str(evidence/'cache/npm'),'npm_config_offline':'true','DEBUG':'electron-builder'}
# Empty dependency graph is the real staged application graph. All remote-main
# runtime imports are Node/Electron built-ins or staged CJS/bundled JS files.
pkg=json.loads((stage/'package.json').read_text())
lock={'name':pkg['name'],'version':pkg['version'],'lockfileVersion':3,'requires':True,'packages':{'':{'name':pkg['name'],'version':pkg['version'],'license':pkg['license']}}}
(stage/'package-lock.json').write_text(json.dumps(lock,indent=2)+'\n')
cmd=[str(nodebin/'npm'),'list','-a','--include','prod','--include','optional','--omit','dev','--json','--long','--silent','--loglevel=error']
p=subprocess.run(cmd,cwd=stage,env=env,capture_output=True,text=True)
(evidence/'npm-list.stdout.json').write_text(p.stdout)
(evidence/'npm-list.stderr.log').write_text(p.stderr)
if p.returncode:raise RuntimeError(f'npm list failed {p.returncode}')
tree=json.loads(p.stdout)
if tree.get('dependencies'):raise RuntimeError('Unexpected runtime dependencies in staged app')
cmd=[str(nodebin/'node'),str(old/'candidate/pi-studio/node_modules/electron-builder/cli.js'),'--projectDir',str(stage),'--config',str(root/'packaging/builder-config.json'),'--win','--dir','--x64','--publish','never']
(evidence/'command.json').write_text(json.dumps({'argv':cmd,'cwd':str(stage),'environment':env,'executed':True,'timeUTC':datetime.datetime.now(datetime.timezone.utc).isoformat()},indent=2)+'\n')
with (evidence/'builder.log').open('w') as log:p=subprocess.run(cmd,cwd=stage,env=env,stdout=log,stderr=subprocess.STDOUT)
(evidence/'exit.json').write_text(json.dumps({'exitCode':p.returncode,'finishedUTC':datetime.datetime.now(datetime.timezone.utc).isoformat()})+'\n')
print(json.dumps({'exitCode':p.returncode,'log':str(evidence/'builder.log')}))
sys.exit(p.returncode)
