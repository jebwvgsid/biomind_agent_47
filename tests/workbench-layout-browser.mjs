import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import assert from 'node:assert/strict';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const legacy=path.resolve(root,'../BioMind_Studio_S2_S3_Implementation_20261008');
const pkgRoot=path.join(legacy,'candidate/pi-studio/node_modules/puppeteer-core');
const pkg=JSON.parse(fs.readFileSync(path.join(pkgRoot,'package.json'),'utf8'));
const puppeteer=(await import(pathToFileURL(path.join(pkgRoot,pkg.main)).href)).default;
const output=process.argv[2]||path.join(root,'evidence/layout-only');
fs.mkdirSync(output,{recursive:true});
const home=path.join(output,'isolated-home');fs.mkdirSync(home,{recursive:true});
const renderer=path.join(root,'desktop/out/renderer');
const result={status:'RUNNING',evidenceLevel:'LAYOUT_ONLY',platform:'Linux Chromium',windows:'NOT_RUN',realBioMind:'NOT_RUN',note:'Unmodified compiled flowflic renderer with explicit read-only synthetic window.pi fixture. No model or server execution.',pageErrors:[],console:[],requests:[],screenshots:[],sourceHashes:{}};
const hash=file=>createHash('sha256').update(fs.readFileSync(file)).digest('hex');
result.sourceHashes['renderer/index.html']=hash(path.join(renderer,'index.html'));
for(const f of fs.readdirSync(path.join(renderer,'assets')).filter(f=>/^index-.*\.(js|css)$/.test(f)))result.sourceHashes[`renderer/assets/${f}`]=hash(path.join(renderer,'assets',f));
result.sourceHashes['upstream/imageassets/newpi01.png']=hash(path.join(root,'upstream/flowflic-pi-studio/imageassets/newpi01.png'));
let browser;
try{
 browser=await puppeteer.launch({executablePath:path.join(legacy,'assets/chrome-152.0.7977.54-linux64/chrome-linux64/chrome'),headless:true,pipe:true,dumpio:true,userDataDir:path.join(output,'chrome-profile'),args:['--no-first-run','--no-default-browser-check','--disable-background-networking','--allow-file-access-from-files','--proxy-server=http://127.0.0.1:9'],env:{PATH:'/usr/bin:/bin',HOME:home,LANG:'C.UTF-8',LC_ALL:'C.UTF-8',TMPDIR:'/tmp'},defaultViewport:{width:1440,height:960}});
 result.browserVersion=await browser.version();
 const page=await browser.newPage();page.setDefaultTimeout(12000);
 page.on('pageerror',error=>result.pageErrors.push(String(error)));
 page.on('console',message=>result.console.push({type:message.type(),text:message.text()}));
 page.on('request',request=>result.requests.push({url:request.url(),method:request.method(),type:request.resourceType()}));
 await page.evaluateOnNewDocument(()=>{
  const no=()=>()=>{};const unsupported=async()=>{throw new Error('LAYOUT_ONLY_READ_ONLY_FIXTURE');};
  const cwd='/fixture/BioMind-Project';
  const projects=[{cwd,name:'BioMind-Project',threads:[{file:'fixture-session-A',id:'fixture-session-A',title:'A · 分析并生成结果',preview:'布局样例数据',updatedAt:Date.now(),messageCount:3},{file:'fixture-session-B',id:'fixture-session-B',title:'B · 资料整理',preview:'布局样例数据',updatedAt:Date.now()-60000,messageCount:2}]},{cwd:'/fixture/Research',name:'Research',threads:[]}];
  const model={provider:'fixture',id:'layout-model',name:'Layout model'};
  const snap=id=>({threadId:id,sessionFile:id,cwd,sessionName:id==='fixture-session-A'?'A · 分析并生成结果':'B · 资料整理',connected:true,isStreaming:false,model,availableModels:[model],thinkingLevel:'off',commands:[],messages:[{role:'user',timestamp:1,content:'整理项目数据，并在 result.md 中说明处理步骤。'},{role:'assistant',timestamp:2,content:[{type:'text',text:'已读取项目材料，下面保存结果文件。'},{type:'toolCall',id:'write-result',name:'write',arguments:{path:cwd+'/result.md',content:'# Result\n\nLayout-only fixture.'}}]},{role:'toolResult',timestamp:3,toolCallId:'write-result',toolName:'write',content:[{type:'text',text:'Successfully wrote result.md'}],isError:false},{role:'assistant',timestamp:4,content:[{type:'text',text:'结果已保存到 [result.md]('+cwd+'/result.md)。\n\n这是布局检查样例，不代表执行过真实 BioMind 任务。'}]}]});
  const events={event:no,extui:no,exit:no,error:no,focusThread:no,automation:no,projectsChanged:no,appUpdate:no,coreUpdate:no};
  window.pi={
   app:{getConfig:async()=>({theme:'light',language:'zh',pinnedProjects:[],pinnedThreads:[],archivedProjects:[],archivedThreads:[]}),getProjects:async()=>projects,resolveRuntime:async()=>({ok:true,node:'fixture',cli:'LAYOUT_ONLY'}),prewarm:async()=>({remote:true}),getVersion:async()=> 'LAYOUT_ONLY',getFileTree:async()=>[{name:'result.md',rel:'result.md',abs:cwd+'/result.md',isDir:false,ext:'.md',size:86},{name:'inputs',rel:'inputs',abs:cwd+'/inputs',isDir:true,ext:'',size:0}],readPreview:async()=>({name:'result.md',ext:'.md',size:86,kind:'markdown',text:'# Result\n\nLAYOUT_ONLY fixture.\n\n| Item | Status |\n| --- | --- |\n| 原始组件布局 | 已载入 |\n| 真实 Windows 验收 | NOT_RUN |'}),fileExists:async()=>true,setConfig:unsupported,openProject:unsupported,searchThreads:async()=>[],editAction:unsupported},
   biomind:{getStatus:async()=>({status:'ready'}),getLocalUIState:async()=>({status:'saved'}),onLocalUIState:no,reportRendererIssue:async()=>({ok:true}),onStatus:no,onSnapshot:no,restoreThreads:async()=>[],activateThread:async()=>true,focusThread:async()=>true,showConnection:unsupported,getControls:async()=>({state:{model,cwd,capabilities:{}},models:[model],resources:{skills:[]}}),control:unsupported,finishInput:unsupported,browseProjects:async()=>({path:'/fixture',parent:'/',entries:[{name:'BioMind-Project',path:cwd}]})},
   thread:{loadHistory:async({sessionFile})=>snap(sessionFile),open:unsupported,close:unsupported,getThinkingLevels:async()=>({levels:['off']}),getBranchMessages:async()=>({messages:[]}),getStats:async()=>({}),prompt:unsupported,steer:unsupported,followUp:unsupported,setModel:unsupported},
   window:{isMaximized:async()=>false,onMaximizedChanged:no,minimize:()=>{},maximize:()=>{},close:()=>{}},
   on:events,plugins:{getSkills:async()=>[],getPackages:unsupported},settings:{getUsage:async()=>({totals:{input:0,output:0,total:0,cost:0},daily:[],models:[]})},automation:{getTasks:unsupported},remote:{}
  };
 });
 await page.goto(pathToFileURL(path.join(renderer,'index.html')).href,{waitUntil:'networkidle0'});
 await page.waitForSelector('.empty-state');
 await page.evaluate(()=>{const mark=document.createElement('div');mark.textContent='LAYOUT_ONLY · Linux Chromium · fixture data · Windows NOT_RUN';mark.style.cssText='position:fixed;right:12px;bottom:5px;z-index:100000;padding:4px 9px;background:#fff1cd;color:#5a4100;border:1px solid #c6a851;border-radius:5px;font:11px monospace;pointer-events:none';document.body.append(mark);});
 const shot=async name=>{await page.screenshot({path:path.join(output,name),fullPage:true});result.screenshots.push(name);};
 await shot('welcome-LAYOUT_ONLY.png');
 await page.evaluate(()=>{const item=[...document.querySelectorAll('.thread')].find(e=>e.textContent.includes('A ·'));if(!item)throw Error('Fixture A row missing');item.click();});
 await page.waitForSelector('.composer');
 await page.waitForFunction(()=>document.body.innerText.includes('结果已保存到'));
 await shot('chat-LAYOUT_ONLY.png');
 await page.evaluate(()=>{const button=[...document.querySelectorAll('.sb-tab')].find(e=>e.textContent.includes('文件'));button.click();});
 await page.waitForFunction(()=>[...document.querySelectorAll('.ft-row')].some(e=>e.textContent.includes('result.md')));
 await page.evaluate(()=>[...document.querySelectorAll('.ft-row')].find(e=>e.textContent.includes('result.md')).click());
 await page.waitForSelector('.preview');
 await page.waitForFunction(()=>document.body.innerText.includes('真实 Windows 验收'));
 await shot('files-preview-LAYOUT_ONLY.png');
 result.dom=await page.evaluate(()=>({text:document.body.innerText,viewport:{width:innerWidth,height:innerHeight},document:{width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight},regions:Object.fromEntries(['.sidebar','.main','.preview','.composer'].map(selector=>{const box=document.querySelector(selector)?.getBoundingClientRect();return [selector,box?{x:box.x,y:box.y,width:box.width,height:box.height}:null]}))}));
 assert.equal(result.dom.document.width,result.dom.viewport.width,'horizontal overflow');
 assert.deepEqual(result.pageErrors,[],'renderer page errors');
 result.status='PASS';
}catch(error){result.status=browser?'FAIL':'BLOCKED';result.error=String(error?.stack||error);process.exitCode=1;}
finally{if(browser)await browser.close();fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');}
console.log(JSON.stringify({status:result.status,evidenceLevel:result.evidenceLevel,error:result.error,screenshots:result.screenshots}));
