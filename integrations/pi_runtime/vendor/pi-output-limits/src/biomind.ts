/** BioMind compatibility for the pinned pi-output-limits extension.
 * Truncation remains Pi's public truncateHead/truncateTail. No executor changes.
 */
import { truncateHead, truncateTail, type ToolResultEvent } from '@earendil-works/pi-coding-agent';
import { accessSync, constants, mkdtempSync, openSync, writeFileSync, closeSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';

export interface Limits { maxBytes: number; maxLines: number }
export interface RuntimeConfig { global: Limits; tools: Map<string, Partial<Limits>>; tailTools: Set<string> }
const plainTools = new Set(['bash', 'powershell', 'read', 'grep', 'rg', 'find', 'ls']);
const pagedFields = new Set(['stdout_delta', 'stderr_delta']);
const bodyFields = new Set(['stdout', 'stderr', 'output', 'log', 'detail', 'diff', 'content', 'text', 'summary', 'description', 'message']);
const listFields = new Set(['hits', 'matches', 'candidates', 'environments', 'entries', 'packages', 'tools', 'capabilities', 'sources', 'path_samples', 'records', 'results', 'plan']);
export const measure = (text: string) => ({ bytes: Buffer.byteLength(text), lines: text ? text.split('\n').length - Number(text.endsWith('\n')) : 0 });
const record = (value: unknown): value is Record<string, any> => !!value && typeof value === 'object' && !Array.isArray(value);

/** Only already-disclosed tool text is saved, inside the authorized project.
 * These are private temporary previews, with no automatic cleanup or durability promise.
 */
export function saveAvailableOutput(cwd: string, text: string): string | undefined {
  let fd: number | undefined;
  try {
    const directory = mkdtempSync(join(resolve(cwd), '.pi-output-limits-'));
    const filename = join(directory, 'output.log');
    fd = openSync(filename, 'wx', 0o600);
    writeFileSync(fd, text, 'utf8');
    return filename;
  } catch { return undefined; }
  finally { if (fd !== undefined) closeSync(fd); }
}
function accessible(filename: unknown): filename is string {
  if (typeof filename !== 'string') return false;
  try { accessSync(filename, constants.R_OK); return statSync(filename).isFile(); } catch { return false; }
}
function take(text: string, budget: Limits, tail: boolean) {
  // Pi's helper assumes positive budgets. An exhausted shared budget has no body.
  if (budget.maxBytes <= 0 || budget.maxLines <= 0) return { content: '', outputBytes: 0, outputLines: 0,
    truncated: !!text, firstLineExceedsLimit: false, lastLinePartial: false };
  return (tail ? truncateTail : truncateHead)(text, budget);
}
function spend(budget: Limits, text: string) {
  const size = measure(text); budget.maxBytes = Math.max(0, budget.maxBytes - size.bytes); budget.maxLines = Math.max(0, budget.maxLines - size.lines);
}

/** Recognize only the installed Pi/BioMind native footer/status formats. */
function unpack(text: string, tool: string, details: any, isError: boolean) {
  const notices: string[] = [];
  let source = details.fullOutputPath as string | undefined;
  let upstream = details.truncation?.truncated === true;
  if (details.discovery?.incomplete && /^\[Search incomplete: [^\n]*\]$/.test(text)) {
    return { body: '', notices: [text], source, upstream: true };
  }
  if (details.linesTruncated) { upstream = true; notices.push('Upstream search shortened long match lines; use read or a more specific search for full lines.'); }
  if (details.matchLimitReached) { upstream = true; notices.push(`Upstream search reached its ${details.matchLimitReached} match limit; this return is not all matches.`); }
  if (tool === 'bash' || tool === 'powershell') {
    const receipt = text.match(/\nBioMind execution: (\{[^\n]*\})$/);
    if (receipt) { notices.unshift(receipt[0].trim()); text = text.slice(0, -receipt[0].length); }
    if (isError) {
      const status = text.match(/(?:\n\n|^)(Command (?:exited with code \d+|aborted|timed out after [\d.]+ seconds|terminated without an exit code))$/);
      if (status) { notices.unshift(status[1]); text = text.slice(0, -status[0].length); }
    }
    const footer = text.match(/\n\n\[(?:Showing|Output truncated|Truncated)[^\n]*Full output: ([^\n]+)\]$/);
    if (footer) { upstream = true; source ??= footer[1]; text = text.slice(0, -footer[0].length); }
  } else if (tool === 'read') {
    const footer = text.match(/\n\n\[(?:Showing lines |\d+ more lines in file\.)[^\n]*Use offset=\d+ to continue\.\]$/);
    if (footer) {
      notices.push(footer[0].trim().replace(/Use offset=\d+ to continue\./, 'Continuation below follows the actually displayed range.'));
      upstream ||= details.truncation?.truncated === true;
      text = text.slice(0, -footer[0].length);
    }
  } else {
    const footer = text.match(/\n\n\[[^\n]*(?:limit reached|Some lines truncated)[^\n]*\]$/);
    if (footer) { notices.push(footer[0].trim()); text = text.slice(0, -footer[0].length); upstream = true; }
  }
  if (details.execution && !notices.some(n => n.startsWith('BioMind execution:'))) notices.push('BioMind execution: ' + JSON.stringify(details.execution));
  return { body: text, notices, source, upstream };
}

export function limitToolResult(event: ToolResultEvent, config: RuntimeConfig, cwd: string) {
  // Registered BioMind JSON tools project fields before serialization. Replies,
  // user quotations and unknown extension structures must never be blindly sliced.
  if (!plainTools.has(event.toolName) || !Array.isArray(event.content)) return;
  const overrides = config.tools.get(event.toolName);
  const limits = { maxBytes: overrides?.maxBytes ?? config.global.maxBytes, maxLines: overrides?.maxLines ?? config.global.maxLines };
  const details = record(event.details) ? event.details : {};
  const blocks = event.content.map((block, index) => block.type === 'text'
    ? { index, original: block.text, ...unpack(block.text, event.toolName, details, event.isError) } : undefined).filter(b => b !== undefined);
  const total = blocks.reduce((sum, b) => { const n = measure(b.body); return { bytes: sum.bytes + n.bytes, lines: sum.lines + n.lines }; }, { bytes: 0, lines: 0 });
  if (total.bytes <= limits.maxBytes && total.lines <= limits.maxLines) return;
  const budget = { ...limits }, content = [...event.content], notes: string[] = [];
  const tail = config.tailTools.has(event.toolName);
  const ordered = tail ? [...blocks].reverse() : blocks;
  let outputBytes = 0, outputLines = 0;
  const sources: Array<{ block: number; path?: string; cwd?: string; scope: string; available: boolean | 'read-succeeded' }> = [];
  for (const block of ordered) {
    const t = take(block.body, budget, tail);
    spend(budget, t.content); outputBytes += t.outputBytes; outputLines += t.outputLines;
    content[block.index] = { ...content[block.index], text: t.content } as typeof content[number];
    const label = `block ${block.index + 1}`;
    if (t.truncated) {
      if (event.toolName === 'read' && !event.isError && typeof event.input.path === 'string') {
        const offset = typeof event.input.offset === 'number' ? event.input.offset : 1;
        // Reuse the exact successful read argument. Pi also accepts @/~/Unicode
        // path variants; inventing a different absolute path here can be wrong.
        const source = event.input.path;
        const next = offset + t.outputLines;
        // read has one text block in the installed Pi; never invent character offsets.
        notes.push(`${label}: read path=${JSON.stringify(source)}, cwd=${cwd}; displayed lines ${t.outputLines ? `${offset}-${next - 1}` : 'none'}. ` +
          (t.firstLineExceedsLimit ? `Line ${offset} exceeds the byte budget; use the existing bash/parser to extract fields or character segments of that line (for example Python splitlines()[${offset - 1}][start:end]); do not repeat the same read offset.`
            : `Use read offset=${next} to continue.`));
        sources.push({ block: block.index + 1, path: source, cwd, scope: 'successful read source argument', available: 'read-succeeded' });
      } else {
        const existing = accessible(block.source) ? block.source : undefined;
        const saved = existing ?? saveAvailableOutput(cwd, block.original);
        const scope = existing ? 'native full output' : block.upstream ? 'upstream already-truncated tool return; original total unknown' : 'this tool return';
        notes.push(`${label}: ${scope}. ` + (saved ? `Read source: ${saved}.` : 'Full output could not be saved; omitted text is not recoverable from this result.') +
          (block.source && !existing ? ` Original source unavailable: ${block.source}.` : '') +
          (['grep', 'rg', 'find', 'ls'].includes(event.toolName) ? ' Refine the existing search, or save a complete search to an authorized output file; this saved return is not all matches.' : ''));
        sources.push({ block: block.index + 1, path: saved, scope, available: !!saved });
      }
    }
    notes.push(...block.notices);
  }
  // One compact footer, regardless of text-block count. Non-text positions remain intact.
  const last = blocks.at(-1)!;
  const footer = `\n\n[pi-output-limits: body ${outputBytes} bytes/${outputLines} lines; shared limit ${limits.maxBytes} bytes/${limits.maxLines} lines. ${[...new Set(notes)].join('\n')}]`;
  content[last.index] = { ...content[last.index], text: (content[last.index] as any).text + footer } as typeof content[number];
  const finalText = content.filter(b => b.type === 'text').map(b => b.text).join('');
  return { content, details: { ...details, outputLimits: { bodyBytes: outputBytes, bodyLines: outputLines,
    finalBytes: Buffer.byteLength(finalText), finalCodePoints: [...finalText].length, textBlocks: blocks.length, truncated: true, sources } } };
}

/** Called by BioMind's existing receipt formatter, before JSON.stringify. */
export function projectReceipt(value: unknown, name: string, cwd: string, limits: Limits): unknown {
  if (!record(value) || name === 'ask_user') return value;
  const budget = { ...limits };
  // Already-paged content must be preserved exactly and consumes the shared body budget.
  const reserve = (item: any, key = '') => {
    if (typeof item === 'string' && (pagedFields.has(key) || key === 'content' && ['memory_read', 'inspect_run'].includes(name))) spend(budget, item);
    else if (record(item)) for (const [k, v] of Object.entries(item)) reserve(v, k);
  };
  reserve(value);
  const omitted: string[] = [];
  const visit = (item: any, key: string, trail: string): any => {
    if (pagedFields.has(key) || key === 'content' && ['memory_read', 'inspect_run'].includes(name)) return item;
    if (typeof item === 'string' && bodyFields.has(key)) {
      const t = take(item, budget, ['stdout', 'stderr', 'log'].includes(key));
      spend(budget, t.content); if (t.truncated) omitted.push(trail); return t.content;
    }
    if (Array.isArray(item) && listFields.has(key)) {
      const result: any[] = [];
      for (const entry of item) {
        const encoded = JSON.stringify(entry), size = measure(encoded);
        if (size.bytes > budget.maxBytes || size.lines > budget.maxLines) { omitted.push(trail + `[${result.length}:]`); break; }
        result.push(entry); spend(budget, encoded);
      }
      return result;
    }
    if (record(item)) return Object.fromEntries(Object.entries(item).map(([k, v]) => [k, visit(v, k, trail ? trail + '.' + k : k)]));
    return item;
  };
  const result = visit(value, '', '');
  if (!omitted.length) return value;
  // A managed execution receipt already has a durable record. Other projections
  // save only this public return, never a private underlying record/environment.
  const existing = accessible(value.receipt_ref) ? value.receipt_ref : undefined;
  const logs = record(value.log_refs) && omitted.every(field => ['stdout', 'stderr'].includes(field))
    ? Object.fromEntries(omitted.filter(field => accessible(value.log_refs[field])).map(field => [field, value.log_refs[field]])) : {};
  const reuseLogs = omitted.every(field => typeof logs[field] === 'string');
  const saved = existing ?? (reuseLogs ? undefined : saveAvailableOutput(cwd, JSON.stringify(value, null, 2)));
  return { ...result, output_preview: { omitted_fields: omitted, source: saved ?? null,
    ...(reuseLogs ? { log_sources: logs } : {}), source_scope: existing ? 'existing execution receipt' : reuseLogs ? 'existing log files' : 'this tool return',
    recovery: saved || reuseLogs ? 'Read the source or use the existing run/memory reader; do not rerun computation.' : 'Full output could not be saved; omitted fields are unavailable.',
    body_bytes: limits.maxBytes - budget.maxBytes, body_lines: limits.maxLines - budget.maxLines } };
}
