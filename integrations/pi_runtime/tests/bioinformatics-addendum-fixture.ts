/** Offline materials only: no sequencing files, jobs or scientific conclusions. */
const markers = {
  goal: 'BIO_SAMPLE_CURRENT_GOAL',
  confirmedDecision: 'BIO_SAMPLE_CONFIRMED_QC_ONLY',
  branchProposal: 'BIO_SAMPLE_BRANCH_PROPOSAL_NOT_CONFIRMED',
  submittedOnly: 'BIO_SAMPLE_SUBMITTED_ONLY',
  running: 'BIO_SAMPLE_RUNNING_NOT_COMPLETED',
  oldResult: 'BIO_SAMPLE_OLD_RESULT_NOT_CURRENT',
  partial: 'BIO_SAMPLE_PARTIAL_NOT_ALL_SAMPLES',
  smoke: 'BIO_SAMPLE_SMOKE_NOT_FULL_ANALYSIS',
  unknown: 'BIO_SAMPLE_UNKNOWN_MISSING_EVIDENCE',
} as const;

const importantPaths = [
  '/synthetic/rice-leaf-rnaseq/samples.tsv',
  '/synthetic/rice-leaf-rnaseq/raw/D3_R2.fastq.gz',
  '/synthetic/rice-leaf-rnaseq/reference/IRGSP-1.0.fa',
  '/synthetic/rice-leaf-rnaseq/reference/annotation.gtf',
  '/synthetic/rice-leaf-rnaseq/current-run/logs/align.83042.log',
  '/synthetic/rice-leaf-rnaseq/current-run/align/D1.sorted.bam.partial',
  '/synthetic/rice-leaf-rnaseq/previous-run-20260926/counts.tsv',
  '/synthetic/rice-leaf-rnaseq/smoke-1000-reads/qc.html',
] as const;

const userGoal = `${markers.goal}：合成任务拟比较水稻叶片对照与干旱条件的转录组；当前先核对六个双端文库及参考材料。`;
const confirmedDecision = `${markers.confirmedDecision}：用户已确认仅做输入核对与质量控制；分组、生物学重复、参考注释匹配确认前，不启动正式差异表达分析。`;
const branchProposal = `${markers.branchProposal}：离开的探索分支曾建议跳过质控、复用旧计数矩阵，并把 FDR 阈值从 0.05 改为 0.20；用户尚未确认这些建议，不能作为返回分支的既定决策。`;
const transcriptText = [
  userGoal,
  confirmedDecision,
  `物种与材料：水稻叶片；参考FASTA标注为IRGSP-1.0。项目清单 ${importantPaths[0]} 中分组和重复关系待核对。`,
  `${markers.submittedOnly}：run_ref=synthetic-submit-83041；队列仅返回已提交，未观察到退出状态、完成回执或结果验收；不能写作分析已完成。`,
  `${markers.running}：run_ref=synthetic-align-83042 仍在运行；目前只有 ${importantPaths[4]} 和 ${importantPaths[5]}，没有终态、完整BAM验收或索引证据。`,
  `${markers.oldResult}：${importantPaths[6]} 属于2026-09-26旧运行、旧样本清单，是否使用相同参考尚未核实；文件存在不等于本次任务产物。`,
  `${markers.partial}：本次输入核对仅5/6文库通过；D3缺少 ${importantPaths[1]}，因此不能声称全部样本已通过，不能形成完整计数矩阵。`,
  `${markers.smoke}：仅前1000条reads的流程连通性试跑生成 ${importantPaths[7]}；这不是完整数据集质控、正式分析或生物学结论。`,
  `${markers.unknown}：${importantPaths[2]} 的校验和、${importantPaths[3]} 与FASTA的匹配、链特异性、D3配对文件及完整运行退出码均缺失或未知。不得补猜；下一步先补齐这些证据。`,
  branchProposal,
].join('\n');

export const BIOINFORMATICS_SAMPLE = {
  userGoal, confirmedDecision, branchProposal, transcriptText, importantPaths, markers,
  evidenceBoundary: 'Offline synthetic input/instruction-delivery fixture only. No real model summary quality, scientific execution or scientific result is evaluated.',
} as const;
