import type { AppConfig, ProjectSummary } from "./types";

function record(value: unknown): value is Record<string, any> { return !!value && typeof value === "object" && !Array.isArray(value); }
function text(value: unknown): value is string { return typeof value === "string"; }
function nonempty(value: unknown): value is string { return text(value) && value.length > 0; }
function invalid(code: string): never { throw new Error(code); }

/** Validate IPC metadata before committing it to the render store. Never
 * invent native IDs, stringify unknown objects, or silently drop sessions. */
export function readProjects(value: unknown): ProjectSummary[] {
  if (!Array.isArray(value)) return invalid("PROJECT_DATA_INVALID");
  return value.map((project) => {
    if (!record(project) || !nonempty(project.cwd) || !text(project.name) || !Array.isArray(project.threads)) return invalid("PROJECT_DATA_INVALID");
    return {
      cwd: project.cwd, name: project.name, pinned: project.pinned === true,
      ...(typeof project.openedAt === "number" && Number.isFinite(project.openedAt) ? { openedAt: project.openedAt } : {}),
      threads: project.threads.map((thread: unknown) => {
        if (!record(thread) || !nonempty(thread.file) || !nonempty(thread.id) || !text(thread.title) || !text(thread.preview) || typeof thread.updatedAt !== "number" || !Number.isFinite(thread.updatedAt) || typeof thread.messageCount !== "number" || !Number.isFinite(thread.messageCount)) return invalid("PROJECT_DATA_INVALID");
        return { file: thread.file, id: thread.id, title: thread.title, preview: thread.preview, updatedAt: thread.updatedAt, messageCount: thread.messageCount, pinned: thread.pinned === true };
      }),
    };
  });
}

export function readWindowConfig(value: unknown): AppConfig {
  if (!record(value)) return invalid("PROJECT_DATA_INVALID");
  const strings = (key: string) => {
    if (value[key] === undefined) return [];
    if (!Array.isArray(value[key]) || !value[key].every(text)) return invalid("PROJECT_DATA_INVALID");
    return [...value[key]] as string[];
  };
  const archivedThreads = value.archivedThreads ?? [];
  if (!Array.isArray(archivedThreads) || !archivedThreads.every((thread) => record(thread) && nonempty(thread.file) && nonempty(thread.cwd) && text(thread.title))) return invalid("PROJECT_DATA_INVALID");
  return {
    ...value,
    piCliPath: text(value.piCliPath) ? value.piCliPath : "",
    pinnedProjects: strings("pinnedProjects"), pinnedThreads: strings("pinnedThreads"), archivedProjects: strings("archivedProjects"), archivedThreads,
    theme: ["dark", "light", "system"].includes(value.theme) ? value.theme : "light",
    language: value.language === "en" ? "en" : "zh",
    remoteSignalingUrl: text(value.remoteSignalingUrl) ? value.remoteSignalingUrl : "",
    remoteSignalingEnabled: value.remoteSignalingEnabled === true,
    remoteStunUrls: strings("remoteStunUrls"),
    lastThreadId: text(value.lastThreadId) ? value.lastThreadId : undefined,
  };
}

export function validateSnapshot(value: unknown): asserts value is Record<string, any> {
  if (!record(value) || !nonempty(value.threadId || value.sessionFile) || !nonempty(value.cwd)) invalid("SESSION_RESTORE_FAILED");
  for (const key of ["messages", "branchMessages", "liveEvents", "models", "commands"]) {
    if (value[key] != null && (!Array.isArray(value[key]) || !value[key].every(record))) invalid("SESSION_RESTORE_FAILED");
  }
  if (value.sessionFile != null && !nonempty(value.sessionFile)) invalid("SESSION_RESTORE_FAILED");
  if (value.sessionName != null && !text(value.sessionName)) invalid("SESSION_RESTORE_FAILED");
  if (value.model != null && (!record(value.model) || !text(value.model.id) || !text(value.model.provider))) invalid("SESSION_RESTORE_FAILED");
}

export function readRemoteDirectory(value: unknown): { path: string; parent: string | null; entries: { name: string; path: string; isDirectory: boolean }[] } {
  if (!record(value) || !nonempty(value.path)) return invalid("REMOTE_DIRECTORY_INVALID");
  const parent = value.parent ?? value.parentPath ?? null;
  const entries = value.entries ?? value.directories ?? [];
  if ((parent !== null && !text(parent)) || !Array.isArray(entries)) return invalid("REMOTE_DIRECTORY_INVALID");
  return { path: value.path, parent, entries: entries.map((item) => {
    if (!record(item) || !nonempty(item.name) || (item.path != null && !nonempty(item.path))) return invalid("REMOTE_DIRECTORY_INVALID");
    return { name: item.name, path: item.path || `${value.path.replace(/\/$/, "")}/${item.name}`, isDirectory: item.isDirectory !== false };
  }) };
}
