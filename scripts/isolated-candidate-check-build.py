#!/usr/bin/env python3
"""Load the copied backend and run Next.prepare without opening a listener."""
import argparse
import json
import os
from pathlib import Path
import subprocess

CODE = r'''
import {createRequire} from 'node:module';
import path from 'node:path';
const root=process.env.BIOMIND_STUDIO_ROOT, require=createRequire(path.join(root,'package.json'));
const checks={}; let app;
try {
  const next=require('next'); checks.nextImport='PASS';
  for(const name of ['api/agent/profile','api/agent/new','api/plugins']){
    require(path.join(root,'.next-pkg/server/app',name,'route.js'));
    checks[name]='PASS';
  }
  app=next({dev:false,dir:root,hostname:'127.0.0.1',port:0});
  await app.prepare(); checks.nextPrepare='PASS';
  await app.close();
  console.log(JSON.stringify({status:'PASS',checks,noListenerOpened:true,actualHttpIdentityVerified:false}));
  process.exit(0);
} catch(error) {
  try{await app?.close();}catch{}
  console.log(JSON.stringify({status:'FAIL',checks,noListenerOpened:true,code:error.code??error.name,
    message:String(error.message).slice(0,1000),actualHttpIdentityVerified:false}));
  process.exit(1);
}
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',type=Path,required=True)
    args=parser.parse_args()
    root=args.target.resolve(strict=True)
    receipt=json.loads((root/'PREPARED.json').read_text())
    assert receipt['root']==str(root) and receipt['productionChanged'] is False
    config=json.loads((root/'private/service.json').read_text())
    # Nonsecret service environment only; not runtime-environment.json or keys.
    env={name:os.environ[name] for name in ('HOME','USER','LOGNAME','LANG','LC_ALL','PATH') if name in os.environ}
    env.update(config['environment'])
    env.update(PI_STUDIO_AGENT_PROFILE='biomind',BIOMIND_SERVER_ID=config['serverId'])
    try:
        result=subprocess.run([config['command'][0],'--input-type=module','-e',CODE],
            cwd=config['cwd'],env=env,capture_output=True,text=True,timeout=90)
        payload=next((json.loads(line) for line in reversed(result.stdout.splitlines()) if line.startswith('{')),None)
        if payload is None:payload={'status':'FAIL','code':'NO_BUILD_CHECK_RESULT'}
        payload['exitCode']=result.returncode
        (root/'evidence/backend-prepare.stderr').write_text(result.stderr)
    except subprocess.TimeoutExpired:
        payload={'status':'FAIL','code':'BACKEND_PREPARE_TIMEOUT','noListenerOpened':True}
    output=root/'evidence/backend-prepare.json'
    output.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n');output.chmod(0o600)
    print(json.dumps(payload,ensure_ascii=False))
    return 0 if payload['status']=='PASS' else 2


if __name__=='__main__':raise SystemExit(main())
