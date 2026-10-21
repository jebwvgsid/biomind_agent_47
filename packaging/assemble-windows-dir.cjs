"use strict";
// Offline fallback for a dependency-free staged Electron app. Uses the same
// allowlisted stage and official cached runtime, without modifying builder.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const root=path.resolve(__dirname,'..'),old=path.resolve(root,'../BioMind_Studio_S2_S3_Implementation_20261008');
const asar=require(path.join(old,'candidate/pi-studio/node_modules/@electron/asar'));
const options=Object.fromEntries(Array.from({length:Math.floor((process.argv.length-2)/2)},(_,i)=>[process.argv[2+i*2],process.argv[3+i*2]]));
const stage=path.resolve(options['--stage']||path.join(root,'build/windows-input')),output=path.resolve(options['--output']||path.join(root,'build/windows/win-unpacked')),runtime=path.join(old,'delivery_correction_20261008/assets/electron-43.3.0/runtime');
const stamp=new Date().toISOString().replace(/[-:.]/g,'');
const ledger=path.resolve(options['--evidence']||path.join(root,`evidence/windows-dir/manual-assembly-${stamp}.json`));
const hash=data=>crypto.createHash('sha256').update(data).digest('hex');
function walk(dir,prefix=''){let files=[];for(const name of fs.readdirSync(dir).sort()){const abs=path.join(dir,name),rel=prefix?`${prefix}/${name}`:name,stat=fs.lstatSync(abs);if(stat.isSymbolicLink())throw Error(`Symlink forbidden: ${rel}`);if(stat.isDirectory())files.push(...walk(abs,rel));else if(stat.isFile())files.push(rel);else throw Error(`Special file forbidden: ${rel}`);}return files;}
(async()=>{
 if(fs.existsSync(ledger))throw Error('Use a fresh assembly evidence path');
 fs.mkdirSync(path.dirname(ledger),{recursive:true});
 fs.mkdirSync(path.dirname(output),{recursive:true});
 const identity=JSON.parse(fs.readFileSync(path.join(stage,'BUILD-IDENTITY.json')));
 const selected=['BUILD-IDENTITY.json',...identity.files.map(row=>row.path)].sort();
 const actual=walk(stage).filter(rel=>rel!=='package-lock.json');
 if(JSON.stringify(selected)!==JSON.stringify(actual))throw Error('Stage differs from explicit BUILD-IDENTITY manifest');
 for(const row of identity.files){const bytes=fs.readFileSync(path.join(stage,row.path));if(bytes.length!==row.bytes||hash(bytes)!==row.sha256)throw Error(`Stage manifest mismatch: ${row.path}`);}
 const moved=fs.existsSync(output)?`${output}.previous-${stamp}`:null;
 if(moved)fs.renameSync(output,moved);
 fs.cpSync(runtime,output,{recursive:true,errorOnExist:true,force:false});
 fs.renameSync(path.join(output,'electron.exe'),path.join(output,'BioMind Studio.exe'));
 const destination=path.join(output,'resources/app.asar');
 await asar.createPackageFromFiles(stage,destination,selected.map(rel=>path.join(stage,rel)));
 const result={status:'ASSEMBLED_PENDING_INTEGRITY_CHECK',kind:'DIRECT_ASAR_CACHED_ELECTRON_RUNTIME',version:identity.version,stage,output,runtime,movedPriorDirectory:moved,asarSha256:hash(fs.readFileSync(destination)),appFiles:selected.length,windows:'NOT_RUN',unsignedApplication:true,peResources:'Original Electron 43.3.0 metadata/icon; executable filename renamed only',createdAt:new Date().toISOString()};
 fs.writeFileSync(ledger,JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exitCode=1;});
