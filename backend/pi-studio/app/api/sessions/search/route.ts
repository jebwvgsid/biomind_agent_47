import { NextResponse } from "next/server";
import { statSync } from "node:fs";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { listAllSessions, resolveSessionPath, buildSessionContext, getSessionEntries, buildBioMindHistoryContext } from "@/lib/session-reader";
import { messageSearchText, excerpt } from "@/lib/message-search";
import { bioMindHistoryTruncated, listBioMindHistory } from "@/lib/biomind-history";
import { isBioMindRemoteProfile } from "@/lib/remote-security";

const MAX_MATCHES = 40;
// Reading + rebuilding context for every session is expensive; cap how many
// sessions we scan per request so a long history doesn't stall the palette.
const MAX_SESSIONS_SCANNED = 200;

interface ContentMatch {
  sessionId: string;
  sessionName: string;
  cwd: string | null;
  entryId: string;
  role: string;
  snippet: string;
  messageIndex: number;
}

async function searchBioMindHistory(q: string) {
  const sessions = await listBioMindHistory();
  const discoveryTruncated = bioMindHistoryTruncated();
  const matches: ContentMatch[] = [];
  const skipped: { sessionId: string; code: string }[] = [];
  let scanned = 0;
  let bytes = 0;
  let truncated = discoveryTruncated;
  const deadline = Date.now() + 15_000;
  for (const s of sessions) {
    if (scanned >= MAX_SESSIONS_SCANNED || matches.length >= MAX_MATCHES || Date.now() >= deadline) { truncated = true; break; }
    scanned++;
    try {
      bytes += statSync(s.path).size;
      if (bytes > 64 * 1024 * 1024) { truncated = true; break; }
      // Both helpers validate the configured session/project boundary. Projection
      // uses the producing runtime's pure native helpers, never SDK open/create.
      const entries = getSessionEntries(s.path);
      const { context } = await buildBioMindHistoryContext(s.path, entries, {
        deferThinking: true, deferToolResultImages: true, deferAllImages: true,
      });
      for (let i = 0; i < context.messages.length; i++) {
        const message = context.messages[i];
        const text = messageSearchText(message);
        if (!text.toLowerCase().includes(q)) continue;
        matches.push({ sessionId: s.id, sessionName: s.name || s.firstMessage || s.id, cwd: s.cwd,
          entryId: context.entryIds[i] ?? "", role: message.role, snippet: excerpt(text, q, 2000), messageIndex: i });
        if (matches.length >= MAX_MATCHES) { truncated = true; break; }
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "";
      skipped.push({ sessionId: s.id, code: /^(?:BIOMIND_)?HISTORY_[A-Z_]{1,64}$/.test(message) ? message : "HISTORY_UNREADABLE" });
    }
  }
  return NextResponse.json({ matches, profile: "biomind", readOnly: true, scanned, truncated,
    skipped, projection: "persisted_default_leaf", discoveryNote: "native_metadata_discovery_may_omit_unreadable_files" });
}

export async function GET(req: Request) {
  const q = new URL(req.url).searchParams.get("q")?.trim().toLowerCase() ?? "";
  if (!q) return NextResponse.json({ matches: [] });
  if (q.length > 512) return NextResponse.json({ error: "SEARCH_QUERY_TOO_LONG" }, { status: 400 });

  try {
    if (isBioMindRemoteProfile()) return await searchBioMindHistory(q);
    const sessions = await listAllSessions();
    const matches: ContentMatch[] = [];
    let scanned = 0;

    for (const s of sessions) {
      if (matches.length >= MAX_MATCHES || scanned >= MAX_SESSIONS_SCANNED) break;
      let filePath: string | null;
      try {
        filePath = await resolveSessionPath(s.id);
      } catch {
        continue;
      }
      if (!filePath) continue;
      scanned += 1;

      try {
        const sm = SessionManager.open(filePath);
        const ctx = buildSessionContext(sm.getEntries() as never, undefined, {
          deferThinking: true,
          deferToolResultImages: true,
        });
        const { messages, entryIds } = ctx;
        for (let i = 0; i < messages.length; i++) {
          const text = messageSearchText(messages[i]);
          if (!text || !text.toLowerCase().includes(q)) continue;
          matches.push({
            sessionId: s.id,
            sessionName: s.name || s.firstMessage || s.id,
            cwd: s.cwd ?? null,
            entryId: entryIds[i] ?? "",
            role: messages[i].role,
            snippet: text,
            messageIndex: i,
          });
          if (matches.length >= MAX_MATCHES) break;
        }
      } catch {
        // Skip a corrupt/unreadable session rather than failing the whole search.
      }
    }

    return NextResponse.json({ matches });
  } catch (error) {
    return NextResponse.json({ error: String(error) }, { status: 500 });
  }
}
