import { NextResponse } from "next/server";
import { resolveSessionPath } from "@/lib/session-reader";
import { startRpcSession, getRpcSession } from "@/lib/rpc-manager";
import { getBioMindServerId, isBioMindRemoteProfile, readBioMindRequest } from "@/lib/biomind-profile";
import { BioMindTransportError, bioMindErrorBody } from "@/lib/biomind-session-adapter";
import { isBioMindProjectAllowed } from "@/lib/remote-security";
import { realpathSync } from "node:fs";
import { inspectBioMindResume } from "@/lib/biomind-resume";

// POST /api/agent/[id] - Send a command to an existing session
export async function POST(
  req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;

  try {
    const body = (isBioMindRemoteProfile() ? await readBioMindRequest(req) : await req.json()) as { type: string; [key: string]: unknown };

    if (isBioMindRemoteProfile() && body.type === "resume_session") {
      if (Object.keys(body).some(key => !["type", "serverId", "project", "sessionId"].includes(key))
          || body.serverId !== getBioMindServerId() || body.sessionId !== id || typeof body.project !== "string"
          || !isBioMindProjectAllowed(body.project) || realpathSync(body.project) !== body.project) {
        throw new BioMindTransportError("MESSAGE_SCOPE_MISMATCH", true);
      }
      const previous = getRpcSession(id);
      if (previous?.isAlive() && previous.cwd !== body.project) throw new BioMindTransportError("MESSAGE_SCOPE_MISMATCH", true);
      const { session, realSessionId } = await startRpcSession(id, "", body.project, { bioMindResume: true });
      if (realSessionId !== id || session.cwd !== body.project) throw new BioMindTransportError("HISTORY_RESTORE_IDENTITY_MISMATCH", true);
      const state = await session.send({ type: "get_state" });
      return NextResponse.json({ success: true, data: state, resumed: !previous?.isAlive() });
    }

    // Fast path: already-running session
    const existing = getRpcSession(id);
    if (existing?.isAlive()) {
      if (isBioMindRemoteProfile() && !isBioMindProjectAllowed(existing.cwd)) throw new BioMindTransportError("PROJECT_NOT_AUTHORIZED", true);
      const result = await existing.send(body);
      return NextResponse.json({ success: true, data: result });
    }

    if (isBioMindRemoteProfile()) throw new BioMindTransportError("HISTORY_READ_ONLY", true);

    const filePath = await resolveSessionPath(id);
    if (!filePath) {
      return NextResponse.json({ error: "Session not found" }, { status: 404 });
    }

    const { session } = await startRpcSession(id, filePath, undefined);
    const result = await session.send(body);

    return NextResponse.json({ success: true, data: result });
  } catch (error) {
    if (isBioMindRemoteProfile()) return NextResponse.json(bioMindErrorBody(error), { status: 409 });
    return NextResponse.json({ error: String(error) }, { status: 500 });
  }
}

// GET /api/agent/[id] - Get current agent state
export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;

  try {
    const session = getRpcSession(id);
    if (!session || !session.isAlive()) {
      if (!isBioMindRemoteProfile()) return NextResponse.json({ running: false });
      let resumable = false, resumeReason = "SESSION_WRITER_HISTORY_UNVERIFIED";
      try { ({ resumable, resumeReason } = await inspectBioMindResume(id)); }
      catch (error) { if (error instanceof BioMindTransportError) resumeReason = error.code; }
      return NextResponse.json({ running: false, readOnly: true, ownerStatus: "not_loaded", automaticReplay: false, resumable, resumeReason });
    }

    if (isBioMindRemoteProfile() && !isBioMindProjectAllowed(session.cwd)) throw new BioMindTransportError("PROJECT_NOT_AUTHORIZED", true);

    const state = await session.send({ type: "get_state" });
    return NextResponse.json({ running: true, state });
  } catch (error) {
    if (isBioMindRemoteProfile()) return NextResponse.json(bioMindErrorBody(error), { status: 409 });
    return NextResponse.json({ error: String(error) }, { status: 500 });
  }
}
