/** Human-readable display copies; the domain's original approval object stays authoritative. */
import type { ExtensionUIContext } from '@earendil-works/pi-coding-agent';

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
  const rows = ['是否执行以下提案？'];
  const add = (label: string, value: unknown) => {
    if (value === undefined) return;
    const text = visible(value);
    rows.push(text.includes('\n') ? `${label}：\n${text.split('\n').map(line => '  ' + line).join('\n')}` : `${label}：${text}`);
  };
  if (proposal.description) add('说明', proposal.description);
  if (payload.kind === 'command' || payload.kind === 'installation') {
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
export async function selectApprovalProposal(ui: Pick<ExtensionUIContext, 'select'>,
  proposal: Record<string, unknown>, choices: string[], signal?: AbortSignal): Promise<string | undefined> {
  let technical = false;
  for (;;) {
    signal?.throwIfAborted();
    const toggle = technical ? '收起校验详情' : '查看校验详情';
    const answer = await ui.select(formatApprovalProposal(proposal, technical), [...choices, toggle], { signal });
    signal?.throwIfAborted();
    if (answer !== toggle) return answer;
    technical = !technical;
  }
}
