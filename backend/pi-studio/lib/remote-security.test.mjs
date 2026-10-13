import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, symlinkSync, writeFileSync, existsSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { registerHooks } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { getBioMindProjectRoots, getBioMindUploadRoot, isBioMindPathAllowed, isBioMindProjectAllowed, isBioMindRequestAllowed, isBioMindApiAllowed } from "./remote-security.ts";
const root = mkdtempSync(path.join(tmpdir(), "biomind-studio-security-"));
const a = path.join(root, "project-a"), b = path.join(root, "project-b"), outside = path.join(root, "outside");
for (const dir of [a, b, outside]) mkdirSync(dir);
const previous = { ...process.env };
process.env.PI_STUDIO_AGENT_PROFILE = "biomind";
process.env.BIOMIND_PROJECT_ROOTS_JSON = JSON.stringify([a,b]);
process.env.BIOMIND_SERVER_ID = 'synthetic-security-server';
delete process.env.BIOMIND_DATA_ROOTS_JSON; delete process.env.PI_WEB_ALLOWED_HOSTS;
test.after(() => { for (const name of Object.keys(process.env)) if (!(name in previous)) delete process.env[name]; Object.assign(process.env, previous); rmSync(root, { recursive: true, force: true }); });
test("operator roots resolve symlinks, reject traversal and never infer home access", () => {
  writeFileSync(path.join(a,"inside.txt"), "synthetic"); writeFileSync(path.join(outside,"outside.txt"),"synthetic");
  symlinkSync(path.join(a,"inside.txt"),path.join(a,"inside-link")); symlinkSync(outside,path.join(a,"escape"));
  assert.deepEqual([...getBioMindProjectRoots()], [a,b]);
  assert.equal(isBioMindProjectAllowed(a),true); assert.equal(isBioMindProjectAllowed(outside),false);
  assert.equal(isBioMindPathAllowed(path.join(a,"inside-link")),true);
  assert.equal(isBioMindPathAllowed(path.join(a,"escape/outside.txt")),false);
  assert.equal(isBioMindPathAllowed(path.join(a,"../outside/outside.txt")),false);
  assert.equal(isBioMindPathAllowed("relative.txt"),false);
});
test("upload roots bind the original authorized project and reject cross-project symlink stores", () => {
  const uploadA = getBioMindUploadRoot(a); assert.equal(uploadA,path.join(a,"pi-web-uploads"));
  symlinkSync(uploadA,path.join(b,"pi-web-uploads"));
  assert.throws(()=>getBioMindUploadRoot(b),/BIOMIND_UPLOAD_SCOPE_MISMATCH/);
  assert.throws(()=>getBioMindUploadRoot(outside),/BIOMIND_PROJECT_NOT_AUTHORIZED/);
  assert.equal(existsSync(path.join(outside,"pi-web-uploads")),false);
});
test("remote Host and Origin reject rebinding, cross-site exports and alternate loopback origins", () => {
  const request = headers => new Request("http://localhost:30141/api/sessions",{headers});
  assert.equal(isBioMindRequestAllowed(request({host:"127.0.0.1:30142",origin:"http://127.0.0.1:30142"})),true);
  for (const headers of [{host:"evil.invalid"},{host:"192.0.2.1"},{host:"localhost@evil.invalid"},{host:"localhost:30141",origin:"null"},{host:"localhost:30141",origin:"http://127.0.0.1:30141"},{host:"localhost:30141","sec-fetch-site":"cross-site"}]) assert.equal(isBioMindRequestAllowed(request(headers)),false);
});
test("remote API allowlist closes every alternate local-agent and privilege-expansion surface", () => {
  for (const url of ["/api/terminal","/api/browser","/api/vision/describe","/api/models-config/test","/api/teams","/api/skills/install","/api/settings/compaction","/api/files/allow-local","/api/univer/writeback","/api/sessions/x/auto-name","/api/default-cwd","/api/agent/x/bash-output"]) {
    for (const method of ["GET","POST","DELETE"]) assert.equal(isBioMindApiAllowed(url,method),false,url+" "+method);
  }
  assert.equal(isBioMindApiAllowed("/api/agent/x/events","GET"),true);
  assert.equal(isBioMindApiAllowed("/api/sessions/x/media","GET"),true);
  assert.equal(isBioMindApiAllowed("/api/sessions/search","GET"),true);
  for (const method of ["POST", "DELETE"]) assert.equal(isBioMindApiAllowed("/api/sessions/search",method),false);
  for (const suffix of ["state", "media"]) assert.equal(isBioMindApiAllowed("/api/sessions/search/"+suffix,"GET"),false);
  assert.equal(isBioMindApiAllowed("/api/agent/new","POST"),true);
  for (const method of ["GET", "POST"]) assert.equal(isBioMindApiAllowed("/api/plugins", method), true);
  for (const method of ["PUT", "PATCH", "DELETE"]) assert.equal(isBioMindApiAllowed("/api/plugins", method), false);
  assert.equal(isBioMindApiAllowed("/api/plugins/anything", "POST"), false);
  for (const method of ["GET", "PATCH"]) assert.equal(isBioMindApiAllowed("/api/agent/instructions", method), true);
  for (const method of ["POST", "DELETE"]) assert.equal(isBioMindApiAllowed("/api/agent/instructions", method), false);
  assert.equal(isBioMindApiAllowed("/api/agent/instructions/other", "PATCH"), false);
  assert.equal(isBioMindApiAllowed("/api/uploads","POST",new URLSearchParams("dir=/tmp")),false);
  assert.equal(isBioMindApiAllowed("/api/uploads","POST",new URLSearchParams("open=1")),false);
  assert.equal(isBioMindApiAllowed("/api/uploads","DELETE"),false);
  assert.equal(isBioMindApiAllowed("/api/files/tmp/a","GET",new URLSearchParams("type=watch")),false);
});
// Resolve local TypeScript modules without installing Next/Studio dependencies.
const projectRoot = fileURLToPath(new URL("../",import.meta.url));
const responseStub = "data:text/javascript,"+encodeURIComponent('export class NextRequest extends Request {} export class NextResponse extends Response { static json(value,init){return Response.json(value,init)} static next(){return new Response(null,{headers:{"x-offline-next":"1"}})} }');
registerHooks({resolve(specifier,context,nextResolve){
  if(specifier==="next/server")return {url:responseStub,shortCircuit:true};
  if(specifier.startsWith("@/"))return nextResolve(pathToFileURL(path.join(projectRoot,specifier.slice(2)+".ts")).href,context);
  try{return nextResolve(specifier,context)}catch(error){if(specifier.startsWith(".")&&!path.extname(specifier))return nextResolve(specifier+".ts",context);throw error;}
}});
test("actual proxy requires existing Basic auth for pages, files, SSE, history and uploads", async () => {
  const {proxy}=await import("../proxy.ts");
  process.env.PI_WEB_PASSWORD="synthetic-offline-password";
  const make=(pathname,method="GET",authorization)=>{const req=new Request("http://localhost:30141"+pathname,{method,headers:{host:"localhost:30141",...(authorization?{authorization}:{})}});req.nextUrl=new URL(req.url);return req;};
  const auth="Basic "+Buffer.from("pi:synthetic-offline-password").toString("base64");
  for(const [url,method] of [["/","GET"],["/api/agent/x/events","GET"],["/api/sessions/x/media","GET"],["/api/files/tmp/a","GET"],["/api/uploads","POST"]]){
    assert.equal(proxy(make(url,method)).status,401);
    assert.equal(proxy(make(url,method,auth)).headers.get("x-offline-next"),"1");
  }
  assert.equal(proxy(make("/api/terminal","POST",auth)).status,403);
  delete process.env.PI_WEB_PASSWORD;assert.equal(proxy(make("/api/sessions")).status,503);
});
test("scoped upload functions preserve existing files and reject unscoped access", async () => {
  const {writeUpload,getUploadPath,deleteUpload,enforceQuota,listUploads}=await import("./uploads.ts");
  assert.throws(()=>writeUpload("unsafe.txt",Buffer.from("x")),/PROJECT_REQUIRED/);
  const receipt=writeUpload("synthetic.txt",Buffer.from("fixture"),a);
  assert.equal(receipt.path,path.join(a,"pi-web-uploads/synthetic.txt"));
  assert.equal(readFileSync(receipt.path,"utf8"),"fixture"); assert.equal(getUploadPath(receipt.name,a),receipt.path);
  assert.equal(deleteUpload(receipt.name),false);assert.deepEqual(enforceQuota(),[]);assert.equal(existsSync(receipt.path),true);
  symlinkSync(path.join(outside,"outside.txt"),path.join(a,"pi-web-uploads/escape.txt"));
  assert.equal(getUploadPath("escape.txt",a),null);assert(!listUploads(a).some(item=>item.name==="escape.txt"));
});
test("actual upload route binds multipart project, preserves file order and streams authorized downloads", async () => {
  const {GET,POST,DELETE}=await import("../app/api/uploads/route.ts");
  const request=(query="",options={})=>{const req=new Request("http://localhost:30141/api/uploads"+query,options);req.nextUrl=new URL(req.url);return req;};
  const form=new FormData();form.set("cwd",a);
  form.append("files",new File(["first,1\n"],"first.csv",{type:"text/csv"}));
  form.append("files",new File(["second,2\n"],"second.csv",{type:"text/csv"}));
  const response=await POST(request("",{method:"POST",body:form}));assert.equal(response.status,200);
  const value=await response.json();assert.equal(value.project,a);assert.deepEqual(value.uploaded.map(item=>item.name),["first.csv","second.csv"]);
  assert.equal(value.serverId,'synthetic-security-server'); assert.deepEqual(value.uploaded.map(item=>item.inputIndex),[0,1]);
  assert.deepEqual(value.uploaded.map(item=>item.sourceName),['first.csv','second.csv']);
  const partial=new FormData();partial.set('cwd',a);partial.append('files',new File(['accepted'],'ok.txt'));partial.append('files',new File(['rejected'],'bad:name.txt'));
  const partialResponse=await POST(request('',{method:'POST',body:partial}));assert.equal(partialResponse.status,207);const partialReceipt=await partialResponse.json();
  assert.equal(partialReceipt.uploaded.length,1);assert.equal(partialReceipt.errors.length,1);assert.equal(partialReceipt.uploaded[0].inputIndex,0);
  assert.equal(readFileSync(partialReceipt.uploaded[0].path,'utf8'),'accepted');
  const download=await GET(request("?cwd="+encodeURIComponent(a)+"&download=first.csv"));
  assert.equal(download.status,200);assert.equal(await download.text(),"first,1\n");assert.match(download.headers.get("content-security-policy"),/sandbox/);
  assert.equal((await GET(request("?download=first.csv"))).status,403);
  assert.equal((await GET(request("?cwd="+encodeURIComponent(outside)+"&download=first.csv"))).status,403);
  for(const cwd of [undefined,outside]){const body=new FormData();if(cwd)body.set("cwd",cwd);body.append("files",new File(["no"],"denied.txt"));assert.equal((await POST(request("",{method:"POST",body}))).status,403);}
  assert.equal((await POST(request("?dir=/outside",{method:"POST"}))).status,403);
  assert.equal((await POST(request("?open=1",{method:"POST"}))).status,403);
  assert.equal((await DELETE(request("?name=first.csv",{method:"DELETE"}))).status,403);
  const tooLarge=await POST(request("",{method:"POST",headers:{"content-length":String(33*1024*1024)},body:"x"}));assert.equal(tooLarge.status,413);
  assert.equal(existsSync(path.join(outside,"pi-web-uploads")),false);
});
