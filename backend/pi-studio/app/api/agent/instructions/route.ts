import { NextResponse } from "next/server";
import { isBioMindRemoteProfile, readBioMindRequest } from "@/lib/biomind-profile";
import { instructionsErrorResponse, targetInstructions } from "@/lib/biomind-instructions";

function failure(error: unknown) {
  const body = instructionsErrorResponse(error);
  const status = body.code === "INSTRUCTIONS_PROJECT_NOT_AUTHORIZED" ? 403
    : body.code === "INSTRUCTIONS_INVALID_REQUEST" ? 400
    : body.code === "INSTRUCTIONS_HELPER_NOT_CONFIGURED" ? 503 : 409;
  return NextResponse.json(body, { status });
}

export async function GET(request: Request) {
  if (!isBioMindRemoteProfile()) return NextResponse.json({ error: "BioMind profile required" }, { status: 404 });
  try {
    const query = new URL(request.url).searchParams;
    if ([...query.keys()].some(key => !["cwd", "scope"].includes(key))
        || query.getAll("cwd").length !== 1 || query.getAll("scope").length !== 1) {
      return NextResponse.json({ error: "INSTRUCTIONS_INVALID_REQUEST", code: "INSTRUCTIONS_INVALID_REQUEST", rejected: true }, { status: 400 });
    }
    return NextResponse.json({ success: true, data: await targetInstructions({ cwd: query.get("cwd"), scope: query.get("scope") }, "read") });
  } catch (error) { return failure(error); }
}

export async function PATCH(request: Request) {
  if (!isBioMindRemoteProfile()) return NextResponse.json({ error: "BioMind profile required" }, { status: 404 });
  try {
    const body = await readBioMindRequest(request);
    return NextResponse.json({ success: true, data: await targetInstructions(body, "save") });
  } catch (error) { return failure(error); }
}
