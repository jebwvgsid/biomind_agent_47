// Actual installed serializer, synthetic complete request chain, offline only.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
const root='/public/home/liusiqi/03.毕设/Biomind_v1';
const runtime=path.join(root,'integrations/pi_runtime');
const fixture='/tmp/biomind_impl_20261002_baseline/serializer-project';
fs.mkdirSync(fixture,{recursive:true});
const {createHarness,FakeDomain,call,text}=await import(pathToFileURL(path.join(runtime,'dist/tests/extension-harness.js')));
const {stream}=await import(pathToFileURL(path.join(runtime,'node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js')));
const {configuredProvider}=await import(pathToFileURL(path.join(runtime,'dist/model-config.js')));
const provider=configuredProvider({schema:'biomind-active-model-config-v2',provider:'deepseek',model:'deepseek-v4-flash',api:'openai-completions',thinking:'off',endpoint_base_url:'https://api.deepseek.com',endpoint_hostname:'api.deepseek.com',origin:'active-launch-environment',confirmed_active:true});
const model={...provider.models[0],provider:'deepseek'};
const summary={schema:'biomind-offline-serializer-comparison-v1',model:'deepseek-v4-flash',serializer:'installed @earendil-works/pi-ai openai-completions',input:'Use the registered synthetic runtime and report its stored identity. Do not run a health check.',fixture:'same synthetic select_environment call + final response; no skills; preference-v1',scope:'scripted provider and FakeDomain; real built-in serializer; no external provider/model calls',counts_unit:'UTF-16 characters and UTF-8 bytes, not tokens',profiles:[],source_sha256:{},limitations:['Does not prove natural model behavior.','No token counter; no token savings claim.','Fresh session/request identifiers vary at fixed formats.','No compaction or retry was triggered by this bounded fixture.']};
for(const name of ['dist/extensions/biomind.js','dist/native-workflow.js','dist/tests/extension-harness.js','dist/model-config.js'])summary.source_sha256[name]=createHash('sha256').update(fs.readFileSync(path.join(runtime,name))).digest('hex');
for(const profile of ['native','protected']){
 const domain=new FakeDomain();
 domain.receipts.set('environments.select',{status:'AVAILABLE',env_name:'synthetic',prefix:'/synthetic/prefix',registry_path:'/synthetic/software_registry.json',healthcheck:'not_run',registration_status:'registered'});
 const h=await createHarness({name:'serializer-'+profile,cwd:fixture,executionProfile:profile,nativePrompt:true,domain,skillPaths:[],script:[call('synthetic-select','select_environment',{env_name:'synthetic'}),text('Registered identity: synthetic; healthcheck not_run.')]});
 try{
  await h.prompt(summary.input);
  if(h.errors.length)throw new Error(JSON.stringify(h.errors));
  const requests=[];
  for(const [index,context] of h.requests.entries()){
   let receipt;
   const response=stream(model,context,{apiKey:'synthetic-not-a-credential',maxTokens:100,env:{},fetch:async(_url,init)=>{
    const body=JSON.parse(init.body);
    const system=body.messages.filter(m=>['system','developer'].includes(m.role));
    const history=body.messages.filter(m=>!['system','developer'].includes(m.role));
    const measure=v=>{const encoded=JSON.stringify(v);return {chars:encoded.length,bytes:Buffer.byteLength(encoded)}};
    receipt={index:index+1,body:measure(body),system:measure(system),tools:measure(body.tools??[]),history:measure(history),message_count:body.messages.length,tools_count:(body.tools??[]).length,tool_names:(body.tools??[]).map(t=>t.function.name),tool_outputs:measure(history.filter(m=>m.role==='tool')),roles:body.messages.map(m=>m.role)};
    return new Response('data: '+JSON.stringify({id:'synthetic',object:'chat.completion.chunk',created:1,model:model.id,choices:[{index:0,delta:{content:'offline'},finish_reason:'stop'}]})+'\n\ndata: [DONE]\n\n',{headers:{'content-type':'text/event-stream'}});
   }});
   const result=await response.result();
   if(!receipt||result.stopReason==='error')throw new Error(result.errorMessage??'serialization failed');
   requests.push(receipt);
  }
  summary.profiles.push({profile,requests,total_requests:requests.length,compaction_count:h.events.filter(e=>e.type.includes('compaction')).length,retry_count:h.events.filter(e=>e.type.includes('retry')).length,tool_result_count:h.events.filter(e=>e.type==='tool_execution_end').length,domain_calls:domain.calls.map(c=>c.method),totals:Object.fromEntries(['body','system','tools','history','tool_outputs'].map(part=>[part,{chars:requests.reduce((n,r)=>n+r[part].chars,0),bytes:requests.reduce((n,r)=>n+r[part].bytes,0)}]))});
 }finally{await h.close();}
}
const output=process.argv[2]??'/tmp/biomind_impl_20261002_baseline/serializer-before.json';
fs.writeFileSync(output,JSON.stringify(summary,null,2)+'\n');
console.log(JSON.stringify(summary.profiles.map(p=>({profile:p.profile,requests:p.total_requests,totals:p.totals,tools:p.requests[0].tool_names}))));
