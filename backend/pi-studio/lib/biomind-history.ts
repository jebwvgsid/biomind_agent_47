import { execFile } from "node:child_process";
import { isAbsolute } from "node:path";
import { getBioMindProjectRoots, isBioMindProjectAllowed } from "./remote-security";

export interface BioMindHistoryInfo {
  id: string; path: string; cwd: string; created: string; modified: string;
  messageCount: number; firstMessage: string; name?: string; parentSessionPath?: string;
  resumable?: boolean; resumeReason?: string;
}
declare global { var __bioMindHistoryTruncated: boolean | undefined; }
export function bioMindHistoryTruncated(): boolean { return globalThis.__bioMindHistoryTruncated === true; }

/** One bounded, read-only invocation of BioMind's pinned native history reader.
 * It neither starts a host per file nor opens history for writable ownership. */
async function queryHistory(extraArgs: string[] = []): Promise<unknown> {
  const node = process.env.BIOMIND_HISTORY_NODE;
  const script = process.env.BIOMIND_HISTORY_QUERY_SCRIPT;
  const root = process.env.BIOMIND_SESSION_ROOT;
  if (![node, script, root].every(value => typeof value === "string" && isAbsolute(value))) throw new Error("BIOMIND_HISTORY_CONFIGURATION_REQUIRED");
  const output = await new Promise<string>((resolve, reject) => {
    execFile(node!, [script!, "--session-root", root!, "--project-roots-json", JSON.stringify([...getBioMindProjectRoots()]), ...extraArgs],
      { shell: false, timeout: 60_000, maxBuffer: 4 * 1024 * 1024, encoding: "utf8", windowsHide: true },
      (error, stdout, stderr) => {
        if (!error) { resolve(stdout); return; }
        // The pinned helper returns a bounded code, never surface raw stderr or paths.
        let code = "BIOMIND_HISTORY_UNAVAILABLE";
        try {
          const parsed = JSON.parse(stderr) as { code?: unknown };
          if (typeof parsed.code === "string" && /^HISTORY_[A-Z_]{1,64}$/.test(parsed.code)) code = parsed.code;
        } catch { /* launch/timeout failures remain generic */ }
        reject(new Error(code));
      });
  });
  try { return JSON.parse(output); } catch { throw new Error("BIOMIND_HISTORY_INVALID_RESPONSE"); }
}
export async function listBioMindHistory(): Promise<BioMindHistoryInfo[]> {
  const parsed = await queryHistory() as { sessions?: BioMindHistoryInfo[]; truncated?: boolean };
  if (!Array.isArray(parsed.sessions) || parsed.sessions.length > 1000) throw new Error("BIOMIND_HISTORY_INVALID_RESPONSE");
  globalThis.__bioMindHistoryTruncated = parsed.truncated === true;
  return parsed.sessions.filter(info => typeof info?.id === "string" && typeof info.path === "string" && isAbsolute(info.path) && isBioMindProjectAllowed(info.cwd));
}

export interface BioMindHistoryProjection {
  leafId: string | null;
  contextEntryIds: string[];
  model: { provider: string; modelId: string } | null;
  thinkingLevel: string;
  note: string;
  sessionId?: string;
  project?: string;
  resumable?: boolean;
  resumeReason?: string;
}
export async function projectBioMindHistory(filePath: string): Promise<BioMindHistoryProjection> {
  const projection = await queryHistory(["--session-file", filePath]) as BioMindHistoryProjection;
  if (!Array.isArray(projection?.contextEntryIds) || projection.contextEntryIds.some(id => typeof id !== "string")) throw new Error("BIOMIND_HISTORY_INVALID_RESPONSE");
  return projection;
}
