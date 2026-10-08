/** Small direct-user file edits. Pi owns the editor and instruction discovery. */
import path from 'node:path';
import { access, chmod, lstat, open, readFile, realpath, rename, stat, unlink, link } from 'node:fs/promises';
import { constants } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';

export interface EditableFile {
  path: string; target: string; text: string; exists: boolean; symlink: boolean;
  hash: string; identity: string; mode: number; bom: boolean; crlf: boolean;
}
const hash = (bytes: Buffer) => createHash('sha256').update(bytes).digest('hex');
export async function readEditableFile(filename: string): Promise<EditableFile> {
  const logical = path.resolve(filename);
  let info;
  try { info = await lstat(logical); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  // Resolve the existing parent too: saving must not silently retarget a link.
  const target = info ? await realpath(logical) : path.join(await realpath(path.dirname(logical)), path.basename(logical));
  const current = info ? await stat(target) : undefined;
  if (current && !current.isFile()) throw new Error('说明来源不是普通文件');
  if (current && current.size > 1024 * 1024) throw new Error('说明超过 1 MiB，请使用外部编辑器');
  const bytes = current ? await readFile(target) : Buffer.alloc(0);
  const bom = bytes.subarray(0, 3).equals(Buffer.from([0xef, 0xbb, 0xbf]));
  const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  return { path: logical, target, text, exists: !!current, symlink: info?.isSymbolicLink() ?? false,
    hash: hash(bytes), identity: current ? `${current.dev}:${current.ino}:${current.mode}:${current.mtimeMs}:${current.ctimeMs}` : 'missing',
    mode: current ? current.mode & 0o777 : 0o600, bom, crlf: text.includes('\r\n') && !/(?<!\r)\n/.test(text) };
}
export async function saveEditableFile(before: EditableFile, text: string): Promise<void> {
  const same = async () => {
    const now = await readEditableFile(before.path);
    if (now.target !== before.target || now.identity !== before.identity || now.hash !== before.hash)
      throw new Error('来源已被外部修改；未覆盖，草稿保留。重新打开后核对再保存');
  };
  await same();
  if (before.exists) await access(before.target, constants.W_OK);
  await access(path.dirname(before.target), constants.W_OK);
  const body = before.crlf ? text.replace(/\r?\n/g, '\r\n') : text;
  const bytes = Buffer.concat([before.bom ? Buffer.from([0xef, 0xbb, 0xbf]) : Buffer.alloc(0), Buffer.from(body)]);
  const temporary = path.join(path.dirname(before.target), `.biomind-edit-${randomUUID()}.tmp`);
  try {
    const file = await open(temporary, 'wx', before.mode);
    try { await file.writeFile(bytes); await file.sync(); } finally { await file.close(); }
    await chmod(temporary, before.mode);
    await same();
    if (before.exists) await rename(temporary, before.target);
    else await link(temporary, before.target); // Atomic, no overwrite when another writer created the path.
  } finally { await unlink(temporary).catch(error => { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }); }
}

export async function memoryDefault(): Promise<{ path?: string; value?: boolean; source: string; error?: string }> {
  const filename = process.env.BIOMIND_PI_CONFIG_PATH;
  if (!filename) return { source: process.env.BIOMIND_PI_HISTORY_SOURCE ?? 'default',
    value: process.env.BIOMIND_PI_HISTORY_DEFAULT === '1' };
  try {
    const file = await readEditableFile(filename), config = JSON.parse(file.text);
    return { path: file.path, source: process.env.BIOMIND_PI_HISTORY_SOURCE ?? 'config',
      value: typeof config.historyEnabled === 'boolean' ? config.historyEnabled : undefined };
  } catch { return { path: filename, source: '未知', error: '默认配置读取失败' }; }
}
export async function saveMemoryDefault(enabled: boolean): Promise<string> {
  const filename = process.env.BIOMIND_PI_CONFIG_PATH;
  if (!filename) throw new Error('未保存：没有既有持久配置来源。请用 --pi-config 选择 BioMind 配置文件');
  const file = await readEditableFile(filename);
  if (!file.exists) throw new Error('未保存：启动配置文件已不存在');
  let config: Record<string, unknown>;
  try { config = JSON.parse(file.text); }
  catch { throw new Error('未保存：启动配置 JSON 解析失败，请先修复该文件'); }
  if (!config || typeof config !== 'object' || Array.isArray(config)) throw new Error('未保存：启动配置不是 JSON 对象');
  config.historyEnabled = enabled;
  await saveEditableFile(file, JSON.stringify(config, null, 2) + '\n');
  return file.path;
}
export const argumentCompletions = (values: string[]) => (prefix: string) => {
  const matches = values.filter(value => value.startsWith(prefix)).map(value => ({ value, label: value }));
  return matches.length ? matches : null;
};
