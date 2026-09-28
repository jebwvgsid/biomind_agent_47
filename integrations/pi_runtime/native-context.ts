/** Filter Pi's already-discovered context files; never discover or load a second copy. */
import fs from 'node:fs';
import path from 'node:path';

export interface NativeContextFile { path: string; content: string; }
const within = (filename: string, root: string) => {
  const relative = path.relative(root, filename);
  return relative === '' || (!path.isAbsolute(relative) && relative !== '..' && !relative.startsWith(`..${path.sep}`));
};

/** Global Pi instructions retain their explicit agent-directory scope. Project
 * instructions remain inside the selected research cwd, including realpath.
 * Native Pi owns precedence (override/AGENTS/CLAUDE), parsing and ordering.
 */
export function filterNativeContextFiles(files: NativeContextFile[], projectRoot: string, agentDir: string): NativeContextFile[] {
  if (!files.length) return [];
  let project: string, agent: string;
  try {
    project = fs.realpathSync(projectRoot);
    agent = fs.existsSync(agentDir) ? fs.realpathSync(agentDir) : path.resolve(agentDir);
  } catch { return []; } // An unavailable boundary never expands instruction scope.
  const seen = new Set<string>();
  return files.filter(file => {
    try {
      const logical = path.resolve(file.path), real = fs.realpathSync(logical);
      const global = path.dirname(logical) === path.resolve(agentDir) && within(real, agent);
      const scoped = within(logical, path.resolve(projectRoot)) && within(real, project);
      if ((!global && !scoped) || seen.has(real)) return false;
      seen.add(real); return true;
    } catch { return false; }
  });
}

/** Pi represents updated instructions as system-section patches. Keep only the
 * latest native project_context value in the outbound view so superseded
 * instructions do not remain as readable historical bodies. This matches
 * source identity, never arbitrary prose, user quotes, or tool messages.
 * The underlying Pi JSONL is retained unchanged.
 */
export function currentNativeProjectContext<T extends { role: string; sections?: Record<string, string | null> }>(messages: T[]): T[] {
  let last = -1;
  messages.forEach((message, index) => {
    if (message.role === 'system' && message.sections && Object.hasOwn(message.sections, 'project_context')) last = index;
  });
  if (last < 0) return messages;
  return messages.map((message, index) => message.role === 'system' && index !== last && message.sections && Object.hasOwn(message.sections, 'project_context')
    ? { ...message, sections: Object.fromEntries(Object.entries(message.sections).filter(([key]) => key !== 'project_context')) } : message);
}
