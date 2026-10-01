// Offline scripted provider + installed Pi/native bash. No credentials/network.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
const out=path.dirname(fileURLToPath(import.meta.url));
const root=path.resolve(out,'../../..');
const runtime=path.join(root,'integrations/pi_runtime');
const {createHarness,FakeDomain,call,text}=await import(pathToFileURL(path.join(runtime,'dist/tests/extension-harness.js')));
const {createBashTool}=await import(pathToFileURL(path.join(runtime,'node_modules/@earendil-works/pi-coding-agent/dist/core/tools/bash.js')));
const ts=(await import(pathToFileURL(path.join(runtime,'node_modules/typescript/lib/typescript.js')))).default;
const {getCurrentTools}=await import(pathToFileURL(path.join(runtime,'node_modules/@earendil-works/pi-ai/dist/utils/transcript.js')));
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'biomind-v2-node-'));
const prefix=path.join(tmp,'prefix');fs.mkdirSync(path.join(prefix,'bin'),{recursive:true});
fs.writeFileSync(path.join(prefix,'bin/Rscript'),'#!/bin/sh\nprintf SYNTHETIC_RSCRIPT\n',{mode:0o755});
const observed={};
const requestTools=req=>getCurrentTools(req.messages);
let h;
try {
  const domain=new FakeDomain();
  domain.receipts.set('environments.select',{availability:'AVAILABLE',selected_candidate:{prefix,audit:{env:'rna_fixture',healthcheck_status:'not_run'}}});
  h=await createHarness({cwd:tmp,executionProfile:'native',nativePrompt:true,domain,
    script:[call('select','select_environment',{env_name:'rna_fixture'}),call('bash','bash',{command:'Rscript'}),text('done')]});
  await h.prompt('Synthetic wiring witness: use the selected registration.');
  const results=h.events.filter(e=>e.type==='tool_execution_end');
  assert.equal(results.length,2);
  assert.equal(results[0].isError,false);
  assert.equal(results[1].isError,true);
  assert.match(JSON.stringify(results[1]),/not found|127/);
  const req=h.requests[0];
  const system=JSON.stringify(req.systemPrompt??'')+JSON.stringify(req.messages.filter(m=>m.role==='system'));
  observed.native={active_tools:requestTools(req).map(t=>t.name),selection_succeeded:true,bash_after_selection_failed:true,
    install_tool_active:requestTools(req).some(t=>t.name==='install_tool'),system_chars:system.length,
    explicit_registry_first_rule:system.includes('已登记且目标明确的环境直接使用'),
    no_default_healthcheck_rule:system.includes('不默认运行 healthcheck'),
    model_requests:h.requests.length,domain_methods:domain.calls.map(c=>c.method),
    meaning:'script forces tool order; does not prove natural model choice; synthetic Domain result + real native bash'};
  assert.equal(observed.native.install_tool_active,false);
  assert.equal(observed.native.explicit_registry_first_rule,false);
  await h.close();h=undefined;
  h=await createHarness({cwd:tmp,executionProfile:'protected',nativePrompt:true,script:[text('done')]});
  await h.prompt('Synthetic short question.');
  observed.protected={active_tools:requestTools(h.requests[0]).map(t=>t.name),
    registry_first_rule:JSON.stringify(h.requests[0]).includes('已登记且目标明确的环境直接使用'),model_requests:h.requests.length};
  await h.close();h=undefined;
  h=await createHarness({cwd:tmp,executionProfile:'native',nativePrompt:true,
    script:[call('handoff','handoff_generate',{goal:'synthetic'}),text('done')]});
  await h.prompt('Synthetic handoff tool activation witness.');
  observed.after_handoff={install_tool_active:requestTools(h.requests[1]).some(t=>t.name==='install_tool'),
    model_requests:h.requests.length,context_edit_count:h.manager.getEntries().filter(e=>e.type==='context_edit').length};
  assert.equal(observed.after_handoff.install_tool_active,true);
  await h.close();h=undefined;
  // Pi tool directly, no shell startup scripts. Fixed harmless commands only.
  const bash=createBashTool(tmp);const updates=[];
  const started=Date.now();
  await bash.execute('stream',{command:'printf start; sleep 0.2; printf end'},undefined,u=>updates.push({ms:Date.now()-started,chars:JSON.stringify(u).length}));
  let plain=await bash.execute('pipeline',{command:'false | tail -1'});
  let failure;
  try{await bash.execute('pipefail',{command:'set -o pipefail; false | tail -1'});}catch(e){failure=String(e);}
  assert(failure?.includes('1'));
  await bash.execute('export',{command:'export AUDIT_EPHEMERAL=present'});
  const next=await bash.execute('new-shell',{command:'printf "${AUDIT_EPHEMERAL-unset}"'});
  assert.match(JSON.stringify(next),/unset/);
  let timed;
  try{await bash.execute('timeout',{command:'sleep 2',timeout:.1});}catch(e){timed=String(e);}
  observed.bash={update_count:updates.length,updates,plain_pipeline_resolved:true,
    pipefail_rejected:true,export_does_not_persist:true,timeout_error:timed,
    note:'tool timestamps are synthetic, not historical UI timing; cancellation descendant test covered by existing Python test'};
  const emit=[];
  for(const n of ['host','native-workflow','stage-context','execution-profile','domain-client','model-config','native-session','extensions/biomind','tests/extension-harness']){
    const src=fs.readFileSync(path.join(runtime,n+'.ts'),'utf8');
    const js=ts.transpileModule(src,{fileName:n+'.ts',compilerOptions:{target:ts.ScriptTarget.ES2023,module:ts.ModuleKind.ESNext,sourceMap:true,esModuleInterop:true}}).outputText;
    const strip=s=>s.replace(/\/\/# sourceMappingURL=.*\n?$/,'').trim();
    emit.push({file:n,transpile_equals_dist_ignoring_map_comment:strip(js)===strip(fs.readFileSync(path.join(runtime,'dist',n+'.js'),'utf8'))});
  }
  observed.source_dist_comparison={method:'in-memory transpileModule, no build/install or source/dist writes; current dirty TS included',files:emit};
  fs.writeFileSync(path.join(out,'node_witness.json'),JSON.stringify(observed,null,2)+'\n');
  console.log(JSON.stringify(observed));
} finally {if(h)await h.close();fs.rmSync(tmp,{recursive:true,force:true});}
