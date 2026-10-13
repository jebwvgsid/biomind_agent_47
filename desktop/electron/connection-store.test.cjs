"use strict";
// Fault-injected Windows error codes on real temporary files; not a Windows run.
const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const { ConnectionStore, ReceiptStorage, atomicJSON, atomicJSONAsync } = require("./connection-store.cjs");
const id = "1".repeat(24), otherId = "2".repeat(24);
const failed = code => Object.assign(new Error(code), { code });
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
function directory(t) { const value = fs.mkdtempSync(path.join(os.tmpdir(), "biomind-storage-lock-")); t.after(() => fs.rmSync(value, { recursive: true, force: true })); return value; }
function io(rename, renameSync = fs.renameSync) { return { ...fs, renameSync, promises: { ...fs.promises, rename } }; }
function store(t, options = {}) {
  return new ConnectionStore(directory(t), { isEncryptionAvailable: () => true, encryptString: () => Buffer.from("synthetic-ciphertext") }, value => ({ ...value, id }), options);
}
const tempFiles = dir => fs.readdirSync(dir).filter(name => name.endsWith(".tmp"));

test("transient Windows rename locks retry asynchronously with handles closed and preserve the last good file", async t => {
  const dir = directory(t), file = path.join(dir, "ui.json"); atomicJSON(file, { value: "old" });
  const waits = [], tempNames = []; let attempts = 0, heartbeat = false;
  const errors = ["EPERM", "EACCES", "EBUSY"];
  const options = { fs: io(async (from, to) => {
    tempNames.push(from); attempts++;
    if (attempts <= errors.length) {
      assert.deepEqual(JSON.parse(fs.readFileSync(file)), { value: "old" });
      // The writer's handle has already closed before it attempts replacement.
      const fd = fs.openSync(from, "r+"); fs.closeSync(fd);
      throw failed(errors[attempts - 1]);
    }
    await fs.promises.rename(from, to);
  }), retryDelays: [1, 2, 3], delay: async ms => { waits.push(ms); await new Promise(resolve => setTimeout(() => { heartbeat = true; resolve(); }, 1)); } };
  await atomicJSONAsync(file, { value: "new" }, options);
  assert.equal(attempts, 4); assert.equal(heartbeat, true); assert.deepEqual(waits, [1, 2, 3]);
  assert.equal(new Set(tempNames).size, 1, "one independent temp is retained during this writer's retries");
  assert.deepEqual(JSON.parse(fs.readFileSync(file)), { value: "new" }); assert.deepEqual(tempFiles(dir), []);
});
test("persistent lock preserves byte-for-byte good data, rejects after bounded attempts and cleans only its own temp", async t => {
  const dir = directory(t), file = path.join(dir, "ui.json"), old = '{"value":"good"}\n'; fs.writeFileSync(file, old);
  const unrelated = file + ".tmp"; fs.writeFileSync(unrelated, "previous unrelated temporary");
  let attempts = 0;
  await assert.rejects(atomicJSONAsync(file, { value: "new" }, { fs: io(async () => { attempts++; throw failed("EPERM"); }), retryDelays: [0, 0], delay: async () => {} }), { code: "EPERM" });
  assert.equal(attempts, 3); assert.equal(fs.readFileSync(file, "utf8"), old);
  assert.deepEqual(tempFiles(dir), [path.basename(unrelated)]); assert.equal(fs.readFileSync(unrelated, "utf8"), "previous unrelated temporary");
});
test("non-lock filesystem errors fail immediately without a destructive fallback", async t => {
  const dir = directory(t), file = path.join(dir, "ui.json"); atomicJSON(file, { intact: true }); let attempts = 0, waits = 0;
  await assert.rejects(atomicJSONAsync(file, { intact: false }, { fs: io(async () => { attempts++; throw failed("ENOSPC"); }), delay: async () => { waits++; } }), { code: "ENOSPC" });
  assert.equal(attempts, 1); assert.equal(waits, 0); assert.equal(JSON.parse(fs.readFileSync(file)).intact, true); assert.deepEqual(tempFiles(dir), []);
});
test("a temporary file that this writer failed to create is never removed", async t => {
  const dir = directory(t), file = path.join(dir, "ui.json"); atomicJSON(file, { intact: true }); let foreign;
  const options = { fs: { ...fs, promises: { ...fs.promises, open: async name => { foreign = name; fs.writeFileSync(name, "not-owned-by-this-writer"); throw failed("EEXIST"); } } } };
  await assert.rejects(atomicJSONAsync(file, { intact: false }, options), { code: "EEXIST" });
  assert.equal(fs.readFileSync(foreign, "utf8"), "not-owned-by-this-writer"); assert.equal(JSON.parse(fs.readFileSync(file)).intact, true);
});
test("UI cache retains A/B native references immediately and serial writes cannot lose the latest focus during a lock", async t => {
  const entered = deferred(), release = deferred(), names = []; let attempts = 0;
  const current = store(t, { fs: io(async (from, to) => {
    names.push(from); attempts++;
    if (attempts === 1) { entered.resolve(); throw failed("EPERM"); }
    return fs.promises.rename(from, to);
  }), retryDelays: [0], delay: () => release.promise });
  const first = current.saveUI(id, { openThreads: [{ cwd: "/project-A", sessionFile: "A" }], lastThreadId: "A" });
  await entered.promise;
  const b = current.ui(id); b.openThreads.push({ cwd: "/project-B", sessionFile: "B" }); current.saveUI(id, b);
  const focused = current.ui(id); focused.lastThreadId = "B"; const last = current.saveUI(id, focused);
  b.openThreads.length = 0; focused.lastThreadId = "mutated-caller-object";
  assert.deepEqual(current.ui(id).openThreads.map(row => row.sessionFile), ["A", "B"]); assert.equal(current.ui(id).lastThreadId, "B");
  const flush = current.flushUI(); release.resolve(); await first; await last; assert.equal(await flush, true);
  const disk = JSON.parse(fs.readFileSync(path.join(current.directory, `ui-${id}.json`)));
  assert.deepEqual(disk.openThreads.map(row => row.sessionFile), ["A", "B"]); assert.equal(disk.lastThreadId, "B");
  assert.equal(new Set(names).size, 2); assert.deepEqual(tempFiles(current.directory), []); assert.equal(current.uiSaveStatus(id).pending, false);
});
test("one locked connection does not block persistence for another connection", async t => {
  const entered = deferred(), release = deferred(); let blocked = false;
  const current = store(t, { fs: io(async (from, to) => {
    if (to.includes(id) && !blocked) { blocked = true; entered.resolve(); throw failed("EBUSY"); }
    return fs.promises.rename(from, to);
  }), retryDelays: [0], delay: () => release.promise });
  const first = current.saveUI(id, { lastThreadId: "A" }); await entered.promise;
  await current.saveUI(otherId, { lastThreadId: "other" });
  assert.equal(JSON.parse(fs.readFileSync(path.join(current.directory, `ui-${otherId}.json`))).lastThreadId, "other");
  release.resolve(); await first;
});
test("persistent UI failure is reported but native refs stay usable; a later successful save reports recovery", async t => {
  const errors = [], recovered = []; let locked = true;
  const current = store(t, { fs: io(async (from, to) => { if (locked) throw failed("EACCES"); return fs.promises.rename(from, to); }), retryDelays: [],
    onUIWriteError: value => errors.push(value), onUIWriteRecovered: value => recovered.push(value) });
  atomicJSON(path.join(current.directory, `ui-${id}.json`), { lastThreadId: "old", openThreads: [{ sessionFile: "old", cwd: "/old" }] });
  const reference = { lastThreadId: "native-A", openThreads: [{ sessionFile: "native-A", cwd: "/A" }] };
  await assert.rejects(current.saveUI(id, reference), { code: "EACCES" });
  assert.equal(await current.flushUI(), false); assert.equal(current.ui(id).lastThreadId, "native-A");
  assert.equal(JSON.parse(fs.readFileSync(path.join(current.directory, `ui-${id}.json`))).lastThreadId, "old");
  assert.deepEqual(errors, [{ id, code: "EACCES" }]); assert.equal(current.uiSaveStatus(id).errorCode, "EACCES");
  locked = false; await current.saveUI(id, current.ui(id)); assert.equal(await current.flushUI(), true);
  assert.deepEqual(recovered, [{ id }]); assert.deepEqual(current.uiSaveStatus(id), { pending: false, readBlocked: false });
});
test("an unreadable UI JSON is preserved, warns, and permits RAM session references without silently overwriting the file", async t => {
  const errors = [], current = store(t, { onUIWriteError: value => errors.push(value) }), file = path.join(current.directory, `ui-${id}.json`);
  fs.writeFileSync(file, "{damaged-but-must-preserve"); assert.deepEqual(current.ui(id), {});
  await assert.rejects(current.saveUI(id, { lastThreadId: "native-A", openThreads: [{ sessionFile: "native-A", cwd: "/A" }] }), { code: "UI_STATE_READ_FAILED" });
  assert.equal(current.ui(id).lastThreadId, "native-A"); assert.equal(fs.readFileSync(file, "utf8"), "{damaged-but-must-preserve");
  assert.equal(current.uiSaveStatus(id).readBlocked, true); assert(errors.every(value => value.code === "UI_STATE_READ_FAILED")); assert.deepEqual(tempFiles(current.directory), []);
});
test("malformed restoration and renderer config shapes cannot poison memory or overwrite their original UI file", async t => {
  for (const malformed of [{ openThreads: {} }, { openThreads: [null] }, { openThreads: [{ cwd: "C:\\local", sessionFile: "native-A" }] },
    { lastThreadId: { id: "A" } }, { config: { pinnedProjects: {} } }, { config: { windowBounds: { width: "1440", height: 900 } } }]) {
    const current = store(t), file = path.join(current.directory, `ui-${id}.json`), original = JSON.stringify(malformed);
    fs.writeFileSync(file, original); assert.deepEqual(current.ui(id), {}); assert.equal(current.uiSaveStatus(id).errorCode, "UI_STATE_READ_FAILED");
    await assert.rejects(current.saveUI(id, { openThreads: [{ cwd: "/A", sessionFile: "native-A" }], lastThreadId: "native-A" }), { code: "UI_STATE_READ_FAILED" });
    assert.equal(fs.readFileSync(file, "utf8"), original); assert.equal(current.ui(id).lastThreadId, "native-A");
  }
});
test("valid archived thread objects and negative monitor coordinates remain compatible; invalid new snapshots are rejected before caching", async t => {
  const current = store(t), value = { openThreads: [{ cwd: "/projects/中文 A", sessionFile: "native-A" }], lastThreadId: null,
    config: { archivedThreads: [{ file: "native-old", cwd: "/projects/old", title: "Archived history" }], pinnedThreads: ["native-A"],
      windowBounds: { x: -1600, y: -50, width: 1440, height: 900, maximized: false }, theme: "system", language: "zh" }, futureMetadata: { ignored: true } };
  await current.saveUI(id, value); const reopened = new ConnectionStore(current.directory, {}, value => value);
  assert.deepEqual(reopened.ui(id), value); assert.equal(reopened.uiSaveStatus(id).readBlocked, false);
  assert.throws(() => current.saveUI(id, { openThreads: [null] }), /INVALID_UI_STATE/); assert.deepEqual(current.ui(id), value);
});
test("credential save failure stays synchronous and preserves the previous encrypted configuration", t => {
  let locked = false;
  const current = store(t, { fs: io(fs.promises.rename, (from, to) => { if (locked) throw failed("EPERM"); return fs.renameSync(from, to); }) });
  current.save({ label: "original" }, "synthetic-password", true); const previous = fs.readFileSync(current.file, "utf8");
  locked = true; assert.throws(() => current.save({ label: "replacement" }, "other-password", true), { code: "EPERM" });
  assert.equal(fs.readFileSync(current.file, "utf8"), previous); assert(!previous.includes("synthetic-password")); assert.deepEqual(tempFiles(current.directory), []);
});
test("receipt write/delete failures remain fail-closed and cannot erase the accepted admission receipt", t => {
  const dir = directory(t), file = path.join(dir, "receipts.json"); let locked = false;
  const receipts = new ReceiptStorage(file, { fs: io(fs.promises.rename, (from, to) => { if (locked) throw failed("EACCES"); return fs.renameSync(from, to); }) });
  receipts.setItem("native-A", JSON.stringify({ clientMessageId: "already-admitted" })); const old = fs.readFileSync(file, "utf8"); locked = true;
  assert.throws(() => receipts.removeItem("native-A"), { code: "EACCES" }); assert.throws(() => receipts.setItem("native-B", "new"), { code: "EACCES" });
  assert.equal(fs.readFileSync(file, "utf8"), old); assert.equal(JSON.parse(receipts.getItem("native-A")).clientMessageId, "already-admitted");
  assert.equal(receipts.getItem("native-B"), null); assert.deepEqual(tempFiles(dir), []);
});
