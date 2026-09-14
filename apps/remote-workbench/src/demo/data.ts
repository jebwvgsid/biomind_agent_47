import type { Approval, Resource, Scenario, Server, Session, Step, Workspace } from '../types.js';

export const servers: Server[] = [
  { id: 'lab', name: '生信计算服务器', alias: 'biomind-lab-demo', description: 'Linux · 演示服务器' },
  { id: 'sandbox', name: '测试服务器', alias: 'biomind-sandbox-demo', description: 'Linux · 演示服务器' },
];
export const workspaces: Workspace[] = [
  { id: 'rice', serverId: 'lab', name: '水稻转录组研究', path: '/demo/projects/rice-rnaseq' },
  { id: 'variant', serverId: 'lab', name: '变异注释探索', path: '/demo/projects/variant-study' },
  { id: 'sandbox', serverId: 'sandbox', name: '方法验证', path: '/demo/workspaces/methods' },
];
export const scenarios: { id: Scenario; label: string }[] = [
  { id: 'approval', label: '等待脚本审批' }, { id: 'plan', label: '等待计划审批' },
  { id: 'running', label: '运行中' }, { id: 'recovery', label: '执行失败 · 恢复中' },
  { id: 'offline', label: '断线待重连' }, { id: 'completed', label: '完成 · 查看结果' },
  { id: 'new', label: '新会话' }, { id: 'disconnected', label: '未连接' },
];
const script = `# 演示模式 · 仅供查看，原型不会执行本脚本
# 工作目录：/demo/projects/rice-rnaseq
# 读取：reads/ 下的演示 FASTQ
# 写入：results/quality/；不覆盖输入文件
set -euo pipefail

mkdir -p results/quality/fastqc
fastqc --threads 4 \\
  --outdir results/quality/fastqc \\
  reads/demo_R1.fastq.gz reads/demo_R2.fastq.gz

multiqc results/quality/fastqc \\
  --outdir results/quality \\
  --filename multiqc_report.html
`;
export function makeScript(root: string): string { return script.replace('/demo/projects/rice-rnaseq', root); }
export function makeSteps(): Step[] {
  return [
    { title: '确认输入文件', detail: '核对双端文件与样本说明', tool: '文件检查', status: 'completed' },
    { title: '评估测序质量', detail: '碱基质量、GC 分布与接头含量', tool: 'FastQC', status: 'pending' },
    { title: '汇总质量报告', detail: '汇总各样本指标，保留原始报告', tool: 'MultiQC', status: 'pending' },
    { title: '核验并解读结果', detail: '检查交付项，给出后续分析建议', tool: 'BioMind', status: 'pending' },
  ];
}
export function makeResources(root: string, complete: boolean): Resource[] {
  return [
    { id: 'r1', name: 'demo_R1.fastq.gz', path: `${root}/reads/demo_R1.fastq.gz`, kind: 'input', format: 'FASTQ', size: '1.2 GB · 演示', status: 'available', preview: '演示文件引用\n此处不读取真实 FASTQ，也不下载原始数据。\n读取方向：R1\n配对文件：demo_R2.fastq.gz' },
    { id: 'r2', name: 'demo_R2.fastq.gz', path: `${root}/reads/demo_R2.fastq.gz`, kind: 'input', format: 'FASTQ', size: '1.3 GB · 演示', status: 'available', preview: '演示文件引用\n此处不读取真实 FASTQ，也不下载原始数据。\n读取方向：R2' },
    { id: 'samples', name: 'samples.tsv', path: `${root}/samples.tsv`, kind: 'input', format: 'TSV', size: '256 B · 演示', status: 'available', preview: 'sample\tcondition\tread1\tread2\ndemo\tcontrol\tdemo_R1.fastq.gz\tdemo_R2.fastq.gz' },
    { id: 'report', name: 'multiqc_report.html', path: `${root}/results/quality/multiqc_report.html`, kind: 'artifact', format: 'HTML', size: complete ? '2.4 MB · 演示' : '—', status: complete ? 'verified' : 'expected', preview: 'MultiQC 质量报告 · 演示模式\n\n以下全部为合成指标，不能用于科学结论。\n样本：demo\n读取对数：24,000,000\nQ30：94.8%\nGC 含量：43.2%\n\n交付核验：演示后端返回 completed / verified。' },
    { id: 'metrics', name: 'quality_summary.tsv', path: `${root}/results/quality/quality_summary.tsv`, kind: 'artifact', format: 'TSV', size: complete ? '186 B · 演示' : '—', status: complete ? 'verified' : 'expected', preview: '# 演示模式：合成数据\nsample\tread_pairs\tq30_percent\tgc_percent\ndemo\t24000000\t94.8\t43.2' },
    { id: 'scratch', name: 'fastqc_notes.txt', path: `${root}/results/quality/fastqc_notes.txt`, kind: 'artifact', format: 'TXT', size: '64 B · 演示', status: 'unverified', preview: '演示模式 · 中间记录\n文件已生成，尚未经交付核验。文件存在不代表分析完成。' },
  ];
}
export function approvalFor(id: string, kind: 'plan' | 'script', version = 1): Approval {
  return { id: `${id}-${kind}-${version}`, kind, version, digest: `demo-${kind}-v${version}-7e2a90c1`, status: 'pending' };
}
export function makeSession(scenario: Scenario, id = `demo-${scenario}`, serverId = 'lab', workspaceId = 'rice'): Session {
  const complete = scenario === 'completed';
  const empty = scenario === 'new' || scenario === 'disconnected';
  const workspace = workspaces.find(w => w.id === workspaceId)!;
  const steps = makeSteps();
  if (complete) steps.forEach(step => step.status = 'completed');
  if (['running', 'offline'].includes(scenario)) steps[1].status = 'running';
  if (scenario === 'recovery') { steps[1].status = 'completed'; steps[2].status = 'failed'; }
  const plan = approvalFor(id, 'plan');
  plan.status = scenario === 'plan' ? 'pending' : 'approved';
  const approval = approvalFor(id, 'script');
  if (['running', 'offline', 'completed', 'recovery'].includes(scenario)) approval.status = 'approved';
  const session: Session = {
    id, runId: `${id}-run-1`, title: empty ? '新会话' : ({ completed: '测序质量评估 · 已完成', recovery: '质量评估 · 恢复记录', offline: '测序质控 · 待重连', plan: 'RNA-seq 分析计划', running: '样本质量检查 · 运行中' } as Partial<Record<Scenario, string>>)[scenario] || '水稻 RNA-seq · 质量评估',
    serverId, workspaceId,
    status: empty ? 'idle' : complete ? 'completed' : scenario === 'approval' ? 'awaiting_script' : scenario === 'plan' ? 'awaiting_plan' : scenario === 'recovery' ? 'recovering' : 'running',
    connection: scenario === 'disconnected' ? 'disconnected' : scenario === 'offline' ? 'interrupted' : 'demo',
    confirmedAt: '2026-09-14T09:42:18+08:00', cursor: empty ? 0 : 3, elapsed: complete ? 186 : 42,
    messages: empty ? [] : [
      { id: `${id}-m1`, role: 'user', time: '09:41', text: '请检查这个工作区的双端 RNA-seq 数据质量，汇总质量报告，并告诉我是否需要做接头过滤。', delivery: 'accepted', references: ['r1', 'r2', 'samples'] },
      { id: `${id}-m2`, role: 'assistant', time: '09:42', text: '我会先核对输入，再用 FastQC 评估测序质量，最后汇总报告并检查交付项。原始数据会保留在服务器工作目录中。' },
    ],
    steps: empty ? [] : steps, approvals: empty ? [] : scenario === 'plan' ? [plan] : [plan, approval], script: makeScript(workspace.path),
    resources: makeResources(workspace.path, complete), executionCount: ['running','offline','completed','recovery'].includes(scenario) ? 1 : 0,
    events: empty ? [] : [
      { seq: 1, time: '09:41:02', type: 'message.accepted', text: '已受理演示请求，创建分析计划。', level: 'info' },
      { seq: 2, time: '09:41:08', type: 'tool.completed', text: '文件检查完成：1 组双端文件与 1 份样本表。', level: 'info' },
      { seq: 3, time: '09:42:18', type: 'approval.requested', text: scenario === 'plan' ? '计划版本 1 等待审核。' : '已确认计划版本 1，生成质控脚本。', level: 'info' },
    ],
  };
  if (complete) session.messages.push({ id: `${id}-result`, role: 'assistant', time: '09:45', text: '演示分析已完成，质量报告和指标汇总均已通过演示核验。合成样本的 Q30 为 94.8%，请在右侧查看报告。本页数值仅用于展示结果交付流程。' });
  if (scenario === 'recovery') session.error = { node: 'Execute → Recovery', summary: '质量评估脚本退出，已进入恢复', detail: '演示错误：MultiQC executable not found（exit 127）。本轮演示保留 FastQC 检查记录，Recovery 正在检查已登记工具路径。', retryable: true, next: '展示恢复方案，再单独审核更新后的脚本。' };
  return session;
}
export function initialSessions(): Session[] {
  return ['approval', 'completed', 'recovery', 'offline'].map(s => makeSession(s as Scenario));
}
