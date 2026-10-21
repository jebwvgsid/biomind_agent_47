"use strict";
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),old=path.resolve(root,'../BioMind_Studio_S2_S3_Implementation_20261008');
const asar=require(path.join(old,'candidate/pi-studio/node_modules/@electron/asar'));
const argv=process.argv.slice(2),named=argv[0]?.startsWith('--');
const options=named?Object.fromEntries(Array.from({length:Math.floor(argv.length/2)},(_,i)=>[argv[i*2],argv[i*2+1]])):{};
const stage=path.resolve(options['--stage']||path.join(root,'build/windows-input')),payload=path.resolve(options['--payload']||path.join(root,'build/windows/win-unpacked')),runtime=path.join(old,'delivery_correction_20261008/assets/electron-43.3.0/runtime');
const archive=path.join(payload,'resources/app.asar'),report=options['--evidence']||(!named&&argv[0])||path.join(root,'evidence/windows-dir/verification.json');
const digest=data=>crypto.createHash('sha256').update(data).digest('hex');
function walk(dir,prefix=''){let files=[];for(const name of fs.readdirSync(dir).sort()){const abs=path.join(dir,name),rel=prefix?`${prefix}/${name}`:name,stat=fs.lstatSync(abs);assert(!stat.isSymbolicLink(),`No symlinks allowed: ${abs}`);if(stat.isDirectory())files.push(...walk(abs,rel));else if(stat.isFile())files.push(rel);else throw Error(`Non-regular payload: ${abs}`);}return files;}
const result={status:'RUNNING',kind:'STATIC_WINDOWS_PACKAGE_INTEGRITY_NOT_RUNTIME',windows:'NOT_RUN',stage,payload,archive,files:[],runtimeFiles:[],payloadFiles:[],errors:[]};
try{
 const expected=walk(stage).filter(rel=>rel!=='package-lock.json');
 const actual=asar.listPackage(archive).map(rel=>rel.replace(/^\//,'')).filter(rel=>!asar.statFile(archive,rel).files);
 assert.deepEqual([...actual].sort(),[...expected].sort(),'ASAR inventory must match allowlisted staged app exactly');
 for(const rel of expected){const source=fs.readFileSync(path.join(stage,rel)),packed=asar.extractFile(archive,rel);const match=source.equals(packed);result.files.push({path:rel,bytes:source.length,stageSha256:digest(source),asarSha256:digest(packed),match});assert(match,`Packaged contents changed: ${rel}`);}
 const identity=JSON.parse(asar.extractFile(archive,'BUILD-IDENTITY.json'));
 for(const item of identity.files){const bytes=asar.extractFile(archive,item.path);assert.equal(bytes.length,item.bytes,`Identity size ${item.path}`);assert.equal(digest(bytes),item.sha256,`Identity digest ${item.path}`);}
 assert.equal(identity.engineUpgrade,false);assert.equal(identity.localAgentIncluded,false);
 if(fs.existsSync(path.join(payload,'BUILD-IDENTITY.json')))assert(fs.readFileSync(path.join(payload,'BUILD-IDENTITY.json')).equals(asar.extractFile(archive,'BUILD-IDENTITY.json')),'External build identity must match ASAR');
 const pkg=JSON.parse(asar.extractFile(archive,'package.json'));assert.equal(pkg.main,'electron/remote-main.cjs');assert.equal(pkg.version,identity.version);
 assert(!actual.some(rel=>/node_modules|pi-bridge|agent-runtime|pi-coding-agent/.test(rel)),'No local Pi/second Agent runtime in package');
 for(const rel of walk(runtime)){
  const mapped=rel==='electron.exe'?'BioMind Studio.exe':rel;
  if(rel==='resources/default_app.asar'&&!fs.existsSync(path.join(payload,mapped))){result.runtimeFiles.push({path:rel,status:'DEFAULT_APP_REMOVED_BY_BUILDER'});continue;}
  const source=fs.readFileSync(path.join(runtime,rel)),packed=fs.readFileSync(path.join(payload,mapped)),match=source.equals(packed);
  result.runtimeFiles.push({path:rel,payloadPath:mapped,bytes:source.length,runtimeSha256:digest(source),payloadSha256:digest(packed),match});assert(match,`Electron runtime changed: ${rel}`);
 }
 for(const rel of walk(payload)){const bytes=fs.readFileSync(path.join(payload,rel));result.payloadFiles.push({path:rel,bytes:bytes.length,sha256:digest(bytes)});}
 result.appVersion=pkg.version;result.renderer=identity.renderer;result.asarSha256=digest(fs.readFileSync(archive));result.status='PASS';
}catch(error){result.status='FAIL';result.errors.push(String(error.stack||error));process.exitCode=1;}
fs.writeFileSync(report,JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify({status:result.status,appFiles:result.files.length,runtimeFiles:result.runtimeFiles.length,payloadFiles:result.payloadFiles.length,errors:result.errors,report}));
