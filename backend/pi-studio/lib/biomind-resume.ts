import { realpathSync } from "node:fs";
import { resolveSessionPath, readSessionHeader } from "./session-reader";
import { projectBioMindHistory } from "./biomind-history";
import { isBioMindProjectAllowed } from "./remote-security";
import { BioMindTransportError } from "./biomind-session-adapter";

/** The browser supplies an ID; the authorized native history helper chooses the file. */
export async function inspectBioMindResume(sessionId: string, expectedProject?: string) {
  if (!/^[A-Za-z0-9._:-]{1,256}$/.test(sessionId)) throw new BioMindTransportError("INVALID_SESSION_ID", true);
  const sessionFile = await resolveSessionPath(sessionId);
  if (!sessionFile) throw new BioMindTransportError("HISTORY_SESSION_NOT_FOUND", true);
  const header = readSessionHeader(sessionFile);
  if (!header || header.id !== sessionId || !isBioMindProjectAllowed(header.cwd)) throw new BioMindTransportError("HISTORY_SESSION_NOT_AUTHORIZED", true);
  const project = realpathSync(header.cwd);
  if (expectedProject !== undefined && (!isBioMindProjectAllowed(expectedProject) || realpathSync(expectedProject) !== project)) {
    throw new BioMindTransportError("MESSAGE_SCOPE_MISMATCH", true);
  }
  try {
    const projection = await projectBioMindHistory(sessionFile);
    if (projection.sessionId !== sessionId || projection.project !== project) {
      throw new BioMindTransportError("HISTORY_IDENTITY_UNAVAILABLE", true);
    }
    return { sessionId, sessionFile, project, resumable: projection.resumable === true,
      resumeReason: projection.resumeReason ?? "SESSION_WRITER_HISTORY_UNVERIFIED" };
  } catch (error) {
    if (error instanceof BioMindTransportError) throw error;
    const message = error instanceof Error ? error.message : "";
    throw new BioMindTransportError(/^HISTORY_[A-Z_]{1,80}$/.test(message) ? message : "HISTORY_UNAVAILABLE", true);
  }
}
