/** Pi supplies schemas/renderers and tool behavior; BioMind supplies readable files. */
import { constants } from 'node:fs';
import { open, realpath } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import ignore, { type Ignore } from 'ignore';
import { minimatch } from 'minimatch';
import { createLsToolDefinition, createFindToolDefinition, createGrepToolDefinition,
  truncateHead, DEFAULT_MAX_BYTES, type ToolDefinition } from '@earendil-works/pi-coding-agent';

export const DISCOVERY_TOOLS = ['ls', 'find', 'grep', 'rg'] as const;
export type DiscoveryKind = typeof DISCOVERY_TOOLS[number];
interface Entry { name: string; kind: 'file' | 'directory' }
export interface DirectorySnapshot {
  status: string;
  path: string;
  kind: 'file' | 'directory';
  entries: Entry[];
  scan_limited?: boolean;
}
export interface DiscoveryBridge {
  list(absolutePath: string, signal: AbortSignal): Promise<DirectorySnapshot>;
  authorize(absolutePath: string, signal: AbortSignal): Promise<Record<string, any>>;
}
const MAX_FILE_BYTES = 32 * 1024 * 1024;
const MAX_SEARCH_BYTES = 128 * 1024 * 1024;
const MAX_ENTRIES = 10000;
const match = (name: string, pattern: string) => minimatch(name, pattern, { dot: true, matchBase: !pattern.includes('/') });
const definitionFor = (kind: DiscoveryKind, cwd: string): ToolDefinition<any, any> => kind === 'ls' ? createLsToolDefinition(cwd)
  : kind === 'find' ? createFindToolDefinition(cwd) : createGrepToolDefinition(cwd);
export function discoveryDefinition(kind: DiscoveryKind, cwd: string): ToolDefinition<any, any> {
  const definition = definitionFor(kind, cwd);
  return { ...definition, name: kind, label: kind,
    description: `${kind === 'rg' ? 'Alias of grep; identical parameters and results. ' : ''}${definition.description} BioMind read permissions apply; protected files and links are excluded.${kind === 'ls' ? '' : ' Search uses ignore files inside the search root.'}` };
}

async function openedFile(filename: string, bridge: DiscoveryBridge, signal: AbortSignal) {
  signal.throwIfAborted();
  const receipt = await bridge.authorize(filename, signal);
  signal.throwIfAborted();
  if (receipt.status !== 'completed' || (receipt.requested_path ?? receipt.path) !== filename) throw new Error('READ_AUTHORIZATION_REQUIRED');
  const handle = await open(receipt.path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await handle.stat({ bigint: true });
    if (!info.isFile() || info.nlink !== 1n || info.dev !== BigInt(receipt.device) || info.ino !== BigInt(receipt.inode)
      || info.mtimeNs !== BigInt(receipt.mtime_ns) || await realpath(`/proc/self/fd/${handle.fd}`) !== receipt.path) {
      throw new Error('FILE_CHANGED_OR_OUT_OF_SCOPE');
    }
    signal.throwIfAborted();
    let bytes: Buffer | undefined;
    const read = async () => {
      if (info.size > BigInt(MAX_FILE_BYTES)) throw new Error('SEARCH_FILE_TOO_LARGE');
      bytes ??= await handle.readFile();
      if (bytes.length > MAX_FILE_BYTES) throw new Error('SEARCH_FILE_TOO_LARGE');
      if (receipt.expected_sha256 && createHash('sha256').update(bytes).digest('hex') !== receipt.expected_sha256) throw new Error('FILE_CHANGED: pinned source');
      return bytes.toString('utf8');
    };
    if (receipt.expected_sha256) await read();
    return { handle, info, read, async verify() {
      signal.throwIfAborted();
      const after = await handle.stat({ bigint: true });
      if (after.mtimeNs !== info.mtimeNs || after.size !== info.size || after.nlink !== 1n
        || await realpath(`/proc/self/fd/${handle.fd}`) !== receipt.path) throw new Error('FILE_CHANGED_OR_OUT_OF_SCOPE');
    } };
  } catch (error) { await handle.close(); throw error; }
}

export function nativeDiscovery(kind: DiscoveryKind, cwd: string, bridge: DiscoveryBridge): ToolDefinition<any, any> {
  return { ...discoveryDefinition(kind, cwd),
    async execute(id, raw, signal, update, ctx) {
      const args = raw as Record<string, any>;
      for (const key of ['limit', 'context']) {
        if (args[key] !== undefined && (!Number.isInteger(args[key]) || args[key] < (key === 'limit' ? 1 : 0) || args[key] > 10000)) throw new Error(`INVALID_SEARCH_${key.toUpperCase()}`);
      }
      if (args.context > 100) throw new Error('SEARCH_CONTEXT_LIMIT: maximum 100 lines');
      // Native grep does not install its abort listener until after resolving rg.
      // Every bridge operation also checks this signal before reading any bytes.
      const callerSignal = signal ?? new AbortController().signal;
      let activeSignal = callerSignal;
      let scanning = false;
      const notices = new Set<string>();
      let visited = 0;
      const list = async (filename: string) => {
        activeSignal.throwIfAborted();
        const snapshot = await bridge.list(filename, activeSignal);
        activeSignal.throwIfAborted();
        if (snapshot.status !== 'completed') throw new Error('READ_AUTHORIZATION_REQUIRED');
        // The initial host permission dialog has no search deadline. Start the
        // work budget only once its directory/file grant has been obtained.
        if (!scanning) { scanning = true; activeSignal = AbortSignal.any([callerSignal, AbortSignal.timeout(30000)]); }
        if (snapshot.scan_limited) notices.add('Directory scan limit reached; narrow the search path.');
        return snapshot;
      };
      const finish = (result: any) => {
        activeSignal.throwIfAborted();
        if (notices.size) return { ...result, content: [...result.content,
          { type: 'text', text: `[Search incomplete: ${[...notices].join(' ')}]` }],
          details: { ...result.details, discovery: { incomplete: true, reasons: [...notices] } } };
        return result;
      };
      if (kind === 'ls') {
        let snapshot: DirectorySnapshot;
        let requested: string;
        const tool = createLsToolDefinition(cwd, { operations: {
          async exists(filename) { requested = filename; snapshot = await list(filename); return true; },
          stat(filename) {
            const kind = filename === requested ? snapshot.kind : snapshot.entries.find(entry => path.join(requested, entry.name) === filename)?.kind;
            if (!kind) throw new Error('FILE_NOT_IN_DIRECTORY_SNAPSHOT');
            return { isDirectory: () => kind === 'directory' };
          },
          readdir() { return snapshot.entries.map(entry => entry.name); },
        } });
        return finish(await tool.execute(id, args as any, activeSignal, update, ctx));
      }

      type Rules = Array<{ root: string; rules: Ignore }>;
      async function* walk(snapshot: DirectorySnapshot, relative = '', inherited: Rules = []): AsyncGenerator<{ filename: string; relative: string; kind: Entry['kind'] }> {
        if (snapshot.kind === 'file') { yield { filename: snapshot.path, relative: path.basename(snapshot.path), kind: 'file' }; return; }
        const rules = [...inherited];
        for (const name of ['.gitignore', '.ignore', ...(kind === 'grep' || kind === 'rg' ? ['.rgignore'] : [])]) {
          if (!snapshot.entries.some(entry => entry.name === name && entry.kind === 'file')) continue;
          const file = await openedFile(path.join(snapshot.path, name), bridge, activeSignal);
          try {
            if (file.info.size > 256n * 1024n) throw new Error('SEARCH_IGNORE_FILE_TOO_LARGE');
            rules.push({ root: relative, rules: ignore({ ignorecase: false }).add(await file.read()) });
            await file.verify();
          } finally { await file.handle.close(); }
        }
        for (const entry of snapshot.entries.sort((a, b) => a.name.localeCompare(b.name))) {
          activeSignal.throwIfAborted();
          if (++visited > MAX_ENTRIES) { notices.add('Search entry limit reached; narrow the search path.'); return; }
          if (entry.name === '.git' || entry.name === 'node_modules') continue;
          const rel = relative ? `${relative}/${entry.name}` : entry.name;
          let excluded = false;
          for (const rule of rules) {
            const result = rule.rules.test((rule.root ? rel.slice(rule.root.length + 1) : rel) + (entry.kind === 'directory' ? '/' : ''));
            if (result.ignored) excluded = true;
            else if (result.unignored) excluded = false;
          }
          if (excluded) continue;
          const filename = path.join(snapshot.path, entry.name);
          yield { filename, relative: rel, kind: entry.kind };
          if (entry.kind === 'directory') yield* walk(await list(filename), rel, rules);
        }
      }
      if (kind === 'find') {
        const tool = createFindToolDefinition(cwd, { operations: {
          exists: () => true,
          async glob(pattern, root, options) {
            const results: string[] = [];
            const snapshot = await list(root);
            if (snapshot.kind !== 'directory') throw new Error(`Not a directory: ${root}`);
            for await (const entry of walk(snapshot)) {
              if (options.ignore.some(pattern => match(entry.relative, pattern))) continue;
              if (match(entry.relative, pattern)) results.push(path.join(root, entry.relative) + (entry.kind === 'directory' ? '/' : ''));
              if (results.length >= options.limit) break;
            }
            return results;
          },
        } });
        return finish(await tool.execute(id, args as any, activeSignal, update, ctx));
      }

      // Pi 0.87.1's GrepOperations cannot replace rg traversal. Give the native
      // tool an already-authorized open descriptor, never an unchecked tree.
      // /proc/<host-pid>/fd keeps the file pinned across the rg subprocess.
      const root = path.resolve(ctx.cwd ?? cwd, args.path?.startsWith('~') ? args.path.replace(/^~(?=\/|$)/, process.env.HOME ?? '~') : args.path ?? '.');
      const limit = args.limit ?? 100;
      const output: string[] = [];
      let matches = 0, bytes = 0, files = 0, linesTruncated = false, outputBytes = 0;
      for await (const entry of walk(await list(root))) {
        if (entry.kind !== 'file' || args.glob && !match(entry.relative, args.glob)) continue;
        const file = await openedFile(entry.filename, bridge, activeSignal);
        try {
          if (file.info.size > BigInt(MAX_FILE_BYTES)) { notices.add('Files larger than 32 MiB were skipped; use a bounded run_command for them.'); continue; }
          bytes += Number(file.info.size);
          if (++files > 1000 || bytes > MAX_SEARCH_BYTES) { notices.add('Search file/byte budget reached; narrow the search path.'); break; }
          const descriptor = `/proc/${process.pid}/fd/${file.handle.fd}`;
          const prefix = String(file.handle.fd);
          const tool = createGrepToolDefinition(cwd, { operations: {
            isDirectory: async () => { activeSignal.throwIfAborted(); return false; },
            readFile: async () => { activeSignal.throwIfAborted(); return file.read(); },
          } });
          const result = await tool.execute(id, { ...args, path: descriptor, glob: undefined, limit: limit - matches } as any, activeSignal, update, ctx);
          await file.verify();
          const text = result.content.filter(item => item.type === 'text').map(item => item.text).join('\n');
          if (text === 'No matches found') continue;
          matches += [...text.matchAll(new RegExp(`^${prefix}:\\d+:`, 'gm'))].length;
          const mapped = text.replace(new RegExp(`^${prefix}(?=[:\\-]\\d+[:\\-])`, 'gm'), () => entry.relative);
          output.push(mapped); outputBytes += Buffer.byteLength(mapped);
          linesTruncated ||= Boolean(result.details?.linesTruncated);
          if (matches >= limit || outputBytes >= DEFAULT_MAX_BYTES) break;
        } finally { await file.handle.close(); }
      }
      const truncated = truncateHead(output.join('\n'), { maxLines: Number.MAX_SAFE_INTEGER });
      const details: Record<string, unknown> = {};
      if (linesTruncated) details.linesTruncated = true;
      if (matches >= limit) { details.matchLimitReached = limit; notices.add(`${limit} matches limit reached; increase limit or narrow the search.`); }
      if (truncated.truncated || outputBytes >= DEFAULT_MAX_BYTES) { details.truncation = truncated; notices.add('Search output limit reached; narrow the search.'); }
      return finish({ content: [{ type: 'text', text: truncated.content || 'No matches found' }], details });
    },
  } as ToolDefinition<any, any>;
}
