import { NextResponse } from "next/server";
import { listAllSessions } from "@/lib/session-reader";
import { getRunningRpcSessionIds, getPendingSessionInfos } from "@/lib/rpc-manager";
import { resolveProject } from "@/lib/worktree";
import { listRegisteredProjects } from "@/lib/registered-projects";
import { isBioMindRemoteProfile, getBioMindProjectRoots } from "@/lib/remote-security";
import { bioMindHistoryTruncated } from "@/lib/biomind-history";

export async function GET() {
  try {
    if (isBioMindRemoteProfile()) {
      let historyError: string | undefined;
      const history = await listAllSessions().catch(error => { historyError = error instanceof Error ? error.message : "BIOMIND_HISTORY_UNAVAILABLE"; return []; });
      const owned = getPendingSessionInfos();
      const historyIds = new Set(history.map(session => session.id));
      // Persisted metadata has the real count/modified/first-message. Live rows
      // only fill the native file's initial unflushed window or failed history.
      return NextResponse.json({ sessions: [...owned.filter(session => !historyIds.has(session.id)), ...history],
        runningSessionIds: getRunningRpcSessionIds(), registeredProjects: [...getBioMindProjectRoots()],
        profile: "biomind", ...(historyError ? { historyError, historyAvailable: false } : { historyAvailable: true, historyTruncated: bioMindHistoryTruncated() }) });
    }
    const sessions = await listAllSessions();
    // pi 延迟写盘：新会话要等首条 assistant 消息才创建 .jsonl，目录扫描看不到。
    // 把内存中尚未落盘的活跃会话合并进列表（按 id 去重），侧栏才能在
    // 「新会话发首条消息」后立即出现该条目，而不是等 30s 缓存过期。
    const scannedIds = new Set(sessions.map((s) => s.id));
    const pending = getPendingSessionInfos().filter((p) => !scannedIds.has(p.id));
    await Promise.all(pending.map(async (p) => {
      try {
        const project = await resolveProject(p.cwd);
        if (project?.projectRoot) p.projectRoot = project.projectRoot;
      } catch {
        // keep cwd as projectRoot fallback
      }
    }));
    return NextResponse.json({
      sessions: [...pending, ...sessions],
      runningSessionIds: getRunningRpcSessionIds(),
      // 「添加项目」持久登记的目录（可能还没有任何会话），侧栏取并集实现「添加即显示」
      registeredProjects: listRegisteredProjects(),
    });
  } catch (error) {
    return NextResponse.json(
      { error: String(error) },
      { status: 500 }
    );
  }
}
