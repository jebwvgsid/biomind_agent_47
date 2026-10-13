import { NextResponse } from "next/server";
import { realpathSync, statSync, type Stats } from "fs";
import { homedir } from "os";
import { isAbsolute, resolve } from "path";
import { allowFileRoot } from "@/lib/file-access";
import { isBioMindProjectAllowed, isBioMindRemoteProfile } from "@/lib/remote-security";

function normalizeCwd(cwd: string): string {
  if (cwd === "~") return homedir();
  if (cwd.startsWith("~/")) return resolve(homedir(), cwd.slice(2));
  return isAbsolute(cwd) ? cwd : resolve(cwd);
}

// POST /api/cwd/validate  body: { cwd: string }
// Validates a candidate workspace before the UI selects it.
export async function POST(req: Request) {
  try {
    const body = await req.json() as { cwd?: unknown };
    const cwd = typeof body.cwd === "string" ? body.cwd.trim() : "";

    if (!cwd) {
      return NextResponse.json({ error: "Path is required" }, { status: 400 });
    }

    const normalizedCwd = normalizeCwd(cwd);
    if (isBioMindRemoteProfile() && (!isAbsolute(cwd) || !isBioMindProjectAllowed(normalizedCwd))) {
      return NextResponse.json({ error: "Project is not authorized" }, { status: 403 });
    }
    let stat: Stats;
    try {
      stat = statSync(normalizedCwd);
    } catch {
      return NextResponse.json({ error: `Directory does not exist: ${cwd}` }, { status: 400 });
    }

    if (!stat.isDirectory()) {
      return NextResponse.json({ error: `Path is not a directory: ${cwd}` }, { status: 400 });
    }

    if (!isBioMindRemoteProfile()) allowFileRoot(normalizedCwd);
    return NextResponse.json({ success: true, cwd: isBioMindRemoteProfile() ? realpathSync(normalizedCwd) : normalizedCwd });
  } catch (error) {
    return NextResponse.json({ error: String(error) }, { status: 500 });
  }
}
