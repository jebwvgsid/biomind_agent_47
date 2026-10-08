/** Human-readable display copies; the domain's original approval object stays authoritative. */
import type { ExtensionUIContext } from '@earendil-works/pi-coding-agent';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const object = (value: unknown): Record<string, any> => value && typeof value === 'object' && !Array.isArray(value)
  ? value as Record<string, any> : {};

function visible(value: unknown): string {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '';
  // Show control characters literally rather than letting a command disguise its
  // contents with terminal escapes, carriage returns or bidirectional overrides.
  return text.replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g,
    character => '\\u' + character.charCodeAt(0).toString(16).padStart(4, '0'));
}

export function formatApprovalProposal(proposal: Record<string, unknown>, technical = false): string {
  const payload = object(proposal.payload);
  const rows = [payload.kind === 'file_read' ? '需要读取项目外输入' : '是否执行以下提案？'];
  const add = (label: string, value: unknown) => {
    if (value === undefined) return;
    const text = visible(value);
    rows.push(text.includes('\n') ? `${label}：\n${text.split('\n').map(line => '  ' + line).join('\n')}` : `${label}：${text}`);
  };
  if (proposal.description) add('说明', proposal.description);
  if (payload.directory_targets?.length) {
    rows[0] = '允许 BioMind 在以下目录工作吗？';
    add('用途',payload.purpose ?? '核对输入并完成本次分析');
    for (const target of payload.directory_targets) {
      add(payload.covered_directory_grants?.length ? '此次新增目录／能力' : '目标目录',target.root);
      add('权限',target.access==='read_write'?'读写：含创建、修改、覆盖、重命名和删除普通文件':'只读：读取、查看元数据和目录；不能修改输入');
    }
    if (payload.covered_directory_grants?.length) add('已有授权（本次不重复授予）',payload.covered_directory_grants.map((g:Record<string,unknown>)=>g.root));
    add('范围','所列目录及子目录；不含目录外链接目标、凭据、控制目录及受保护 runtime 写入');
    add('执行','批准后，范围内普通操作免逐次确认；命令保持既有沙箱、网络隔离及角色限制');
    add('输出','仅当前项目或另行批准的读写范围；只读输入保持只读');
    add('有效期','当前持久会话，含恢复及同会话后续任务；不含 /new 或 fork');
    add('当前项目',payload.project);
    rows.push('项目归属不变，不加载目标目录的设置、hooks 或扩展。manual、取消、撤销仍有效。');
    rows.push('调整范围／仅本次：可选择原明确目标、仅当前请求或仅授权范围，保留逐次动作确认。');
    if (payload.path) add('触发文件或目录',payload.path);
    if (payload.targets?.some((t:Record<string,unknown>)=>t.target_kind==='file')) add('精细选项的原明确目标',payload.targets.map((t:Record<string,unknown>)=>t.root));
  } else if (payload.kind === 'path_access') {
    add('当前项目',payload.project);add('用途',payload.purpose);
    for (const target of payload.targets ?? []) {
      add('请求路径',target.requested_path);add('canonical 目标',target.root);
      add('能力',target.access==='read_write'?'读写（包含读取）':'读取');
      add('范围',target.target_kind==='file'?'精确文件':target.recursive?'目录及子目录':'仅此目录');
    }
    add('建议有效期',payload.suggested_lifetime==='session'?'当前持久会话':'当前任务');
    rows.push('最终有效期由下方选择决定；当前会话含以后恢复本 session，不含 /new 或 fork。');
    rows.push('自动审批模式保持不变；manual 下具体写入或命令仍需批准。项目与输出要求保持不变。');
    rows.push('命令目录读取为本次文件快照：每目录 256 项/2 秒、总 512 挂载；私有项排除，链接/特殊文件拒绝，后续新增文件不可见。');
  } else if (payload.kind === 'file_read') {
    add('用途', proposal.description ?? '核对输入并完成本次分析');
    add('规范目标', payload.path);
    add('目标类型', payload.target_kind === 'directory' ? '目录' : '精确文件');
    add('包含子目录', payload.recursive === true ? '是' : '否');
    if (payload.scope_candidate) {
      add('可选目录范围', object(payload.scope_candidate).root);
      add('目录范围包含子目录', object(payload.scope_candidate).recursive === true ? '是' : '否');
    }
    add('操作', '查看元数据及读取内容；不允许修改原始数据');
    add('有效期', '下方选择当前任务或当前持久会话（含恢复；不含 /new 或 fork）');
    add('原项目', payload.project);
    rows.push('项目、会话与结果归属保持不变。命令执行仍需其原有批准。');
    rows.push('本次决定授予文件读取许可；正式命令能否受进程级只读保护由启动回执确认。');
  } else if (payload.kind === 'command' || payload.kind === 'installation') {
    add('操作', payload.kind === 'command' ? '执行命令' : '安装软件');
    add('工作目录', payload.cwd);
    const args = object(payload.kind === 'command' ? payload.arguments : payload.request);
    add('软件环境', args.env_name);
    if (Object.keys(object(args.env)).length) {
      rows.push('环境变量：');
      for (const [key, value] of Object.entries(args.env)) {
        rows.push('  ' + visible(JSON.stringify(key).slice(1, -1)) + '=' + visible(JSON.stringify(value)));
      }
    }
    rows.push('');
    add('命令', payload.kind === 'installation' ? payload.code ?? args.cmd : args.cmd);
    if (args.argv !== undefined) add('程序及参数（argv）', args.argv);
    const access=object(payload.access_projection);
    if (access.write_roots) add('本次可写根',access.write_roots);
    if (access.reads?.length) add('本次只读文件',access.reads);
    if (access.directory_snapshots?.length) add('目录只读快照及可见性限制',access.directory_snapshots);
    // Preserve every other operative argument. Only bookkeeping leaves the main view.
    const remaining = Object.fromEntries(Object.entries(args).filter(([key]) => !['cmd', 'argv', 'env', 'env_name'].includes(key)));
    if (Object.keys(remaining).length) add('其他执行参数', remaining);
  } else {
    const main = Object.fromEntries(Object.entries(payload).filter(([key]) => !['source_hashes', 'environment_hash', 'human_required'].includes(key)));
    add('操作内容', main);
  }
  if (technical) {
    rows.push('');
    add('完整提案与校验信息', proposal);
  }
  return rows.join('\n');
}

/** Viewing details never returns a decision or changes its original ordering. */
export async function selectApprovalProposal(ui: Pick<ExtensionUIContext, 'select'> & Partial<Pick<ExtensionUIContext, 'custom' | 'onTerminalInput'>>,
  proposal: Record<string, unknown>, choices: string[], signal?: AbortSignal): Promise<string | undefined> {
  if (ui.custom) {
    // Pi owns the overlay, focus and terminal lifetime. Only the display copy
    // scrolls: the original proposal/hash and the decision order never change.
    const require = createRequire(import.meta.resolve('@earendil-works/pi-coding-agent'));
    const { Text, SelectList, matchesKey, Key, truncateToWidth, visibleWidth } = await import(pathToFileURL(require.resolve('@earendil-works/pi-tui')).href);
    return ui.custom<string | undefined>((tui, theme, _keys, done) => {
      let technical = false, offset = 0, ended = false;
      let removeInput: (() => void) | undefined;
      const finish = (value?: string) => { if (ended) return; ended = true; removeInput?.(); signal?.removeEventListener('abort', abort); done(value); };
      const abort = () => finish();
      removeInput = ui.onTerminalInput?.(data => {
        if (matchesKey(data, Key.ctrl('c'))) { finish(); return { consume: true }; }
        return undefined;
      });
      const listTheme = {
        selectedPrefix: (text: string) => theme.fg('accent', text), selectedText: (text: string) => theme.fg('accent', text),
        description: (text: string) => theme.fg('muted', text), scrollInfo: (text: string) => theme.fg('dim', text), noMatch: (text: string) => text,
      };
      let visibleChoices=choices.length;
      const makeList=(count:number,selected=0)=>{
        const result=new SelectList(choices.map(value=>({value,label:value})),count,listTheme);
        result.setSelectedIndex(selected);
        result.onSelect=(item:{value:string})=>finish(item.value);
        result.onCancel=()=>finish();return result;
      };
      let list=makeList(visibleChoices);
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) queueMicrotask(abort);
      return {
        render(width: number) {
          const inner = Math.max(1, width - 4);
          const rows = Math.max(12, tui.terminal.rows - 2);
          const count=Math.max(2,Math.min(choices.length,Math.floor((rows-8)/2)));
          if(count!==visibleChoices){
            const selected=choices.indexOf(list.getSelectedItem()?.value ?? '');
            visibleChoices=count;list=makeList(count,Math.max(0,selected));
          }
          const footer = new Text('↑↓ 选择 · Enter 确认 · Esc/Ctrl+C 取消并暂停', 0, 0).render(inner);
          const options = list.render(inner);
          const budget = Math.max(1, rows - options.length - footer.length - 5);
          const detail = new Text(formatApprovalProposal(proposal, technical), 0, 0).render(inner);
          offset = Math.max(0, Math.min(offset, Math.max(0, detail.length - budget)));
          const page = detail.slice(offset, offset + budget);
          const frame = (left: string, right: string) => theme.fg('borderAccent', left + '─'.repeat(Math.max(0, width - 2)) + right);
          const line = (text: string) => {
            const clipped = truncateToWidth(text, inner);
            return theme.bg('customMessageBg', theme.fg('borderAccent', '│ ') + clipped
              + ' '.repeat(Math.max(0, inner - visibleWidth(clipped))) + theme.fg('borderAccent', ' │'));
          };
          return [frame('┌', '┐'), line(theme.bold('审批 · 默认拒绝')),
            ...[...page, ...Array(Math.max(0, budget - page.length)).fill('')].map(line),
            line(theme.fg('muted', `${offset + 1}–${Math.min(detail.length, offset + budget)}/${detail.length} · PgUp/PgDn 滚动 · Tab 详情`)),
            frame('├', '┤'), ...options.map(line), ...footer.map((text: string) => line(theme.fg('muted', text))), frame('└', '┘')];
        },
        invalidate() { list.invalidate(); },
        handleInput(data: string) {
          if (matchesKey(data, Key.escape) || matchesKey(data, Key.ctrl('c'))) return finish();
          if (matchesKey(data, Key.pageUp)) offset = Math.max(0, offset - 8);
          else if (matchesKey(data, Key.pageDown)) offset += 8;
          else if (matchesKey(data, Key.tab)) { technical = !technical; offset = 0; }
          else list.handleInput(data);
          tui.requestRender();
        },
        dispose() { removeInput?.(); signal?.removeEventListener('abort', abort); },
      };
    // Cover the entire row. A narrower native overlay deliberately preserves
    // the base row at its sides (welcome labels / selected tool backgrounds).
    }, { overlay: true, overlayOptions: { width: '100%', maxHeight: '100%', anchor: 'center' } });
  }
  let technical = false;
  for (;;) {
    signal?.throwIfAborted();
    const toggle = technical ? '收起校验详情' : '查看校验详情';
    const summary = formatApprovalProposal(proposal, technical).split('\n').slice(0, 5).map(line => line.slice(0, 100)).join('\n');
    const answer = await ui.select(summary, [...choices, toggle], { signal });
    signal?.throwIfAborted();
    if (answer !== toggle) return answer;
    technical = !technical;
  }
}
