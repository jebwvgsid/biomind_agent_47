import { spawn } from "node:child_process";
import { realpathSync } from "node:fs";
import { getBioMindLaunchConfig, getBioMindServerId } from "./biomind-profile";
import { isBioMindProjectAllowed } from "./remote-security";
import { beginPackageManagement } from "./biomind-package-gate";
import { packageManagedOwners, resumePackageOwner } from "./rpc-manager";

export type PackageRequest = { action: "list" | "check" | "install" | "remove" | "update" | "config" | "trust" | "repair";
  scope?: "user" | "project"; cwd?: string; source?: string; declaration?: unknown; trusted?: boolean | null; expectedRevision?: string };
export type PackageResult = { schema: "BioMindPluginsV1"; status: "ok" | "BUSY" | "BLOCKED" | "repair_required" | "error";
  code: string; packages?: unknown[]; context?: { piVersion?: string; cwd?: string | null; [key: string]: unknown };
  changed: boolean; reloadRequired: boolean; repairRequired: boolean; [key: string]: unknown };
const actions = new Set(["list", "check", "install", "remove", "update", "config", "trust", "repair"]);
const fields = new Set(["action", "scope", "cwd", "source", "declaration", "trusted", "expectedRevision"]);
export function packageFailure(code: string, status: PackageResult["status"] = "error"): PackageResult {
  return { schema: "BioMindPluginsV1", status, code, changed: false, reloadRequired: false, repairRequired: status === "repair_required" };
}
export function validatePackageRequest(input: Record<string, unknown>): PackageRequest {
  if (Object.keys(input).some(key => !fields.has(key)) || !actions.has(String(input.action))) throw new Error("INVALID_PACKAGE_REQUEST");
  if (input.scope !== undefined && !["user", "project"].includes(String(input.scope))) throw new Error("INVALID_PACKAGE_SCOPE");
  if (input.cwd !== undefined && (typeof input.cwd !== "string" || !isBioMindProjectAllowed(input.cwd))) throw new Error("PROJECT_NOT_AUTHORIZED");
  if ((input.scope === "project" || input.action === "trust") && !input.cwd) throw new Error("PROJECT_REQUIRED");
  for (const key of ["source", "expectedRevision"] as const) if (input[key] !== undefined && (typeof input[key] !== "string" || !input[key] || input[key].length > 4096 || /[\0\r\n]/.test(input[key]))) throw new Error("INVALID_PACKAGE_REQUEST");
  if (["install", "remove", "config", "repair"].includes(String(input.action)) && !input.source) throw new Error("PACKAGE_SOURCE_REQUIRED");
  if (input.trusted !== undefined && input.trusted !== null && typeof input.trusted !== "boolean") throw new Error("INVALID_PACKAGE_TRUST");
  if (input.action === "trust" && input.trusted === undefined) throw new Error("EXPLICIT_PROJECT_TRUST_REQUIRED");
  if (Buffer.byteLength(JSON.stringify(input)) > 65536) throw new Error("PACKAGE_REQUEST_TOO_LARGE");
  return { ...input, ...(input.cwd ? { cwd: realpathSync(String(input.cwd)) } : {}) } as PackageRequest;
}
/** Fixed server-owned entrypoint and config; stdout is machine JSON, stderr is drained but never exposed. */
export function runPackageCli(request: PackageRequest, options: { timeoutMs?: number; spawnChild?: typeof spawn; launch?: ReturnType<typeof getBioMindLaunchConfig> } = {}): Promise<PackageResult> {
  const launch = options.launch ?? getBioMindLaunchConfig();
  return new Promise(resolve => {
    const child = (options.spawnChild ?? spawn)(launch.python, ["-B", launch.entrypoint, "plugins", "--json", "--pi-config", launch.config],
      { cwd: request.cwd ?? "/", stdio: ["pipe", "pipe", "pipe"], shell: false });
    let output = "", excessive = false, settled = false;
    const finish = (result: PackageResult) => { if (!settled) { settled = true; if (timer) clearTimeout(timer); resolve(result); } };
    // Python already bounds native management to 900 seconds. This independent
    // deadline also handles a broken wrapper / missing close event. Signal only
    // OUR management wrapper, never owners or a process group. Descendant flock
    // descriptors remain held until those descendants exit, blocking unsafe loads.
    const timer = setTimeout(() => {
      try { child.kill("SIGTERM"); } catch { /* timeout result remains unknown */ }
      finish(packageFailure("PACKAGE_CLI_TIMEOUT_OUTCOME_UNKNOWN", request.action === "list" || request.action === "check" ? "error" : "repair_required"));
    }, options.timeoutMs ?? 910_000);
    child.stdout.on("data", chunk => { if (Buffer.byteLength(output) + chunk.length > 2 * 1024 * 1024) excessive = true; else if (!excessive) output += chunk.toString("utf8"); });
    child.stderr.on("data", () => {});
    child.stdin.on("error", () => {});
    child.on("error", () => finish(packageFailure("PACKAGE_CLI_START_FAILED")));
    child.on("close", code => {
      try {
        const result = JSON.parse(output) as PackageResult;
        if (excessive || result.schema !== "BioMindPluginsV1" || !["ok", "BUSY", "BLOCKED", "repair_required", "error"].includes(result.status)
          || typeof result.code !== "string" || !/^[A-Z_0-9]+$/.test(result.code) || (result.context && result.context.piVersion !== "0.87.1")
          || (code !== 0 && result.status === "ok")) throw new Error();
        finish(result);
      } catch { finish(packageFailure("PACKAGE_CLI_OUTCOME_UNKNOWN", request.action === "list" || request.action === "check" ? "error" : "repair_required")); }
    });
    child.stdin.end(JSON.stringify(request) + "\n");
  });
}
export async function manageBioMindPackages(input: Record<string, unknown>, runner = runPackageCli): Promise<PackageResult> {
  let request: PackageRequest;
  try { request = validatePackageRequest(input); }
  catch (error) { return { ...packageFailure(error instanceof Error ? error.message : "INVALID_PACKAGE_REQUEST"), serverId: getBioMindServerId() }; }
  const attach = (value: PackageResult): PackageResult => ({ ...value, serverId: getBioMindServerId() });
  if (request.action === "list" || request.action === "check") return attach(await runner(request));
  const release = beginPackageManagement();
  if (!release) return attach(packageFailure("PACKAGE_OPERATION_IN_PROGRESS", "BUSY"));
  try {
    // Configuration-only writes use the common CLI's short settings mutex;
    // shared workers remain alive and the receipt reports reloadRequired.
    if (["config", "trust"].includes(request.action)) return attach(await runner(request));
    const owners = [] as ReturnType<typeof packageManagedOwners>;
    const cancellationFailures: string[] = [];
    let result: PackageResult, mutationStarted = false;
    try {
      // Legacy / broken scope checks precede ANY request to stop an owner.
      // BUSY file leases never authorize stopping an independent CLI worker.
      const check = await runner({ action: "check", ...(request.action === "update" ? {} : { scope: request.scope ?? "user" }), ...(request.cwd ? { cwd: request.cwd } : {}) });
      if (check.status !== "ok" && !(check.status === "BUSY" && check.code === "BUSY")
        && !(request.action === "repair" && check.status === "repair_required" && check.code === "PACKAGE_REPAIR_REQUIRED")) return attach(check);
      owners.push(...packageManagedOwners().filter(owner => request.scope !== "project" || owner.cwd === request.cwd));
      for (const owner of owners) await owner.preparePackageShutdown();
      const stopped = await Promise.allSettled(owners.map(owner => owner.shutdownForPackages()));
      const failed = stopped.find(row => row.status === "rejected");
      if (failed?.status === "rejected") throw failed.reason;
      mutationStarted = true;
      result = await runner(request);
    } catch (error) {
      const code = error instanceof Error && /^[A-Z_0-9]+$/.test(error.message) ? error.message : "PACKAGES_STATE_UNAVAILABLE";
      result = packageFailure(mutationStarted ? "PACKAGE_OPERATION_OUTCOME_UNKNOWN" : code, mutationStarted ? "repair_required" : "BUSY");
    } finally {
      const live = owners.filter(owner => owner.isAlive());
      const cancelled = await Promise.allSettled(live.map(owner => owner.cancelPackageShutdown()));
      cancelled.forEach((row, index) => { if (row.status === "rejected") cancellationFailures.push(live[index].sessionId); });
    }
    const resumedSessionIds: string[] = [], reloadFailures = [...cancellationFailures,
      ...owners.filter(owner => !owner.isAlive() && !owner.packageExitConfirmed()).map(owner => owner.sessionId)];
    // Partial/unknown package writes must never restart workers on uncertain code.
    if (result.status !== "repair_required" && !result.repairRequired) {
      for (const owner of owners.filter(owner => owner.packageExitConfirmed())) {
        try { await resumePackageOwner(owner.sessionId, owner.cwd); resumedSessionIds.push(owner.sessionId); }
        catch { reloadFailures.push(owner.sessionId); }
      }
    }
    return attach({ ...result, resumedSessionIds, reloadFailures, ...(reloadFailures.length ? { reloadRequired: true } : {}) });
  } finally { release(); }
}
