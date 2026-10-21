import { selectRemoteFolder } from "./components/RemoteFolderPicker";
import { create } from "zustand";
import type {
  AppConfig,
  AppRuntime,
  ArchivedThread,
  AutomationTask,
  ContentBlock,
  ExtUiRequest,
  FileNode,
  ModelInfo,
  PendingFollowUp,
  PermissionLevel,
  PluginPackage,
  PreviewPayload,
  ProjectSummary,
  SkillInfo,
  SkillHubSkill,
  ThreadState,
  Toast,
  ToolRun,
  ViewAttachment,
  ViewMessage,
} from "./lib/types";
import { cleanOutput, extensionsAlreadyLatest, hasLibuvAssertion, lastLine, stripAnsi } from "./lib/update";
import { readProjects, readRestoration, readWindowConfig, validateSnapshot } from "./lib/remoteData";
import { reportRendererIssue } from "./lib/rendererSafety";

/* ------------------------------------------------------------------ *
 * Pure helpers
 * ------------------------------------------------------------------ */

let _c = 0;
const uid = () => `${Date.now().toString(36)}-${(_c++).toString(36)}-${Math.random().toString(36).slice(2, 7)}`;

export interface ParsedSkillBlock {
  name: string;
  location: string;
  content: string;
  userMessage?: string;
}

/** Pi expands /skill:name into a structured user-message block. */
export function parseSkillBlock(text: string): ParsedSkillBlock | null {
  const normalized = text.replace(/\r\n/g, "\n");
  const match = normalized.match(/^<skill name="([^"]+)" location="([^"]+)">\n([\s\S]*?)\n<\/skill>(?:\n\n([\s\S]+))?$/);
  if (!match) return null;
  return {
    name: match[1],
    location: match[2],
    content: match[3],
    userMessage: match[4]?.trim() || undefined,
  };
}

export interface ParsedUserMessage {
  text: string;
  attachments: ViewAttachment[];
}

/**
 * File attachments are supplied to Pi as an internal text envelope because
 * the runtime accepts images but not local file paths. Keep that envelope out
 * of the user bubble and turn it into the attachment metadata the renderer
 * can display beside the user's text.
 */
export function parseUserMessage(text: string): ParsedUserMessage {
  const normalized = (text || "").replace(/\r\n/g, "\n");
  const attachments: ViewAttachment[] = [];
  const fileBlock = /<file\b([^>]*?)\/>|<file\b([^>]*)>([\s\S]*?)<\/file>/gi;
  const readAttribute = (attrs: string, name: string): string | undefined => {
    const match = attrs.match(new RegExp(`(?:^|\\s)${name}="([^"]*)"`, "i"));
    return match?.[1];
  };

  let visible = "";
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = fileBlock.exec(normalized))) {
    const attrs = match[1] || match[2] || "";
    const name = readAttribute(attrs, "name");
    // Do not consume arbitrary user-authored <file> markup without the
    // attribute emitted by processAttachments().
    if (!name) continue;

    visible += normalized.slice(cursor, match.index);
    cursor = fileBlock.lastIndex;
    const attachment: ViewAttachment = { name };
    const path = readAttribute(attrs, "path");
    const note = readAttribute(attrs, "note");
    const error = readAttribute(attrs, "error");
    if (path) attachment.path = path;
    if (note) attachment.note = note;
    if (error) attachment.error = error;
    attachments.push(attachment);
  }

  if (!attachments.length) return { text: normalized, attachments };
  visible += normalized.slice(cursor);
  return { text: visible.trim(), attachments };
}

/** Match the command typed by the user to Pi's expanded skill message. Pi
 * persists/emits the latter, while the renderer shows the former optimistically
 * before the first RPC event arrives. */
function matchesOptimisticUserMessage(optimisticText: string, serverText: string): boolean {
  const normalize = (value: string) => value.replace(/\r\n/g, "\n").trim();
  const visibleServerText = parseUserMessage(serverText).text;
  if (normalize(optimisticText) === normalize(visibleServerText)) return true;

  const skill = parseSkillBlock(visibleServerText);
  if (!skill) return false;
  const invocation = normalize(optimisticText).match(/^\/skill:([^\s]+)(?:[ \t]+([\s\S]*))?$/i);
  if (!invocation) return false;

  return (
    invocation[1].toLowerCase() === skill.name.toLowerCase() &&
    normalize(invocation[2] || "") === normalize(skill.userMessage || "")
  );
}

/** Replace Pi's expanded skill envelope with the actual user request for
 * titles and previews. A skill without extra text falls back to its name. */
export function getDisplayUserPrompt(text: string): string {
  const visibleText = parseUserMessage(text).text;
  const skill = parseSkillBlock(visibleText);
  return skill ? skill.userMessage || `skill: ${skill.name}` : visibleText;
}

function mergeServerAttachments(server: ViewAttachment[], optimistic: ViewAttachment[] | undefined): ViewAttachment[] {
  if (!server.length) return optimistic || [];
  if (!optimistic?.length) return server;
  return server.map((attachment, index) => {
    if (attachment.path) return attachment;
    const sameName = optimistic.find((candidate) => candidate.name.toLowerCase() === attachment.name.toLowerCase());
    const fallback = sameName || optimistic[index];
    return fallback ? { ...attachment, path: fallback.path } : attachment;
  });
}

/** Keep automation session titles aligned with the selected UI language,
 * including sessions created before the title was localized. */
export function localizeAutomationThreadTitle(
  sessionName: string | null | undefined,
  language: AppConfig["language"],
): string {
  const name = (sessionName || "").trim();
  const match = name.match(/^(?:自动化|Automation)\s*[:：]\s*(.+)$/i);
  if (!match) return name;
  return `${language === "zh" ? "自动化" : "Automation"}: ${match[1].trim()}`;
}

export function getDisplayThreadTitle(
  sessionName: string | null | undefined,
  promptText: string,
  language: AppConfig["language"],
): string {
  const name = localizeAutomationThreadTitle(sessionName, language);
  const prompt = getDisplayUserPrompt(promptText).trim();
  const placeholder = /^(?:new thread|new task|新线程|新建任务)$/i.test(name);
  return name && !placeholder && !/^<skill(?:\s|>)/i.test(name) ? name : prompt;
}

/**
 * Session files come from both the renderer's project scan and the main
 * process. On Windows those sources can disagree on slash direction or leave
 * a trailing separator, which must not make two references to one thread look
 * like different threads.
 */
export function normalizeThreadFile(value: string | null | undefined): string {
  // Linux paths are case-sensitive; a backslash is a valid filename character.
  const path = value || "";
  return path === "/" ? path : path.replace(/\/+$/, "");
}

async function withTimeout<T>(promise: Promise<T>, timeoutMs: number, message: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<T>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error(message)), timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * Pi intentionally delays creating a new session's JSONL file until the first
 * assistant message exists. The sidebar is disk-backed, so without this merge
 * a successfully accepted user prompt remains invisible while the agent works.
 *
 * Add live sessions that already contain a user prompt as transient summaries.
 * Once Pi flushes the JSONL, the matching disk summary wins automatically.
 */
function mergeLiveThreadsIntoProjects(
  projects: ProjectSummary[],
  live: Record<string, ThreadState>,
  archivedProjects: string[] = [],
  archivedThreads: ArchivedThread[] = [],
  pinnedProjects: string[] = [],
  pinnedThreads: string[] = [],
  language: AppConfig["language"] = "en",
): ProjectSummary[] {
  projects = readProjects(projects);
  const archived = new Set(archivedProjects.map((cwd) => cwd));
  const archivedThreadFiles = new Set(archivedThreads.map((thread) => thread.file));
  const pinnedProjectSet = new Set(pinnedProjects.map((cwd) => cwd));
  const pinnedThreadSet = new Set(pinnedThreads.map((file) => file));
  const pinnedProjectRank = new Map(pinnedProjects.map((cwd, index) => [cwd, index]));
  const pinnedThreadRank = new Map(pinnedThreads.map((file, index) => [file, index]));
  const next = projects.map((project) => ({
    ...project,
    pinned: project.pinned || pinnedProjectSet.has(project.cwd),
    threads: project.threads
      .filter((thread) => !archivedThreadFiles.has(thread.file))
      .map((thread) => ({
        ...thread,
        title: localizeAutomationThreadTitle(thread.title, language),
        pinned: thread.pinned || pinnedThreadSet.has(thread.file),
      })),
  }));
  const byCwd = new Map(next.map((project) => [project.cwd, project]));

  for (const [threadId, thread] of Object.entries(live)) {
    if (archived.has(thread.cwd)) continue;
    const file = thread.sessionFile || threadId;
    if (!file || file.startsWith("opening-") || file.startsWith("boot:")) continue;
    if (archivedThreadFiles.has(file)) continue;
    const firstUser = thread.messages.find((message) => message.role === "user");
    if (!firstUser) continue;

    let project = byCwd.get(thread.cwd);
    if (!project) {
      const name = thread.cwd.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || thread.cwd;
      project = {
        cwd: thread.cwd,
        name,
        pinned: pinnedProjectSet.has(thread.cwd),
        threads: [],
      };
      next.unshift(project);
      byCwd.set(thread.cwd, project);
    }
    if (project.threads.some((summary) => summary.file === file)) continue;

    const userMessages = thread.messages.filter((message) => message.role === "user");
    const lastUser = userMessages[userMessages.length - 1] || firstUser;
    const firstText = getDisplayUserPrompt(firstUser.text || "").trim();
    project.threads.unshift({
      file,
      id: file,
      title: getDisplayThreadTitle(thread.sessionName, firstText, language).slice(0, 80) || (language === "zh" ? "新线程" : "New Thread"),
      preview: firstText.slice(0, 120) || (firstUser.images?.length ? "图片消息" : ""),
      updatedAt: lastUser.timestamp || Date.now(),
      messageCount: thread.messages.filter((message) => message.role === "user" || message.role === "assistant").length,
      pinned: pinnedThreadSet.has(file),
    });
  }

  for (const project of next) {
    project.threads.sort((a, b) => {
      const aPinned = a.pinned ? 0 : 1;
      const bPinned = b.pinned ? 0 : 1;
      if (aPinned !== bPinned) return aPinned - bPinned;
      if (a.pinned && b.pinned) {
        return (
          (pinnedThreadRank.get(a.file) ?? Number.MAX_SAFE_INTEGER) -
          (pinnedThreadRank.get(b.file) ?? Number.MAX_SAFE_INTEGER)
        );
      }
      return b.updatedAt - a.updatedAt;
    });
  }
  next.sort((a, b) => {
    const aPinned = a.pinned ? 0 : 1;
    const bPinned = b.pinned ? 0 : 1;
    if (aPinned !== bPinned) return aPinned - bPinned;
    if (a.pinned && b.pinned) {
      return (
        (pinnedProjectRank.get(a.cwd) ?? Number.MAX_SAFE_INTEGER) -
        (pinnedProjectRank.get(b.cwd) ?? Number.MAX_SAFE_INTEGER)
      );
    }
    if (a.openedAt !== undefined || b.openedAt !== undefined) {
      if (a.openedAt === undefined) return 1;
      if (b.openedAt === undefined) return -1;
      return b.openedAt - a.openedAt;
    }
    return (b.threads[0]?.updatedAt ?? 0) - (a.threads[0]?.updatedAt ?? 0);
  });
  return next;
}

/** Keep the open chat title in lockstep with the authoritative sidebar summary.
 * The sidebar is refreshed from session JSONL, while an already-open chat can
 * still hold the old session_info value (or null for a newly named session). */
function syncOpenThreadTitles(
  threads: Record<string, ThreadState>,
  projects: ProjectSummary[],
): Record<string, ThreadState> {
  const titleByFile = new Map<string, string>();
  for (const project of projects) {
    for (const summary of project.threads) {
      const file = normalizeThreadFile(summary.file || summary.id);
      const title = (summary.title || "").trim();
      if (file && title) titleByFile.set(file, title);
    }
  }

  let next = threads;
  for (const [id, thread] of Object.entries(threads)) {
    // A stale fresh-session flag must not block title reconciliation once the
    // thread already contains a real transcript.
    if (thread.isNewSession && thread.messages.length === 0) continue;
    const file = normalizeThreadFile(thread.sessionFile || id);
    const title = titleByFile.get(file);
    if (!title || thread.sessionName === title) continue;
    if (next === threads) next = { ...threads };
    next[id] = { ...thread, sessionName: title };
  }
  return next;
}

function findProjectThreadTitle(projects: ProjectSummary[], file: string | null | undefined): string | null {
  const target = normalizeThreadFile(file);
  if (!target) return null;
  for (const project of projects) {
    const summary = project.threads.find((thread) => normalizeThreadFile(thread.file || thread.id) === target);
    if (summary?.title?.trim()) return summary.title.trim();
  }
  return null;
}

function textOfContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((b: any) => (b && b.type === "text" ? b.text : "")).filter(Boolean).join("\n");
  return "";
}

function imagesOfContent(content: unknown): { dataUrl: string; mimeType: string }[] {
  if (!Array.isArray(content)) return [];
  const out: { dataUrl: string; mimeType: string }[] = [];
  for (const b of content as any[]) {
    if (b && b.type === "image" && b.data) out.push({ dataUrl: `data:${b.mimeType || "image/png"};base64,${b.data}`, mimeType: b.mimeType || "image/png" });
  }
  return out;
}

function blocksOfContent(content: unknown): ContentBlock[] {
  if (!Array.isArray(content)) return [];
  const out: ContentBlock[] = [];
  for (const [contentIndex, b] of (content as any[]).entries()) {
    if (!b) continue;
    if (b.type === "text") out.push({ type: "text", text: b.text || "" });
    else if (b.type === "thinking") out.push({ type: "thinking", thinking: b.thinking || "" });
    else if (b.type === "toolCall") {
      out.push({
        type: "toolCall",
        id: typeof b.id === "string" && b.id ? b.id : `tc-${contentIndex}`,
        name: typeof b.name === "string" && b.name ? b.name : "tool",
        arguments: b.arguments || {},
        contentIndex,
      });
    }
  }
  return out;
}

function addText(blocks: ContentBlock[], delta: string): ContentBlock[] {
  const b = [...blocks];
  const last = b[b.length - 1];
  if (last && last.type === "text") b[b.length - 1] = { ...last, text: last.text + delta };
  else b.push({ type: "text", text: delta });
  return b;
}
function addThinking(blocks: ContentBlock[], delta: string): ContentBlock[] {
  const b = [...blocks];
  const last = b[b.length - 1];
  if (last && last.type === "thinking") b[b.length - 1] = { ...last, thinking: last.thinking + delta };
  else b.push({ type: "thinking", thinking: delta });
  return b;
}

function usableToolName(value: unknown): string | undefined {
  const name = typeof value === "string" ? value.trim() : "";
  return name && name.toLowerCase() !== "tool" ? name : undefined;
}

function isToolContentIndex(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isPlaceholderToolCallId(value: unknown): boolean {
  return typeof value === "string" && /^tc-\d+$/.test(value);
}

function hasToolArguments(value: unknown): boolean {
  return !!value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length > 0;
}

function sameToolArguments(left: unknown, right: unknown): boolean {
  if (!hasToolArguments(left) || !hasToolArguments(right)) return false;
  try {
    return JSON.stringify(left) === JSON.stringify(right);
  } catch {
    return false;
  }
}

function toolBlockById(
  blocks: ContentBlock[],
  id?: string,
  contentIndex?: number,
): Extract<ContentBlock, { type: "toolCall" }> | undefined {
  if (id) {
    const byId = blocks.find((block): block is Extract<ContentBlock, { type: "toolCall" }> => block.type === "toolCall" && block.id === id);
    if (byId) return byId;
  }
  if (isToolContentIndex(contentIndex)) {
    return blocks.find(
      (block): block is Extract<ContentBlock, { type: "toolCall" }> =>
        block.type === "toolCall" && block.contentIndex === contentIndex,
    );
  }
  return undefined;
}

function findToolRun(
  runs: Record<string, ToolRun>,
  id?: string,
  name?: unknown,
  args?: unknown,
  contentIndex?: number,
): { key: string; run: ToolRun } | undefined {
  if (id && runs[id]) return { key: id, run: runs[id] };
  const pending = Object.entries(runs).filter(([, run]) => run.completed !== true);
  if (isToolContentIndex(contentIndex)) {
    const byContentIndex = pending.find(([, run]) => run.contentIndex === contentIndex);
    if (byContentIndex) return { key: byContentIndex[0], run: byContentIndex[1] };
  }
  if (hasToolArguments(args)) {
    const byArgs = pending.find(([, run]) => sameToolArguments(run.args, args));
    if (byArgs) return { key: byArgs[0], run: byArgs[1] };
  }
  const usableName = usableToolName(name);
  if (usableName) {
    const byName = pending.find(([, run]) => usableToolName(run.name)?.toLowerCase() === usableName.toLowerCase());
    if (byName) return { key: byName[0], run: byName[1] };
  }
  const placeholder = isToolContentIndex(contentIndex) ? undefined : pending.find(([key]) => isPlaceholderToolCallId(key));
  return placeholder ? { key: placeholder[0], run: placeholder[1] } : undefined;
}

function renameToolRun(
  runs: Record<string, ToolRun>,
  fromId: string | undefined,
  toId: string,
  contentIndex?: number,
): Record<string, ToolRun> {
  if (!fromId || fromId === toId || !runs[fromId]) return runs;
  const from = runs[fromId];
  const to = runs[toId];
  const merged: ToolRun = {
    ...from,
    ...(to || {}),
    id: toId,
    name: usableToolName(to?.name) || usableToolName(from.name) || to?.name || from.name || "tool",
    args: hasToolArguments(to?.args) ? to.args : from.args || to?.args || {},
    contentIndex: to?.contentIndex ?? from.contentIndex ?? contentIndex,
    argsStr: to?.argsStr || from.argsStr,
  };
  const next = { ...runs };
  delete next[fromId];
  next[toId] = merged;
  return next;
}

function upsertToolBlock(blocks: ContentBlock[], id: string, name: string, args?: unknown, contentIndex?: number): ContentBlock[] {
  const previous = toolBlockById(blocks, id, contentIndex);
  const nextId = id || previous?.id || (isToolContentIndex(contentIndex) ? `tc-${contentIndex}` : `tc-${uid()}`);
  const nextName = usableToolName(name) || usableToolName(previous?.name) || previous?.name || "tool";
  const previousArgs = previous?.arguments;
  const nextArgs = hasToolArguments(args) ? args : hasToolArguments(previousArgs) ? previousArgs : args ?? previousArgs ?? {};
  if (previous) {
    const nextBlock = { ...previous, id: nextId, name: nextName, arguments: nextArgs };
    if (isToolContentIndex(contentIndex)) nextBlock.contentIndex = contentIndex;
    return blocks.map((block) => (block === previous ? nextBlock : block));
  }
  return [
    ...blocks,
    {
      type: "toolCall",
      id: nextId,
      name: nextName,
      arguments: nextArgs,
      ...(isToolContentIndex(contentIndex) ? { contentIndex } : {}),
    },
  ];
}

function updateToolBlockName(
  blocks: ContentBlock[] | undefined,
  id: string,
  name: string,
  args?: unknown,
  contentIndex?: number,
): ContentBlock[] | undefined {
  if (!blocks) return blocks;
  return upsertToolBlock(blocks, id, name, args, contentIndex);
}

function updateLatestMessageToolBlock(
  messages: ViewMessage[],
  fromId: string | undefined,
  toId: string,
  name: string,
  args?: unknown,
  contentIndex?: number,
): ViewMessage[] {
  const next = [...messages];
  for (let i = next.length - 1; i >= 0; i--) {
    const blocks = next[i].blocks;
    if (!blocks) continue;
    const hasMatch = blocks.some(
      (block) =>
        block.type === "toolCall" &&
        ((!!fromId && block.id === fromId) || block.id === toId || (isToolContentIndex(contentIndex) && block.contentIndex === contentIndex)),
    );
    if (!hasMatch) continue;
    next[i] = { ...next[i], blocks: upsertToolBlock(blocks, toId, name, args, contentIndex) };
    break;
  }
  return next;
}

function reconcileAssistantBlocks(
  previousBlocks: ContentBlock[],
  finalContent: unknown,
  runs: Record<string, ToolRun>,
): { blocks: ContentBlock[]; toolRuns: Record<string, ToolRun> } {
  const finalBlocks = blocksOfContent(finalContent);
  if (!finalBlocks.length) return { blocks: previousBlocks, toolRuns: runs };
  let nextRuns = runs;
  const blocks = finalBlocks.map((block) => {
    if (block.type !== "toolCall") return block;
    const previous = toolBlockById(previousBlocks, block.id, block.contentIndex);
    const match =
      findToolRun(nextRuns, block.id, block.name, block.arguments, block.contentIndex) ||
      findToolRun(nextRuns, previous?.id, previous?.name, previous?.arguments, previous?.contentIndex);
    const matchedId = match?.key;
    const realMatchedId = matchedId && !isPlaceholderToolCallId(matchedId) ? matchedId : undefined;
    const realPreviousId = previous?.id && !isPlaceholderToolCallId(previous.id) ? previous.id : undefined;
    const id = realMatchedId || realPreviousId || block.id;
    if (matchedId && matchedId !== id) nextRuns = renameToolRun(nextRuns, matchedId, id, block.contentIndex);
    const run = nextRuns[id];
    const name = usableToolName(block.name) || usableToolName(run?.name) || usableToolName(previous?.name) || "tool";
    const args = hasToolArguments(block.arguments)
      ? block.arguments
      : hasToolArguments(run?.args)
        ? run?.args
        : previous?.arguments || {};
    nextRuns = {
      ...nextRuns,
      [id]: {
        ...(run || { id, args: {}, running: false }),
        id,
        name,
        args,
        contentIndex: block.contentIndex,
      },
    };
    return { ...block, id, name, arguments: args };
  });
  return { blocks, toolRuns: nextRuns };
}

const newAssistant = (key?: string): ViewMessage => ({ key: key || `a-${uid()}`, role: "assistant", blocks: [], timestamp: Date.now() });

/** Match visible user/assistant messages to Pi's stable session-entry ids.
 * Text plus branch order handles repeated content without leaking ids from an
 * inactive branch. */
function attachBranchEntryIds(
  messages: ViewMessage[],
  branchMessages: { entryId: string; role: "user" | "assistant"; text: string; replyStartedAt?: number; replyEndedAt?: number }[] | undefined,
): ViewMessage[] {
  if (!branchMessages?.length) return messages;
  let cursor = 0;
  return messages.map((message) => {
    if (message.role !== "user" && message.role !== "assistant") return message;
    const text =
      message.role === "user"
        ? message.text || ""
        : (message.blocks || []).map((block) => (block.type === "text" ? block.text : "")).filter(Boolean).join("\n");
    let match = -1;
    for (let i = cursor; i < branchMessages.length; i++) {
      if (branchMessages[i]?.role === message.role && branchMessages[i]?.text === text) {
        match = i;
        break;
      }
    }
    if (match < 0) {
      for (let i = cursor; i < branchMessages.length; i++) {
        if (branchMessages[i]?.role === message.role) {
          match = i;
          break;
        }
      }
    }
    if (match < 0) return message;
    cursor = match + 1;
    const matched = branchMessages[match];
    return { ...message, branchEntryId: matched.entryId,
      replyStartedAt: matched.replyStartedAt ?? message.replyStartedAt,
      replyEndedAt: matched.replyEndedAt ?? message.replyEndedAt };
  });
}

/** Convert a flat list of pi AgentMessages into renderable views + initial tool runs. */
function historyToView(
  messages: any[],
  branchMessages?: { entryId: string; role: "user" | "assistant"; text: string; replyStartedAt?: number; replyEndedAt?: number }[],
): { views: ViewMessage[]; toolRuns: Record<string, ToolRun> } {
  const toolResultById: Record<string, { text: string; isError: boolean; nestedCalls?: ToolRun["nestedCalls"] }> = {};
  for (const m of messages || []) {
    if (m?.role === "toolResult" && m.toolCallId) {
      toolResultById[m.toolCallId] = { text: textOfContent(m.content), isError: !!m.isError, nestedCalls: m.nestedCalls };
    }
  }
  const views: ViewMessage[] = [];
  const toolRuns: Record<string, ToolRun> = {};
  (messages || []).forEach((m, i) => {
    if (!m) return;
    if (m.role === "user") {
      const parsed = parseUserMessage(textOfContent(m.content));
      views.push({
        key: `hu-${i}`,
        role: "user",
        text: parsed.text,
        images: imagesOfContent(m.content),
        attachments: parsed.attachments,
        timestamp: m.timestamp,
      });
    } else if (m.role === "assistant") {
      const blocks = blocksOfContent(m.content);
      for (const b of blocks) {
        if (b.type === "toolCall") {
          const tr = toolResultById[b.id];
          toolRuns[b.id] = {
            id: b.id,
            name: b.name,
            args: b.arguments,
            running: false,
            contentIndex: b.contentIndex,
            completed: !!tr,
            isError: tr?.isError,
            resultText: tr?.text,
            nestedCalls: tr?.nestedCalls,
          };
        }
      }
      views.push({
        key: `ha-${i}`,
        role: "assistant",
        blocks,
        timestamp: m.timestamp,
        provider: m.provider,
        model: m.responseModel || m.model,
        stopReason: m.stopReason,
        errorMessage: m.errorMessage,
      });
    }
  });
  return { views: attachBranchEntryIds(views, branchMessages), toolRuns };
}

function pendingToArgs(p: PendingFollowUp): {
  imgs?: { data: string; mimeType: string }[];
  atts?: { abs: string; name: string }[];
} {
  const imgs = p.images.map((im) => ({ data: im.base64, mimeType: im.mimeType }));
  const atts = p.files.map((f) => ({ abs: f.abs, name: f.name }));
  return { imgs: imgs.length ? imgs : undefined, atts: atts.length ? atts : undefined };
}

function pendingPromptText(p: PendingFollowUp): string {
  const text = p.text.trim();
  const references = (p.htmlReferences || [])
    .map((reference) => reference.reference.trim())
    .filter(Boolean)
    .join("\n\n");
  return text ? (references ? `${text}\n\n${references}` : text) : references;
}

/**
 * Registered extension commands are handled by Pi outside the agent loop.
 * They can complete successfully without emitting agent_start/agent_settled,
 * so they must not leave the renderer in its optimistic streaming state.
 */
function isExtensionCommandPrompt(text: string, commands: any[] | undefined): boolean {
  const match = text.trim().match(/^\/([^\s]+)/);
  if (!match) return false;
  const invocation = match[1];
  return (commands || []).some((command) =>
    command?.source === "extension" && String(command.name || "").trim() === invocation,
  );
}

function emptyThread(cwd: string): ThreadState {
  return {
    cwd,
    sessionFile: null,
    sessionName: null,
    model: null,
    models: [],
    thinking: "off",
    levels: ["off"],
    commands: [],
    isStreaming: false,
    messages: [],
    streaming: null,
    toolRuns: {},
    permission: "sandbox",
  };
}

function threadFromResponse(res: any, fallback: ThreadState, pendingEditorText?: string): ThreadState {
  validateSnapshot(res);
  const liveEvents = Array.isArray(res.liveEvents) ? res.liveEvents : [];
  const messageKey = (message: any) => JSON.stringify([message?.role, message?.timestamp ?? null, message?.content]);
  const covered = new Set(liveEvents.filter((event: any) => event.message && ["message_start", "message_update", "message_end"].includes(event.type)).map((event: any) => messageKey(event.message)));
  // Native history may already contain a complete message in the live snapshot.
  // Drop only its latest matching occurrence; repeated older messages remain.
  const messages = [...(res.messages || [])].reverse().filter((message: any) => {
    const key = messageKey(message);
    if (!covered.has(key)) return true;
    covered.delete(key); return false;
  }).reverse();
  const { views, toolRuns } = historyToView(messages, res.branchMessages || []);
  const replyStartedAt = res.isStreaming ? res.replyStartedAt ?? fallback.replyStartedAt : undefined;
  if (replyStartedAt !== undefined) {
    for (let i = views.length - 1; i >= 0 && views[i].role === "assistant"; i--) {
      if (views[i].replyEndedAt !== undefined) break;
      views[i].replyStartedAt = replyStartedAt;
    }
  }
  let thread: ThreadState = {
    ...emptyThread(res.cwd || fallback.cwd),
    sessionFile: res.sessionFile,
    sessionName: res.sessionName,
    model: res.model,
    models: res.models || fallback.models || [],
    thinking: res.thinkingLevel || fallback.thinking || "off",
    levels: fallback.levels || ["off"],
    commands: res.commands || [],
    connected: res.connected !== false,
    isStreaming: !!res.isStreaming,
    replyStartedAt,
    streaming: replyStartedAt !== undefined && views[views.length - 1]?.role !== "assistant" ? { ...newAssistant(), replyStartedAt } : null,
    messages: views,
    toolRuns,
    permission: res.permission || fallback.permission || "sandbox",
    pendingEditorText: pendingEditorText ?? fallback.pendingEditorText,
    pendingFollowUp: fallback.pendingFollowUp,
    extensionStatuses: fallback.extensionStatuses,
    extensionWidgets: fallback.extensionWidgets,
  };
  for (const event of liveEvents) thread = reduceThread(thread, event);
  return thread;
}

/* ------------------------------------------------------------------ *
 * Event reducer (one thread)
 * ------------------------------------------------------------------ */

function reduceThread(t: ThreadState, event: any): ThreadState {
  if (!event || typeof event !== "object") return t;
  if (event.parentToolCallId && String(event.type).startsWith("tool_execution_")) {
    const parent = t.toolRuns[event.parentToolCallId];
    if (!parent) return t;
    const calls = [...(parent.nestedCalls?.calls || [])];
    const idx = calls.findIndex(c => c.id === event.toolCallId);
    const prev = idx < 0 ? undefined : calls[idx];
    const call = { ...prev, id: String(event.toolCallId), name: String(event.toolName || prev?.name || "tool"), arguments: event.args || prev?.arguments, status: (event.type === "tool_execution_end" ? event.isError ? "error" : "ok" : "unfinished") as "ok" | "error" | "unfinished", error: event.isError ? textOfContent(event.result?.content) : undefined };
    if (idx < 0) calls.push(call); else calls[idx] = call;
    return { ...t, toolRuns: { ...t.toolRuns, [event.parentToolCallId]: { ...parent, nestedCalls: { calls, complete: calls.every(c => c.status !== "unfinished") } } } };
  }
  switch (event.type) {
    case "agent_start": {
      const replyStartedAt = t.replyStartedAt ?? event.replyStartedAt ?? Date.now();
      return { ...t, isStreaming: true, replyStartedAt,
        streaming: t.streaming || { ...newAssistant(), replyStartedAt }, error: undefined };
    }
    case "biomind_result": {
      // Finishing the Main request does not establish scientific success. Keep
      // a server-reported failure visible even when no assistant text arrived.
      if (event.runtime_status === "error" || event.runtime_status === "failed") return { ...t, error: "BioMind 服务器报告本次执行失败；未自动重试。请检查工具过程和服务器运行状态。" };
      if (event.scientific_success === false) return { ...t, error: "BioMind 已结束本次执行，但服务器报告科学目标未达成。请检查实际产物与工具结果。" };
      return t;
    }
    case "agent_settled": {
      // If a streaming assistant message never got message_end, finalize it.
      const endedAt = event.replyEndedAt ?? Date.now();
      const messages = t.streaming ? [...t.messages, t.streaming] : t.messages;
      return { ...t, isStreaming: false, replyStartedAt: undefined, streaming: null,
        messages: messages.map(message => message.role === "assistant" && message.replyStartedAt === t.replyStartedAt && t.replyStartedAt !== undefined
          ? { ...message, replyEndedAt: endedAt } : message) };
    }
    case "message_start": {
      const m = event.message;
      if (!m) return t;
      if (m.role === "user") {
        const serverText = textOfContent(m.content);
        const parsedServer = parseUserMessage(serverText);
        const serverImages = imagesOfContent(m.content);
        let optimisticIndex = -1;
        for (let i = t.messages.length - 1; i >= 0; i--) {
          const candidate = t.messages[i];
          if (!candidate?.key.startsWith("opt-")) continue;
          if (!serverText || matchesOptimisticUserMessage(candidate.text || "", serverText)) {
            optimisticIndex = i;
            break;
          }
        }
        if (optimisticIndex >= 0) {
          // Connection remaps and concurrent events may append another message
          // after this bubble. Promote the matching optimistic item in place,
          // retaining local image data if the live Pi event omits its payload.
          const optimistic = t.messages[optimisticIndex];
          const promoted: ViewMessage = {
            ...optimistic,
            key: `u-${uid()}`,
            text: parsedServer.text || optimistic.text,
            images: serverImages.length ? serverImages : optimistic.images,
            attachments: mergeServerAttachments(parsedServer.attachments, optimistic.attachments),
            timestamp: m.timestamp,
          };
          const messages = [...t.messages];
          messages[optimisticIndex] = promoted;
          return { ...t, messages };
        }
        const view: ViewMessage = {
          key: `u-${uid()}`,
          role: "user",
          text: parsedServer.text,
          images: serverImages,
          attachments: parsedServer.attachments,
          timestamp: m.timestamp,
        };
        return { ...t, messages: [...t.messages, view] };
      }
      if (m.role === "assistant") {
        return { ...t, streaming: { ...newAssistant(), replyStartedAt: t.replyStartedAt } };
      }
      return t;
    }
    case "message_end": {
      const m = event.message;
      if (m?.role === "assistant") {
        const streaming = t.streaming || { ...newAssistant(), replyStartedAt: t.replyStartedAt };
        const reconciled = reconcileAssistantBlocks(streaming.blocks || [], m.content, t.toolRuns);
        const final: ViewMessage = {
          ...streaming,
          blocks: reconciled.blocks,
          provider: m.provider || streaming.provider,
          model: m.responseModel || m.model || streaming.model,
          stopReason: m.stopReason,
          errorMessage: m.errorMessage,
          timestamp: m.timestamp || streaming.timestamp,
        };
        return { ...t, streaming: null, messages: [...t.messages, final], toolRuns: reconciled.toolRuns };
      }
      return t;
    }
    case "message_update": {
      const ame = event.assistantMessageEvent;
      if (!ame) {
        const message = event.message;
        if (message?.role !== "assistant" || !Array.isArray(message.content)) return t;
        const streaming = t.streaming || { ...newAssistant(), replyStartedAt: t.replyStartedAt };
        const reconciled = reconcileAssistantBlocks(streaming.blocks || [], message.content, t.toolRuns);
        return { ...t, isStreaming: true, streaming: { ...streaming, blocks: reconciled.blocks, timestamp: message.timestamp || streaming.timestamp, provider: message.provider || streaming.provider, model: message.responseModel || message.model || streaming.model }, toolRuns: reconciled.toolRuns };
      }
      const s = t.streaming || { ...newAssistant(), replyStartedAt: t.replyStartedAt };
      let blocks = s.blocks || [];
      // Copy-on-write: text/thinking deltas (the overwhelmingly common case)
      // must NOT create a new toolRuns object, or every memoized consumer of
      // toolRuns re-renders on every token.
      let runs = t.toolRuns;
      switch (ame.type) {
        case "text_delta":
          blocks = addText(blocks, ame.delta || "");
          break;
        case "thinking_delta":
          blocks = addThinking(blocks, ame.delta || "");
          break;
        case "toolcall_start": {
          const ci = isToolContentIndex(ame.contentIndex) ? ame.contentIndex : undefined;
          const partial = isToolContentIndex(ci) ? ame.partial?.content?.[ci] : undefined;
          const providedId = typeof ame.toolCall?.id === "string" && ame.toolCall.id ? ame.toolCall.id : typeof partial?.id === "string" && partial.id ? partial.id : undefined;
          const existingBlock = toolBlockById(blocks, providedId, ci);
          const existingMatch = findToolRun(runs, providedId, ame.toolCall?.name || partial?.name, ame.toolCall?.arguments || partial?.arguments, ci);
          const id = providedId || (isToolContentIndex(ci) ? existingMatch?.key || `tc-${ci}` : existingMatch?.key || `tc-${uid()}`);
          runs = renameToolRun(runs, existingMatch?.key, id, ci);
          const existingRun = runs[id];
          const name = usableToolName(ame.toolCall?.name) || usableToolName(partial?.name) || usableToolName(existingRun?.name) || usableToolName(existingBlock?.name) || "tool";
          const candidateArgs = ame.toolCall?.arguments || partial?.arguments;
          const args = hasToolArguments(candidateArgs) ? candidateArgs : existingRun?.args || existingBlock?.arguments || {};
          blocks = upsertToolBlock(blocks, id, name, args, ci);
          runs = {
            ...runs,
            [id]: {
              ...(existingRun || { id, args: {}, running: false, argsStr: "" }),
              id,
              name,
              args,
              contentIndex: ci,
            },
          };
          break;
        }
        case "toolcall_delta": {
          const ci = isToolContentIndex(ame.contentIndex) ? ame.contentIndex : undefined;
          const partial = isToolContentIndex(ci) ? ame.partial?.content?.[ci] : undefined;
          const providedId = typeof ame.toolCall?.id === "string" && ame.toolCall.id ? ame.toolCall.id : typeof partial?.id === "string" && partial.id ? partial.id : undefined;
          const match = findToolRun(runs, providedId, partial?.name, partial?.arguments, ci);
          const block = toolBlockById(blocks, providedId, ci);
          const id = providedId || match?.key || block?.id || (isToolContentIndex(ci) ? `tc-${ci}` : undefined);
          if (id) {
            runs = renameToolRun(runs, match?.key, id, ci);
            const r = runs[id] || { id, name: block?.name || partial?.name || "tool", args: block?.arguments || partial?.arguments || {}, running: false, argsStr: "" };
            const name = usableToolName(partial?.name) || usableToolName(r.name) || "tool";
            const args = hasToolArguments(partial?.arguments) ? partial.arguments : r.args;
            blocks = upsertToolBlock(blocks, id, name, args, ci);
            runs = { ...runs, [id]: { ...r, id, name, args, contentIndex: ci, argsStr: (r.argsStr || "") + (ame.delta || "") } };
          }
          break;
        }
        case "toolcall_end": {
          const ci = isToolContentIndex(ame.contentIndex) ? ame.contentIndex : undefined;
          const partial = isToolContentIndex(ci) ? ame.partial?.content?.[ci] : undefined;
          const providedId = typeof ame.toolCall?.id === "string" && ame.toolCall.id ? ame.toolCall.id : typeof partial?.id === "string" && partial.id ? partial.id : undefined;
          const match = findToolRun(runs, providedId, ame.toolCall?.name || partial?.name, ame.toolCall?.arguments || partial?.arguments, ci);
          const initialId = providedId || match?.key || (isToolContentIndex(ci) ? `tc-${ci}` : undefined);
          if (initialId) {
            runs = renameToolRun(runs, match?.key, initialId, ci);
            const r = runs[initialId];
            let parsed: unknown = ame.toolCall?.arguments || partial?.arguments;
            if (!hasToolArguments(parsed) && r?.argsStr) {
              try {
                parsed = JSON.parse(r.argsStr);
              } catch {
                /* Keep the last parsed arguments when the delta is incomplete. */
              }
            }
            const args = hasToolArguments(parsed) ? parsed : r?.args || {};
            const name = usableToolName(ame.toolCall?.name) || usableToolName(partial?.name) || usableToolName(r?.name) || "tool";
            blocks = upsertToolBlock(blocks, initialId, name, args, ci);
            runs = {
              ...runs,
              [initialId]: {
                ...(r || { id: initialId, running: false }),
                id: initialId,
                name,
                args,
                contentIndex: ci,
              },
            };
          }
          break;
        }
        default:
          break;
      }
      if (blocks === s.blocks && runs === t.toolRuns && s === t.streaming) return t;
      return { ...t, streaming: { ...s, blocks }, toolRuns: runs };
    }
    case "tool_execution_start": {
      const id = typeof event.toolCallId === "string" ? event.toolCallId : "";
      if (!id) return t;
      const match = findToolRun(t.toolRuns, id, event.toolName, event.args);
      let toolRuns = renameToolRun(t.toolRuns, match?.key, id, match?.run?.contentIndex);
      const prev = toolRuns[id] || { id, name: event.toolName || "tool", args: {}, running: false };
      const name = usableToolName(event.toolName) || usableToolName(prev.name) || "tool";
      const args = hasToolArguments(event.args) ? event.args : prev.args;
      const contentIndex = prev.contentIndex ?? match?.run?.contentIndex;
      const updated = {
        ...prev,
        id,
        name,
        args,
        contentIndex,
        running: true,
        completed: false,
        isError: false,
        startedAt: prev.startedAt || Date.now(),
        endedAt: undefined,
      };
      return {
        ...t,
        messages: updateLatestMessageToolBlock(t.messages, match?.key, id, name, args, contentIndex),
        streaming: t.streaming ? { ...t.streaming, blocks: updateToolBlockName(t.streaming.blocks, id, name, args, contentIndex) } : t.streaming,
        toolRuns: { ...toolRuns, [id]: updated },
      };
    }
    case "tool_execution_update": {
      const id = typeof event.toolCallId === "string" ? event.toolCallId : "";
      if (!id) return t;
      const match = findToolRun(t.toolRuns, id, event.toolName, event.args);
      let toolRuns = renameToolRun(t.toolRuns, match?.key, id, match?.run?.contentIndex);
      const prev = toolRuns[id] || { id, name: event.toolName || "tool", args: {}, running: true };
      const name = usableToolName(event.toolName) || usableToolName(prev.name) || "tool";
      const args = hasToolArguments(event.args) ? event.args : prev.args;
      const contentIndex = prev.contentIndex ?? match?.run?.contentIndex;
      return {
        ...t,
        messages: updateLatestMessageToolBlock(t.messages, match?.key, id, name, args, contentIndex),
        streaming: t.streaming ? { ...t.streaming, blocks: updateToolBlockName(t.streaming.blocks, id, name, args, contentIndex) } : t.streaming,
        toolRuns: {
          ...toolRuns,
          [id]: { ...prev, id, name, args, contentIndex, partialText: textOfContent(event.partialResult?.content), startedAt: prev.startedAt || Date.now() },
        },
      };
    }
    case "tool_execution_end": {
      const id = typeof event.toolCallId === "string" ? event.toolCallId : "";
      if (!id) return t;
      const match = findToolRun(t.toolRuns, id, event.toolName, event.args);
      let toolRuns = renameToolRun(t.toolRuns, match?.key, id, match?.run?.contentIndex);
      const prev = toolRuns[id] || { id, name: event.toolName || "tool", args: {}, running: false };
      const name = usableToolName(event.toolName) || usableToolName(prev.name) || "tool";
      const args = hasToolArguments(event.args) ? event.args : prev.args;
      const contentIndex = prev.contentIndex ?? match?.run?.contentIndex;
      const endedAt = Date.now();
      return {
        ...t,
        messages: updateLatestMessageToolBlock(t.messages, match?.key, id, name, args, contentIndex),
        streaming: t.streaming ? { ...t.streaming, blocks: updateToolBlockName(t.streaming.blocks, id, name, args, contentIndex) } : t.streaming,
        toolRuns: {
          ...toolRuns,
          [id]: {
            ...prev,
            id,
            name,
            args,
            contentIndex,
            running: false,
            completed: true,
            isError: !!event.isError,
            resultText: textOfContent(event.result?.content),
            nestedCalls: event.result?.nestedCalls || prev.nestedCalls,
            partialText: undefined,
            startedAt: prev.startedAt || endedAt,
            endedAt,
          },
        },
      };
    }
    default:
      return t;
  }
}

/* ------------------------------------------------------------------ *
 * Store
 * ------------------------------------------------------------------ */

interface FileTreeEntry {
  nodes: FileNode[];
  loaded: boolean;
  expanded: boolean;
}

interface PiStore {
  // app
  config: AppConfig | null;
  runtime: AppRuntime | null;
  projects: ProjectSummary[];
  projectsLoading: boolean;

  // layout
  sidebarOpen: boolean;
  previewOpen: boolean;
  /** Preview occupies the chat workspace while preserving the mounted chat state. */
  previewExpanded: boolean;
  sidebarTab: "threads" | "files";

  // projects / threads
  activeProjectCwd: string | null;
  expandedProjects: Record<string, boolean>;
  openThreadIds: string[];
  activeThreadId: string | null;
  threads: Record<string, ThreadState>;

  // files / preview
  fileTree: Record<string, FileTreeEntry>;
  previewPath: string | null;
  previewRoot: string | null;
  previewPayload: PreviewPayload | null;
  previewLoading: boolean;

  // overlay
  toasts: Toast[];
  extuiQueue: { threadId: string; request: ExtUiRequest }[];

  // actions
  bootstrap: () => Promise<void>;
  connectionStatus: string;
  connectionRevision: number;
  restoreRemoteThreads: () => Promise<void>;
  applyRemoteSnapshot: (snapshot: any) => Promise<void>;
  refreshProjects: (strict?: boolean) => Promise<void>;
  openProjectFolder: () => Promise<void>;
  setProjectPinned: (cwd: string, pinned: boolean) => Promise<void>;
  unpinProject: (cwd: string) => Promise<void>;
  setThreadPinned: (file: string, pinned: boolean) => Promise<void>;
  archiveProject: (cwd: string) => Promise<void>;
  restoreProject: (cwd: string) => Promise<void>;
  archiveThread: (cwd: string, file: string, title?: string) => Promise<void>;
  deleteThread: (cwd: string, file: string, title?: string) => Promise<void>;
  restoreThread: (file: string) => Promise<void>;
  toggleProject: (cwd: string) => void;
  setActiveProject: (cwd: string) => void;

  openThread: (cwd: string, sessionFile?: string, permission?: PermissionLevel) => Promise<string | null>;
  /** Ensure a live pi process backs the thread (adopting the warm spare).
   *  Resolves with the thread id, or null if the connection failed. Safe to
   *  call repeatedly: concurrent calls share one in-flight connect. */
  ensureConnected: (threadId: string) => Promise<string | null>;
  /** Create a new thread in the active project, prompting for a folder if none is open. */
  newTask: () => Promise<void>;
  closeThread: (id: string) => Promise<void>;
  setActiveThread: (id: string) => void;
  sendPrompt: (threadId: string, text: string, images?: { data: string; mimeType: string }[], attachments?: { abs: string; name: string }[], mode?: "steer" | "followUp") => Promise<{ accepted: boolean; receipt?: any }>;
  setPendingFollowUp: (threadId: string, pending: PendingFollowUp | null) => void;
  sendPendingSteering: (threadId: string, mode?: "steer" | "followUp") => Promise<void>;
  abortThread: (id: string) => Promise<void>;
  refreshOpenThreadModels: () => Promise<void>;
  setModel: (id: string, provider: string, modelId: string) => Promise<void>;
  setThinking: (id: string, level: string) => Promise<void>;
  newSessionInThread: (id: string) => Promise<void>;
  forkThreadFromAgentReply: (id: string, entryId: string) => Promise<void>;
  cloneThread: (id: string, entryId: string) => Promise<void>;
  renameThread: (id: string, name: string) => Promise<void>;

  setSidebarTab: (t: "threads" | "files") => void;
  toggleSidebar: () => void;
  togglePreview: () => void;
  togglePreviewExpanded: () => void;
  loadFileTree: (cwd: string, rel?: string) => Promise<void>;
  toggleFolder: (cwd: string, rel: string) => void;
  openPreview: (abs: string, projectRoot?: string) => Promise<void>;
  closePreview: () => void;

  pushToast: (kind: Toast["kind"], text: string) => void;
  dismissToast: (id: string) => void;

  handleEvent: (threadId: string, event: any) => void;
  handleExtUi: (threadId: string, req: ExtUiRequest) => void;
  respondExtUi: (threadId: string, id: string, payload: Record<string, unknown>) => Promise<void>;

  handleExit: (threadId: string, info: { code: number | null; stderr: string }) => void;
  handleError: (threadId: string, message: string) => void;

  // settings overlay
  settingsOpen: boolean;
  openSettings: () => void;
  closeSettings: () => void;

  // search overlay
  searchOpen: boolean;
  openSearch: () => void;
  closeSearch: () => void;
  /** Open a thread by session file, or focus it if already open. */
  goToThread: (cwd: string, file: string) => Promise<void>;

  // plugins overlay
  pluginsOpen: boolean;
  packages: PluginPackage[];
  skills: (SkillInfo & { resourceId?: string; threadId?: string; cwd?: string })[];
  pluginsLoading: boolean;
  pluginsLoadRevision: number;
  openPlugins: () => void;
  closePlugins: () => void;
  loadPlugins: () => Promise<void>;
  togglePackage: (source: string, enabled: boolean) => Promise<void>;
  installPackage: (source: string) => Promise<void>;
  removePackage: (source: string) => Promise<void>;
  updatePackages: (source?: string) => Promise<void>;
  toggleSkill: (path: string, enabled: boolean) => Promise<void>;
  installSkill: (skill: SkillHubSkill) => Promise<boolean>;

  // automation overlay
  automationOpen: boolean;
  tasks: AutomationTask[];
  openAutomation: () => void;
  closeAutomation: () => void;
  loadTasks: () => Promise<void>;
  saveTask: (task: AutomationTask) => Promise<void>;
  deleteTask: (id: string) => Promise<void>;
  runTaskNow: (id: string) => Promise<void>;

  // thread permission / folder
  setPermission: (threadId: string, level: PermissionLevel) => Promise<void>;
  switchThreadFolder: (threadId: string) => Promise<void>;
  /** Move a not-yet-sent task to another working folder without losing the composer draft. */
  changeDraftThreadFolder: (threadId: string, cwd: string) => Promise<void>;

  // edit menu
  editAction: (action: "copy" | "cut" | "paste" | "delete" | "selectAll") => Promise<void>;
}

const treeKey = (cwd: string, rel?: string) => `${cwd}::${rel || ""}`;

/** In-flight background connects keyed by thread id, so a click and a
 *  same-tick prompt share one process boot instead of spawning two. */
const connectPromises = new Map<string, Promise<string | null>>();
const pendingInputs = new Set<string>();
const uncertainPendingInputs = new Set<string>();
const respondingQuestions = new Set<string>();
let openIntent = 0;

/* ------------------------------------------------------------------ *
 * Event batching
 * ------------------------------------------------------------------ *
 * During streaming, pi emits many small events per second (one per text
 * delta). Applying each in its own React update keeps the main thread
 * saturated and makes unrelated UI (clicks, typing) lag. Coalesce: queue
 * incoming events and fold them into ONE store update per animation frame.
 * Render cost becomes per-frame instead of per-token, and a fast stream
 * never renders more than ~60 times a second no matter the event rate.
 */
const eventQueue: { threadId: string; event: any }[] = [];
let flushScheduled = false;

function scheduleEventFlush(): void {
  if (flushScheduled) return;
  flushScheduled = true;
  requestAnimationFrame(() => {
    flushScheduled = false;
    const batch = eventQueue.splice(0, eventQueue.length);
    if (batch.length === 0) return;

    // Group per thread and remember which threads settled this frame.
    const byThread = new Map<string, any[]>();
    const settledIds: string[] = [];
    for (const { threadId, event } of batch) {
      let arr = byThread.get(threadId);
      if (!arr) {
        arr = [];
        byThread.set(threadId, arr);
      }
      arr.push(event);
      if (event?.type === "agent_settled") settledIds.push(threadId);
    }

    useStore.setState((s) => {
      let changed = false;
      const threads = { ...s.threads };
      for (const [threadId, events] of byThread) {
        const t0 = threads[threadId];
        if (!t0) continue;
        let t = t0;
        for (const ev of events) t = reduceThread(t, ev);
        if (t !== t0) {
          threads[threadId] = t;
          changed = true;
        }
      }
      return changed ? { threads } : s;
    });

    // Deliver queued follow-ups after the settled state is applied.
    for (const threadId of settledIds) {
      const st = useStore.getState();
      const p = st.threads[threadId]?.pendingFollowUp;
      if (p) {
        void st.sendPendingSteering(threadId, "followUp");
      }
      // Message events do not include their persisted session entry ids.
      // Refresh once the turn settles so the Agent reply's Fork/Clone actions
      // are available without reopening the thread.
      window.pi.thread
        .getBranchMessages(threadId)
        .then((res: any) =>
          useStore.setState((s) =>
            s.threads[threadId]
              ? {
                  threads: {
                    ...s.threads,
                    [threadId]: {
                      ...s.threads[threadId],
                      messages: attachBranchEntryIds(s.threads[threadId].messages, res?.messages || []),
                    },
                  },
                }
              : s,
          ),
        )
        .catch(() => {});
      // The first session entry may only be visible on disk once the turn has
      // settled. Keep the project/thread index in lockstep with that lifecycle.
      const latest = useStore.getState();
      latest.refreshProjects();

      // Agent tools may have created or updated preview files. Refresh the
      // visible project's root tree, and reload an open preview so edits to the
      // HTML itself or any linked CSS/JS become visible without reopening it.
      const cwd = latest.threads[threadId]?.cwd;
      if (cwd && latest.fileTree[treeKey(cwd, "")]?.loaded) latest.loadFileTree(cwd, "");
      if (
        cwd &&
        latest.previewOpen &&
        latest.previewPath &&
        latest.previewRoot === cwd
      ) {
        latest.openPreview(latest.previewPath, cwd);
      }
    }
  });
}

export const useStore = create<PiStore>()((set, get) => ({
  config: null,
  runtime: null,
  projects: [],
  projectsLoading: false,
  sidebarOpen: true,
  previewOpen: false,
  previewExpanded: false,
  sidebarTab: "threads",
  activeProjectCwd: null,
  expandedProjects: {},
  openThreadIds: [],
  activeThreadId: null,
  threads: {},
  fileTree: {},
  previewPath: null,
  previewRoot: null,
  previewPayload: null,
  previewLoading: false,
  toasts: [],
  extuiQueue: [],
  settingsOpen: false,

  connectionStatus: "restoring",
  connectionRevision: 0,

  applyRemoteSnapshot: async (snapshot) => {
    validateSnapshot(snapshot);
    const id = snapshot.threadId || snapshot.sessionFile;
    set((s) => {
      const thread: ThreadState = { ...threadFromResponse(snapshot, s.threads[id] || emptyThread(snapshot.cwd)), connected: snapshot.connected !== false, loading: false };
      // activateThread replays the authoritative pending questions from this snapshot.
      return { threads: { ...s.threads, [id]: thread }, extuiQueue: s.extuiQueue.filter((item) => item.threadId !== id) };
    });
    if (snapshot.liveSnapshotTruncated) get().pushToast("warning", "实时过程超过恢复窗口，仅展示保留部分；完整历史以服务器记录为准。");
    await window.pi.biomind.activateThread(id);
  },

  restoreRemoteThreads: async () => {
    const { snapshots, failures } = readRestoration(await window.pi.biomind.restoreThreads());
    readWindowConfig(get().config);
    // Reject a changed binding before committing any snapshot or failure.
    for (const item of [...snapshots.map((snapshot) => ({ threadId: snapshot.threadId || snapshot.sessionFile, cwd: snapshot.cwd })), ...failures]) {
      if (get().threads[item.threadId] && get().threads[item.threadId].cwd !== item.cwd) throw new Error("SESSION_RESTORE_FAILED");
    }
    for (const snapshot of snapshots || []) await get().applyRemoteSnapshot(snapshot);
    set((state) => {
      const threads = { ...state.threads };
      for (const failure of failures) {
        threads[failure.threadId] = { ...(threads[failure.threadId] || emptyThread(failure.cwd)), sessionFile: failure.threadId,
          connected: false, loading: false, error: `此会话未恢复（${failure.code}）；未自动重发任务。` };
      }
      const restoredIds = [...snapshots.map((snapshot: any) => snapshot.threadId || snapshot.sessionFile), ...failures.map((failure) => failure.threadId)].filter((id: string) => !!threads[id]);
      const preferred = state.activeThreadId || state.config?.lastThreadId;
      const activeThreadId = preferred && threads[preferred] ? preferred : null;
      const cwd = activeThreadId ? threads[activeThreadId].cwd : state.activeProjectCwd;
      return {
        threads,
        openThreadIds: [...new Set([...state.openThreadIds, ...restoredIds])], activeThreadId,
        activeProjectCwd: cwd,
        expandedProjects: cwd ? { ...state.expandedProjects, [cwd]: true } : state.expandedProjects,
      };
    });
    if (failures.length) get().pushToast("warning", `${failures.length} 个会话未恢复；请查看对应会话的错误，未自动重发任何任务。`);
    await get().refreshProjects(true);
  },

  bootstrap: async () => {
    try {
    // These calls are deliberately independent. Project discovery can be slow
    // and a failed config/project request must not leave runtime=null forever,
    // which the title bar previously rendered as a permanent "starting…".
    const runtimeTask = withTimeout(
      Promise.resolve().then(() => window.pi.app.resolveRuntime()),
      12_000,
      "Timed out while locating the Pi runtime",
    )
      .then((runtime) => {
        if (!runtime || typeof runtime.ok !== "boolean") throw new Error("BOOTSTRAP_FAILED");
        set({ runtime });
        if (!runtime.ok) get().pushToast("warning", runtime.error || "Could not locate Pi runtime");
      })
      .catch((e: any) => {
        const error = "无法确认服务器运行状态，请检查连接后重试。";
        reportRendererIssue("unhandledrejection", "BOOTSTRAP_FAILED", e);
        set({ runtime: { ok: false, error } });
        get().pushToast("warning", error);
      });

    const [configResult, projectsResult] = await Promise.allSettled([
      Promise.resolve().then(() => window.pi.app.getConfig()).then(readWindowConfig),
      Promise.resolve().then(() => window.pi.app.getProjects()).then(readProjects),
    ]);

    if (configResult.status === "fulfilled") {
      set({ config: configResult.value });
    } else {
      reportRendererIssue("unhandledrejection", "PROJECT_DATA_INVALID", configResult.reason);
      get().pushToast("error", "窗口设置未能载入，请重新加载界面。");
    }

    if (projectsResult.status === "fulfilled") {
      const appConfig = configResult.status === "fulfilled" ? configResult.value : null;
      const projects = mergeLiveThreadsIntoProjects(
        projectsResult.value,
        {},
        appConfig?.archivedProjects || [],
        appConfig?.archivedThreads || [],
        appConfig?.pinnedProjects || [],
        appConfig?.pinnedThreads || [],
        appConfig?.language || "en",
      );
      set((state) => ({
        projects,
        threads: syncOpenThreadTitles(state.threads, projects),
        activeProjectCwd: state.activeProjectCwd || projects[0]?.cwd || null,
        expandedProjects:
          state.activeProjectCwd || !projects[0]
            ? state.expandedProjects
            : { ...state.expandedProjects, [projects[0].cwd]: true },
      }));
    } else {
      reportRendererIssue("unhandledrejection", "PROJECT_DATA_INVALID", projectsResult.reason);
      get().pushToast("error", "项目列表未能载入；已有会话与任务保持不变。");
    }

    await runtimeTask;
    const revision = get().connectionRevision;
    try {
      await get().restoreRemoteThreads();
      const state = await window.pi.biomind.getStatus();
      if (get().connectionRevision === revision) set({ connectionStatus: configResult.status === "rejected" || projectsResult.status === "rejected" ? "error" : state?.status === "ready" ? "ready" : state?.status || "disconnected" });
    } catch (error: any) {
      if (get().connectionRevision === revision) set({ connectionStatus: "error" });
      reportRendererIssue("unhandledrejection", "SESSION_RESTORE_FAILED", error);
      get().pushToast("error", "服务器会话恢复失败；未自动重发任何任务。");
    }
    } catch (error) {
      set({ connectionStatus: "error" });
      reportRendererIssue("unhandledrejection", "BOOTSTRAP_FAILED", error);
      get().pushToast("error", "工作台初始化未完成，可重新加载恢复已有会话。");
    }
  },

  refreshProjects: async (strict = false) => {
    try {
      const diskProjects = await window.pi.app.getProjects();
      // Include prompts accepted by Pi even before its delayed JSONL flush.
      set((s) => {
        const projects = mergeLiveThreadsIntoProjects(
          diskProjects,
          s.threads,
          s.config?.archivedProjects || [],
          s.config?.archivedThreads || [],
          s.config?.pinnedProjects || [],
          s.config?.pinnedThreads || [],
          s.config?.language || "en",
        );
        return {
          projects,
          threads: syncOpenThreadTitles(s.threads, projects),
        };
      });
    } catch (e: any) {
      reportRendererIssue("unhandledrejection", "PROJECT_DATA_INVALID", e);
      get().pushToast("error", "项目列表未能更新；保留已有项目与会话。");
      if (strict) throw e;
    }
  },

  openProjectFolder: async () => {
    try {
      const path = await selectRemoteFolder(get().activeProjectCwd || undefined);
      if (!path) return;
      const actual = await window.pi.app.openProject(path);
      if (typeof actual !== "string" || !actual) throw new Error("PROJECT_DATA_INVALID");
      await get().refreshProjects();
      set({ activeProjectCwd: actual, activeThreadId: null, expandedProjects: { ...get().expandedProjects, [actual]: true } });
    } catch (e: any) {
      reportRendererIssue("unhandledrejection", "PROJECT_OPEN_FAILED", e);
      get().pushToast("error", "远端项目未能打开，请检查连接后重试。");
    }
  },

  setProjectPinned: async (cwd, pinned) => {
    try {
      const config = await window.pi.app.setProjectPinned({ cwd, pinned });
      set({ config });
      await get().refreshProjects();
    } catch (e: any) {
      get().pushToast("error", e?.message || (pinned ? "Pin project failed" : "Unpin project failed"));
    }
  },
  unpinProject: async (cwd) => get().setProjectPinned(cwd, false),
  setThreadPinned: async (file, pinned) => {
    try {
      const config = await window.pi.app.setThreadPinned({ file, pinned });
      set({ config });
      await get().refreshProjects();
    } catch (e: any) {
      get().pushToast("error", e?.message || (pinned ? "Pin thread failed" : "Unpin thread failed"));
    }
  },
  archiveProject: async (cwd) => {
    try {
      const current = get().config;
      if (!current) return;
      const archived = current.archivedProjects || [];
      if (!archived.some((path) => path === cwd)) {
        const config = await window.pi.app.setConfig({ archivedProjects: [...archived, cwd] });
        set({ config });
      }
      // Close live views from this folder. Session files remain untouched and
      // reappear exactly as before when the project is restored.
      const ids = Object.entries(get().threads)
        .filter(([, thread]) => thread.cwd === cwd)
        .map(([id]) => id);
      for (const id of ids) await get().closeThread(id);
      await get().refreshProjects();
      set((s) => ({
        activeProjectCwd:
          s.activeProjectCwd === cwd
            ? s.projects[0]?.cwd || null
            : s.activeProjectCwd,
      }));
      get().pushToast("info", "项目已归档，可在设置的“归档项目”中恢复。");
    } catch (e: any) {
      get().pushToast("error", "归档项目失败：" + (e?.message || e));
    }
  },
  restoreProject: async (cwd) => {
    try {
      const current = get().config;
      if (!current) return;
      const config = await window.pi.app.setConfig({
        archivedProjects: (current.archivedProjects || []).filter(
          (path) => path !== cwd,
        ),
      });
      set({ config });
      await get().refreshProjects();
      get().pushToast("success", "项目已恢复到侧栏。");
    } catch (e: any) {
      get().pushToast("error", "恢复项目失败：" + (e?.message || e));
    }
  },
  archiveThread: async (cwd, file, title) => {
    try {
      const current = get().config;
      if (!current || !file || file.startsWith("opening-") || file.startsWith("boot:")) return;
      const archived = current.archivedThreads || [];
      const alreadyArchived = archived.some((thread) => thread.file === file);
      if (!alreadyArchived) {
        const config = await window.pi.app.setConfig({
          archivedThreads: [
            ...archived,
            {
              file,
              cwd,
              title: title?.trim() || file.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || file,
            },
          ],
        });
        set({ config });
      }

      // Close an open view without touching its JSONL session file. The
      // session remains available and can be restored from Settings.
      const ids = Object.entries(get().threads)
        .filter(([id, thread]) => (thread.sessionFile || id) === file)
        .map(([id]) => id);
      for (const id of ids) await get().closeThread(id);
      await get().refreshProjects();
      get().pushToast("info", "线程已归档，可在设置的“归档线程”中恢复。");
    } catch (e: any) {
      get().pushToast("error", "归档线程失败：" + (e?.message || e));
    }
  },
  deleteThread: async (_cwd, file, _title) => {
    try {
      if (!file || file.startsWith("opening-") || file.startsWith("boot:")) return;
      const result = await window.pi.thread.delete(file);
      if (result?.config) set({ config: result.config });

      // The main process stops the bridge before removing the JSONL. Remove
      // every renderer view that points at the same persisted session too.
      const target = normalizeThreadFile(file);
      const ids = Object.entries(get().threads)
        .filter(([id, thread]) => normalizeThreadFile(thread.sessionFile || id) === target)
        .map(([id]) => id);
      for (const id of ids) await get().closeThread(id);
      await get().refreshProjects();
      get().pushToast("success", "线程已永久删除，无法恢复。");
    } catch (e: any) {
      get().pushToast("error", "永久删除线程失败：" + (e?.message || e));
    }
  },
  restoreThread: async (file) => {
    try {
      const current = get().config;
      if (!current || !file) return;
      const archived = current.archivedThreads || [];
      const next = archived.filter((thread) => thread.file !== file);
      if (next.length === archived.length) return;
      const config = await window.pi.app.setConfig({ archivedThreads: next });
      set({ config });
      await get().refreshProjects();
      get().pushToast("success", "线程已恢复到侧栏。");
    } catch (e: any) {
      get().pushToast("error", "恢复线程失败：" + (e?.message || e));
    }
  },

  toggleProject: (cwd) => set((s) => ({ expandedProjects: { ...s.expandedProjects, [cwd]: !s.expandedProjects[cwd] } })),
  setActiveProject: (cwd) => { openIntent++; set({ activeProjectCwd: cwd, activeThreadId: null }); },

  openThread: async (cwd, sessionFile, permission) => {
    const intent = ++openIntent;
    // Already on screen: just activate. If it was only disk-rendered so far
    // (no live process yet), kick off / reuse the background connect so it
    // becomes interactive.
    if (sessionFile) {
      const existing = Object.entries(get().threads).find(([id, thread]) => normalizeThreadFile(thread.sessionFile || id) === normalizeThreadFile(sessionFile) && thread.cwd === cwd);
      if (existing) sessionFile = existing[0];
    }
    if (sessionFile && get().threads[sessionFile]) {
      set((s) => ({
        activeThreadId: sessionFile,
        openThreadIds: s.openThreadIds.includes(sessionFile) ? s.openThreadIds : [...s.openThreadIds, sessionFile],
        activeProjectCwd: cwd,
        threads: {
          ...s.threads,
          [sessionFile]: {
            ...s.threads[sessionFile],
            sessionName:
              getDisplayThreadTitle(
                findProjectThreadTitle(s.projects, sessionFile) || s.threads[sessionFile].sessionName,
                s.threads[sessionFile].messages.find((message) => message.role === "user")?.text || "",
                s.config?.language || "en",
              ) || s.threads[sessionFile].sessionName,
            isNewSession: false,
            creatingSession: false,
          },
        },
        expandedProjects: { ...s.expandedProjects, [cwd]: true },
      }));
      if (!get().threads[sessionFile].connected) get().ensureConnected(sessionFile);
      return sessionFile;
    }

    if (sessionFile) {
      // RESUMING an existing session. Its full transcript lives in the .jsonl
      // on disk, so render it instantly (milliseconds) and connect the pi
      // process in the background — no blocking "starting pi" spinner.
      try {
        const hist: any = await window.pi.thread.loadHistory({ cwd, sessionFile });
        const hydrated = threadFromResponse(hist, emptyThread(hist.cwd || cwd));
        const views = hydrated.messages;
        const sidebarTitle = findProjectThreadTitle(get().projects, hist.sessionFile || sessionFile);
        const firstUserText = views.find((message) => message.role === "user")?.text || "";
        const thread: ThreadState = {
          ...hydrated,
          sessionFile: hist.sessionFile || sessionFile,
          sessionName: getDisplayThreadTitle(sidebarTitle || hist.sessionName, firstUserText, get().config?.language || "en") || null,
          isNewSession: false,
          creatingSession: false,
          model: hist.model,
          models: hist.models || [],
          thinking: hist.thinkingLevel || "off",
          commands: hist.commands || [],
          loading: false,
          connected: !!hist.connected,
          isStreaming: !!hist.isStreaming,
          messages: views,
          toolRuns: hydrated.toolRuns,
          permission: hist.permission || permission || "sandbox",
        };
        set((s) => ({
          threads: { ...s.threads, [sessionFile]: thread },
          openThreadIds: s.openThreadIds.includes(sessionFile) ? s.openThreadIds : [...s.openThreadIds, sessionFile],
          activeThreadId: intent === openIntent ? sessionFile : s.activeThreadId,
          activeProjectCwd: intent === openIntent ? hist.cwd || cwd : s.activeProjectCwd,
          expandedProjects: { ...s.expandedProjects, [hist.cwd || cwd]: true },
        }));
        if (hist.liveSnapshotTruncated) get().pushToast("warning", "实时过程超过恢复窗口，仅展示保留部分；完整历史以服务器记录为准。");
        await window.pi.biomind.activateThread(sessionFile);
        if (hist.connected) {
          window.pi.thread
            .getThinkingLevels(sessionFile)
            .then((r: any) => set((s) => (s.threads[sessionFile] ? { threads: { ...s.threads, [sessionFile]: { ...s.threads[sessionFile], levels: r?.levels || ["off"] } } } : s)))
            .catch(() => {});
        } else {
          get().ensureConnected(sessionFile);
        }
        return sessionFile;
      } catch (e: any) {
        get().pushToast("error", "Open thread failed: " + (e?.message || e));
        return null;
      }
    }

    // NEW TASK (no session file): nothing on disk to show, but the empty chat
    // + composer appear instantly and the pi process connects in the
    // background (adopting the warm spare). No blocking "starting pi" spinner.
    // The temp id is remapped to the real session file once connected.
    const tempId = `opening-${uid()}`;
    const placeholder: ThreadState = { ...emptyThread(cwd), loading: false, connected: false, permission: permission || "sandbox" };
    placeholder.isNewSession = true;
    set((s) => ({
      threads: { ...s.threads, [tempId]: placeholder },
      openThreadIds: s.openThreadIds.includes(tempId) ? s.openThreadIds : [...s.openThreadIds, tempId],
      activeThreadId: tempId,
      activeProjectCwd: cwd,
      expandedProjects: { ...s.expandedProjects, [cwd]: true },
    }));
    get().ensureConnected(tempId);
    return tempId;
  },

  ensureConnected: (threadId) => {
    const t = get().threads[threadId];
    if (!t) return Promise.resolve(null);
    if (t.connected) return Promise.resolve(threadId);
    const inflight = connectPromises.get(threadId);
    if (inflight) return inflight;
    set((s) => ({ threads: { ...s.threads, [threadId]: { ...s.threads[threadId], loading: true } } }));
    const p = (async () => {
      try {
        // For a resumed session thread:open returns the same session file as
        // the id, so the thread keeps its key; the remap guard is defensive.
        const res: any = await window.pi.thread.open({ cwd: t.cwd, sessionFile: t.sessionFile || undefined, permission: t.permission, clientOpenId: threadId });
        const id = res.threadId || threadId;
        const { views, toolRuns } = historyToView(res.messages || [], res.branchMessages || []);
        set((s) => {
          const prev = s.threads[threadId] || s.threads[id];
          const hydrated = threadFromResponse(res, prev || t);
          // Preserve optimistic user bubbles added before the connect finished
          // (e.g. a fast first send on a brand-new thread); live history never
          // contains them, so without this they would be dropped on merge/remap.
          const optimistic = (prev?.messages || []).filter((m) => m.key.startsWith("opt-"));
          const hasOptimisticUser = optimistic.some((message) => message.role === "user");
          // A no-session open is a fresh task even when the adopted warm bridge
          // reports the previous session's metadata. Keep that boundary until
          // the first prompt creates the new task title. If a prompt raced the
          // connection, preserve its optimistic title instead.
          const freshSession = !hasOptimisticUser && (
            res.isNewSession === true ||
            prev?.isNewSession === true ||
            (!t.sessionFile && !prev?.sessionFile && !(prev?.messages || []).some((message) => message.role === "user"))
          );
          const currentFile = res.sessionFile || t.sessionFile || id;
          const sidebarTitle = findProjectThreadTitle(s.projects, currentFile);
          const firstUserText = (prev?.messages || views).find((message) => message.role === "user")?.text || "";
          const merged: ThreadState = {
            ...hydrated,
            sessionFile: res.sessionFile || t.sessionFile,
            sessionName: freshSession
              ? null
              : (getDisplayThreadTitle(
                  sidebarTitle || res.sessionName || prev?.sessionName,
                  firstUserText,
                  s.config?.language || "en",
                ) || null),
            isNewSession: freshSession ? true : prev?.isNewSession,
            creatingSession: prev?.creatingSession,
            model: res.model ?? prev?.model ?? null,
            models: res.models || [],
            thinking: res.thinkingLevel || prev?.thinking || "off",
            commands: res.commands || [],
            loading: false,
            connected: true,
            isStreaming: !!res.isStreaming || optimistic.length > 0,
            messages: optimistic.length ? [...hydrated.messages, ...optimistic] : hydrated.messages,
            toolRuns: hydrated.toolRuns,
            permission: res.permission || t.permission,
            pendingEditorText: prev?.pendingEditorText,
            extensionStatuses: prev?.extensionStatuses,
            extensionWidgets: prev?.extensionWidgets,
          };
          const threads: Record<string, ThreadState> = { ...s.threads, [id]: merged };
          let openThreadIds = s.openThreadIds;
          let activeThreadId = s.activeThreadId;
          if (id !== threadId) {
            delete threads[threadId];
            openThreadIds = openThreadIds.map((x) => (x === threadId ? id : x));
            if (activeThreadId === threadId) activeThreadId = id;
          }
          return { threads, openThreadIds, activeThreadId };
        });
        if (res.liveSnapshotTruncated) get().pushToast("warning", "实时过程超过恢复窗口，仅展示保留部分；完整历史以服务器记录为准。");
        await window.pi.biomind.activateThread(id);
        // A brand-new session just appeared on disk (temp id remapped to the
        // real session file); refresh the sidebar so it shows under its project.
        if (id !== threadId) get().refreshProjects();
        window.pi.thread
          .getThinkingLevels(id)
          .then((r: any) => set((s) => (s.threads[id] ? { threads: { ...s.threads, [id]: { ...s.threads[id], levels: r?.levels || ["off"] } } } : s)))
          .catch(() => {});
        return id;
      } catch (e: any) {
        // Keep the disk-rendered transcript visible; only mark the failure.
        set((s) => (s.threads[threadId] ? { threads: { ...s.threads, [threadId]: { ...s.threads[threadId], connected: false, loading: false, error: e?.message || "connect failed" } } } : s));
        get().pushToast("error", "连接 BioMind 会话失败：" + (e?.message || e));
        return null;
      } finally {
        connectPromises.delete(threadId);
      }
    })();
    connectPromises.set(threadId, p);
    return p;
  },

  closeThread: async (id) => {
    // Closing a view never terminates or detaches the server-owned Main task.
    // Keep its state and event subscription, so reopening reuses the same ID.
    set((s) => {
      const openThreadIds = s.openThreadIds.filter((x) => x !== id);
      const activeThreadId = s.activeThreadId === id ? openThreadIds[openThreadIds.length - 1] || null : s.activeThreadId;
      const activeProjectCwd = activeThreadId ? s.threads[activeThreadId]?.cwd || s.activeProjectCwd : s.activeProjectCwd;
      return { openThreadIds, activeThreadId, activeProjectCwd };
    });
  },

  setActiveThread: (id) => {
    openIntent++;
    set((s) => {
      const cwd = s.threads[id]?.cwd;
      return cwd
        ? { activeThreadId: id, openThreadIds: s.openThreadIds.includes(id) ? s.openThreadIds : [...s.openThreadIds, id], activeProjectCwd: cwd, expandedProjects: { ...s.expandedProjects, [cwd]: true } }
        : { activeThreadId: id };
    });
  },

  newTask: async () => {
    let cwd: string | null = get().activeProjectCwd;
    if (!cwd) {
      const p = await selectRemoteFolder(get().activeProjectCwd || undefined);
      if (!p || Array.isArray(p)) return;
      await window.pi.app.openProject(p);
      await get().refreshProjects();
      set({ activeProjectCwd: p, activeThreadId: null, expandedProjects: { ...get().expandedProjects, [p]: true } });
      cwd = p;
    }
    if (cwd) {
      // Flush any just-persisted current session into the sidebar before
      // switching to a fresh empty task.
      await get().refreshProjects();
      await get().openThread(cwd);
    }
  },

  sendPrompt: async (threadId, text, images, attachments, mode) => {
    if (get().connectionStatus !== "ready" || !get().threads[threadId]?.connected) { get().pushToast("warning", "服务器状态尚未恢复，任务未发送。"); return { accepted: false }; }
    const trimmed = (text || "").trim();
    const hasImg = !!images && images.length > 0;
    const hasAtt = !!attachments && attachments.length > 0;
    if (!trimmed && !hasImg && !hasAtt) return { accepted: false };
    const currentThread = get().threads[threadId];
    const wasStreaming = !!currentThread?.isStreaming;
    const extensionCommand = !wasStreaming && isExtensionCommandPrompt(trimmed, currentThread?.commands);
    const optimisticTitle = getDisplayThreadTitle(null, trimmed, get().config?.language || "en").trim().slice(0, 80);
    const optimistic: ViewMessage = {
      key: `opt-${uid()}`,
      role: "user",
      text: trimmed,
      images: (images || []).map((im) => ({ dataUrl: `data:${im.mimeType};base64,${im.data}`, mimeType: im.mimeType })),
      attachments: (attachments || []).map((file) => ({ name: file.name, path: file.abs })),
      timestamp: Date.now(),
      sendKind: wasStreaming ? (mode === "followUp" ? "followUp" : "steer") : undefined,
    };
    // Show the user's bubble immediately, even if the process is still
    // connecting in the background — the chat must never look frozen.
    set((s) => {
      const t = s.threads[threadId];
      if (!t) return s;
      const hasUserMessage = t.messages.some((message) => message.role === "user");
      return {
        threads: {
          ...s.threads,
          [threadId]: {
            ...t,
            sessionName: t.isNewSession
              ? (optimisticTitle || null)
              : (t.sessionName || (!hasUserMessage && optimisticTitle ? optimisticTitle : null)),
            isNewSession: false,
            creatingSession: false,
            messages: [...t.messages, optimistic],
            // An extension command is handled outside Pi's agent loop and may
            // emit no agent lifecycle events. Do not wait forever for a
            // matching agent_settled event that Pi will never send.
            isStreaming: extensionCommand ? false : true,
            error: undefined,
          },
        },
      };
    });
    // A disk-rendered or brand-new thread may not have a live process yet.
    // ensureConnected keeps the optimistic bubble across the connect/remap and
    // resolves with the thread's final id (a new task starts under a temp id).
    const tid = await get().ensureConnected(threadId);
    if (!tid) {
      // Connection failed: roll back the bubble. The thread is still under its
      // original id (remap only happens on success).
      set((s) => {
        const t = s.threads[threadId];
        if (!t) return s;
        return { threads: { ...s.threads, [threadId]: { ...t, isStreaming: false, messages: t.messages.filter((m) => m.key !== optimistic.key) } } };
      });
      return { accepted: false };
    }
    const piImages = (images || []).map((im) => ({ type: "image", data: im.data, mimeType: im.mimeType }));
    try {
      // A fresh/disk-loaded thread may not have populated its command list
      // until ensureConnected completed. Re-evaluate before sending so a
      // successful extension command cannot strand the composer in streaming.
      const connectedThread = get().threads[tid];
      const handledOutsideAgentLoop = !wasStreaming && isExtensionCommandPrompt(trimmed, connectedThread?.commands);
      if (handledOutsideAgentLoop) {
        set((s) => {
          const t = s.threads[tid];
          return t && t.isStreaming ? { threads: { ...s.threads, [tid]: { ...t, isStreaming: false } } } : s;
        });
      }
      let response: any;
      if (wasStreaming) {
        // mode: "steer" interrupts current work; "followUp" waits until agent finishes
        if (mode === "followUp") {
          response = await window.pi.thread.followUp({ threadId: tid, text: trimmed, images: piImages, attachments });
        } else {
          response = await window.pi.thread.steer({ threadId: tid, text: trimmed, images: piImages, attachments });
        }
      } else {
        response = await window.pi.thread.prompt({ threadId: tid, text: trimmed, images: piImages, attachments });
        if (response?.disposition === "handled") {
          set((s) => { const t = s.threads[tid]; return t ? { threads: { ...s.threads, [tid]: { ...t, isStreaming: false } } } : s; });
        }
      }
      // Pi creates/persists a new session lazily on its first prompt.
      await get().refreshProjects();
      return { accepted: true, receipt: response?.receipt };
    } catch (e: any) {
      set((s) => {
        const t = s.threads[tid];
        if (!t) return s;
        return { threads: { ...s.threads, [tid]: { ...t, connected: false, error: e?.message || "发送结果未知；请等待服务器状态恢复" } } };
      });
      get().pushToast("error", e?.message || "发送结果未知；草稿已保留，不会自动重发");
      return { accepted: false };
    }
  },

  setPendingFollowUp: (threadId, pending) => {
    if (pending !== get().threads[threadId]?.pendingFollowUp) uncertainPendingInputs.delete(threadId);
    set((s) => (s.threads[threadId] ? { threads: { ...s.threads, [threadId]: { ...s.threads[threadId], pendingFollowUp: pending } } } : s));
  },

  sendPendingSteering: async (threadId, mode = "steer") => {
    const p = get().threads[threadId]?.pendingFollowUp;
    if (!p || pendingInputs.has(threadId) || (mode === "followUp" && uncertainPendingInputs.has(threadId))) return;
    pendingInputs.add(threadId);
    try {
      const { imgs, atts } = pendingToArgs(p);
      const result = await get().sendPrompt(threadId, pendingPromptText(p), imgs, atts, mode);
      if (result.accepted) {
        get().setPendingFollowUp(threadId, null);
        if (result.receipt) await window.pi.biomind.finishInput(result.receipt);
      } else { uncertainPendingInputs.add(threadId); }
    } catch (error: any) {
      get().pushToast("error", error?.message || "输入确认状态未知");
    } finally { pendingInputs.delete(threadId); }
  },

  abortThread: async (id) => {
    try {
      await window.pi.thread.abort(id);
    } catch (e: any) {
      get().pushToast("error", e?.message || "abort failed");
    }
  },

  refreshOpenThreadModels: async () => {
    const connected = Object.entries(get().threads).filter(([, thread]) => thread.connected);
    const results = await Promise.allSettled(
      connected.map(async ([id]) => ({ id, response: await window.pi.thread.refreshModels(id) })),
    );
    set((state) => {
      const threads = { ...state.threads };
      for (const result of results) {
        if (result.status !== "fulfilled") continue;
        const { id, response } = result.value as { id: string; response: { models?: ModelInfo[] } };
        const thread = threads[id];
        if (thread) threads[id] = { ...thread, models: response?.models || [] };
      }
      return { threads };
    });
  },

  setModel: async (id, provider, modelId) => {
    if (!(await get().ensureConnected(id))) return;
    try {
      const res: any = await window.pi.thread.setModel({ threadId: id, provider, modelId });
      const nextModel = res?.model || res || { provider, id: modelId };
      set((s) =>
        s.threads[id]
          ? {
              threads: {
                ...s.threads,
                [id]: {
                  ...s.threads[id],
                  model: nextModel,
                  ...(typeof res?.thinkingLevel === "string" ? { thinking: res.thinkingLevel } : {}),
                },
              },
            }
          : s,
      );
      window.pi.thread
        .getThinkingLevels(id)
        .then((r: any) => set((s) => (s.threads[id] ? { threads: { ...s.threads, [id]: { ...s.threads[id], levels: r?.levels || ["off"] } } } : s)))
        .catch(() => {});
    } catch (e: any) {
      get().pushToast("error", e?.message || "set model failed");
    }
  },

  setThinking: async (id, level) => {
    if (!(await get().ensureConnected(id))) return;
    try {
      const res: any = await window.pi.thread.setThinking({ threadId: id, level });
      const effectiveLevel = typeof res?.thinkingLevel === "string" ? res.thinkingLevel : level;
      set((s) => (s.threads[id] ? { threads: { ...s.threads, [id]: { ...s.threads[id], thinking: effectiveLevel } } } : s));
    } catch (e: any) {
      get().pushToast("error", e?.message || "set thinking failed");
    }
  },

  newSessionInThread: async (id) => {
    const current = get().threads[id];
    if (!current) return;
    // Reset the title before the RPC starts so the old session name cannot
    // remain in the header while the new session is being created.
    set((s) => (s.threads[id]
      ? { threads: { ...s.threads, [id]: { ...s.threads[id], sessionName: null, isNewSession: true, creatingSession: true } } }
      : s));
    if (!(await get().ensureConnected(id))) {
      set((s) => (s.threads[id] ? { threads: { ...s.threads, [id]: { ...s.threads[id], isNewSession: false, creatingSession: false } } } : s));
      return;
    }
    try {
      const res: any = await window.pi.thread.newSession(id);
      if (res?.cancelled) {
        set((s) => (s.threads[id] ? { threads: { ...s.threads, [id]: { ...s.threads[id], isNewSession: false, creatingSession: false } } } : s));
        return;
      }
      const newId = res.threadId || id;
      const { views, toolRuns } = historyToView(res.messages || [], res.branchMessages || []);
      const thread: ThreadState = {
        ...emptyThread(res.cwd || get().threads[id]?.cwd || ""),
        sessionFile: res.sessionFile,
        // A newly created session starts unnamed; its first prompt supplies
        // the display title. Do not carry the previous session name across.
        sessionName: null,
        isNewSession: true,
        creatingSession: false,
        model: res.model,
        models: res.models || get().threads[id]?.models || [],
        thinking: res.thinkingLevel || "off",
        commands: res.commands || [],
        messages: views,
        toolRuns,
        permission: res.permission || get().threads[id]?.permission || "sandbox",
      };
      set((s) => {
        const threads: Record<string, ThreadState> = { ...s.threads, [newId]: thread };
        if (newId !== id) delete threads[id];
        const openThreadIds = s.openThreadIds.map((x) => (x === id ? newId : x));
        return { threads, openThreadIds, activeThreadId: newId };
      });
      if (newId !== id) get().refreshProjects();
    } catch (e: any) {
      set((s) => (s.threads[id] ? { threads: { ...s.threads, [id]: { ...s.threads[id], isNewSession: false, creatingSession: false } } } : s));
      get().pushToast("error", e?.message || "new session failed");
    }
  },

  forkThreadFromAgentReply: async (id, entryId) => {
    const liveId = await get().ensureConnected(id);
    if (!liveId) return;
    const source = get().threads[liveId];
    const zh = get().config?.language === "zh";
    if (!source || source.isStreaming) {
      get().pushToast("warning", zh ? "请等待当前回复结束后再创建分支。" : "Wait for the current reply to finish before forking.");
      return;
    }
    try {
      const res: any = await window.pi.thread.fork({ threadId: liveId, entryId });
      if (res?.cancelled) return;
      const newId = res.threadId || liveId;
      const next = threadFromResponse(res, source, res.selectedText || undefined);
      set((s) => {
        const threads: Record<string, ThreadState> = { ...s.threads };
        if (newId !== liveId) {
          threads[liveId] = { ...source, connected: false, isStreaming: false, streaming: null };
        }
        threads[newId] = next;
        const openThreadIds =
          newId === liveId || s.openThreadIds.includes(newId) ? s.openThreadIds : [...s.openThreadIds, newId];
        return { threads, openThreadIds, activeThreadId: newId, activeProjectCwd: next.cwd };
      });
      get().pushToast("info", zh ? "已从所选智能体回复创建分支。" : "Created a fork from the selected Agent reply.");
      get().refreshProjects();
    } catch (e: any) {
      get().pushToast("error", e?.message || "fork failed");
    }
  },

  cloneThread: async (id, entryId) => {
    const liveId = await get().ensureConnected(id);
    if (!liveId) return;
    const source = get().threads[liveId];
    const zh = get().config?.language === "zh";
    if (!source || source.isStreaming) {
      get().pushToast("warning", zh ? "请等待当前回复结束后再克隆。" : "Wait for the current reply to finish before cloning.");
      return;
    }
    try {
      const res: any = await window.pi.thread.clone({ threadId: liveId, entryId });
      if (res?.cancelled) return;
      const newId = res.threadId || liveId;
      const next = threadFromResponse(res, source);
      set((s) => {
        const threads: Record<string, ThreadState> = { ...s.threads };
        if (newId !== liveId) {
          threads[liveId] = { ...source, connected: false, isStreaming: false, streaming: null };
        }
        threads[newId] = next;
        const openThreadIds =
          newId === liveId || s.openThreadIds.includes(newId) ? s.openThreadIds : [...s.openThreadIds, newId];
        return { threads, openThreadIds, activeThreadId: newId, activeProjectCwd: next.cwd };
      });
      get().pushToast("info", zh ? "已克隆截至所选智能体回复的分支。" : "Cloned the branch through the selected Agent reply.");
      get().refreshProjects();
    } catch (e: any) {
      get().pushToast("error", e?.message || "clone failed");
    }
  },

  renameThread: async (id, name) => {
    if (!(await get().ensureConnected(id))) return;
    try {
      await window.pi.thread.setName({ threadId: id, name });
      set((s) => (s.threads[id] ? { threads: { ...s.threads, [id]: { ...s.threads[id], sessionName: name } } } : s));
      get().refreshProjects();
    } catch (e: any) {
      get().pushToast("error", e?.message || "rename failed");
    }
  },

  setSidebarTab: (t) => set({ sidebarTab: t }),
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  togglePreview: () =>
    set((s) => ({
      previewOpen: !s.previewOpen,
      previewExpanded: s.previewOpen ? false : s.previewExpanded,
    })),
  togglePreviewExpanded: () =>
    set((s) => ({
      previewExpanded: s.previewOpen ? !s.previewExpanded : false,
    })),

  loadFileTree: async (cwd, rel) => {
    const key = treeKey(cwd, rel);
    try {
      const nodes = await window.pi.app.getFileTree(cwd, rel);
      set((s) => ({ fileTree: { ...s.fileTree, [key]: { nodes, loaded: true, expanded: s.fileTree[key]?.expanded ?? true } } }));
    } catch (e: any) {
      get().pushToast("error", e?.message || "load tree failed");
    }
  },

  toggleFolder: (cwd, rel) => {
    const key = treeKey(cwd, rel);
    const cur = get().fileTree[key];
    if (cur?.expanded) {
      set((s) => ({ fileTree: { ...s.fileTree, [key]: { ...cur, expanded: false } } }));
      return;
    }
    if (cur?.loaded) {
      set((s) => ({ fileTree: { ...s.fileTree, [key]: { ...cur, expanded: true } } }));
      return;
    }
    set((s) => ({ fileTree: { ...s.fileTree, [key]: { nodes: [], loaded: false, expanded: true } } }));
    get().loadFileTree(cwd, rel);
  },

  openPreview: async (abs, projectRoot) => {
    const root = projectRoot || get().previewRoot || undefined;
    set({ previewOpen: true, previewPath: abs, previewRoot: root || null, previewLoading: true, previewPayload: null });
    try {
      const payload = await window.pi.app.readPreview(abs, root);
      set({ previewPayload: payload, previewLoading: false });
    } catch (e: any) {
      set({ previewLoading: false, previewPayload: { name: abs.split(/[\\/]/).pop() || abs, ext: "", size: 0, kind: "missing", message: e?.message || "read failed" } });
    }
  },

  closePreview: () =>
    set({
      previewOpen: false,
      previewExpanded: false,
      previewPath: null,
      previewRoot: null,
      previewPayload: null,
    }),

  pushToast: (kind, text) => {
    const id = uid();
    set((s) => ({
      // Keep the source copy so a toast created before config bootstrap is
      // re-rendered in the selected language once the config arrives.
      toasts: [...s.toasts, { id, kind, text: String(text) }],
    }));
    setTimeout(() => get().dismissToast(id), 5200);
  },
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),

  handleEvent: (threadId, event) => {
    if (event?.type === "extension_ui_resolved") {
      set((s) => ({ extuiQueue: s.extuiQueue.filter((item) => item.threadId !== threadId || item.request.id !== String(event.id)) }));
      return;
    }
    // Queued and folded into one store update per frame (see scheduleEventFlush).
    eventQueue.push({ threadId, event });
    scheduleEventFlush();
  },

  handleExtUi: (threadId, req) => {
    const m = req?.method;
    if (m === "notify") {
      get().pushToast(req.notifyType === "error" ? "error" : req.notifyType === "warning" ? "warning" : "info", (req.message || req.title || "") as string);
      return;
    }
    if (m === "set_editor_text") {
      set((s) => (s.threads[threadId] ? { threads: { ...s.threads, [threadId]: { ...s.threads[threadId], pendingEditorText: req.text as string } } } : s));
      return;
    }
    if (m === "setTitle") {
      if (req.title) document.title = String(req.title);
      return;
    }
    if (m === "setStatus" || m === "setWidget") {
      set((s) => {
        const thread = s.threads[threadId];
        if (!thread) return s;
        if (m === "setStatus") {
          const statuses = { ...thread.extensionStatuses };
          if (req.statusText == null) delete statuses[String(req.statusKey)];
          else statuses[String(req.statusKey)] = String(req.statusText).replace(/\x1b\[[0-9;]*m/g, "");
          return { threads: { ...s.threads, [threadId]: { ...thread, extensionStatuses: statuses } } };
        }
        const widgets = { ...thread.extensionWidgets };
        if (!Array.isArray(req.widgetLines)) delete widgets[String(req.widgetKey)];
        else widgets[String(req.widgetKey)] = { lines: req.widgetLines.map((line: unknown) => String(line).replace(/\x1b\[[0-9;]*m/g, "")), placement: req.widgetPlacement === "belowEditor" ? "belowEditor" : "aboveEditor" };
        return { threads: { ...s.threads, [threadId]: { ...thread, extensionWidgets: widgets } } };
      });
      return;
    }
    // dialog methods -> queue
    if (m === "select" || m === "confirm" || m === "input" || m === "editor") {
      set((s) => {
        const index = s.extuiQueue.findIndex((item) => item.threadId === threadId && item.request.id === req.id);
        const extuiQueue = [...s.extuiQueue];
        if (index >= 0) extuiQueue[index] = { threadId, request: req };
        else extuiQueue.push({ threadId, request: req });
        return { extuiQueue };
      });
      return;
    }
  },

  respondExtUi: async (threadId, id, payload) => {
    const key = `${threadId}\0${id}`;
    if (respondingQuestions.has(key)) return;
    respondingQuestions.add(key);
    try {
      const accepted = await window.pi.thread.extuiResponse({ threadId, id, payload });
      if (accepted !== true) throw new Error("UI_RESPONSE_STATUS_UNKNOWN");
      set((s) => ({ extuiQueue: s.extuiQueue.filter((item) => item.threadId !== threadId || item.request.id !== id) }));
    } catch (error: any) {
      get().pushToast("error", error?.message || "服务器未确认授权答复；请求仍保留");
    } finally { respondingQuestions.delete(key); }
  },

  handleExit: (threadId, info) => {
    // The thread may already be gone (intentional close / permission switch);
    // its exit is then expected and must not raise an error toast.
    if (!get().threads[threadId]) return;
    set((s) => {
      const t = s.threads[threadId];
      if (!t) return s;
      return { threads: { ...s.threads, [threadId]: { ...t, connected: false, error: `BioMind 状态连接中断 (code ${info.code})` } } };
    });
    const tail = (info.stderr || "").trim().split(/\r?\n/).slice(-3).join(" | ");
    get().pushToast("error", `BioMind connection exited (${info.code})${tail ? ": " + tail : ""}`);
  },

  handleError: (threadId, message) => {
    set((s) => {
      const t = s.threads[threadId];
      if (!t) return s;
      return { threads: { ...s.threads, [threadId]: { ...t, connected: false, error: message } } };
    });
    get().pushToast("error", message);
  },

  openSettings: () => set({ settingsOpen: true }),
  closeSettings: () => set({ settingsOpen: false }),

  searchOpen: false,
  openSearch: () => set({ searchOpen: true }),
  closeSearch: () => set({ searchOpen: false }),
  goToThread: async (cwd, file) => {
    const s = get();
    if (s.openThreadIds.includes(file)) {
      // Re-activating an already rendered transcript must still make it an
      // existing thread. A stale fresh-session marker would otherwise force
      // the chat header to remain "New Thread" after switching back here.
      const current = s.threads[file];
      const sidebarTitle = findProjectThreadTitle(s.projects, file);
      const firstUserText = current?.messages.find((message) => message.role === "user")?.text || "";
      set((state) => ({
        activeThreadId: file,
        activeProjectCwd: cwd,
        threads: state.threads[file]
          ? {
              ...state.threads,
              [file]: {
                ...state.threads[file],
                sessionName: getDisplayThreadTitle(
                  sidebarTitle || state.threads[file].sessionName,
                  firstUserText,
                  state.config?.language || "en",
                ) || state.threads[file].sessionName,
                isNewSession: false,
                creatingSession: false,
              },
            }
          : state.threads,
        expandedProjects: { ...state.expandedProjects, [cwd]: true },
      }));
      return;
    }
    await s.openThread(cwd, file);
  },

  // ---- plugins ----
  pluginsOpen: false,
  packages: [],
  skills: [],
  pluginsLoading: false,
  pluginsLoadRevision: 0,
  openPlugins: () => {
    set({ pluginsOpen: true });
    get().loadPlugins();
  },
  closePlugins: () => set({ pluginsOpen: false }),
  loadPlugins: async () => {
    const before = get(), threadId = before.activeThreadId, thread = threadId ? before.threads[threadId] : undefined;
    const revision = before.pluginsLoadRevision + 1;
    set({ packages: [], skills: [], pluginsLoading: true, pluginsLoadRevision: revision });
    try {
      if (!threadId || !thread?.connected || thread.cwd !== before.activeProjectCwd) throw new Error("请先选择已连接的真实 BioMind 会话；只读历史不能配置 Skills。");
      const skills = await window.pi.plugins.getSkills({ threadId, cwd: thread.cwd });
      if (get().pluginsLoadRevision === revision && get().activeThreadId === threadId && get().activeProjectCwd === thread.cwd) set({ skills });
    } catch (e: any) {
      if (get().pluginsLoadRevision === revision) get().pushToast("error", "加载插件失败：" + (e?.message || e));
    } finally {
      if (get().pluginsLoadRevision === revision) set({ pluginsLoading: false });
    }
  },
  togglePackage: async (source, enabled) => {
    set((s) => ({ packages: s.packages.map((p) => (p.source === source ? { ...p, enabled } : p)) }));
    try {
      await window.pi.plugins.setPackageEnabled(source, enabled);
    } catch (e: any) {
      get().pushToast("error", e?.message || "切换失败");
      get().loadPlugins();
    }
  },
  installPackage: async (source) => {
    try {
      const res: any = await window.pi.plugins.installPackage(source);
      if (res?.output) get().pushToast(res.ok ? "info" : "warning", String(res.output).slice(0, 300));
      await get().loadPlugins();
    } catch (e: any) {
      get().pushToast("error", "安装失败：" + (e?.message || e));
    }
  },
  removePackage: async (source) => {
    try {
      await window.pi.plugins.removePackage(source);
      await get().loadPlugins();
    } catch (e: any) {
      get().pushToast("error", "移除失败：" + (e?.message || e));
    }
  },
  updatePackages: async (source) => {
    const label = source ? "更新扩展" : "更新全部扩展";
    try {
      const res: any = await window.pi.plugins.updatePackages(source);
      const raw = stripAnsi(res?.output || "");
      const text = cleanOutput(raw);
      const assertion = hasLibuvAssertion(raw);

      if (res?.ok) {
        if (extensionsAlreadyLatest(text)) {
          get().pushToast("info", source ? "该扩展已是最新版本。" : "所有扩展已是最新版本。");
        } else {
          get().pushToast("success", source ? "扩展已更新到最新版本。" : "所有扩展已更新到最新版本。");
        }
      } else if (assertion) {
        // The libuv assertion fires during process teardown on Windows — the
        // actual update (npm) likely completed before the crash.
        if (/Updated/i.test(text)) {
          get().pushToast("success", source ? "扩展已更新。" : "扩展已更新。");
        } else if (extensionsAlreadyLatest(text)) {
          get().pushToast("info", source ? "该扩展已是最新版本。" : "所有扩展已是最新版本。");
        } else {
          get().pushToast("warning", "更新命令已执行，但进程退出异常。请检查扩展版本。");
        }
      } else {
        get().pushToast("error", `${label}失败：` + (lastLine(text) || "未知错误"));
      }
      await get().loadPlugins();
    } catch (e: any) {
      get().pushToast("error", `${label}失败：` + (e?.message || e));
    }
  },
  toggleSkill: async (path, enabled) => {
    const before = get(), threadId = before.activeThreadId, thread = threadId ? before.threads[threadId] : undefined;
    try {
      if (!threadId || !thread?.connected || thread.cwd !== before.activeProjectCwd) throw new Error("请先选择已连接的真实 BioMind 会话；只读历史不能配置 Skills。");
      const skill = before.skills.find(row => row.path === path && row.threadId === threadId && row.cwd === thread.cwd);
      if (!skill?.resourceId) throw new Error("当前会话的 Skills 列表已变化，请刷新后再试。");
      const result = await window.pi.plugins.setSkillEnabled({ threadId, cwd: thread.cwd, resourceId: skill.resourceId, path, enabled });
      if (get().activeThreadId === threadId && get().activeProjectCwd === thread.cwd) set({ skills: result.skills });
      if (result.effect === "reload_required") get().pushToast("info", "Skills 配置已保存；服务器重新加载后生效。");
    } catch (e: any) {
      get().pushToast("error", e?.message || "切换失败");
      if (get().activeThreadId === threadId) await get().loadPlugins();
    }
  },
  installSkill: async (skill) => {
    const zh = get().config?.language === "zh";
    try {
      const result: any = await window.pi.plugins.installSkill({ source: skill.source, skillId: skill.skillId });
      if (!result?.ok) {
        get().pushToast("error", `${zh ? "安装技能失败：" : "Skill installation failed: "}${String(result?.output || (zh ? "未知错误" : "Unknown error")).slice(-500)}`);
        return false;
      }
      get().pushToast("success", `${zh ? "技能已安装：" : "Skill installed: "}${skill.name}`);
      await get().loadPlugins();
      return true;
    } catch (e: any) {
      get().pushToast("error", `${zh ? "安装技能失败：" : "Skill installation failed: "}${e?.message || e}`);
      return false;
    }
  },

  // ---- automation ----
  automationOpen: false,
  tasks: [],
  openAutomation: () => {
    get().pushToast("info", "自动化尚未迁移到远端 BioMind；本候选不创建本地定时任务。");
  },
  closeAutomation: () => set({ automationOpen: false }),
  loadTasks: async () => {
    try {
      const tasks = await window.pi.automation.getTasks();
      set({ tasks });
    } catch (e: any) {
      get().pushToast("error", "加载任务失败：" + (e?.message || e));
    }
  },
  saveTask: async (task) => {
    try {
      await window.pi.automation.saveTask(task);
      await get().loadTasks();
    } catch (e: any) {
      get().pushToast("error", "保存任务失败：" + (e?.message || e));
    }
  },
  deleteTask: async (id) => {
    try {
      await window.pi.automation.deleteTask(id);
      await get().loadTasks();
    } catch (e: any) {
      get().pushToast("error", e?.message || "删除失败");
    }
  },
  runTaskNow: async (id) => {
    try {
      await window.pi.automation.runNow(id);
      get().pushToast("info", "服务器已接受自动化任务。");
      await get().loadTasks();
      await get().refreshProjects();
    } catch (e: any) {
      get().pushToast("error", "执行失败：" + (e?.message || e));
    }
  },

  // ---- thread permission / folder ----
  setPermission: async (_threadId, _level) => {
    get().pushToast("info", "授权由服务器 BioMind 管理；此窗口不会切换为裸 Pi 完全权限。");
  },

  switchThreadFolder: async (threadId) => {
    try {
      const path = await selectRemoteFolder(get().activeProjectCwd || undefined);
      if (!path || Array.isArray(path)) return;
      await window.pi.app.openProject(path);
      await get().refreshProjects();
      await get().openThread(path, undefined, get().threads[threadId]?.permission);
    } catch (e: any) {
      get().pushToast("error", "切换文件夹失败：" + (e?.message || e));
    }
  },
  changeDraftThreadFolder: async (threadId, cwd) => {
    const original = get().threads[threadId];
    if (!original || original.cwd === cwd) return;
    if (original.messages.some((message) => message.role === "user" || message.role === "assistant")) {
      get().pushToast("warning", "只能在发送第一条消息前更换任务文件夹。");
      return;
    }
    try {
      // Resolve the old optimistic id first so its process can be closed
      // reliably. Open the replacement before closing it: activeThreadId never
      // becomes null, so React preserves the Composer's unsent local draft.
      const oldId = (await get().ensureConnected(threadId)) || threadId;
      await window.pi.app.openProject(cwd);
      await get().refreshProjects();
      const newId = await get().openThread(cwd, undefined, original.permission);
      if (!newId) return;
      await get().closeThread(oldId);
    } catch (e: any) {
      get().pushToast("error", "切换文件夹失败：" + (e?.message || e));
    }
  },

  // ---- edit menu ----
  editAction: async (action) => {
    try {
      await window.pi.app.editAction(action);
    } catch {
      /* ignore */
    }
  },
}));
