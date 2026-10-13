/** Target-level instruction edits reuse Main's CAS helper without creating an owner. */
import { execFile } from "node:child_process";
import { realpathSync, statSync } from "node:fs";
import { isAbsolute } from "node:path";
import { getBioMindLaunchConfig } from "./biomind-profile";
import { isBioMindProjectAllowed } from "./remote-security";

const SCOPES = new Set(["global", "project", "project-base"]);
const FIELDS = new Set(["cwd", "scope", "expectedVersion", "text", "create"]);
const MAX_TEXT_BYTES = 1024 * 1024;

export class BioMindInstructionsError extends Error {
  constructor(readonly code: string, readonly rejected = true, readonly currentVersion?: string) {
    super(code);
  }
}

export interface InstructionsRequest {
  cwd: string;
  scope: "global" | "project" | "project-base";
  expectedVersion?: string;
  text?: string;
  create?: boolean;
}

export function validateInstructionsRequest(value: unknown, action: "read" | "save"): InstructionsRequest {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new BioMindInstructionsError("INSTRUCTIONS_INVALID_REQUEST");
  const input = value as Record<string, unknown>;
  if (Object.keys(input).some(key => !FIELDS.has(key)) || typeof input.cwd !== "string" || !isAbsolute(input.cwd)
      || input.cwd.includes("\0") || input.cwd.length > 4096 || typeof input.scope !== "string" || !SCOPES.has(input.scope)) {
    throw new BioMindInstructionsError("INSTRUCTIONS_INVALID_REQUEST");
  }
  if (action === "read" && ["text", "expectedVersion", "create"].some(key => key in input)) throw new BioMindInstructionsError("INSTRUCTIONS_INVALID_REQUEST");
  if (action === "save" && (typeof input.text !== "string" || Buffer.byteLength(input.text) > MAX_TEXT_BYTES
      || typeof input.expectedVersion !== "string" || input.expectedVersion.length > 1024
      || input.expectedVersion.length === 0 || (input.create !== undefined && typeof input.create !== "boolean"))) {
    throw new BioMindInstructionsError("INSTRUCTIONS_INVALID_REQUEST");
  }
  if (!isBioMindProjectAllowed(input.cwd)) throw new BioMindInstructionsError("INSTRUCTIONS_PROJECT_NOT_AUTHORIZED");
  return { cwd: realpathSync(input.cwd), scope: input.scope as InstructionsRequest["scope"],
    ...(action === "save" ? { expectedVersion: input.expectedVersion as string, text: input.text as string,
      ...(input.create !== undefined ? { create: input.create as boolean } : {}) } : {}) };
}

function operatorFile(variable: string): string {
  const configured = process.env[variable];
  if (!configured || !isAbsolute(configured)) throw new BioMindInstructionsError("INSTRUCTIONS_HELPER_NOT_CONFIGURED");
  try {
    const filename = realpathSync(configured);
    if (!statSync(filename).isFile()) throw new Error();
    return filename;
  } catch { throw new BioMindInstructionsError("INSTRUCTIONS_HELPER_NOT_CONFIGURED"); }
}

export async function targetInstructions(value: unknown, action: "read" | "save") {
  const input = validateInstructionsRequest(value, action);
  // Executable, helper and configuration are deployment-owned. The browser can
  // select a scope, never a filename, executable, environment or agent directory.
  const node = operatorFile("BIOMIND_HISTORY_NODE");
  const helper = operatorFile("BIOMIND_INSTRUCTIONS_QUERY_SCRIPT");
  const { config, serverId } = getBioMindLaunchConfig();
  const { cwd, ...request } = input;
  const output = await new Promise<string>((resolve, reject) => {
    const child = execFile(node, [helper, "--config", config, "--project", cwd],
      { cwd, shell: false, timeout: 20_000, maxBuffer: 2 * 1024 * 1024, encoding: "utf8", windowsHide: true },
      (_error, stdout) => {
        // A helper's bounded error envelope is useful even with a nonzero exit.
        if (stdout.trim()) { resolve(stdout); return; }
        reject(new BioMindInstructionsError(action === "save" ? "INSTRUCTIONS_OUTCOME_UNKNOWN" : "INSTRUCTIONS_READ_FAILED", action !== "save"));
      });
    child.stdin?.on("error", () => { /* execFile completion determines the outcome; never replay a write. */ });
    child.stdin?.end(JSON.stringify({ action, ...request }) + "\n");
  });
  let result: { status?: unknown; data?: Record<string, unknown>; code?: unknown; currentVersion?: unknown };
  try { result = JSON.parse(output); }
  catch { throw new BioMindInstructionsError("INSTRUCTIONS_INVALID_RESPONSE", action !== "save"); }
  if (result?.status === "error") {
    const code = typeof result.code === "string" && /^INSTRUCTIONS_[A-Z_]{1,80}$/.test(result.code)
      ? result.code : "INSTRUCTIONS_OPERATION_FAILED";
    throw new BioMindInstructionsError(code, true, typeof result.currentVersion === "string" ? result.currentVersion : undefined);
  }
  const data = result?.data;
  if (result?.status !== "ok" || !data || typeof data !== "object" || Array.isArray(data)
      || data.scope !== input.scope || typeof data.text !== "string" || Buffer.byteLength(data.text) > MAX_TEXT_BYTES
      || typeof data.version !== "string" || typeof data.hash !== "string" || typeof data.path !== "string"
      || typeof data.exists !== "boolean" || data.ownerLoaded !== false || data.loaded !== null) {
    throw new BioMindInstructionsError("INSTRUCTIONS_INVALID_RESPONSE", action !== "save");
  }
  return { ...data, serverId, project: cwd };
}

export function instructionsErrorResponse(error: unknown) {
  const known = error instanceof BioMindInstructionsError;
  const code = known ? error.code : "INSTRUCTIONS_OPERATION_FAILED";
  return { error: code, code, rejected: known ? error.rejected : false,
    ...(known && error.currentVersion !== undefined ? { currentVersion: error.currentVersion } : {}) };
}
