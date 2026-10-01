// Offline current-build reconstruction, NOT a recovered historical request body.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath,pathToFileURL} from 'node:url';
const out=path.dirname(fileURLToPath(import.meta.url));
const root=path.resolve(out,'../../..'), runtime=path.join(root,'integrations/pi_runtime');
const {createHarness,FakeDomain,text}=await import(pathToFileURL(path.join(runtime,'dist/tests/extension-harness.js')));
const {stream}=await import(pathToFileURL(path.join(runtime,'node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js')));
const {configuredProvider}=await import(pathToFileURL(path.join(runtime,'dist/model-config.js')));
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'biomind-reconstruct-'));
const domain=new FakeDomain();domain.snapshot.preferences={text:fs.readFileSync(path.join(root,'user_preferences.md'),'utf8'),status:'loaded'};
const h=await createHarness({cwd:tmp,executionProfile:'native',nativePrompt:true,domain,skillPaths:[path.join(root,'skills')],script:[text('synthetic seed')]});
try {
  await h.prompt('Synthetic seed for current-build prompt reconstruction.');
  const sessionFile='/public/home/liusiqi/.pi/agent/sessions/--public-home-liusiqi-03.毕设-benchmarks-02.测试26.9--/2026-10-01T08-19-56-956Z_01a0f68c-7b84-70ca-bfcd-d234aa37b81b.jsonl';
  const rows=fs.readFileSync(sessionFile,'utf8').trim().split('\n').map(JSON.parse);
  const historical=rows.slice(0,50).filter(r=>r.type==='message').map(r=>r.message);
  const context={messages:[...h.requests[0].messages.filter(m=>m.role==='system'),...historical]};
  const provider=configuredProvider({schema:'biomind-active-model-config-v2',provider:'deepseek',model:'deepseek-v4-flash',
    api:'openai-completions',thinking:'off',endpoint_base_url:'https://api.deepseek.com',endpoint_hostname:'api.deepseek.com',
    origin:'active-launch-environment',confirmed_active:true});
  const model={...provider.models[0],provider:'deepseek'};
  let summary;
  const response=stream(model,context,{apiKey:'synthetic-not-a-credential',maxTokens:100,env:{},fetch:async(_url,init)=>{
    const body=JSON.parse(init.body);const names=(body.tools??[]).map(t=>t.function.name);
    const system=body.messages.filter(m=>m.role==='system'||m.role==='developer').map(m=>m.content).join('\n');
    summary={layer:'current installed Pi built-in openai-completions serializer + in-memory fake fetch, all sockets forbidden',
      historical_messages_through_line:50,historical_message_count:historical.length,
      serialized_message_count:body.messages.length,serialized_body_bytes:Buffer.byteLength(init.body),
      system_chars:system.length,tools:names,duplicate_tool_names:names.filter((n,i)=>names.indexOf(n)!==i),
      tool_definition_bytes:Buffer.byteLength(JSON.stringify(body.tools)),
      message_chars_by_role:Object.fromEntries([...new Set(body.messages.map(m=>m.role))].map(role=>[role,body.messages.filter(m=>m.role===role).reduce((n,m)=>n+JSON.stringify(m).length,0)])),
      registry_rule_present:system.includes('已登记且目标明确的环境直接使用'),
      skill_body_occurrences:body.messages.filter(m=>m.role==='tool'&&JSON.stringify(m).includes('Rscript --version || Rscript --version || Rscript --version')).length,
      limits:['historical raw HTTP body not saved','current skills/preferences/native system rules and synthetic cwd/request metadata used',
        'does not establish exact historical activation or prompt bytes','byte/character counts are not tokens or savings estimates']};
    return new Response('data: '+JSON.stringify({id:'synthetic',object:'chat.completion.chunk',created:1,model:model.id,choices:[{index:0,delta:{content:'offline'},finish_reason:'stop'}]})+'\n\ndata: [DONE]\n\n',{headers:{'content-type':'text/event-stream'}});
  }});
  const result=await response.result();
  if(!summary||result.stopReason==='error')throw new Error(result.errorMessage??'serialization failed');
  fs.writeFileSync(path.join(out,'request_reconstruction.json'),JSON.stringify(summary,null,2)+'\n');
  console.log(JSON.stringify(summary));
} finally {await h.close();fs.rmSync(tmp,{recursive:true,force:true});}
