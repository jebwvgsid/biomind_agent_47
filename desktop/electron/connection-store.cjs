"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const RETRYABLE_RENAME = new Set(["EPERM", "EACCES", "EBUSY"]);
const RENAME_DELAYS = [25, 75, 150, 300, 500];
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const temporaryPath = file => path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.${randomUUID()}.tmp`);
const record = value => !!value && typeof value === "object" && !Array.isArray(value);
const nativeId = value => typeof value === "string" && /^[A-Za-z0-9._:-]{1,256}$/.test(value);
const linuxPath = value => typeof value === "string" && value.startsWith("/") && !/[\0\r\n\\]/.test(value);
function validUI(value) {
  if (!record(value)) return false;
  if (value.openThreads !== undefined && (!Array.isArray(value.openThreads) || !value.openThreads.every(row => record(row) && linuxPath(row.cwd) && nativeId(row.sessionFile)))) return false;
  if (value.lastThreadId !== undefined && value.lastThreadId !== null && !nativeId(value.lastThreadId)) return false;
  const config = value.config;
  if (config === undefined) return true;
  if (!record(config)) return false;
  for (const key of ["pinnedProjects", "pinnedThreads", "archivedProjects"]) {
    if (config[key] !== undefined && (!Array.isArray(config[key]) || !config[key].every(item => typeof item === "string"))) return false;
  }
  if (config.archivedThreads !== undefined && (!Array.isArray(config.archivedThreads) || !config.archivedThreads.every(row => record(row) &&
    typeof row.file === "string" && row.file.length > 0 && typeof row.cwd === "string" && row.cwd.length > 0 && typeof row.title === "string"))) return false;
  if (config.theme !== undefined && !["system", "light", "dark"].includes(config.theme)) return false;
  if (config.language !== undefined && !["zh", "en"].includes(config.language)) return false;
  const bounds = config.windowBounds;
  if (bounds !== undefined) {
    if (!record(bounds)) return false;
    for (const key of ["x", "y", "width", "height"]) if (bounds[key] !== undefined && (!Number.isFinite(bounds[key]) || ["width", "height"].includes(key) && bounds[key] <= 0)) return false;
    if (bounds.maximized !== undefined && typeof bounds.maximized !== "boolean") return false;
  }
  return true;
}

// Critical credential/admission writes retain their synchronous fail-closed
// contract. Never remove a valid destination to work around a Windows lock.
function atomicJSON(file, value, options = {}) {
  const io = options.fs || fs, serialized = JSON.stringify(value, null, 2);
  io.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = temporaryPath(file);
  let fd, created = false;
  try {
    fd = io.openSync(tmp, "wx", 0o600); created = true;
    try { io.writeFileSync(fd, serialized); io.fsyncSync(fd); }
    finally { io.closeSync(fd); fd = undefined; }
    io.renameSync(tmp, file);
  } finally {
    if (fd !== undefined) try { io.closeSync(fd); } catch {}
    if (created) try { io.unlinkSync(tmp); } catch { /* Only this writer's temporary file. */ }
  }
}
async function atomicJSONAsync(file, value, options = {}) {
  const io = (options.fs || fs).promises, serialized = JSON.stringify(value, null, 2);
  const delays = options.retryDelays || RENAME_DELAYS, pause = options.delay || delay;
  const tmp = temporaryPath(file);
  await io.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  let handle, created = false;
  try {
    handle = await io.open(tmp, "wx", 0o600); created = true;
    await handle.writeFile(serialized, "utf8"); await handle.sync(); await handle.close(); handle = null;
    for (let attempt = 0; ; attempt++) {
      try { await io.rename(tmp, file); break; }
      catch (error) {
        if (!RETRYABLE_RENAME.has(error.code) || attempt >= delays.length) throw error;
        // Yield to Electron between attempts; no synchronous sleep or delete.
        await pause(delays[attempt]);
      }
    }
  } finally {
    if (handle) try { await handle.close(); } catch {}
    if (created) try { await io.unlink(tmp); } catch { /* Never unlink the destination. */ }
  }
}
function readJSON(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, "utf8")); }
  catch (e) { if (e.code === "ENOENT") return fallback; throw new Error("LOCAL_SETTINGS_INVALID"); }
}

/** Only connection/UI preferences and input receipts live here. Session history
 * and BioMind settings remain on the server. Passwords use OS safeStorage. */
class ConnectionStore {
  constructor(directory, safeStorage, validateProfile, options = {}) {
    this.directory = directory; this.safeStorage = safeStorage; this.validateProfile = validateProfile;
    this.file = path.join(directory, "connections-v3.json");
    this.options = options; this.uiCache = new Map(); this.uiWrites = new Map(); this.uiVersions = new Map();
    this.uiErrors = new Map(); this.uiReadBlocked = new Set(); this.uiPersistedVersions = new Map();
    this.onUIWriteError = options.onUIWriteError; this.onUIWriteRecovered = options.onUIWriteRecovered;
  }
  data() { return readJSON(this.file, { profiles: [], secrets: {}, lastId: null }); }
  encryptionAvailable() {
    return this.safeStorage.isEncryptionAvailable() && this.safeStorage.getSelectedStorageBackend?.() !== "basic_text";
  }
  list() { return this.data().profiles.map(p => this.validateProfile(p)); }
  save(profile, password, remember) {
    const p = this.validateProfile(profile), data = this.data();
    data.profiles = [p, ...data.profiles.filter(x => x.id !== p.id)].slice(0, 20);
    if (remember === true) {
      if (password) {
        if (!this.encryptionAvailable()) throw new Error("SECURE_STORAGE_UNAVAILABLE");
        data.secrets[p.id] = this.safeStorage.encryptString(password).toString("base64");
      } else if (!data.secrets[p.id]) throw new Error("INVALID_SERVER_PASSWORD");
    } else if (remember === false) delete data.secrets[p.id];
    data.lastId = p.id;
    data.secrets = Object.fromEntries(Object.entries(data.secrets).filter(([id]) => data.profiles.some(x => x.id === id)));
    atomicJSON(this.file, data, this.options); return p;
  }
  password(id) {
    const encoded = this.data().secrets[id];
    if (!encoded) return "";
    if (!this.encryptionAvailable()) throw new Error("SECURE_STORAGE_UNAVAILABLE");
    try { return this.safeStorage.decryptString(Buffer.from(encoded, "base64")); }
    catch { throw new Error("SAVED_CREDENTIAL_UNAVAILABLE"); }
  }
  hasPassword(id) { return Boolean(this.data().secrets[id]); }
  clear(id, remove = false) {
    const data = this.data(); delete data.secrets[id];
    if (remove) { data.profiles = data.profiles.filter(x => x.id !== id); if (data.lastId === id) data.lastId = null; }
    atomicJSON(this.file, data, this.options);
  }
  last() { const d = this.data(); return this.list().find(p => p.id === d.lastId); }
  ui(id) {
    if (!/^[a-f0-9]{24}$/.test(id)) throw new Error("INVALID_CONNECTION_ID");
    if (!this.uiCache.has(id)) {
      try {
        const value = readJSON(path.join(this.directory, `ui-${id}.json`), {});
        if (!validUI(value)) throw new Error("LOCAL_SETTINGS_INVALID");
        this.uiCache.set(id, value);
      } catch {
        // Keep an unreadable/corrupt file untouched. Native server sessions can
        // still be selected in this process, with an explicit persistence warning.
        this.uiCache.set(id, {}); this.uiReadBlocked.add(id); this.uiErrors.set(id, "UI_STATE_READ_FAILED");
        try { this.onUIWriteError?.({ id, code: "UI_STATE_READ_FAILED" }); } catch {}
      }
    }
    return JSON.parse(JSON.stringify(this.uiCache.get(id)));
  }
  saveUI(id, value) {
    if (!/^[a-f0-9]{24}$/.test(id)) throw new Error("INVALID_CONNECTION_ID");
    if (!this.uiCache.has(id)) this.ui(id);
    const snapshot = JSON.parse(JSON.stringify(value));
    if (!validUI(snapshot)) throw new Error("INVALID_UI_STATE");
    this.uiCache.set(id, snapshot);
    const version = (this.uiVersions.get(id) || 0) + 1; this.uiVersions.set(id, version);
    const previous = this.uiWrites.get(id) || Promise.resolve();
    const write = previous.catch(() => {}).then(async () => {
      // A later UI snapshot already includes the synchronously cached changes.
      // Skip obsolete queued snapshots; never let them overwrite the latest one.
      if (version !== this.uiVersions.get(id)) return;
      if (this.uiReadBlocked.has(id)) throw Object.assign(new Error("UI_STATE_READ_FAILED"), { code: "UI_STATE_READ_FAILED" });
      await atomicJSONAsync(path.join(this.directory, `ui-${id}.json`), snapshot, this.options);
      this.uiPersistedVersions.set(id, version);
      if (version === this.uiVersions.get(id) && this.uiErrors.delete(id)) {
        try { this.onUIWriteRecovered?.({ id }); } catch {}
      }
    });
    this.uiWrites.set(id, write);
    // Existing synchronous callers can continue using the authoritative native
    // session while a noncritical UI preference waits for disk availability.
    // Explicit await still observes failure; ignored promises are handled here.
    void write.catch(error => {
      if (version === this.uiVersions.get(id)) {
        const code = error.code || "UI_STATE_SAVE_FAILED"; this.uiErrors.set(id, code);
        try { this.onUIWriteError?.({ id, code }); } catch {}
      }
    });
    return write;
  }
  uiSaveStatus(id) {
    return { pending: (this.uiVersions.get(id) || 0) > (this.uiPersistedVersions.get(id) || 0),
      readBlocked: this.uiReadBlocked.has(id), ...(this.uiErrors.has(id) ? { errorCode: this.uiErrors.get(id) } : {}) };
  }
  async flushUI() {
    for (;;) {
      const writes = [...this.uiWrites.entries()], results = await Promise.allSettled(writes.map(([, write]) => write));
      if (writes.length === this.uiWrites.size && writes.every(([id, write]) => this.uiWrites.get(id) === write)) return results.every(result => result.status === "fulfilled");
    }
  }
}

/** Storage-compatible durable admission receipts reused by biomind-client.ts.
 * No prompt text, attachment bytes, transcript or memory is stored here. */
class ReceiptStorage {
  constructor(file, options = {}) { this.file = file; this.options = options; }
  data() { return readJSON(this.file, {}); }
  get length() { return Object.keys(this.data()).length; }
  key(i) { return Object.keys(this.data())[i] ?? null; }
  getItem(k) { return this.data()[k] ?? null; }
  setItem(k, v) { const d = this.data(); d[k] = String(v); atomicJSON(this.file, d, this.options); }
  removeItem(k) { const d = this.data(); delete d[k]; atomicJSON(this.file, d, this.options); }
  clear() { atomicJSON(this.file, {}, this.options); }
}
module.exports = { ConnectionStore, ReceiptStorage, atomicJSON, atomicJSONAsync, readJSON };
