export type RendererIssueSource = "render" | "window_error" | "unhandledrejection";
export type RendererIssueCode = "RENDER_FAILED" | "WINDOW_ERROR" | "UNHANDLED_REJECTION" | "BOOTSTRAP_FAILED" | "PROJECT_DATA_INVALID" | "REMOTE_DIRECTORY_INVALID" | "LOCAL_UI_STATE_UNSAVED" | "SESSION_RESTORE_FAILED" | "PROJECT_OPEN_FAILED" | "CONNECTION_VIEW_FAILED";
export type RendererIssue = { source: RendererIssueSource; code: RendererIssueCode; name: string; stack: string };

const errorNames = new Set(["Error", "TypeError", "RangeError", "ReferenceError", "SyntaxError", "URIError", "EvalError", "AggregateError"]);
const subscribers = new Set<(issue: RendererIssue) => void>();
let latest: RendererIssue | null = null;
let lastReport = "";
let reportWindow = 0;
let reportCount = 0;

// Never transmit error.message, rejection strings, React component props, or
// absolute paths. The main process applies the same allowlist independently.
export function safeRendererIssue(source: RendererIssueSource, code: RendererIssueCode, error?: unknown): RendererIssue {
  let name = "Error";
  let stack = "";
  try {
    if (error && typeof error === "object") {
      const candidate = error as { name?: unknown; stack?: unknown };
      if (typeof candidate.name === "string" && errorNames.has(candidate.name)) name = candidate.name;
      if (typeof candidate.stack === "string") {
        stack = candidate.stack.slice(0, 8192).split("\n")
          .filter((line) => /^\s*at\s+/.test(line))
          .flatMap((line) => [...line.matchAll(/(?:^|[\\/\s(])((?:index|preload)(?:-[A-Za-z0-9_-]{6,16})?\.(?:js|cjs|mjs)):(\d{1,7}):(\d{1,7})(?=[)\s]|$)/g)])
          .slice(0, 6).map((match) => `at ${match[1]}:${match[2]}:${match[3]}`).join("\n");
      }
    }
  } catch { /* A rejected object's getters must not break the recovery UI. */ }
  return { source, code, name, stack };
}

export function reportRendererIssue(source: RendererIssueSource, code: RendererIssueCode, error?: unknown): RendererIssue {
  const issue = safeRendererIssue(source, code, error);
  latest = issue;
  for (const listener of subscribers) { try { listener(issue); } catch { /* isolated listener */ } }
  const now = Date.now();
  if (now - reportWindow > 60_000) { reportWindow = now; reportCount = 0; lastReport = ""; }
  const key = JSON.stringify(issue);
  if (key !== lastReport && reportCount < 20) {
    lastReport = key; reportCount++;
    try { void Promise.resolve(window.pi?.biomind?.reportRendererIssue?.(issue)).catch(() => {}); }
    catch { /* Diagnostics are best effort and may never cause another failure. */ }
  }
  return issue;
}

export function watchRendererIssues(listener: (issue: RendererIssue) => void) {
  subscribers.add(listener);
  if (latest) listener(latest);
  return () => { subscribers.delete(listener); };
}

export function installRendererErrorHandlers(target: Window = window): () => void {
  const onError = (event: ErrorEvent) => { reportRendererIssue("window_error", "WINDOW_ERROR", event.error); };
  const onRejection = (event: PromiseRejectionEvent) => { reportRendererIssue("unhandledrejection", "UNHANDLED_REJECTION", event.reason); };
  target.addEventListener("error", onError);
  target.addEventListener("unhandledrejection", onRejection);
  return () => { target.removeEventListener("error", onError); target.removeEventListener("unhandledrejection", onRejection); };
}

/** Persist a window pointer only. Failure must not replace/close a session. */
export async function persistWindowThread(threadId: string | null): Promise<void> {
  try { await window.pi.biomind.focusThread(threadId); }
  catch (error) { reportRendererIssue("unhandledrejection", "LOCAL_UI_STATE_UNSAVED", error); }
}

export function reloadExistingWorkbench(): void { window.location.reload(); }

export async function showConnectionSafely(): Promise<void> {
  try { await window.pi.biomind.showConnection(); }
  catch (error) { reportRendererIssue("unhandledrejection", "CONNECTION_VIEW_FAILED", error); }
}
