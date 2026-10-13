/** Operator-owned boundaries for the single-owner BioMind server profile. */
import { existsSync, mkdirSync, realpathSync, statSync } from "node:fs";
import path from "node:path";

export function isBioMindRemoteProfile(): boolean {
  return process.env.PI_STUDIO_AGENT_PROFILE === "biomind";
}

function configuredRoots(name: string, required = false): Set<string> {
  const raw = process.env[name];
  if (!raw && !required) return new Set();
  let values: unknown;
  try { values = JSON.parse(raw ?? ""); } catch { throw new Error("BIOMIND_ROOTS_NOT_CONFIGURED"); }
  if (!Array.isArray(values) || values.length > 128 || (required && values.length === 0)) throw new Error("BIOMIND_ROOTS_NOT_CONFIGURED");
  const roots = new Set<string>();
  for (const value of values) {
    if (typeof value !== "string" || value.length > 4096 || !path.isAbsolute(value) || value.includes("\0")) throw new Error("BIOMIND_INVALID_ROOT");
    const root = realpathSync(value);
    if (!statSync(root).isDirectory() || root === path.parse(root).root) throw new Error("BIOMIND_INVALID_ROOT");
    roots.add(root);
  }
  return roots;
}

export function getBioMindProjectRoots(): Set<string> {
  return configuredRoots("BIOMIND_PROJECT_ROOTS_JSON", true);
}

export function getBioMindFileRoots(): Set<string> {
  return new Set([...getBioMindProjectRoots(), ...configuredRoots("BIOMIND_DATA_ROOTS_JSON")]);
}

export function isBioMindPathAllowed(target: string, roots = getBioMindFileRoots()): boolean {
  if (typeof target !== "string" || !path.isAbsolute(target) || target.includes("\0")) return false;
  try {
    const real = realpathSync(target);
    return [...roots].some(root => {
      const relative = path.relative(root, real);
      return relative === "" || (!relative.startsWith(".." + path.sep) && relative !== ".." && !path.isAbsolute(relative));
    });
  } catch { return false; }
}

export function isBioMindProjectAllowed(cwd: string): boolean {
  try { return statSync(cwd).isDirectory() && isBioMindPathAllowed(cwd, getBioMindProjectRoots()); }
  catch { return false; }
}

/** Uploads reuse Studio storage, but only an operator-selected existing root. */
export function getBioMindUploadRoot(project?: string): string {
  if (project !== undefined) {
    if (!isBioMindProjectAllowed(project)) throw new Error("BIOMIND_PROJECT_NOT_AUTHORIZED");
    const cwd = realpathSync(project);
    const target = path.join(cwd, "pi-web-uploads");
    if (!existsSync(target)) mkdirSync(target);
    if (!isBioMindPathAllowed(target, new Set([cwd])) || !statSync(target).isDirectory()) throw new Error("BIOMIND_UPLOAD_SCOPE_MISMATCH");
    return realpathSync(target);
  }
  const value = process.env.PI_WEB_UPLOADS_DIR;
  if (!value || !isBioMindPathAllowed(value) || !statSync(value).isDirectory()) throw new Error("BIOMIND_UPLOAD_ROOT_NOT_CONFIGURED");
  return realpathSync(value);
}

/** No cross-site export exception in the authenticated remote profile. */
export function isBioMindRequestAllowed(request: Request): boolean {
  const authority = request.headers.get("host");
  if (!authority || /[\s/@\\]/.test(authority)) return false;
  try {
    const actual = new URL(request.url);
    const external = new URL(actual.protocol + "//" + authority);
    const hostname = external.hostname.toLowerCase();
    const configured = (process.env.PI_WEB_ALLOWED_HOSTS ?? "").split(",").map(value => value.trim().toLowerCase()).filter(Boolean);
    if (!["127.0.0.1", "[::1]", "localhost", ...configured].includes(hostname)) return false;
    if (request.headers.get("sec-fetch-site") === "cross-site") return false;
    const origin = request.headers.get("origin");
    return !origin || origin === external.origin;
  } catch { return false; }
}

/** Explicit surface: an unrelated Studio route cannot launch a second agent. */
export function isBioMindApiAllowed(pathname: string, method: string, searchParams = new URLSearchParams()): boolean {
  if (pathname === "/api/plugins") return method === "GET" || method === "POST";
  // A bounded Main file helper, not a general settings/SDK route.
  if (pathname === "/api/agent/instructions") return method === "GET" || method === "PATCH";
  // Search is an explicit read-only route, never a session-id wildcard.
  if (pathname === "/api/sessions/search") return method === "GET";
  if (pathname.startsWith("/api/sessions/search/")) return false;
  if (method === "GET") {
    if (["/api/agent/profile", "/api/agent/running", "/api/agent/running/events", "/api/sessions", "/api/projects/registered", "/api/cwd/browse", "/api/file-index", "/api/uploads"].includes(pathname)) return true;
    if (/^\/api\/agent\/[^/]+(?:\/events)?$/.test(pathname)) return true;
    if (/^\/api\/sessions\/[^/]+(?:\/(?:state|media))?$/.test(pathname)) return true;
    if (pathname.startsWith("/api/files/") && searchParams.get("type") !== "watch" && !["save", "allow-local", "open-external", "reveal"].includes(pathname.slice("/api/files/".length))) return true;
  }
  if (method === "POST") {
    if (["/api/agent/new", "/api/cwd/validate", "/api/projects/registered"].includes(pathname)) return true;
    if (/^\/api\/agent\/[^/]+$/.test(pathname) && !["running", "profile"].includes(pathname.split("/").at(-1)!)) return true;
    if (pathname === "/api/uploads" && !searchParams.has("dir") && !searchParams.has("open")) return true;
  }
  return false;
}
