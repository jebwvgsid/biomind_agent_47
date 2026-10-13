"use strict";
const fs = require("node:fs"), path = require("node:path");
const CODES = new Set(["RENDER_FAILED", "WINDOW_ERROR", "UNHANDLED_REJECTION", "BOOTSTRAP_FAILED", "PROJECT_DATA_INVALID", "REMOTE_DIRECTORY_INVALID", "LOCAL_UI_STATE_UNSAVED", "SESSION_RESTORE_FAILED", "PROJECT_OPEN_FAILED", "CONNECTION_VIEW_FAILED", "WORKBENCH_RENDERER_STOPPED", "WORKBENCH_LOAD_FAILED", "WORKBENCH_UNRESPONSIVE", "UI_STATE_SAVE_FAILED", "UI_STATE_READ_FAILED", "UI_STATE_RECOVERED", "UI_FLUSH_TIMEOUT", "EPERM", "EACCES", "EBUSY", "ENOSPC", "EROFS"]);
const NAMES = new Set(["Error", "TypeError", "RangeError", "ReferenceError", "SyntaxError", "URIError", "EvalError", "AggregateError"]);
function sanitize(issue = {}) {
  const source = ["render", "window_error", "unhandledrejection", "main", "storage"].includes(issue.source) ? issue.source : "main";
  // Never write error.message, paths, URLs, prompts or credentials. Only accept
  // generated bundle basenames and numeric locations from bounded stack input.
  const frames = typeof issue.stack === "string" ? [...issue.stack.slice(0, 8192).matchAll(/^\s*at\s+(?:[^\n]*[ /\\(])?((?:index(?:-[A-Za-z0-9_-]{6,32})?|DocumentPreview-[A-Za-z0-9_-]{6,32}|SpreadsheetPreview-[A-Za-z0-9_-]{6,32}|jszip\.min-[A-Za-z0-9_-]{6,32})\.js):(\d{1,7}):(\d{1,7})\)?\s*$/gm)].slice(0, 8).map(m => `${m[1]}:${m[2]}:${m[3]}`) : [];
  return { source, code: CODES.has(issue.code) ? issue.code : "RENDER_FAILED", name: NAMES.has(issue.name) ? issue.name : "Error", frames };
}
function createDiagnostics(directory, version) {
  let entries = 0;
  return issue => {
    if (++entries > 200) return false;
    try {
      const file = path.join(directory, "diagnostics.jsonl");
      fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
      if (fs.existsSync(file) && fs.statSync(file).size > 128 * 1024) return false;
      fs.appendFileSync(file, JSON.stringify({ at: new Date().toISOString(), version, ...sanitize(issue) }) + "\n", { mode: 0o600 });
      return true;
    } catch { return false; }
  };
}
module.exports = { createDiagnostics, sanitize };
