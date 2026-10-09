import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';
import {DomainClient} from '../domain-client.js';

test('published host binding rejects another business tree and service before any process starts',()=>{
  const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../../../..');
  const other=fs.mkdtempSync(path.join(tmpdir(),'biomind-release-mismatch-'));
  const foreignService=path.join(other,'service.py');fs.writeFileSync(foreignService,'raise RuntimeError("must never run")\n');
  const previous=process.env.BIOMIND_PI_RELEASE_ROOT;
  try{
    process.env.BIOMIND_PI_RELEASE_ROOT=root;
    const options={cwd:other,python:'/synthetic/python-must-not-spawn',sessionId:'isolated-release-test'};
    assert.throws(()=>new DomainClient({...options,installRoot:other}),/BIOMIND_BUILD_INSTALL_ROOT_MISMATCH/);
    assert.throws(()=>new DomainClient({...options,installRoot:root,servicePath:foreignService}),/BIOMIND_BUILD_DOMAIN_SOURCE_MISMATCH/);
    process.env.BIOMIND_PI_RELEASE_ROOT=other;
    assert.throws(()=>new DomainClient({...options,installRoot:root}),/BIOMIND_BUILD_INSTALL_ROOT_MISMATCH/);
  }finally{if(previous===undefined)delete process.env.BIOMIND_PI_RELEASE_ROOT;else process.env.BIOMIND_PI_RELEASE_ROOT=previous;fs.rmSync(other,{recursive:true,force:true});}
});
