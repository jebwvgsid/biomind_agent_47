import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const require = createRequire(import.meta.url);
const runtime = fileURLToPath(new URL("../../Biomind_v1/integrations/pi_runtime/", import.meta.url));
const ts = require(process.env.BIOMIND_TEST_TYPESCRIPT || path.join(runtime, "node_modules/typescript/lib/typescript.js"));
function load(file, mocks = {}) {
  const compiled = ts.transpileModule(readFileSync(new URL(file, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  });
  const loadedModule = { exports: {} };
  new Function("require", "module", "exports", compiled.outputText)(name => Object.hasOwn(mocks, name) ? mocks[name] : require(name), loadedModule, loadedModule.exports);
  return loadedModule.exports;
}

test("actual search route and native helper are bounded/read-only for v1/v3/Main/summary/branch/corrupt samples", async t => {
  const root = mkdtempSync(path.join(tmpdir(), "biomind-s1-search-"));
  const project = path.join(root, "project"); const outside = path.join(root, "outside"); const sessions = path.join(root, "sessions");
  for (const dir of [project, outside, sessions]) mkdirSync(dir);
  const beforeEnv = { ...process.env };
  const guardLog = path.join(root, "guard.jsonl");
  const guard = path.join(root, "readonly-guard.mjs");
  // Runs in each real history subprocess before the pinned Pi module loads.
  // All filesystem mutation APIs and socket creation fail closed. Fixture setup
  // occurs in the parent before this guard is installed in child NODE_OPTIONS.
  writeFileSync(guard, `import fs from 'node:fs'; import fsp from 'node:fs/promises'; import net from 'node:net'; import {syncBuiltinESMExports} from 'node:module';
const log=${JSON.stringify(guardLog)};const logFd=fs.openSync(log,'a');const logWrite=fs.writeSync.bind(fs);const record=(_path,text)=>logWrite(logFd,text);
const deny=(name)=>(...args)=>{record(log,JSON.stringify({type:'denied',name})+'\\n');throw new Error('READONLY_GUARD_'+name)};
for(const name of ['writeFile','appendFile','truncate','ftruncate','rename','unlink','mkdir','rm','rmdir','symlink','link','copyFile','cp','createWriteStream']){if(fs[name])fs[name]=deny(name);if(fs[name+'Sync'])fs[name+'Sync']=deny(name+'Sync');if(fsp[name])fsp[name]=deny('promises.'+name)}
for(const name of ['write','writeSync','writev','writevSync']){const original=fs[name].bind(fs);fs[name]=(...args)=>args[0]===1||args[0]===2?original(...args):deny(name)(...args)}
for(const target of [fs,fsp])for(const name of target===fs?['open','openSync']:['open']){const original=target[name].bind(target);target[name]=(...args)=>{if(args[1]!=='r'&&args[1]!==0) return deny(name)(...args);return original(...args)}}
net.connect=deny('network.connect');net.createConnection=deny('network.createConnection');syncBuiltinESMExports();record(log,JSON.stringify({type:'guard_started'})+'\\n');
`);
  Object.assign(process.env, { PI_STUDIO_AGENT_PROFILE: "biomind", BIOMIND_PROJECT_ROOTS_JSON: JSON.stringify([project]),
    BIOMIND_SESSION_ROOT: sessions, BIOMIND_HISTORY_NODE: process.execPath,
    BIOMIND_HISTORY_QUERY_SCRIPT: path.join(runtime, "dist/history-query.js"),
    NODE_OPTIONS: `--import=${guard}`, PI_OFFLINE: "1", HOME: root, PI_CODING_AGENT_DIR: path.join(root, "agent") });
  const now = "2026-10-06T00:00:00.000Z";
  const header = (id, version = 3, cwd = project) => ({ type: "session", version, id, cwd, timestamp: now });
  const user = (id, parentId, text) => ({ type: "message", id, parentId, timestamp: now, message: { role: "user", content: text, timestamp: 1 } });
  const samples = {
    "v1.jsonl": [header("v1", 1), { type: "message", message: { role: "user", content: "needle legacy", timestamp: 1 } }],
    "v3.jsonl": [header("v3"), user("v3-user", null, "needle v3")],
    "main.jsonl": [header("main"), { type: "custom", id: "main-state", parentId: null, customType: "biomind-main", data: { phase: "Main" }, timestamp: now }, user("main-user", "main-state", "needle Main")],
    "branch-summary.jsonl": [header("branch"), user("root", null, "root context"), user("discarded", "root", "needle discarded branch"),
      user("chosen", "root", "needle chosen branch"), { type: "compaction", id: "summary", parentId: "chosen", summary: "needle compact summary", firstKeptEntryId: "chosen", tokensBefore: 1000, timestamp: now }, user("after", "summary", "needle after summary")],
    "unknown.jsonl": [header("unknown", 99), user("unknown-user", null, "needle unknown")],
    "foreign.jsonl": [header("foreign", 3, outside), user("foreign-user", null, "needle forbidden")],
  };
  for (const [name, rows] of Object.entries(samples)) writeFileSync(path.join(sessions, name), rows.map(row => JSON.stringify(row)).join("\n") + "\n");
  writeFileSync(path.join(sessions, "corrupt.jsonl"), JSON.stringify(header("corrupt")) + "\n" + JSON.stringify(user("corrupt-user", null, "needle corrupt")) + "\n{broken\n");
  writeFileSync(path.join(sessions, "unreadable-header.jsonl"), "not a session header\n{broken\n");
  const hashes = () => Object.fromEntries(readdirSync(sessions).sort().map(name => [name, createHash("sha256").update(readFileSync(path.join(sessions, name))).digest("hex")]));
  const before = hashes();
  let writerCalls = 0;
  const forbidden = () => { writerCalls++; throw new Error("ORDINARY_SDK_MUST_NOT_RUN"); };
  const sdk = { SessionManager: { open: forbidden, create: forbidden, listAll: forbidden }, buildContextEntries: forbidden, buildSessionContext: forbidden, getAgentDir: forbidden };
  const security = load("./remote-security.ts");
  const history = load("./biomind-history.ts", { "./remote-security": security });
  const reader = load("./session-reader.ts", {
    "@earendil-works/pi-coding-agent": sdk, "./remote-security": security, "./biomind-history": history,
    "./normalize": load("./normalize.ts"), "./session-path": load("./session-path.ts"),
    "./worktree": { resolveProject: forbidden }, "./team/store": { readTeamsIndex: forbidden, TeamStore: {} },
  });
  const route = load("../app/api/sessions/search/route.ts", {
    "next/server": { NextResponse: { json: Response.json } }, "@earendil-works/pi-coding-agent": sdk,
    "@/lib/session-reader": reader, "@/lib/message-search": load("./message-search.ts"),
    "@/lib/biomind-history": history, "@/lib/remote-security": security,
  });
  try {
    const response = await route.GET(new Request("http://localhost/api/sessions/search?q=needle"));
    const result = await response.json();
    if (response.status !== 200) {
      try { execFileSync(process.execPath, [process.env.BIOMIND_HISTORY_QUERY_SCRIPT, "--session-root", sessions, "--project-roots-json", JSON.stringify([project])], { encoding: "utf8" }); }
      catch (error) { t.diagnostic(JSON.stringify({ helperError: error.message, helperCode: error.code, helperStatus: error.status, helperStderr: String(error.stderr ?? "") })); }
    }
    assert.equal(response.status, 200, JSON.stringify(result));
    assert.equal(result.readOnly, true); assert.equal(result.truncated, true);
    assert(result.matches.some(match => match.sessionId === "v3"));
    assert(result.matches.some(match => match.sessionId === "main"));
    assert(result.matches.some(match => match.entryId === "summary"));
    assert(result.matches.some(match => match.entryId === "chosen"));
    assert(!result.matches.some(match => match.entryId === "discarded" || match.sessionId === "foreign"));
    for (const sessionId of ["v1", "unknown"]) assert(result.skipped.some(row => row.sessionId === sessionId && row.code === "HISTORY_FORMAT_UNSUPPORTED"), JSON.stringify(result));
    assert(result.skipped.some(row => row.sessionId === "corrupt" && row.code === "HISTORY_SESSION_INVALID"));
    assert.deepEqual(hashes(), before); assert.equal(writerCalls, 0);
    const guarded = readFileSync(guardLog, "utf8").trim().split("\n").map(line => JSON.parse(line));
    assert(guarded.length >= 7); assert(guarded.every(row => row.type === "guard_started"));
    const evidence = { classification: "TRANSPORT_REAL_READONLY_HELPER", before, after: hashes(), ordinarySdkWriterCalls: writerCalls, guardedHistoryProcesses: guarded.length, result };
    t.diagnostic(JSON.stringify(evidence));
    if (process.env.BIOMIND_S1_EVIDENCE_DIR) writeFileSync(path.join(process.env.BIOMIND_S1_EVIDENCE_DIR, "search-hashes-and-response.json"), JSON.stringify(evidence, null, 2) + "\n");
    assert.equal((await route.GET(new Request("http://localhost/api/sessions/search?q=" + "x".repeat(513)))).status, 400);
    assert.deepEqual(await (await route.GET(new Request("http://localhost/api/sessions/search"))).json(), { matches: [] });
  } finally {
    for (const key of Object.keys(process.env)) if (!(key in beforeEnv)) delete process.env[key];
    Object.assign(process.env, beforeEnv); rmSync(root, { recursive: true, force: true });
  }
});
