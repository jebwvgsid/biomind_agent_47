import { NextRequest, NextResponse } from "next/server";
import { stat } from "fs/promises";
import path from "node:path";
import { getBioMindProjectRoots, isBioMindProjectAllowed, isBioMindRemoteProfile } from "@/lib/remote-security";
import {
  getBrowseStartDirectory,
  getParentDirectory,
  listDirectories,
  listWindowsDrives,
  resolveDirectory,
  shouldShowWindowsDrivePicker,
} from "@/lib/directory-browser";

// GET /api/cwd/browse?path=...：列出文件系统中的可读子目录。
export async function GET(request: NextRequest) {
  try {
    const requested = request.nextUrl.searchParams.get("path")?.trim();
    if (isBioMindRemoteProfile()) {
      if (!requested) return NextResponse.json({ path: "", parentPath: null,
        directories: [...getBioMindProjectRoots()].map(root => ({ name: path.basename(root), path: root })) });
      if (!isBioMindProjectAllowed(requested)) return NextResponse.json({ error: "Project is not authorized" }, { status: 403 });
    }

    if (shouldShowWindowsDrivePicker(requested)) {
      return NextResponse.json({
        path: "",
        parentPath: null,
        drives: await listWindowsDrives(),
        directories: [],
      });
    }

    const candidate = getBrowseStartDirectory(requested);

    let resolved: string;
    try {
      resolved = await resolveDirectory(candidate);
    } catch {
      return NextResponse.json({ error: "Directory does not exist" }, { status: 404 });
    }

    const directoryStat = await stat(resolved);
    if (!directoryStat.isDirectory()) {
      return NextResponse.json({ error: "Path is not a directory" }, { status: 400 });
    }

    const directories = await listDirectories(resolved);

    return NextResponse.json({
      path: resolved,
      parentPath: isBioMindRemoteProfile() && !isBioMindProjectAllowed(getParentDirectory(resolved) ?? "") ? null : getParentDirectory(resolved),
      directories: isBioMindRemoteProfile() ? directories.filter(entry => isBioMindProjectAllowed(entry.path)) : directories,
    });
  } catch (error) {
    return NextResponse.json({ error: String(error) }, { status: 500 });
  }
}
