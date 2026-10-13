import { realpathSync, statSync } from "node:fs";
import { isAbsolute } from "node:path";
import { BioMindTransportError } from "./biomind-session-adapter";
import { getBioMindProjectRoots, isBioMindRemoteProfile } from "./remote-security";

export { isBioMindRemoteProfile };

function serverFile(name: string): string {
  const value = process.env[name];
  if (!value || !isAbsolute(value)) throw new BioMindTransportError("BIOMIND_SERVER_CONFIGURATION_REQUIRED", true);
  try {
    const actual = realpathSync(value);
    if (!statSync(actual).isFile()) throw new Error();
    return actual;
  } catch { throw new BioMindTransportError("BIOMIND_SERVER_CONFIGURATION_INVALID", true); }
}
export function getBioMindServerId(): string {
  // A deployment-unique non-secret ID is required. Identical filesystem layouts
  // on two servers must never produce the same inferred connection identity.
  const identity = process.env.BIOMIND_SERVER_ID;
  if (!identity || !/^[A-Za-z0-9._:-]{1,128}$/.test(identity)) throw new BioMindTransportError("BIOMIND_SERVER_ID_REQUIRED", true);
  return identity;
}
export function getBioMindLaunchConfig() {
  return { python: serverFile("BIOMIND_LAUNCHER_PYTHON"), entrypoint: serverFile("BIOMIND_ENTRYPOINT"), config: serverFile("BIOMIND_RUNTIME_CONFIG"),
    pipeLauncher: serverFile("BIOMIND_PIPE_LAUNCHER"), serverId: getBioMindServerId() };
}
export function getAgentProfile() {
  if (!isBioMindRemoteProfile()) return { profile: "pi", serverId: "local-pi" };
  getBioMindLaunchConfig(); // fail closed before enabling any UI send control
  return { profile: "biomind", serverId: getBioMindServerId(), protocolVersion: 1,
    allowedProjects: [...getBioMindProjectRoots()],
    capabilities: { getState: true, images: true, imageInputEnabled: false, modelCapabilitySource: "owner_ready",
      packages: { supported: true, available: true, scope: ["user", "project"], protocol: "BioMindPluginsV1", runtimeVersion: "0.87.1" },
      newSession: true, reconnectOwnedSession: true, historyReadOnly: true, historyAvailable: Boolean(process.env.BIOMIND_HISTORY_NODE && process.env.BIOMIND_HISTORY_QUERY_SCRIPT && process.env.BIOMIND_SESSION_ROOT),
      automaticReplay: false, clearQueue: false,
      ...Object.fromEntries(['getModels', 'setModel', 'setThinking', 'getTools', 'setTools', 'getCommands',
        'instructions', 'promptSnapshot', 'resources', 'setSkill', 'autoCompaction', 'memorySettings'].map(name =>
        [name, { supported: true, available: false, reason: 'OWNER_NOT_LOADED', scope: 'session' }])),
      planTools: false, team: false, bash: false, autoName: false, historyExport: false,
      reload: true, compact: true, settings: ["history_enabled", "memory_mode"], jobs: true,
      desktopDetach: true, transparentProcessRecovery: false } };
}

/** Bound bytes before JSON parsing; a content-length header is not authoritative. */
export async function readBioMindRequest(req: Request): Promise<Record<string, unknown>> {
  const limit = 10 * 1024 * 1024;
  const reader = req.body?.getReader();
  if (!reader) throw new BioMindTransportError("INVALID_JSON_BODY", true);
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      bytes += part.value.byteLength;
      if (bytes > limit) { await reader.cancel(); throw new BioMindTransportError("HTTP_BODY_TOO_LARGE", true); }
      chunks.push(part.value);
    }
    const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
    return parsed;
  } catch (error) {
    if (error instanceof BioMindTransportError) throw error;
    throw new BioMindTransportError("INVALID_JSON_BODY", true);
  } finally { reader.releaseLock(); }
}
