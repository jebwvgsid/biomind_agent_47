/** BioMind footer built from Pi's public context and footer data. */
import path from 'node:path';
import type { ContextUsage, ExtensionContext, SessionEntry } from '@earendil-works/pi-coding-agent';

export interface ProductFooterLayoutTools {
  visibleWidth(text: string): number;
  truncateToWidth(text: string, width: number, ellipsis?: string): string;
}
type Sanitize = (value: unknown, fallback?: string) => string;
interface Totals { input: number; output: number; cacheRead: number; cacheWrite: number; cost: number; cacheHit?: number }
const amount = (value: unknown): number => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0;

function totalUsage(entries: readonly SessionEntry[]): Totals {
  const totals: Totals = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0 };
  for (const entry of entries) {
    const usage = entry.type === 'usage' ? entry.usage
      : entry.type === 'message' && (entry.message.role === 'assistant' || entry.message.role === 'toolResult') ? entry.message.usage
      : entry.type === 'branch_summary' || entry.type === 'compaction' ? entry.usage : undefined;
    if (!usage) continue;
    totals.input += amount(usage.input); totals.output += amount(usage.output);
    totals.cacheRead += amount(usage.cacheRead); totals.cacheWrite += amount(usage.cacheWrite);
    totals.cost += amount(usage.cost?.total);
    if (entry.type === 'message' && entry.message.role === 'assistant') {
      const prompt = amount(usage.input) + amount(usage.cacheRead) + amount(usage.cacheWrite);
      totals.cacheHit = prompt > 0 ? amount(usage.cacheRead) / prompt * 100 : undefined;
    }
  }
  return totals;
}

function compactTokens(value: number): string {
  if (value < 1000) return String(value);
  if (value < 10000) return `${(value / 1000).toFixed(1)}k`;
  if (value < 1000000) return `${Math.round(value / 1000)}k`;
  if (value < 10000000) return `${(value / 1000000).toFixed(1)}M`;
  return `${Math.round(value / 1000000)}M`;
}
function contextTokens(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? `${(value / 1000).toFixed(1).replace(/\.0$/, '')}k` : '?';
}
function displayCwd(cwd: string): string {
  const home = process.env.HOME || process.env.USERPROFILE;
  if (!home) return cwd;
  const relative = path.relative(path.resolve(home), path.resolve(cwd));
  return relative === '' ? '~' : relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)
    ? `~${path.sep}${relative}` : cwd;
}

/** No files, credentials, sessions, or model requests are changed by this projection. */
export function installProductFooter(ctx: ExtensionContext, tools: ProductFooterLayoutTools, sanitize: Sanitize): void {
  if (ctx.mode !== 'tui' || !ctx.hasUI) return;
  ctx.ui.setFooter((tui, theme, footerData) => {
    let disposed = false, cachedSession: string | undefined, cachedLeaf: string | null | undefined;
    let cachedModel: ExtensionContext['model'], cachedModelKey: string | undefined;
    let name = '';
    let totals: Totals | undefined, context: ContextUsage | undefined;
    const unsubscribe = footerData.onBranchChange(() => { if (!disposed) tui.requestRender(); });
    return {
      render(width: number): string[] {
        if (disposed || !Number.isFinite(width) || width < 1) return [];
        const columns = Math.floor(width), session = ctx.sessionManager.getSessionId(), leaf = ctx.sessionManager.getLeafId();
        const model = ctx.model, modelKey = JSON.stringify([model?.provider, model?.id, model?.contextWindow]);
        // A running clock or native spinner may redraw many times between entries.
        // Context projection and cumulative history scans only run when their inputs change.
        if (!totals || session !== cachedSession || leaf !== cachedLeaf || model !== cachedModel || modelKey !== cachedModelKey) {
          totals = totalUsage(ctx.sessionManager.getEntries());
          context = ctx.getContextUsage();
          // Pi resolves this by scanning session_info entries. Renaming advances the leaf.
          name = sanitize(ctx.sessionManager.getSessionName(), '');
          cachedSession = session; cachedLeaf = leaf; cachedModel = model; cachedModelKey = modelKey;
        }
        const fit = (text: string, available = columns) => tools.truncateToWidth(text, Math.max(0, available), '…');
        const capacity = context?.contextWindow ?? model?.contextWindow;
        const contextText = `上下文 ${contextTokens(context?.tokens)}/${contextTokens(capacity && capacity > 0 ? capacity : undefined)}`;
        const percent = context?.percent ?? 0;
        const stats = [percent > 90 ? theme.fg('error', contextText) : percent > 70 ? theme.fg('warning', contextText) : contextText];
        if (totals.input) stats.push(`↑${compactTokens(totals.input)}`);
        if (totals.output) stats.push(`↓${compactTokens(totals.output)}`);
        if (totals.cacheRead) stats.push(`R${compactTokens(totals.cacheRead)}`);
        if (totals.cacheWrite) stats.push(`W${compactTokens(totals.cacheWrite)}`);
        if ((totals.cacheRead || totals.cacheWrite) && totals.cacheHit !== undefined) stats.push(`CH${totals.cacheHit.toFixed(1)}%`);
        // These synchronous public methods inspect metadata; they do not resolve keys.
        const subscription = !!model && (model.provider === 'kimi-coding' ||
          ctx.modelRegistry.isUsingOAuth(model) && ctx.modelRegistry.getProvider(model.provider)?.auth.oauth?.isSubscription === true);
        if (totals.cost || subscription) stats.push(`$${totals.cost.toFixed(3)}${subscription ? ' (sub)' : ''}`);
        const left = fit(stats.join(' ')), leftWidth = tools.visibleWidth(left);
        const modelName = sanitize(model?.id, 'no-model');
        const thinking = sanitize(ctx.thinkingLevel, 'off');
        const modelOnly = model?.reasoning ? `${modelName} • ${thinking === 'off' ? 'thinking off' : thinking}` : modelName;
        let right = modelOnly;
        if (model && footerData.getAvailableProviderCount() > 1) {
          const qualified = `(${sanitize(model.provider)}) ${modelOnly}`;
          if (leftWidth + 2 + tools.visibleWidth(qualified) <= columns) right = qualified;
        }
        const available = columns - leftWidth - 2;
        const rightText = available > 0 ? fit(right, available) : '';
        const padding = rightText ? ' '.repeat(Math.max(2, columns - leftWidth - tools.visibleWidth(rightText))) : '';
        let directory = sanitize(displayCwd(ctx.sessionManager.getCwd()), '未知目录');
        const branch = sanitize(footerData.getGitBranch(), '');
        if (branch) directory += ` (${branch})`;
        if (name) directory += ` • ${name}`;
        const lines = [theme.fg('dim', fit(directory)), theme.fg('dim', left) + theme.fg('dim', padding + rightText)];
        const statuses = [...footerData.getExtensionStatuses()].sort(([a], [b]) => a.localeCompare(b))
          .map(([, text]) => sanitize(text, '')).filter(Boolean);
        if (statuses.length) lines.push(theme.fg('dim', fit(statuses.join(' '))));
        return lines;
      },
      invalidate() {},
      dispose() { if (!disposed) { disposed = true; unsubscribe(); } },
    };
  });
}
