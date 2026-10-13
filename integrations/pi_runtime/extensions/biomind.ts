/** BioMind's sole Pi extension. Native CLI, SDK and tests load this module. */
import { planStageBoundary, boundaryDrafts, assertStageBoundary, type StageBoundary } from '../stage-context.js';
import { nativeRead } from '../native-read.js';
import { runtimeBash, type RuntimeBashLaunch } from '../native-bash.js';
import { DISCOVERY_TOOLS, discoveryDefinition, nativeDiscovery, type DirectorySnapshot } from '../native-discovery.js';
import { nativeMutation } from '../native-mutation.js';
import { filterNativeContextFiles, currentNativeProjectContext } from '../native-context.js';
import path from 'node:path';
import { homedir } from 'node:os';
import { readFileSync } from 'node:fs';
import { executionProfile, sessionProfile, type ExecutionProfile } from '../execution-profile.js';
import { NATIVE_TOOLS, NATIVE_RULES, nativeTools } from '../native-workflow.js';
import { realpath, stat } from 'node:fs/promises';
import { createReadToolDefinition, createWriteToolDefinition, createEditToolDefinition, getAgentDir } from '@earendil-works/pi-coding-agent';
import { randomUUID } from 'node:crypto';
import { configuredProvider, validateModelSelection } from '../model-config.js';
import { Type, type TSchema } from 'typebox';
import type { AgentMessage } from '@earendil-works/pi-agent-core';
import type { ExtensionAPI, ExtensionContext, SessionEntry, ToolDefinition } from '@earendil-works/pi-coding-agent';
import { createDomainClient, DomainHandshakeTimeoutError, type DomainOptions, type DomainLifecycle } from '../domain-client.js';
import { canaryTools, installUsageObservation, isCanaryMode } from '../usage-observer.js';
import { toolRenderers, receiptDetails, projectToolReceipt, displayPreview } from '../tool-display.js';
import { installBioMindProductUI, formatPermissionStatus, type BioMindBackendState } from '../product-ui.js';
import { inspectWelcomePreference, inspectNativeWelcomeInstructions, describeWelcomeInstructions, describeWelcomeMemory, describeWelcomeAuthentication,
  type PreferenceStatus, type ProjectInstructionStatus } from '../welcome-status.js';
import { formatApprovalProposal, selectApprovalProposal } from '../approval-display.js';
import { readEditableFile, saveEditableFile, memoryDefault, saveMemoryDefault, argumentCompletions } from '../instructions.js';
import { listProjects, createProject, restartProjectCommand } from '../project-launcher.js';
import { readTargetSessionRoot } from '../native-session.js';
import { loadCompactionInstructions } from '../compaction-addendum.js';
import { controlInstructions, InstructionControlError } from '../studio-instructions.js';
import {acquireSessionWriter,SessionWriterError} from '../session-writer.js';
import {borrowNativeWriter,writerHeader} from '../session-writer-host.js';

export interface DomainConnection {
  readonly startupFailed?: boolean;
  readonly lifecycle?: DomainLifecycle;
  on?(event: 'lifecycle', listener: (event: DomainLifecycle) => void): unknown;
  off?(event: 'lifecycle', listener: (event: DomainLifecycle) => void): unknown;
  call(method: string, params: Record<string, unknown>, options?: { signal?: AbortSignal; requestId?: string }): Promise<unknown>;
  hostCall(method: string, params: Record<string, unknown>, options?: { signal?: AbortSignal; requestId?: string }): Promise<unknown>;
  close(options: { disconnectOnly: true }): void | Promise<void>;
}
export interface BioMindExtensionOptions {
  domainFactory?: (options: DomainOptions) => DomainConnection;
  executionProfile?: ExecutionProfile;
}
export interface BioMindUIRequest {
  id: string;
  requestId: string;
  sessionId: string;
  kind: 'question' | 'approval';
  question: string;
  toolCallId: string;
  choices?: string[];
  method?: 'editor';
  prefill?: string;
  proposal?: Record<string, unknown>;
}
export interface BioMindUIResponse {
  id: string;
  requestId: string;
  sessionId: string;
  answer?: string;
  choice?: 'y' | 'n' | 'scope' | 'edit' | 'cancel' | 'session' | 'read_only' | 'read_session' | 'select' | 'directory_session' | 'adjust';
  selections?: Array<{ index: number; access: 'read' | 'read_write'; lifetime: 'request' | 'session' }>;
  replacement?: Record<string, unknown>;
  cancelled?: boolean;
  action_hash?: string;
}
export interface RequestState {
  requestId: string;
  ownerSessionId?: string;
  role: 'Main';
  status: 'active' | 'settled' | 'aborted' | 'error' | 'paused' | 'blocked';
  pauseReason?: string;
  revision?: number;
  userBasis?: Array<{ text: string; revision: number }>;
  boundary?: StageBoundary;
  handoff?: { toolCallId: string; goal: string; manifest?: string; bound?: Record<string, any> };
  handoffConsumed?: boolean; // Read-only legacy field; new requests never create a handoff.
  plan?: Array<{ step: string; status: 'pending' | 'in_progress' | 'completed' }>;
  planSources?: string[]; // Legacy metadata only; never used to withdraw an existing plan.
  maintenance?: { consumed: boolean; consent?: boolean; outboxId?: string; prompt?: string; sources?: unknown; applied?: boolean };
}
interface Snapshot {
  preferences?: unknown;
  approval_mode?: unknown;
  request_paused?: boolean;
  authorization_refs?: unknown[];
  history_enabled: boolean;
  revision?: string;
  diagnostics?: unknown;
  memory_mode?: string;
  maintenance?: { enabled?: boolean };
  archive_enabled?: boolean;
  memory_root?: string | null;
}
const REQUEST = 'biomind.request';
const EMPTY = Type.Object({}, { additionalProperties: false });
// Tool execution consumes validated records; keep schema inference bounded as this catalog grows.
const OBJECT = (properties: Record<string, TSchema>): TSchema => Type.Object(properties, { additionalProperties: false });
const nonempty = () => Type.String({ minLength: 1 });
const optionalString = () => Type.Optional(Type.String());
const MAX_QUESTION_PREFILL_BYTES = 64 * 1024;
const asObject = (value: unknown): Record<string, any> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : {};
const customEntries = (branch: readonly SessionEntry[], name: string) => branch.filter(
  (entry): entry is Extract<SessionEntry, { type: 'custom' }> => entry.type === 'custom' && entry.customType === name);
export function requestFromBranch(branch: readonly SessionEntry[]): RequestState | undefined {
  const raw = customEntries(branch, REQUEST).at(-1)?.data as Record<string, any> | undefined;
  if (!raw?.requestId) return undefined;
  // Legacy entries remain untouched on disk. Their role and pending transition
  // are not executable instructions; user facts still belong to this request.
  const { handoff, handoffConsumed: _consumed, ...record } = raw;
  return { ...record, role: 'Main',
    userBasis: raw.userBasis?.length ? raw.userBasis : handoff?.bound?.user_basis ?? [],
    boundary: raw.boundary?.kind === 'task' ? raw.boundary : undefined } as RequestState;
}
function textOf(message: unknown): string {
  const content = asObject(message).content;
  return typeof content === 'string' ? content : Array.isArray(content) ? content.filter(item => item.type === 'text').map(item => item.text).join('\n') : '';
}
function historicalFacts(message: AgentMessage): AgentMessage {
  if (message.role !== 'custom' || message.customType !== 'biomind.handoff') return message;
  let content = textOf(message);
  try {
    const { instruction: _instruction, phase: _phase, ...facts } = JSON.parse(content);
    content = JSON.stringify({ historical_record: 'prior handoff facts; no transition or execution instruction', ...facts });
  } catch { content = '[历史交接记录，仅保留事实，不触发执行或角色切换]\n' + content; }
  return { ...message, content };
}
/** Only migrate obsolete role-handoff instructions; Pi owns history and summaries.
 * Retrieval settings and source metadata never redact already accepted messages.
 */
export function normalizeLegacyHistory(messages: AgentMessage[]): AgentMessage[] {
  return messages.map(historicalFacts);
}

const MAIN_TOOLS = [...DISCOVERY_TOOLS, 'request_path_access', 'probe_command', 'ask_user', 'select_environment', 'register_environment',
  'inspect_run', 'wait_run', 'read_run_log', 'cancel_run', 'file_stat', 'read', 'memory_search', 'memory_read',
  'update_plan', 'run_command', 'install_tool', 'write', 'edit', 'memory_write'];
const MAINTENANCE_RULES = `当前阶段：记忆维护。仅处理本次已完成对话的记忆维护；不继续或启动分析任务。仅在尚未提交时使用 maintenance_submit 提交有当前证据支持的 patch 或空 operations；提交后核对实际回执并结束。`;
const COMMON_RULES = `你是运行在 Pi 中的 BioMind 单一主 agent，在同一会话完成需求理解、核查、执行、纠错与交付。

【任务推进】
按用户当前请求回答或执行；只要求讨论、方案或审计时，不擅自实施修改或启动正式分析。信息足够就推进；仅对影响结果且无法从已有材料解决的关键歧义，用 ask_user 等待真实回答。遵循最新修订、明确检查和停止条件；历史不自动触发重算，旧偏好和工具输出不是授权。update_plan 是可选笔记，普通问答和文件操作无需科学模板、TaskSpec、计划或交接摘要。

【环境与安装】
需要外部生信软件或分析环境时，先 select_environment 查询登记（可一次提交多个 tools），再考虑软件定位；普通 shell 的 PATH 未找到不表示未安装。仅普通文件读取、ls/rg 无需查登记。
查询只列当前阶段必需工具；可选工具、多个别名不同时作为必需项。UNKNOWN 或多候选不代表未安装；先根据候选已登记入口明确 env_name 选择。禁止自行猜软件根目录后 find；仍不明确就询问或 blocked。
某个环境的 command -v 未命中只能说明该次 shell 的 PATH；不能外推到其它已登记环境或整个服务器，也不能据此要求重装或换模型。Skill 的旧环境名不是软件唯一位置；若关键科学输入已经 blocked，就结束该阶段，不继续探测或追加未经证实的软件阻塞。
优先复用适用 binding_ref，否则 select_environment 查询登记。适用登记直接使用，不例行重复探测，也不借查询、刷新或初始化隐式探测。AVAILABLE 仅表示可选择，registered 不等于本次检查通过，未知能力/版本仍为 unknown。
未命中不等于未安装；必要时 mode=locate 有界定位、register_environment 补登记。用户明确要求的检查仍须先完成，环境复用不代替科学输入 QC。真实缺包仅诊断对应能力，不因取消、超时或输入错误重装或判整个环境损坏。
安装必须必要且已有安装意图确认；安装、目标验证、登记分别按真实回执报告。登记失败只恢复登记；状态未知先对账，不重装。
发现未登记的已有软件，先用 probe_command 对确切入口做适配该软件的短检查，再 register_environment；使用 verification_runs 引用真实检查回执及有辨识度的输出，不把同名文件或单独退出码当验证。检查可接受软件实际 help 退出码，不假定都支持 --version。相同任务中已验证且未变更的环境直接复用，不反复扫描、探针或登记。
install_tool 已自动验证登记时直接使用结果，不再重复登记；版本或环境切换必须明确选择，不能覆盖另一个环境。

【执行与恢复】
需持久观察的科学计算用 run_command，保存真实 run_ref 和完整日志，按需查询、等待、读日志或取消；不以 nohup/& 冒充托管作业，不用模型轮询维持 UI 心跳。
复用当前请求与执行 scope 中唯一适用绑定，多绑定时明确引用。环境相关 bash 显式传 binding_ref，绑定仅对该次调用有效；裸 bash 用宿主环境，export/activate 不跨工具继承，用户 ! 不受环境选择影响。
同 call 重放查询原执行，不自动重跑；取消请求不等于已确认清理完成，无法确认保留 unknown。
普通同步 bash 省略 timeout 使用有限默认预算（通常60秒，单位秒）；probe_command 的 timeout_ms 单位毫秒。定位与探针用短预算及累计预算，仅在配置的软件位置和本任务允许来源核查，不扫描用户根目录或无关项目。超时先利用已有证据决定下一步，不原样重试或把超时当作未安装。正式计算用合理的显式长预算或 run_command，不套用定位短预算。

【验证与交付】
以本次真实回执、产物及任务所需 QC 判断科学结果；settled、isError:false、文件存在或管道末尾 rc 不能单独证明成功。分析包装用 pipefail/PIPESTATUS 或先保存原分析 rc 再显示摘要，不改变裸 shell 默认语义。
核对所需样本、路径、字段与产物来源，旧产物不冒充本次计算，合法空结果按对应格式解释。按用户要求交付报告、reproduce.sh 和状态表；复现脚本生成后不自动执行。只引用真实路径，不把远端文件说成已下载。
缺少 Q 行到样本ID的可信映射、参考坐标或其他关键科学输入时，只在本任务授权输入及直接相关来源记录内有限核查；仍缺证据就停止依赖该输入的阶段。交互模式 ask_user 问具体缺失文件；非交互 benchmark 明确报告 blocked 并结束。行数相等、文件同名或当前 VCF 顺序不能证明 Q 顺序；不猜映射、不删 Q、不换统计模型、不跨题搜索、不修改原题来跑通。保留独立检查、核查范围、未执行阶段及恢复所需文件。
先核对关键输入结构与映射证据，再投入全文件统计、格式转换或关联计算；已有 Skill 的有界输入检查可优先复用。确认缺映射后立即交付 blocked，不为这个已确定的科学阻塞继续找软件、尝试其它环境或做无关计算。探针因信号退出（如139）只报告入口曾启动和异常，不能宣称软件功能正常。

【资源与开发】
按需读取 Skill 正文及相对 baseDir 的 references/scripts；脚本资源属于 Skill，输入输出和工作目录属于当前项目。保护既有 dirty 内容，按逻辑单元验证后仅本地提交本轮 delta；未经明确授权不 push、合并、发布或改写历史。

【有界恢复与失败收尾｜BIOMIND_FAILURE_RECOVERY_V1】

你的目标是取得可信、可复现的分析结果，而不是通过持续重试维持“任务仍在进行”的表象。
正常情况下主动推进；发生失败时只做有依据、有边界的恢复。
触发停止条件后，停止本次自动执行，保存已有结果并明确报告，不能继续以修复、搜索、验证或等待为名延长任务。

1. 先判断是否真的发生失败

根据工具返回、程序状态和实际产物判断，不凭表面现象下结论。

以下情况需要进入失败判断：
工具调用被拒绝、脚本报错、程序异常退出、已确认的执行超时、关键输出缺失或输出未通过必要验证。

以下情况不能单独作为失败证据：
正常警告、暂时没有日志、工具提前返回“仍在运行”、尚在合理等待范围内的计算或已批准作业排队。

如果原进程仍在运行，应追踪同一进程或作业，不重复启动相同计算。
不要把工具等待结束误认为计算进程已经结束，也不要把持续打印日志视为科学结果已经有效。

2. 失败后先做有限诊断，不盲目重跑

首先明确：
失败发生在哪个步骤；
实际错误证据是什么；
目前哪些结果仍然有效；
下一次修复准备改变什么；
通过什么最小检查判断修复是否有效。

只陈述可核实的错误、改动和验证标准，不需要输出内部推理过程。

优先使用当前错误日志、工具参数说明、明确输入清单、已登记环境和已知软件入口。
每轮诊断默认最多进行三次针对性的轻量检查。
已有检查结果可以复用，不重复读取同一内容来制造进展。

对“文件不存在”，先核对工作目录、路径变量、分隔符解析和输入映射。
不能在脚本拼错路径时直接认定用户缺少正式输入。

对“程序被杀”或异常退出，区分已经证实的原因和待定位原因。
没有相应证据，不得直接归因为内存不足。

对环境问题，优先查询已登记环境和明确的软件入口。
不得无边界地扫描整个 HOME、整个项目集合或全盘寻找兼容库。
安装、升级或修改环境仍须遵守原有授权规则。

3. 同一失败点最多两轮修复

同一失败点，是同一任务步骤或交付目标尚未成功通过验证的阻塞状态。

首次执行失败后，最多允许两轮有依据的修复。
每轮可以包含必要的代码或参数修正、一次相应的轻量预检，以及至多一次目标步骤重执行。
如果预检或重执行再次失败，该修复轮已经消耗，不能在同一轮内部继续修改、继续验证而不计数。

因此，同一失败点最多是：
首次尝试 + 第一轮修复 + 第二轮修复。

这是上限，不是必须完成的次数。
没有合理修复依据，或已经发现不可自行解决的阻塞时，应提前停止。

以下行为不清零修复次数：
更换脚本名称、工具、输出目录或环境；
错误信息发生变化，但该步骤仍未通过验证；
把失败步骤重新命名或拆成更小的步骤；
重新规划、压缩上下文、恢复会话；
通过 shell、Python、R、工作流脚本或并行任务隐藏额外重试。

不得编写无限重试或高次数重试循环。
脚本内部的目标步骤重执行，也属于同一失败点的尝试额度。

只有该步骤的核心结果已经通过必要验证，才能关闭当前失败点并进入正常后续分析。
不同正常步骤不共享一个机械的“三次工具调用”限制。

4. 每次重试必须具有实质依据

重试前，必须能说明本次修复与上次失败之间的对应关系。

允许的依据包括：
修正了已经定位的语法或参数错误；
纠正了已经核实的路径、输入格式或样本映射；
按照实际版本说明调整了不兼容调用；
根据可靠资源证据调整线程数、批大小等执行参数。

没有新的证据或实质变化，不得原样重跑。
仅“再试一次”“可能这次可以”“换个名字运行”不构成依据。

有证据的短暂网络或服务故障可以进行一次有限退避重试，但仍占用已有修复额度和时间预算。

修复后优先做语法检查、输入检查或最小可行验证，再决定是否运行昂贵的正式计算。
小样本、降规模或替代数据只能作为明确标注的调试验证，不能冒充正式结果。

不得仅为了跑通而擅自改变已经约定的样本范围、细胞纳入标准、分组、表型、协变量、修饰类型定义、统计检验或其他关键科学口径。
未经授权的方法或科学口径变更，不属于普通技术修复。

5. 使用明确的时间预算，区分排障与正常计算

任务有总时限时，根据工具或运行环境提供的真实时间戳记录起点、已用时间和剩余预算，不凭感觉计时。

默认累计诊断与失败修复验证预算：
取任务总预算的 20% 与 10 分钟中的较小值。

默认收尾预留：
取任务总预算的 10% 与 5 分钟中的较小值。

例如总预算为 60 分钟：
累计诊断与失败修复验证默认最多 10 分钟；
至少预留 5 分钟收尾；
到第 55 分钟尚未完成时，应进入收尾，不再启动新的分析或修复。

排障预算包括：
错误定位、反复查找、修改后验证、失败退避以及无进展的重试等待。

通过针对性验证后，具有正常运行证据的正式科研计算使用分析运行预算，不因超过排障分钟数就被认定失败。
但恢复运行并不清零当前失败点的尝试次数；仍须等待该步骤结果通过验证。

每次准备启动计算或重试前，都应检查是否还留有执行、验证和收尾的时间。
预算不足时停止，不能指望外层超时器替你完成报告。

未指定任务总时限时，不擅自给所有科研任务设置一小时截止。
同一失败点限次、有限诊断和禁止盲目重试仍然适用。

对可能长时间阻塞的调用，使用当前工具实际支持的 timeout、作业查询或取消机制。
简单路径查找、版本确认和帮助查询默认采用短时、有限范围的检查。
正式长计算应设置适合其规模、且不超过剩余任务预算的运行条件，不能机械套用诊断命令的短超时。

6. 触发条件后停止，而不是再找一个办法

出现以下任一情况，应停止当前失败点的自动恢复：

a. 两轮修复后仍未解决；
b. 没有新的、可验证的修复依据；
c. 排障预算已经耗尽；
d. 剩余任务预算不足以继续执行并完成收尾；
e. 已确认缺少必需输入、权限、资源或必须由用户决定的科学口径；
f. 用户要求停止。

如果失败涉及必需步骤，或使主要交付目标无法成立：
结束本次任务的自动执行，不继续启动其他分析来回避停止结论。

只有失败项明确属于独立、可选的子项，且不影响其他结果有效性时：
可以将该子项标记为未完成，并继续原计划内不依赖它的工作。
不得自行缩减必需交付项后宣称全部完成。

进入任务停止状态后，只允许进行有限收尾：
取消本任务明确拥有的运行进程或作业；
保存并检查已有结果；
记录失败证据；
输出最终报告。

不得再启动新的计算、修复、环境搜索、安装或替代方案尝试。
只能取消可以确认属于本任务的进程或作业，不得误杀共享环境中的其他任务。

如果无法确认取消成功，应报告“取消未确认”及已知进程或作业信息，不能声称已经完全停止。
清理本身也不得进入无限重试。

7. 保存进度，不因恢复会话而忘记失败历史

复用当前任务已有的进度记录、计划或输出文件，维护最小失败摘要：
失败步骤；
已经使用的修复次数；
关键错误证据；
已尝试的改动与验证结果；
已验证的输出路径；
已知时间预算；
是否已经进入停止状态。

首次失败后尽早保存这些事实，不要等到最后一分钟才开始整理。
恢复中的记录不能提前写成“已停止”或“已完成”。

不要引入独立记忆体系或额外数据库。
如果当前任务尚无记录文件，可以在已经授权的任务输出目录中保存一个简短进度文件。

压缩或恢复会话后，先读取既有失败摘要，不得把次数重置为零。
如果失败历史不完整，应核对已有任务记录；无法确认剩余额度时，不得假定还有完整的两轮重试机会。

复用经过验证、且输入和参数未发生相关变化的中间结果。
不要因为后续一步失败，就重新运行整条分析流程。

8. 停止时必须交付报告

停止时，立即在对话中明确说明已经停止及直接原因，并在已有授权的任务输出目录保存报告。

报告至少包含：

执行状态：已停止；如有进程尚未确认停止，明确区分。
交付状态：未完成，或部分完成。
停止原因：代码/调用错误、环境异常、已确认缺输入或权限、资源限制、重试额度耗尽、时间预算不足，或科学口径待确认。
证据边界：哪些原因已确认，哪些仍待定位。
已完成且通过验证的步骤及实际结果路径。
已经存在但尚未验证的中间产物。
失败步骤和因此未执行的后续步骤。
已执行的修复次数、具体改动、每次结果及日志位置。
已知的时间消耗和进程/作业清理状态。
恢复任务所需的最小条件，以及可以从哪个有效检查点继续。

如果宿主统一使用 blocked 状态，应在原因中区分内部执行失败和真正的外部条件缺失。
不得把智能体脚本错误或调用错误包装成“用户未提供数据”。
不得把只存在文件、程序退出码为零或小样本调试通过，直接写成正式分析完成。

报告文件写入失败时，立即在对话中输出完整报告并说明保存失败。
不能为了生成一份漂亮报告而再次陷入工具重试循环。
只能引用已经确认存在的文件路径。

停止后，不自动恢复原失败循环。
收到有效的新输入、环境修复，或用户明确追加的重试授权后，先复核历史与预算，再从合理检查点恢复。
不能把追加一次尝试理解为重新获得无限重试权限。`;
const PROTECTED_RULES = `当前 execution:protected。文件读取、写入及命令继续经过原权限账本与执行保护，不能因工具可见或环境 prefix 已解析而视为目录已授权。项目外路径通过原工具申请缺失范围，已有授权可复用；用 request_path_access 汇总真实输入/输出范围，不能用 ask_user 重复代替宿主审批，拒绝后不能换工具、call id 或别名绕过。
read_paths 声明实际文件或小范围子树；只读快照限制及链接拒绝按回执处理，不能把 Conda 根加入分析输入。已选环境使用原只读运行时挂载。external cwd 不隐含可写，也不改变项目、session、request 或资源归属。probe_command 仅用于必要检查，保持只读、网络隔离和有界预算。
受控命令中的 ls/find 只说明当前挂载视图，不能断言宿主未安装。保护前置阻塞先解释原 reason，必要时读取 receipt_ref，不用 echo/ls、删除 read_paths 或裸 shell 绕过；修复后使用新 call 重新授权和 claim。execution_started 只证明包装进程已启动，不能替代业务成功。write_paths 不授予权限或覆盖输入保护；OUTPUT_CONTRACT_CONFLICT 应修订任务要求。`;

export function createBioMindExtension(options: BioMindExtensionOptions = {}) {
  // Host configuration is captured once; project files and model arguments are not inputs.
  const profile = executionProfile(options.executionProfile ?? process.env.BIOMIND_PI_EXECUTION_PROFILE ?? 'protected');
  const native = profile === 'native';
  // Only transport references survive resource reload. Pi keeps all request,
  // role, plan and transcript state on its current session branch.
  const registryKey = Symbol.for('biomind.pi.domain-connections.v1');
  const registryHost = globalThis as typeof globalThis & { [registryKey]?: Map<string, DomainConnection> };
  const connections = options.domainFactory ? new Map<string, DomainConnection>()
    : (registryHost[registryKey] ??= new Map<string, DomainConnection>());
  return function biomind(pi: ExtensionAPI): void {
    // Native CLI registers the same selection inside its own process. JSON
    // mode registers directly on its ModelRuntime and does not set this field.
    const selectionText = process.env.BIOMIND_PI_MODEL_SELECTION;
    if (selectionText) {
      const selection = validateModelSelection(JSON.parse(selectionText));
      const provider = configuredProvider(selection);
      if (provider) pi.registerProvider(selection.provider, provider);
    }
    const toolNames = (names: readonly string[]) => canaryTools((native ? nativeTools(names) : [...names]).sort());
    let userToolSelection: string[] | null = null;
    const coreToolNames = new Set([...MAIN_TOOLS,...NATIVE_TOOLS,'maintenance_submit']);
    const pluginTools = () => (typeof pi.getAllTools === 'function' ? pi.getAllTools() : [])
      .map(tool=>tool.name).filter(name=>!coreToolNames.has(name));
    const allowedMainTools = () => canaryTools([...new Set([...toolNames(MAIN_TOOLS),...(native?pluginTools():[])])]);
    const mainTools = () => allowedMainTools().filter(name=>userToolSelection===null || userToolSelection.includes(name));
    const publishToolPolicy = () => pi.events.emit('biomind:tools_policy',{allowed:allowedMainTools(),selected:userToolSelection,
      denied:native?[]:pluginTools().map(name=>({name,reason:'PROTECTED_PLUGIN_TOOL_UNSUPPORTED'})),
      pluginExecution:native?'native_account_permissions':'not_authorized_by_protected_domain'});
    let lastAppliedTools:string[]|undefined;
    const sameTools=(a:readonly string[],b:readonly string[])=>JSON.stringify([...a].sort())===JSON.stringify([...b].sort());
    const persistToolSelection=()=>{
      if(!activeContext)return;
      const saved=customEntries(activeContext.sessionManager.getBranch(),'biomind.tool_selection').at(-1)?.data as any;
      if(saved?.version===1&&JSON.stringify(saved.selected)===JSON.stringify(userToolSelection))return;
      pi.appendEntry('biomind.tool_selection',{version:1,selected:userToolSelection});
    };
    const restoreToolSelection=(ctx:ExtensionContext)=>{
      const saved=customEntries(ctx.sessionManager.getBranch(),'biomind.tool_selection').at(-1)?.data as any;
      if(saved?.version===1&&(saved.selected===null||Array.isArray(saved.selected)&&saved.selected.every((name:unknown)=>typeof name==='string')))
        userToolSelection=saved.selected===null?null:[...saved.selected];
      else userToolSelection=null;
      lastAppliedTools=undefined;
    };
    const setActiveTools = (names: readonly string[]) => {
      if(names===MAIN_TOOLS&&lastAppliedTools&&activeContext&&readySessions.has(activeContext.sessionManager.getSessionId())
          &&!state(activeContext)?.maintenance?.outboxId&&typeof pi.getActiveTools==='function'){
        const effective=pi.getActiveTools();
        // Preserve the original UI's explicit setActiveTools changes, including [].
        if(!sameTools(effective,lastAppliedTools)){userToolSelection=[...effective];persistToolSelection();}
      }
      const next=names===MAIN_TOOLS?mainTools():toolNames(names);
      pi.setActiveTools(next);lastAppliedTools=[...next];
    };

    const existingSessions = new Set<string>();
    const readySessions = new Set<string>();
    const backendStates = new Map<string, BioMindBackendState>();
    const backendObservers = new Map<string, { connection: DomainConnection; listener: (event: DomainLifecycle) => void }>();
    const notifiedFailures = new Set<string>();
    let backendEpoch = 0;
    let migrationConsumed = false;
    const clients = connections;
    const pendingUI = new Map<string, { request: BioMindUIRequest; resolve: (response: BioMindUIResponse) => void; reject: (error: Error) => void }>();
    const inputQueue: Array<{ text: string; kind: 'steer' | 'followUp' }> = [];
    // Versions of bytes actually read/written in this request. Approval tokens
    // stay in the domain authority; this map carries no approval capability.
    const readVersions = new Map<string, string>();
    let initialUserPending = false;
    let preparedInputRequestId: string | undefined;
    let activeContext: ExtensionContext | undefined;
    let writerReady=false;
    const claimWriter=async(ctx:ExtensionContext,reason:string,previousSessionFile?:string,target?:string)=>{
      if(options.domainFactory)return; // Synthetic domain harness owns no production writer.
      const sessionFile=target??ctx.sessionManager.getSessionFile();if(!sessionFile)return;
      const id=target?writerHeader(target).id:ctx.sessionManager.getSessionId();
      const mode=target||reason==='resume'||reason==='startup'&&process.env.BIOMIND_PI_SESSION_EXISTING==='1'?'resume':'new';
      if(process.env.BIOMIND_SESSION_WRITER_MANAGED==='parent')await borrowNativeWriter({sessionFile,sessionId:id,cwd:ctx.cwd,mode,reason,previousSessionFile});
      else if(process.env.BIOMIND_SESSION_WRITER_MANAGED==='json')await acquireSessionWriter({sessionFile,sessionId:id,cwd:ctx.cwd,mode,ownerGeneration:process.env.BIOMIND_SESSION_WRITER_OWNER_GENERATION??''});
      else throw new SessionWriterError('SESSION_WRITER_PARENT_REQUIRED');
    };
    pi.events.on('biomind:tools_selection',(raw:unknown)=>{
      const selected=asObject(raw).selected;
      if(selected!==null && (!Array.isArray(selected)||selected.some(name=>typeof name!=='string'||!allowedMainTools().includes(name))))return;
      userToolSelection=selected===null?null:[...selected];
      if(asObject(raw).persist!==false)persistToolSelection();
      lastAppliedTools=undefined;
      if(activeContext && !state(activeContext)?.maintenance?.outboxId)setActiveTools(MAIN_TOOLS);
    });
    let snapshot: Snapshot = { history_enabled: false };
    let snapshotKnown = false;
    let snapshotFailed = false;
    let instructionsFailed = false;
    let welcomePreference: PreferenceStatus = { status: 'unknown' };
    let welcomeProject: ProjectInstructionStatus | undefined;
    let welcomeEpoch = 0;
    const instructionDrafts = new Map<string, string>();
    const savedInstructions = new Set<string>();
    const savedGlobalInstructions = new Set<string>();
    let lastOutcome: 'completed' | 'aborted' | 'error' = 'completed';
    let scientificBlock: Record<string, any> | undefined;
    let scientificDeadline: ReturnType<typeof setTimeout> | undefined;
    const clearScientificDeadline = () => { clearTimeout(scientificDeadline); scientificDeadline = undefined; };
    // Delivery bookkeeping only; messages and stable entry identities stay in Pi.
    // The original archive writer owns deduplication and actual persistence.
    const archiveSubmitted = new Set<string>();
    const state = (ctx: ExtensionContext) => {
      const value=requestFromBranch(ctx.sessionManager.getBranch());
      return value?.ownerSessionId && value.ownerSessionId !== ctx.sessionManager.getSessionId() ? undefined : value;
    };
    const refreshProductUI = installBioMindProductUI(pi, state, { details: ctx => ({
      backend: backendStates.get(ctx.sessionManager.getSessionId()) ?? { state: 'starting' },
      memory: describeWelcomeMemory(snapshotKnown ? snapshot : undefined, process.env, snapshotFailed),
      instructions: describeWelcomeInstructions(snapshotKnown ? asObject(snapshot.preferences) : welcomePreference,
        welcomeProject, savedInstructions, instructionsFailed, savedGlobalInstructions),
      authentication: describeWelcomeAuthentication(ctx),
    }), permissions: {
      status: ctx => observePermissions(ctx, { action: 'status' }),
      revoke: (ctx, grant, listing) => observePermissions(ctx, { action: 'revoke', grant_id: grant.grant_id,
        request_scope: listing.request_scope, policy_revision: listing.policy_revision }),
    } });
    installUsageObservation(pi, state);
    const sessionId = (ctx: ExtensionContext) => ctx.sessionManager.getSessionId();
    const updateBackend = (ctx: ExtensionContext, next: BioMindBackendState) => {
      const id = sessionId(ctx);
      if (next.state === 'failed' && backendStates.get(id)?.state === 'failed') return;
      backendStates.set(id, next);
      if (next.state !== 'ready') {
        readySessions.delete(id);
        setActiveTools([]);
      }
      pi.events.emit('biomind:backend', { sessionId: id, ...next });
      refreshProductUI();
      if (next.state === 'failed' && !notifiedFailures.has(id)) {
        notifiedFailures.add(id);
        ctx.ui.notify(`BioMind ${next.stage === 'runtime' ? '后台连接中断' : '后台初始化失败'}，分析输入已停止：${next.message ?? '原因未知'}`, 'error');
      }
    };
    const unobserveBackend = (id: string) => {
      const observed = backendObservers.get(id);
      if (observed) observed.connection.off?.('lifecycle', observed.listener);
      backendObservers.delete(id);
    };
    const publish = (ctx: ExtensionContext, value: RequestState) => {
      pi.appendEntry(REQUEST, value);
      pi.events.emit('biomind:state', { ...value, sessionId: sessionId(ctx) });
      ctx.ui.setStatus('biomind', `${value.role} · ${value.status}`);
    };
    const client = (ctx: ExtensionContext) => {
      const id = sessionId(ctx);
      let value = clients.get(id);
      if (!value) {
        value = (options.domainFactory ?? createDomainClient)({ cwd: ctx.cwd, sessionId: id,
          executionProfile: profile, profileSource: process.env.BIOMIND_PI_PROFILE_SOURCE ?? 'host',
          sessionExisting: existingSessions.has(id),
          migrateProfile: !migrationConsumed && process.env.BIOMIND_PI_MIGRATE_PROFILE === '1',
          python: process.env.BIOMIND_PI_PYTHON, dataRoot: process.env.BIOMIND_PI_DATA_ROOT,
          installRoot: process.env.BIOMIND_PI_INSTALL_ROOT,
          registryPath: process.env.BIOMIND_PI_REGISTRY_PATH, skillsRoot: process.env.BIOMIND_PI_SKILLS_ROOT,
          preferencesPath: process.env.BIOMIND_PI_PREFERENCES_PATH, memoryRoot: process.env.BIOMIND_PI_MEMORY_ROOT,
          wikiRoot: process.env.BIOMIND_PI_WIKI_ROOT, historyEnabled: process.env.BIOMIND_PI_HISTORY_ENABLED === '1' });
        clients.set(id, value);
        migrationConsumed = true;
      }
      if (backendObservers.get(id)?.connection !== value) {
        unobserveBackend(id);
        const connection = value;
        const listener = (event: DomainLifecycle) => {
          const currentContext = activeContext;
          if (!currentContext || sessionId(currentContext) !== id || clients.get(id) !== connection) return;
          if (!['unavailable', 'failed', 'closed'].includes(event.state)) return;
          updateBackend(currentContext, { state: event.state, stage: event.stage, message: event.error?.message });
          if (event.state === 'unavailable') {
            inputQueue.splice(0); preparedInputRequestId = undefined;
            currentContext.abort();
            currentContext.ui.setStatus('biomind-execution', '后台不可用 · 分析输入已停止');
          }
        };
        backendObservers.set(id, { connection, listener });
        connection.on?.('lifecycle', listener);
        if (connection.lifecycle) listener(connection.lifecycle);
      }
      return value;
    };
    const begin = async (ctx: ExtensionContext, text = '') => {
      const value: RequestState = { requestId: randomUUID(), ownerSessionId: sessionId(ctx), role: 'Main', status: 'active',
        revision: 1, userBasis: text ? [{ text, revision: 1 }] : [], boundary: planStageBoundary(ctx.sessionManager.getBranch(), 'task') };
      const ownerSession = sessionId(ctx);
      let connection: DomainConnection | undefined;
      try {
        connection = client(ctx);
        await connection.hostCall('request.begin', { request_id: value.requestId, role: 'main' });
      }
      catch (error) {
        if (connection?.startupFailed && clients.get(ownerSession) === connection) {
          clients.delete(ownerSession);
          // Release only the failed startup transport. The next user input
          // creates a fresh service; never replay the request automatically.
          try { await connection.close({ disconnectOnly: true }); }
          catch { pi.events.emit('biomind:diagnostic', { code: 'DOMAIN_STARTUP_DISCONNECT_FAILED' }); }
        }
        // An unaccepted identity must never replace the branch's accepted owner.
        // In particular, a live job can reject a new request and still need cancellation.
        lastOutcome = 'error'; setActiveTools([]); throw error;
      }
      publish(ctx, value); setActiveTools(MAIN_TOOLS); archiveSubmitted.clear(); readVersions.clear(); lastOutcome = 'completed';
      return value;
    };
    const current = (ctx: ExtensionContext) => {
      const value = state(ctx);
      if (!value || value.status !== 'active') throw new Error('No active BioMind request');
      return value;
    };
    const pause = async (ctx: ExtensionContext, reason: string) => {
      const value = state(ctx); if (!value) return;
      publish(ctx, { ...value, status: 'paused', pauseReason: reason });
      lastOutcome = 'aborted';
      try { await client(ctx).hostCall('request.pause', { reason }, { requestId: value.requestId }); }
      finally { await refreshApprovalStatus(ctx); ctx.abort(); }
      ctx.ui.notify('当前任务已暂停；没有终止已运行作业。继续需明确恢复，原拒绝不会变为批准。', 'info');
    };
    const archiveTurn = async (ctx: ExtensionContext) => {
      if (!snapshot.archive_enabled) return;
      const value = state(ctx); if (!value) return;
      const branch = ctx.sessionManager.getBranch();
      const start = branch.findIndex(entry => entry.type === 'custom' && entry.customType === REQUEST && asObject(entry.data).requestId === value.requestId);
      if (start < 0) return;
      for (const entry of branch.slice(start + 1)) {
        if (entry.type !== 'message' || !['user', 'assistant', 'toolResult'].includes(entry.message.role)) continue;
        const eventId = `pi:${sessionId(ctx)}:${entry.id}`;
        if (archiveSubmitted.has(eventId)) continue;
        const message = entry.message;
        const publicText = textOf(message);
        // Thinking blocks, provider credentials and full transport messages are
        // never archival content. Tool receipts keep their public text and IDs.
        const content = publicText.slice(0, 64000);
        try {
          const receipt = asObject(await client(ctx).hostCall('archive.append', {
            event_id: eventId, event_type: message.role === 'user' ? 'user_message'
              : message.role === 'assistant' ? 'assistant_message' : 'tool_result',
            stage: value.role.toLowerCase(), content,
            source_refs: [eventId], metadata: { pi_entry_id: entry.id, pi_parent_id: entry.parentId,
              pi_request_id: value.requestId, projection_truncated: publicText.length > content.length,
              ...(message.role === 'toolResult' ? { tool_name: message.toolName, tool_call_id: message.toolCallId, is_error: message.isError } : {}) },
          }, { requestId: value.requestId }));
          if (['enqueued', 'coalesced'].includes(String(receipt.status))) archiveSubmitted.add(eventId);
          pi.events.emit('biomind:archive', { eventId, ...receipt });
        } catch {
          pi.events.emit('biomind:diagnostic', { code: 'ARCHIVE_ENQUEUE_FAILED' });
        }
      }
    };
    const applySettings = async (ctx: ExtensionContext, patch: Record<string, unknown>) => {
      for (const key of Object.keys(patch)) if (!['history_enabled', 'memory_mode'].includes(key)) throw new Error('Unsupported memory setting');
      if ('history_enabled' in patch && typeof patch.history_enabled !== 'boolean') throw new Error('history_enabled must be boolean');
      if ('memory_mode' in patch && !['ask', 'auto', 'manual', 'disabled'].includes(String(patch.memory_mode))) throw new Error('Unsupported memory mode');
      const result = asObject(Object.keys(patch).length ? await client(ctx).hostCall('settings.update', patch) : await client(ctx).call('context.snapshot', {}));
      if (typeof result.history_enabled !== 'boolean') throw new Error('Invalid settings receipt');
      snapshot = result as Snapshot;
      snapshotKnown = true; snapshotFailed = false;
      return { history_enabled: result.history_enabled, memory_mode: result.memory_mode, revision: result.revision };
    };
    pi.registerCommand('biomind-memory', {
      description: '记忆作用域：status | history on/off | mode ask/auto/manual/disabled | default on/off',
      getArgumentCompletions: argumentCompletions(['status', 'history on', 'history off', 'mode ask', 'mode auto', 'mode manual', 'mode disabled', 'default on', 'default off']),
      async handler(args, ctx) {
        const parts = args.trim().split(/\s+/); let patch: Record<string, unknown> = {};
        let savedMessage = '';
        try {
          if (!args.trim() || parts[0] === 'status' && parts.length === 1) patch = {};
          else if (parts[0] === 'default' && parts.length === 2 && ['on', 'off'].includes(parts[1])) {
            if (ctx.mode !== 'tui' || !ctx.hasUI) throw new Error('保存默认值请使用终端入口');
            const filename = process.env.BIOMIND_PI_CONFIG_PATH;
            if (!filename) throw new Error('未保存：请用 --pi-config 选择既有 BioMind 配置文件');
            if (!await ctx.ui.confirm('保存记忆启动默认值', `${filename}\nhistoryEnabled → ${parts[1] === 'on'}\n仅修改此字段；当前运行开关不变。`)) return;
            await saveMemoryDefault(parts[1] === 'on');
            savedMessage = '启动默认值已保存；当前运行开关不变。重启仍可能受更高优先级启动参数覆盖。\n';
          }
          else if (parts[0] === 'history' && parts.length === 2 && ['on', 'off'].includes(parts[1])) patch = { history_enabled: parts[1] === 'on' };
          else if (parts[0] === 'mode' && parts.length === 2) patch = { memory_mode: parts[1] };
          else throw new Error('用法：/biomind-memory status | history on/off | mode ask/auto/manual/disabled | default on/off');
          const result = await applySettings(ctx, patch), defaults = await memoryDefault();
          ctx.ui.notify(`${savedMessage}项目知识检索：${result.history_enabled ? '开启' : '关闭'}（本次运行）\n启动生效默认：${process.env.BIOMIND_PI_HISTORY_DEFAULT === '1' ? '开启' : '关闭'}；来源：${process.env.BIOMIND_PI_HISTORY_SOURCE ?? '未知'}\n磁盘默认：${defaults.error ?? (defaults.value === undefined ? '未配置' : defaults.value ? '开启' : '关闭')}；${defaults.path ?? '无可写配置，使用 --pi-config 选择'}\n记忆根：${snapshot.memory_root ?? process.env.BIOMIND_PI_MEMORY_ROOT ?? '未配置'}\n记忆整理模式：${result.memory_mode ?? '未知'}；自动整理：${snapshot.maintenance?.enabled ? '按现有策略允许' : '未启用'}\n历史会话与已读工具结果仍保留；关闭只阻止新的检索，不删除历史或让模型忘记已读内容。`, 'info');
          pi.events.emit('biomind:settings_result', { status: 'ok', sessionId: sessionId(ctx), ...result });
        } catch (error) { snapshotKnown = false; snapshotFailed = true; ctx.ui.notify(savedMessage + String(error), 'error'); pi.events.emit('biomind:settings_result', { status: 'error', code: 'SETTINGS_UPDATE_FAILED', message: String(error) }); }
      },
    });
    pi.registerCommand('instructions', {
      description: '查看实际说明来源；global/project 编辑；project-base 编辑被遮蔽的 AGENTS.md',
      getArgumentCompletions: argumentCompletions(['status', 'global', 'project', 'project-base']),
      async handler(args, ctx) {
        if (ctx.mode !== 'tui' || !ctx.hasUI) return;
        const choice = args.trim() || 'status';
        if (!['status', 'global', 'project', 'project-base'].includes(choice)) { ctx.ui.notify('用法：/instructions [status|global|project|project-base]', 'info'); return; }
        try {
          await applySettings(ctx, {});
          const preference = asObject(snapshot.preferences);
          const files = filterNativeContextFiles(ctx.getSystemPromptOptions().contextFiles ?? [], ctx.cwd, getAgentDir());
          const local = files.filter(file => path.dirname(path.resolve(file.path)) === path.resolve(ctx.cwd));
          const effective = local[0];
          welcomeProject = effective ? { path: effective.path, state: effective.content.trim() ? '已加载' : '为空' } : undefined;
          const preferred = typeof preference.preferred_path === 'string' ? preference.preferred_path : process.env.BIOMIND_PI_PREFERENCES_PATH;
          const globalPath = typeof preference.source_path === 'string' ? preference.source_path : preferred;
          const preferenceState = preference.status === 'unreadable' ? '加载失败' : preference.status === 'missing' || preference.exists === false ? '未找到'
            : preference.empty ? '存在为空' : preference.status === 'truncated' ? '已加载（按现有长度上限截断）' : preference.status === 'loaded' ? '已加载（每次请求刷新）' : '未知';
          const overridePath = path.join(ctx.cwd, 'AGENTS.override.md');
          const overrideExists = await stat(overridePath).then(value => value.isFile()).catch(() => false);
          const basePath = path.join(ctx.cwd, 'AGENTS.md');
          const projectPath = effective?.path ?? (overrideExists ? overridePath : basePath);
          const rows = [`项目根：${ctx.cwd}`, `全局偏好：${globalPath ?? '未知'}；来源：${String(preference.source ?? preference.source_kind ?? '未知')}；${preferenceState}`];
          if (preferred && preferred !== globalPath) rows.push(`首选来源：${preferred}（缺失，当前使用回退来源）`);
          for (const file of files) {
            let stateText = file.content.trim() ? '已加载' : '存在为空';
            try { const disk = await readEditableFile(file.path); if (disk.text !== file.content) stateText = '修改后待刷新'; }
            catch { stateText = '加载后文件不可读'; }
            if (file === effective) welcomeProject = { path: file.path, state: stateText };
            rows.push(`Pi 说明：${file.path}；${stateText}${savedInstructions.has(path.resolve(file.path)) ? '；已保存，当前会话待刷新' : ''}`);
          }
          if (!effective) rows.push(`项目根说明：${projectPath}；未加载（未找到或边界过滤，保存后 /reload）`);
          if (overrideExists) rows.push(`同目录 AGENTS.override.md 遮蔽 AGENTS.md；默认编辑 ${projectPath}`);
          rows.push('全局偏好与 Pi 用户级 AGENTS 各自生效；不复制内容。保存与请求生效分别确认。');
          instructionsFailed = false;
          if (choice === 'status') { ctx.ui.notify(rows.join('\n'), 'info'); return; }
          const target = choice === 'global' ? (preference.exists === false ? preferred ?? globalPath : globalPath) : choice === 'project-base' ? basePath : projectPath;
          if (!target) throw new Error('无法确定实际偏好来源；请先检查启动 preferencesPath');
          const before = await readEditableFile(target);
          const shadowed = choice === 'project-base' && overrideExists;
          const detail = `${before.path}${before.symlink ? `\n软链接真实目标：${before.target}` : ''}${shadowed ? '\n此文件被 AGENTS.override.md 遮蔽，修改暂不会生效' : ''}`;
          const draft = await ctx.ui.editor(`编辑说明（Enter 完成，Esc 取消）\n${detail}`, instructionDrafts.get(before.path) ?? before.text);
          if (draft === undefined) { ctx.ui.notify('已取消，未写入说明。', 'info'); return; }
          instructionDrafts.set(before.path, draft);
          if (!await ctx.ui.confirm('确认保存说明', `${detail}\n字符数 ${before.text.length} → ${draft.length}；${draft === before.text ? '内容未变' : draft === '' ? '清空文件' : '替换为编辑器中的内容'}\n保存后当前会话待刷新。`)) { ctx.ui.notify('未保存；草稿保留，重新执行本命令可继续编辑。', 'info'); return; }
          await saveEditableFile(before, draft);
          instructionDrafts.delete(before.path); savedInstructions.add(before.path);
          if ([globalPath, preferred].some(filename => filename && path.resolve(filename) === before.path)) savedGlobalInstructions.add(before.path);
          if (choice !== 'global' && !shadowed) welcomeProject = { path: before.path, state: '已保存，待 /reload' };
          ctx.ui.notify(`已保存：${before.path}。当前会话待刷新；${shadowed ? '此文件仍被 override 遮蔽。' : '使用原生 /reload 刷新。'}不会中断科学作业。`, 'info');
          if (!shadowed && ctx.isIdle() && await ctx.ui.confirm('现在刷新说明？', '使用 Pi 原生 reload；保留当前会话与科学作业。')) await ctx.reload();
        } catch (error) { snapshotKnown = false; instructionsFailed = true; ctx.ui.notify(`说明操作失败：${String(error)}。未保存的草稿仅保留在本次运行，重载或退出前可重新打开。`, 'error'); }
      },
    });
    // Pi may expose a settings-derived relative directory. Preserve its current
    // project meaning before serializing a restart into a different project.
    const sessionDirectory = (ctx: ExtensionContext) => {
      const value = ctx.sessionManager.getSessionDir();
      return value ? path.resolve(ctx.cwd, value) : value;
    };
    const launcherConfig = (ctx: ExtensionContext) => ({ agentDir: getAgentDir(), sessionDir: sessionDirectory(ctx),
      memoryRoot: process.env.BIOMIND_PI_MEMORY_ROOT, wikiRoot: process.env.BIOMIND_PI_WIKI_ROOT, installRoot: process.env.BIOMIND_PI_INSTALL_ROOT,
      launcherPython: process.env.BIOMIND_PI_LAUNCHER_PYTHON });
    const restartOptions = (ctx: ExtensionContext) => ({ configPath: process.env.BIOMIND_PI_CONFIG_PATH,
      sessionDir: sessionDirectory(ctx), node: process.env.BIOMIND_PI_NODE, modelRecord: process.env.BIOMIND_PI_MODEL_RECORD });
    pi.registerCommand('project', {
      description: '项目目录与创建入口；跨项目使用安全重启',
      getArgumentCompletions: argumentCompletions(['list', 'create', 'open']),
      async handler(args, ctx) {
        if (ctx.mode !== 'tui' || !ctx.hasUI) return;
        try {
          let choice = args.trim();
          const listing = await listProjects(launcherConfig(ctx), ctx.cwd);
          const rows = listing.projects.map(project => `${project.root} [${project.sources.join(', ')}；${project.availability}]`);
          ctx.ui.notify(`当前项目：${ctx.cwd}\n${rows.join('\n')}${listing.incomplete.length ? `\n列表不完整：${listing.incomplete.join('; ')}` : ''}\n跨项目请在终端重启；当前输入、会话与科学作业保留。`, 'info');
          if (!choice) choice = await ctx.ui.select('项目操作（取消保留当前会话）', ['list', 'open', 'create']) ?? 'list';
          if (choice === 'list') return;
          if (choice === 'open') {
            const selected = await ctx.ui.select('选择项目；仅显示重启命令', rows);
            if (selected === undefined) return;
            const project = listing.projects[rows.indexOf(selected)];
            if (project) ctx.ui.notify(`请在系统终端执行：\n${restartProjectCommand(project.root, restartOptions(ctx))}`, 'info');
          } else if (choice === 'create') {
            const target = await ctx.ui.input('新项目目录（相对当前项目或绝对路径）');
            if (!target?.trim()) return;
            if (!await ctx.ui.confirm('创建项目', `${path.resolve(ctx.cwd, target)}\n仅创建必要目录、缺失的 AGENTS.md 与目录登记；不启动分析。`)) return;
            const receipt = await createProject(launcherConfig(ctx), ctx.cwd, target);
            ctx.ui.notify(displayPreview(receipt), 'info');
          } else ctx.ui.notify('用法：/project [list|open|create]；系统终端可用 biomind project create/open <目录>', 'info');
        } catch (error) { ctx.ui.notify(`项目操作失败：${String(error)}`, 'error'); }
      },
    });
    pi.events.on('biomind:settings_request', async (raw: unknown) => {
      const request = asObject(raw);
      try {
        if (!activeContext) throw new Error('No active BioMind session');
        const patch = Object.fromEntries(Object.entries(request).filter(([key]) => key !== 'id'));
        const result = await applySettings(activeContext, patch);
        pi.events.emit('biomind:settings_result', { id: request.id, status: 'ok', sessionId: sessionId(activeContext), ...result });
      } catch (error) { pi.events.emit('biomind:settings_result', { id: request.id, status: 'error', code: 'SETTINGS_UPDATE_FAILED', message: String(error) }); }
    });
    pi.events.on('biomind:instructions_request',async(raw:unknown)=>{
      const request=asObject(raw);
      try {
        if(!activeContext)throw new InstructionControlError('INSTRUCTIONS_OWNER_UNAVAILABLE');
        await applySettings(activeContext,{});
        const {id,loadedContextFiles,...operation}=request;
        const data=await controlInstructions({cwd:activeContext.cwd,agentDir:getAgentDir(),preference:asObject(snapshot.preferences),
          contextFiles:Array.isArray(loadedContextFiles)?loadedContextFiles:[],ownerLoaded:true},operation);
        if(data.saved){savedInstructions.add(data.path);if(data.scope==='global')savedGlobalInstructions.add(data.path);}
        pi.events.emit('biomind:instructions_result',{id,status:'ok',data});
      }catch(error){pi.events.emit('biomind:instructions_result',{id:request.id,status:'error',
        code:error instanceof InstructionControlError?error.code:'INSTRUCTIONS_READ_FAILED',
        ...(error instanceof InstructionControlError && error.currentVersion?{currentVersion:error.currentVersion}:{})});}
    });
    pi.events.on('biomind:packages_state_request',async(raw:unknown)=>{
      const request=asObject(raw);
      try{
        const ctx=activeContext;
        if(!ctx||!writerReady||!readySessions.has(sessionId(ctx)))throw new Error('PACKAGES_STATE_UNAVAILABLE');
        if(!ctx.isIdle()||pendingUI.size||inputQueue.length||state(ctx)?.maintenance?.outboxId&&!state(ctx)?.maintenance?.applied)
          throw new Error('PACKAGES_BUSY');
        const live=asObject(await client(ctx).call('session.lifecycle',{action:'inspect'}));
        if(live.active_jobs!==false||live.count!==0||!ctx.isIdle()||pendingUI.size||inputQueue.length)
          throw new Error(live.active_jobs===true||Number(live.count)>0?'PACKAGES_BUSY':'PACKAGES_STATE_UNAVAILABLE');
        pi.events.emit('biomind:packages_state_result',{id:request.id,status:'ok',idle:true,activeJobs:0,sessionId:sessionId(ctx)});
      }catch(error){pi.events.emit('biomind:packages_state_result',{id:request.id,status:'error',
        code:error instanceof Error&&error.message==='PACKAGES_BUSY'?'PACKAGES_BUSY':'PACKAGES_STATE_UNAVAILABLE'});}
    });
    const observePermissions = async (ctx: ExtensionContext, input: Record<string, unknown>): Promise<Record<string, any>> => {
      if (native && input.action === 'status') return { execution_profile: profile, approval_mode: { mode: 'not_applicable' }, grants: [] };
      if (native) throw new Error('native 普通工具不使用目录 grant；审批操作请在 protected 新进程中进行。');
      const requestId = state(ctx)?.requestId;
      if (input.action === 'status') return asObject(await client(ctx).hostCall('permissions.status', {}, { requestId }));
      if (input.action !== 'revoke' || typeof input.grant_id !== 'string' || !input.grant_id.trim()
          || !input.request_scope || typeof input.policy_revision !== 'string') throw new Error('Invalid permission revocation');
      if (!requestId) throw new Error('Permission revocation requires the original BioMind request');
      return asObject(await client(ctx).hostCall('permissions.revoke', { grant_id: input.grant_id,
        request_scope: input.request_scope, policy_revision: input.policy_revision }, { requestId }));
    };
    const refreshApprovalStatus = async (ctx: ExtensionContext) => {
      if (native) { ctx.ui.setStatus('biomind-approval','approval:N/A'); return observePermissions(ctx,{action:'status'}); }
      try {
        const listing=await observePermissions(ctx,{action:'status'});
        const count=Array.isArray(listing.grants)?listing.grants.filter((g:Record<string,unknown>)=>g.ordinary_operations_active).length:0;
        ctx.ui.setStatus('biomind-approval', `approval:${listing.approval_mode?.mode ?? 'manual'}${count ? ` · 目录免确认:${count}` : ''}${listing.paused ? ' · paused' : ''}`);
        return listing;
      } catch { ctx.ui.setStatus('biomind-approval','approval:unknown'); return undefined; }
    };
    pi.events.on('biomind:permissions_request', async (raw: unknown) => {
      const request = asObject(raw);
      try {
        if (!activeContext) throw new Error('No active BioMind session');
        const result = await observePermissions(activeContext, request);
        pi.events.emit('biomind:permissions_result', { id: request.id, status: 'ok', sessionId: sessionId(activeContext), result });
      } catch (error) { pi.events.emit('biomind:permissions_result', { id: request.id, status: 'error', code: 'PERMISSIONS_REQUEST_FAILED', message: String(error) }); }
    });
    const observeJob = async (ctx: ExtensionContext, input: Record<string, unknown>) => {
      const action = String(input.action ?? '');
      if (!['list', 'status', 'logs', 'cancel'].includes(action)) throw new Error('Unknown job action');
      const requestId = state(ctx)?.requestId;
      if (action === 'cancel' && !requestId) throw new Error('Job cancellation requires a BioMind request on the current branch');
      if (action === 'list') return client(ctx).call('session.lifecycle', { action: 'inspect' }, { requestId });
      if (typeof input.run_ref !== 'string' || !input.run_ref.trim()) throw new Error('run_ref is required');
      if (input.after_cursor !== undefined && typeof input.after_cursor !== 'string') throw new Error('after_cursor must be an opaque string');
      const method = action === 'status' ? 'exec.status' : action === 'logs' ? 'exec.read_log' : 'exec.cancel';
      return client(ctx).call(method, { run_ref: input.run_ref, ...(action === 'logs' && input.after_cursor ? { after_cursor: input.after_cursor } : {}),
        ...(action === 'cancel' ? { reason: typeof input.reason === 'string' ? input.reason : 'Explicit user job cancellation' } : {}) }, { requestId });
    };
    pi.registerCommand('biomind-job', {
      description: '不调用模型直接观察作业：list | status <run_ref> | logs <run_ref> [cursor] | cancel <run_ref>',
      getArgumentCompletions: argumentCompletions(['list', 'status ', 'logs ', 'cancel ']),
      async handler(args, ctx) {
        const parts = args.trim().split(/\s+/);
        try {
          if (parts.length > (parts[0] === 'logs' ? 3 : parts[0] === 'list' ? 1 : 2)) throw new Error('Unexpected job command arguments');
          const result = await observeJob(ctx, { action: parts[0], run_ref: parts[1], after_cursor: parts[2] });
          ctx.ui.notify(displayPreview(result), 'info'); pi.events.emit('biomind:job_result', { status: 'ok', sessionId: sessionId(ctx), result });
        } catch (error) { ctx.ui.notify(String(error), 'error'); pi.events.emit('biomind:job_result', { status: 'error', code: 'JOB_REQUEST_FAILED', message: String(error) }); }
      },
    });
    pi.events.on('biomind:job_request', async (raw: unknown) => {
      const request = asObject(raw);
      try {
        if (!activeContext) throw new Error('No active BioMind session');
        const result = await observeJob(activeContext, request);
        pi.events.emit('biomind:job_result', { id: request.id, status: 'ok', sessionId: sessionId(activeContext), result });
      } catch (error) { pi.events.emit('biomind:job_result', { id: request.id, status: 'error', code: 'JOB_REQUEST_FAILED', message: String(error) }); }
    });
    const reply = (value: unknown, extra: Record<string, unknown> = {}) => {
      const projected = projectToolReceipt(value);
      const object = asObject(projected);
      const execution_facts = Object.fromEntries(['run_ref', 'status', 'exit_code', 'log_path', 'log_cursor', 'next_cursor', 'request_scope', 'call_id',
        'returncode', 'terminal', 'log_refs', 'receipt_ref', 'cancel_requested', 'stop_requested', 'task_cancelled', 'approval', 'approval_id',
        'error', 'reason', 'phase', 'protection_phase', 'execution_started', 'count', 'unique_inode_count', 'affected_capability', 'host_boundary',
        'current_project_root', 'requested_workdir', 'instruction', 'runtime_binding', 'runtime_launch', 'binding_ref', 'binding_scope', 'registration_sources', 'healthcheck_status',
        'path', 'path_samples', 'backend', 'errno', 'probe_exit_code', 'permission_status']
        .filter(key => object[key] !== undefined).map(key => [key, object[key]]));
      return { content: [{ type: 'text' as const, text: JSON.stringify(projected) }], details: { ...receiptDetails(object), execution_facts, ...extra } };
    };
    const rejectPending = (reason: string, targetSession?: string) => {
      for (const [id, waiter] of pendingUI) if (!targetSession || waiter.request.sessionId === targetSession) {
        pendingUI.delete(id); waiter.reject(new Error(reason));
      }
    };
    pi.events.on('biomind:ui_response', (raw: unknown) => {
      const response = asObject(raw) as BioMindUIResponse;
      const waiter = pendingUI.get(response.id);
      if (!waiter || response.requestId !== waiter.request.requestId || response.sessionId !== waiter.request.sessionId
          || (response.action_hash !== undefined && response.action_hash !== waiter.request.proposal?.action_hash)) {
        pi.events.emit('biomind:diagnostic', { code: 'STALE_OR_UNKNOWN_UI_RESPONSE', id: response.id }); return;
      }
      pendingUI.delete(response.id); waiter.resolve(response);
    });
    const approvalOptions = (proposal: Record<string, unknown>, fine = false) => {
      if (!Array.isArray(proposal.allowed_decisions)) throw new Error('Authorization did not specify allowed decisions');
      const offered = new Set(proposal.allowed_decisions.filter((value): value is string => typeof value === 'string'));
      // The current permission service explicitly supports installation editing
      // before checking its stored y/n list; other proposal kinds cannot edit.
      if (asObject(proposal.payload).kind === 'installation') offered.add('edit');
      const reading = asObject(proposal.payload).kind === 'file_read';
      const paths = asObject(proposal.payload).kind === 'path_access';
      if (paths) offered.add('select');
      const directories = asObject(proposal.payload).directory_targets as Array<Record<string, unknown>> | undefined;
      const directoryLabel = directories?.length === 1
        ? directories[0].access === 'read' ? '允许本会话读取此目录（范围内免逐次确认）' : '允许本会话在此目录工作（范围内免逐次确认）'
        : '允许本会话使用所列目录（范围内免逐次确认）';
      if (offered.has('directory_session') && !fine) return [
        {choice:'n',label:'拒绝'}, {choice:'directory_session',label:directoryLabel},
        {choice:'adjust',label:'调整范围／仅本次'}, {choice:'cancel',label:'取消'},
      ];
      const options = [
        { choice: 'directory_session', label: directoryLabel },
        { choice: 'n', label: '拒绝' }, { choice: 'y', label: paths ? '批准提案范围（当前任务）' : reading ? '只读授权该明确目标（当前分析请求）' : '明确同意执行此提案' },
        { choice: 'scope', label: reading ? '只读授权提案列出的目录及子目录（当前分析请求）' : '明确授权提案中列出的目录范围内文件工具写入' },
        { choice: 'session', label: '批准提案明确目标（当前持久会话，含以后恢复）' },
        { choice: 'read_only', label: '仅批准读取（当前任务）' },
        { choice: 'read_session', label: '仅批准读取（当前持久会话）' },
        { choice: 'select', label: '逐项选择权限子集与有效期' },
        { choice: 'edit', label: '编辑后重新确认' }, { choice: 'cancel', label: '取消' },
      ].filter(option => option.choice === 'cancel' || offered.has(option.choice));
      return [options.find(option => option.choice === 'n') ?? options.find(option => option.choice === 'cancel')!, ...options.filter(option => option.choice !== 'n')];
    };
    async function ask(ctx: ExtensionContext, kind: 'question' | 'approval', toolCallId: string, question: string,
      signal?: AbortSignal, proposal?: Record<string, unknown>, choices?: string[], fine = false,
      editor?: { prefill?: string }): Promise<BioMindUIResponse> {
      if (kind === 'approval') signal = signal ? AbortSignal.any([signal, AbortSignal.timeout(300_000)]) : AbortSignal.timeout(300_000);
      const approval = kind === 'approval' ? approvalOptions(proposal ?? {}, fine) : [];
      if (kind === 'approval') choices = approval.map(option => option.label);
      const request: BioMindUIRequest = { id: randomUUID(), requestId: current(ctx).requestId, sessionId: sessionId(ctx), kind, question, toolCallId, proposal, choices,
        ...(editor ? { method: 'editor' as const, ...(editor.prefill !== undefined ? { prefill: editor.prefill } : {}) } : {}) };
      signal?.throwIfAborted();
      if (ctx.hasUI) {
        if (kind === 'question') {
          const answer = editor ? await ctx.ui.editor(question, editor.prefill)
            : choices?.length ? await ctx.ui.select(question, choices, { signal }) : await ctx.ui.input(question, undefined, { signal });
          signal?.throwIfAborted();
          return { ...request, answer, cancelled: answer === undefined };
        }
        // A blank Enter chooses the first (deny) item, never approval.
        const answer = await selectApprovalProposal(ctx.ui, proposal ?? {}, choices!, signal);
        const choice = (approval.find(option => option.label === answer)?.choice ?? 'cancel') as NonNullable<BioMindUIResponse['choice']>;
        if (choice === 'adjust') return ask(ctx,kind,toolCallId,question,signal,proposal,undefined,true);
        let replacement: Record<string, unknown> | undefined;
        if (choice === 'edit') {
          const edited = await ctx.ui.input('输入修改后的命令；随后需要重新确认', undefined, { signal });
          if (!edited?.trim()) return { ...request, choice: 'cancel', cancelled: true };
          replacement = { cmd: edited };
        }
        if (choice === 'select') {
          const selections: NonNullable<BioMindUIResponse['selections']> = [];
          const targets=asObject(proposal?.payload).targets as Array<Record<string, any>>;
          for (const [index,target] of targets.entries()) {
            const options=[{label:'跳过此项'} as {label:string;access?:'read'|'read_write';lifetime?:'request'|'session'}];
            for (const lifetime of ['request','session'] as const) for (const access of ['read','read_write'] as const) {
              if (access==='read_write' && target.access!=='read_write' || access==='read' && !target.existed_at_proposal) continue;
              options.push({label:`${access==='read'?'读取':'读写'} · ${lifetime==='request'?'当前任务':'当前持久会话'}`,access,lifetime});
            }
            const answer=await ctx.ui.select(`选择：${target.root}`,options.map(o=>o.label),{signal});
            if (answer===undefined) return {...request,choice:'cancel',cancelled:true};
            const option=options.find(o=>o.label===answer);
            if (option?.access && option.lifetime) selections.push({index,access:option.access,lifetime:option.lifetime});
          }
          return selections.length ? {...request,choice:'y',selections} : {...request,choice:'cancel',cancelled:true};
        }
        return { ...request, choice, replacement, cancelled: choice === 'cancel' };
      }
      return new Promise((resolve, reject) => {
        const abort = () => { pendingUI.delete(request.id); reject(signal?.reason ?? new Error('User interaction aborted; no answer or approval was recorded')); };
        pendingUI.set(request.id, { request,
          resolve: response => { signal?.removeEventListener('abort', abort); resolve(response); },
          reject: error => { signal?.removeEventListener('abort', abort); reject(error); } });
        signal?.addEventListener('abort', abort, { once: true });
        if (signal?.aborted) { abort(); return; }
        pi.events.emit('biomind:ui_request', request);
        // No timers or model calls are used while waiting for the matching host response.
      });
    }
    const invoke = async (ctx: ExtensionContext, method: string, params: Record<string, unknown>, signal?: AbortSignal) => {
      const request = current(ctx); signal?.throwIfAborted();
      const value = await client(ctx).call(method, params, { signal, requestId: request.requestId });
      signal?.throwIfAborted();
      if (state(ctx)?.requestId !== request.requestId) throw new Error('Late result belongs to an inactive request');
      return value;
    };
    async function withApproval(ctx: ExtensionContext, method: string, params: Record<string, unknown>, toolCallId: string, signal?: AbortSignal,
      dispatch?: (method: string, params: Record<string, unknown>) => Promise<unknown>) {
      const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
      const checkOwner = () => {
        signal?.throwIfAborted();
        if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_MUTATION_OWNER');
      };
      // This callback is constructed only inside the extension. Preserve the
      // privileged host transport on both initial calls and approval retries.
      const perform = dispatch ?? ((nextMethod, nextParams) => invoke(ctx, nextMethod, nextParams, signal));
      let value = await perform(method, params);
      checkOwner();
      while (asObject(value).status === 'waiting_approval') {
        checkOwner();
        const proposal = asObject(asObject(value).approval);
        if (!proposal.request_id || !proposal.action_hash || !proposal.request_scope) throw new Error('Incomplete authorization object');
        // Consult the original live ledger immediately before presenting a wait.
        // Another directory decision may already cover this action's own hash.
        const live = asObject(await client(ctx).hostCall('permissions.status', {approval_id:proposal.request_id}, {signal,requestId:ownerRequest}));
        checkOwner();
        if (live.status === 'allowed') { value = await perform(method,params); checkOwner(); continue; }
        if (live.status === 'cancelled' || live.status === 'denied') return live;
        let response: BioMindUIResponse;
        try { response = await ask(ctx, 'approval', toolCallId, formatApprovalProposal(proposal), signal, proposal); }
        catch (error) {
          try { await client(ctx).hostCall('approval.decide', { request_id: proposal.request_id, action_hash: proposal.action_hash,
            request_scope: proposal.request_scope, choice: 'cancel' }, { requestId: ownerRequest }); }
          finally { await pause(ctx, error instanceof Error && error.name === 'TimeoutError' ? 'approval_timeout' : 'approval_interrupted'); }
          throw error;
        }
        checkOwner();
        if (response.choice === 'adjust') response = await ask(ctx,'approval',toolCallId,formatApprovalProposal(proposal),signal,proposal,undefined,true);
        const choice = response.cancelled ? 'cancel' : response.choice;
        if (!choice || !approvalOptions(proposal,true).some(option => option.choice === choice)) { await pause(ctx, 'invalid_approval_response'); throw new Error('Approval requires an explicit offered decision'); }
        value = await client(ctx).hostCall('approval.decide', { request_id: proposal.request_id, action_hash: proposal.action_hash,
          request_scope: proposal.request_scope, choice, ...(response.selections ? { selections:response.selections } : {}), ...(response.replacement ? { replacement: response.replacement } : {}) },
          { signal, requestId: ownerRequest });
        checkOwner();
        if (choice === 'n' || choice === 'cancel') { await pause(ctx, choice === 'n' ? 'approval_denied' : 'approval_cancelled'); return value; }
        if (choice === 'edit') {
          if (asObject(value).status !== 'waiting_approval') return value;
          // Keep the original call identity. Python executes the newly approved
          // object's payload; replaying model/edited arguments must not replace it.
          continue;
        }
        await refreshApprovalStatus(ctx);
        value = await perform(method === 'install.propose' ? 'install.execute' : method, params);
        checkOwner();
      }
      return value;
    }
    function register(name: string, description: string, parameters: TSchema,
      execute: (id: string, args: Record<string, any>, signal: AbortSignal | undefined, ctx: ExtensionContext) => Promise<unknown>,
      prepareArguments?: ToolDefinition['prepareArguments']) {
      pi.registerTool({ name, label: name, description, parameters, prepareArguments, executionMode: 'sequential',
        ...toolRenderers(name),
        async execute(id, args, signal, _update, ctx) { return reply(await execute(id, asObject(args), signal, ctx)); },
      } as ToolDefinition);
    }
    if (native) pi.registerTool(runtimeBash(async (input, signal, ctx) => {
      const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
      const result = await client(ctx).hostCall('environments.launch', input, { signal, requestId: ownerRequest });
      signal?.throwIfAborted();
      if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_ENVIRONMENT_OWNER');
      return result as RuntimeBashLaunch;
    }, undefined, receipt => {
      pi.appendEntry('biomind.bash_execution', receipt);
      pi.events.emit('biomind:bash_execution', receipt);
    }, async ctx => {
      const admission = asObject(await client(ctx).hostCall('request.dispatch', { tool: 'bash' }, { requestId: current(ctx).requestId }));
      if (admission.status !== 'allowed') throw new Error('SCIENTIFIC_STAGE_BLOCKED: ' + JSON.stringify(admission));
    }));
    // Both metadata and Pi content reads use the same live Pi resource registry.
    // Trusted directories are host-derived and never part of a model schema.
    async function authorizeRead(ctx: ExtensionContext, absolutePath: string, id: string, signal?: AbortSignal,
      method: 'files.read_authorize' | 'files.list' = 'files.read_authorize') {
      const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
      const checkOwner = () => {
        signal?.throwIfAborted();
        if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_READ_OWNER');
      };
      checkOwner();
      const skillDirs = pi.getCommands().filter(command => command.source === 'skill')
        .map(command => path.dirname(command.sourceInfo.path));
      const trustedSkillDirs: string[] = [];
      let target = absolutePath;
      for (const directory of skillDirs) {
        const canonical = await realpath(directory).catch(() => undefined);
        if (!canonical) continue;
        trustedSkillDirs.push(canonical);
        if (absolutePath.startsWith(directory + path.sep)) {
          const resolved = await realpath(absolutePath);
          if (!resolved.startsWith(canonical + path.sep)) throw new Error('SKILL_RESOURCE_OUTSIDE_ROOT');
          target = resolved;
        }
      }
      checkOwner();
      const receipt = asObject(await withApproval(ctx, method,
        { path: target, call_id: id, trusted_skill_dirs: trustedSkillDirs }, id, signal,
        async (method, params) => {
          checkOwner();
          const result = await client(ctx).hostCall(method, params, { signal, requestId: ownerRequest });
          checkOwner(); return result;
        }));
      checkOwner(); return { ...receipt, requested_path: absolutePath };
    }
    if (!native) for (const kind of DISCOVERY_TOOLS) {
      pi.registerTool({ ...discoveryDefinition(kind, process.cwd()), executionMode: 'sequential',
        async execute(id, args, signal, update, ctx) {
          const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
          const result = await nativeDiscovery(kind, ctx.cwd, {
            list: async (filename, activeSignal) => await authorizeRead(ctx, filename, id, activeSignal, 'files.list') as unknown as DirectorySnapshot,
            authorize: (filename, activeSignal) => authorizeRead(ctx, filename, id, activeSignal),
          }).execute(id, args, signal, update, ctx);
          signal?.throwIfAborted();
          if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_READ_OWNER');
          return result;
        },
      } as ToolDefinition);
    }
    // Reuse Pi's schema, renderer, paging, image handling and errors. Only file
    // access is adapted to BioMind's existing domain boundary.
    if (!native) pi.registerTool({ ...createReadToolDefinition(process.cwd()), executionMode: 'sequential',
      async execute(id, args, signal, update, ctx) {
        const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
        const result = await nativeRead(ctx.cwd, absolutePath => authorizeRead(ctx, absolutePath, id, signal))
          .execute(id, args, signal, update, ctx);
        signal?.throwIfAborted();
        if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_READ_OWNER');
        if (result.details?.sha256 && result.details?.write_path) {
          readVersions.set(`${ownerSession}:${ownerRequest}:${result.details.write_path}`, result.details.sha256);
        }
        return result;
      },
    });
    register('file_stat', native ? '按当前账号权限核对文件路径与元数据。' : '核对真实文件路径与元数据；项目外输入建议一次目录会话只读授权；最终范围由宿主明确批准。', OBJECT({ path: nonempty() }),
      async (id, args, signal, ctx) => {
        if (!native) return authorizeRead(ctx, args.path.startsWith('~') ? args.path : path.resolve(ctx.cwd, args.path), id, signal);
        signal?.throwIfAborted();
        const expanded = args.path === '~' ? homedir() : args.path.startsWith('~/') ? path.join(homedir(), args.path.slice(2)) : args.path;
        const filename = path.resolve(ctx.cwd, expanded);
        const info = await stat(filename); signal?.throwIfAborted();
        return { path: filename, realpath: await realpath(filename), size: info.size, is_file: info.isFile(), is_directory: info.isDirectory(), mtime_ms: info.mtimeMs };
      });
    if (!native) for (const kind of ['write', 'edit'] as const) {
      const definition = kind === 'write' ? createWriteToolDefinition(process.cwd()) : createEditToolDefinition(process.cwd());
      pi.registerTool({ ...definition, executionMode: 'sequential',
        async execute(id, args, signal, update, ctx) {
          const requestId = current(ctx).requestId;
          const ownerSession = sessionId(ctx);
          const checkOwner = () => {
            signal?.throwIfAborted();
            if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== requestId) throw new Error('STALE_MUTATION_OWNER');
          };
          const key = (absolutePath: string) => `${ownerSession}:${requestId}:${absolutePath}`;
          const hostMutation = async (method: string, input: Record<string, unknown>) => {
            checkOwner();
            const result = await client(ctx).hostCall(method, input, { signal, requestId });
            checkOwner(); return asObject(result);
          };
          return nativeMutation(kind, ctx.cwd, {
            replay: input => hostMutation('files.mutation_replay', input),
            snapshot: async absolutePath => await withApproval(ctx,'files.mutation_snapshot', { path: absolutePath,call_id:id },id,signal,hostMutation) as any,
            commit: async input => { checkOwner(); return asObject(await withApproval(ctx, 'files.write', input, id, signal)); },
            readVersion: absolutePath => { checkOwner(); return readVersions.get(key(absolutePath)); },
            rememberVersion: (absolutePath, hash) => { checkOwner(); readVersions.set(key(absolutePath), hash); },
          }).execute(id, args, signal, update, ctx);
        },
      } as ToolDefinition);
    }
    const validateEditorQuestion = (args: Record<string, any>) => {
      const editor = args.method === 'editor';
      if ((args.method !== undefined && !editor) || (editor && args.choices !== undefined)
        || (args.prefill !== undefined && (!editor || typeof args.prefill !== 'string'
          || Buffer.byteLength(args.prefill, 'utf8') > MAX_QUESTION_PREFILL_BYTES))) {
        throw new Error('INVALID_QUESTION_EDITOR_INPUT');
      }
      return editor;
    };
    register('ask_user', '询问关键选择并等待真实用户回答；method=editor 可请求多行编辑回答，不猜测答案。', OBJECT({
      question: nonempty(), choices: Type.Optional(Type.Array(nonempty())),
      method: Type.Optional(Type.Literal('editor')),
      prefill: Type.Optional(Type.String({ maxLength: MAX_QUESTION_PREFILL_BYTES, description: '仅 method=editor 可用；UTF-8 最多 64 KiB。' })),
    }),
      async (id, args, signal, ctx) => {
        const editor = validateEditorQuestion(args);
        const response = await ask(ctx, 'question', id, args.question, signal, undefined, args.choices, false,
          editor ? { prefill: args.prefill } : undefined);
        if (response.cancelled || response.answer === undefined) { await pause(ctx, 'question_cancelled'); throw new Error('User cancelled the question; no answer available'); }
        return { question_id: response.id, request_id: response.requestId, question: args.question, answer: response.answer };
      }, args => {
        // Pi normally coerces optional strings/nulls before tool_call/execute.
        // Reject invalid editor input while its original JSON types are intact.
        validateEditorQuestion(asObject(args));
        return args as Record<string, unknown>;
      });
    register('update_plan', '可选的当前分支工作笔记，不是执行许可或必须产物合同。', OBJECT({ steps: Type.Array(OBJECT({ step: nonempty(), status: Type.Union([Type.Literal('pending'), Type.Literal('in_progress'), Type.Literal('completed')]) })) }),
      async (_id, args, _signal, ctx) => { const value = current(ctx); publish(ctx, { ...value, plan: args.steps }); return { plan: args.steps, required_for_execution: false }; });
    const runtimeRequirements = OBJECT({ env: optionalString(), prefix: optionalString(), backend: optionalString(), host: optionalString(),
      packages: Type.Optional(Type.Array(Type.String())), tools: Type.Optional(Type.Array(Type.String())), capabilities: Type.Optional(Type.Array(Type.String())),
      versions: Type.Optional(Type.Record(Type.String(), Type.String())) });
    register('select_environment', '需要外部生信软件时优先调用，先查登记再定位；可一次查询多个 tools。返回 AVAILABLE/UNKNOWN、冲突/失效/注册源错误及真实来源和 binding_ref；未命中不表示服务器未安装。已有适用登记不重复探测。mode=locate 仅在明确 prefix 或已知环境清单做有界定位，单次15秒、request累计60秒，同查询复用结果。命中后 run_command 使用绑定；环境相关 bash 必须显式传 binding_ref，不能依赖全局激活。',
      OBJECT({ requirements: Type.Optional(runtimeRequirements), skill_id: optionalString(), env_name: optionalString(), runtime_scope: optionalString(), mode: Type.Optional(Type.Union([Type.Literal('select'), Type.Literal('locate')])) }),
      async (_id, args, signal, ctx) => {
        if (Object.keys(args).some(key => !['requirements', 'skill_id', 'env_name', 'runtime_scope', 'mode'].includes(key))) throw new Error('ENVIRONMENT_QUERY_FIELDS_INVALID');
        if (args.mode === 'locate' && (args.skill_id || args.runtime_scope)) throw new Error('ENVIRONMENT_LOCATE_USES_REQUIREMENTS_OR_ENV_NAME_ONLY');
        const ownerSession = sessionId(ctx), ownerRequest = current(ctx).requestId;
        const loaded_skills = pi.getCommands().filter(command => command.source === 'skill')
          .map(command => ({ name: command.name.replace(/^skill:/, ''), file_path: command.sourceInfo.path }));
        const { mode, ...selection } = args;
        const result = mode === 'locate'
          ? await client(ctx).call('environments.locate', { requirements: { ...(selection.requirements ?? {}), ...(selection.env_name ? { env: selection.env_name } : {}) } }, { signal, requestId: ownerRequest })
          : await client(ctx).hostCall('environments.select', { ...selection, loaded_skills }, { signal, requestId: ownerRequest });
        signal?.throwIfAborted();
        if (sessionId(ctx) !== ownerSession || current(ctx).requestId !== ownerRequest) throw new Error('STALE_ENVIRONMENT_OWNER');
        return result;
      });
    register('register_environment', '先用 probe_command 验证确切入口再登记已有软件；verification_runs 关联当前请求真实探针回执及辨识输出。写入与查询相同权威，幂等保留多环境。安装工具已自动登记则不重复调用。本工具不执行安装或探针；无 verification_runs 时仅保存声明，source_note/evidence_refs 不能升级为验证事实。',
      OBJECT({ env_name: nonempty(), prefix: nonempty(), packages: Type.Optional(Type.Array(nonempty())), tools: Type.Optional(Type.Array(nonempty())),
        capabilities: Type.Optional(Type.Array(nonempty())), entries: Type.Optional(Type.Record(Type.String(), Type.String())),
        runtime_env: Type.Optional(Type.Record(Type.String(), Type.String())), env_runner: optionalString(), runner_argv: Type.Optional(Type.Array(Type.String())),
        versions: Type.Optional(Type.Record(Type.String(), Type.String())),
        verification_runs: Type.Optional(Type.Array(OBJECT({ tool: nonempty(), run_ref: nonempty(), expected_output: nonempty(),
          exit_codes: Type.Optional(Type.Array(Type.Integer({ minimum: 0, maximum: 255 }), { minItems: 1, maxItems: 8 })) }), { minItems: 1, maxItems: 16 })),
        source_note: optionalString(), evidence_refs: Type.Optional(Type.Array(Type.String())) }),
      async (id, args, signal, ctx) => {
        const params = { call_id: id, ...args };
        return native ? invoke(ctx, 'environments.register', params, signal) : withApproval(ctx, 'environments.register', params, id, signal);
      });
    const mapping: Array<[string, string, string, TSchema]> = [
      ['inspect_run', 'exec.status', '查询已有 run_ref 的真实状态；失败作业不等于查询失败。按需用 record=command 读取已记录命令/cwd/环境引用，record=script 和 script_index 读取实际冻结脚本；offset/max_chars 分页，遵守当前请求范围。不触发检查或重跑。', OBJECT({ run_ref: nonempty(), record: Type.Optional(Type.Union([Type.Literal('command'), Type.Literal('script')])), script_index: Type.Optional(Type.Integer({ minimum: 0 })), offset: Type.Optional(Type.Integer({ minimum: 0 })), max_chars: Type.Optional(Type.Integer({ minimum: 1, maximum: 20000 })) })],
      ['wait_run', 'exec.wait', '等待已有作业的有限状态变化；界面心跳不需要模型重复调用。', OBJECT({ run_ref: nonempty(), after_cursor: optionalString(), max_wait_ms: Type.Optional(Type.Integer({ minimum: 0, maximum: 30000 })) })],
      ['read_run_log', 'exec.read_log', '分页读取真实日志；游标是服务返回的原始不透明字符串，完整日志留在原文件。', OBJECT({ run_ref: nonempty(), after_cursor: optionalString() })],
      ['cancel_run', 'exec.cancel', '明确取消指定科学作业；与中断当前对话不同。', OBJECT({ run_ref: nonempty(), reason: nonempty() })],
      ['memory_search', 'memory.search', '按当前历史读取开关和项目 scope 查询长期记忆。', OBJECT({ query: nonempty(), source: Type.Optional(Type.Union([Type.Literal('wiki'), Type.Literal('projects'), Type.Literal('archive'), Type.Literal('current')])), scope: optionalString() })],
      ['memory_read', 'memory.read', '读取可用来源；关闭历史时不能读取旧项目历史。', OBJECT({ source_ref: nonempty(), scope: optionalString(), historical: Type.Optional(Type.Boolean()), offset: Type.Optional(Type.Integer({ minimum: 0 })), max_chars: Type.Optional(Type.Integer({ minimum: 1, maximum: 20000 })) })],
    ];
    for (const [name, method, description, schema] of mapping) register(name, description, schema, async (_id, args, signal, ctx) => invoke(ctx, method, args, signal));
    const pathSchema=OBJECT({paths:Type.Array(OBJECT({path:nonempty(),access:Type.Union([Type.Literal('read'),Type.Literal('read_write')]),target_kind:Type.Union([Type.Literal('file'),Type.Literal('directory')]),recursive:Type.Boolean()}),{minItems:1,maxItems:16}),purpose:nonempty(),lifetime:Type.Optional(Type.Union([Type.Literal('request'),Type.Literal('session')]))});
    if (!native) register('request_path_access','申请数据文件/目录的读取或读写范围。宿主选择最终子集与有效期；不改变项目、输出要求或自动审批模式。',pathSchema,
      async(id,args,signal,ctx)=>withApproval(ctx,'permissions.propose',{...args,call_id:id},id,signal));
    const commandSchema = OBJECT({ cmd: optionalString(), argv: Type.Optional(Type.Array(Type.String(), { minItems: 1 })), workdir: nonempty(), env_name: optionalString(), binding_ref: optionalString(), runtime_scope: optionalString(), requirements: Type.Optional(runtimeRequirements), timeout_ms: Type.Optional(Type.Integer({ minimum: 1 })), description: optionalString(),
      write_paths: Type.Optional(Type.Array(nonempty(),{description:native ? '可选写入意图记录，不授予权限或限制目录视图。' : '本次动作使用的写入文件/目录，相对 workdir；外部范围仍需宿主批准，不改变任务输出要求。'})),
      read_paths: Type.Optional(Type.Array(nonempty(), { description: native ? '可选输入来源记录，不构造沙箱目录视图。' : '只读输入文件/小目录，相对项目；URI 仅对 :: 前文件授权。目录每次最多 256 项、2 秒、总 512 挂载，private 排除、链接拒绝；启动后新增文件不可见。' })) });
    const probeSchema = OBJECT({ ...asObject(asObject(commandSchema).properties), environment_diagnostic: Type.Optional(Type.Boolean({ description: '仅环境故障定点诊断设 true：单次 15 秒、当前 request 累计 30 秒。用户明确检查及科学输入 QC 不设置此标志。' })) });
    register('probe_command', (native ? '有时限的本地程序核查；保留观察取消，不提供强制只读或网络隔离。' : '用户要求或实际错误需要的短程序核查：项目和输入只读、隔离网络、仅临时区可写，仍使用宿主授权和原作业观察取消。') + '默认15秒/64 KiB输出；用户明确检查或QC可用timeout_ms，不设置environment_diagnostic。仅环境故障定点诊断设environment_diagnostic=true，单次15秒、每request累计30秒；正式计算和安装不受诊断短预算影响。', probeSchema,
      async (id, args, signal, ctx) => withApproval(ctx, 'exec.probe', { call_id: id, arguments: args }, id, signal,
        (method, params) => client(ctx).hostCall(method, params, { signal, requestId: current(ctx).requestId })));
    register('run_command', (native ? '启动本地托管命令并返回 run_ref；保留持久日志、观察、取消与重连对账，无 BioMind 目录或命令审批。' : '通过领域执行服务启动受控命令并返回 run_ref，保留持久日志、观察和取消。') + '支持 shell 或 argv，计划可选。显式 binding_ref/env_name 使用准确目标；否则复用本请求和 scope 的唯一适用绑定，多绑定须明确指定。无绑定且无运行时要求时保持宿主执行。', commandSchema,
      async (id, args, signal, ctx) => withApproval(ctx, 'exec.start', { call_id: id, arguments: args }, id, signal));
    register('install_tool', '提出安装方案并等待原意图确认。验证必须检查目标 prefix 内的准确工具；结构化 executable/python_import/r_package 检查不能由任意 echo 成功替代。安装完成但登记失败时只恢复登记。', OBJECT({ cmd: nonempty(), env_name: nonempty(), description: nonempty(), packages: Type.Optional(Type.Array(Type.String())), prefix: nonempty(),
      verification_checks: Type.Array(OBJECT({ kind: Type.Union(['executable','python_import','r_package'].map(v => Type.Literal(v))), tool: nonempty(), entry: optionalString(), args: Type.Optional(Type.Array(Type.String())), modules: Type.Optional(Type.Array(nonempty())), packages: Type.Optional(Type.Array(nonempty())) }), { minItems: 1 }),
      entries: Type.Optional(Type.Record(Type.String(), Type.String())), runtime_env: Type.Optional(Type.Record(Type.String(), Type.String())), env_runner: optionalString(), runner_argv: Type.Optional(Type.Array(Type.String())) }),
      async (id, args, signal, ctx) => withApproval(ctx, 'install.propose', { call_id: id, arguments: args }, id, signal));
    register('memory_write', '按原策略写 Wiki 或显式偏好；不把偏好当安装授权。', OBJECT({ target: Type.Optional(Type.Union([Type.Literal('wiki'), Type.Literal('preference')])), operation: nonempty(), path: optionalString(), content: Type.String(), expected_hash: optionalString(), scope: optionalString(), source_refs: Type.Optional(Type.Array(Type.String())) }),
      async (id, args, signal, ctx) => withApproval(ctx, 'memory.write', { call_id: id, ...args }, id, signal));
    register('maintenance_submit', '仅在已启用的记忆维护窗口提交当前证据支持的patch；空operations表示无可保存事实。',
      OBJECT({ operations: Type.Array(Type.Record(Type.String(), Type.Unknown())), summary: optionalString() }),
      async (_id, args, signal, ctx) => {
        const value = current(ctx); const candidate = value.maintenance;
        if (!candidate?.outboxId || candidate.applied) throw new Error('No armed maintenance candidate');
        const result = await client(ctx).hostCall('memory.maintenance.apply', { outbox_id: candidate.outboxId, patch: args }, { signal, requestId: value.requestId });
        signal?.throwIfAborted(); if (state(ctx)?.requestId !== value.requestId) throw new Error('Maintenance result belongs to an inactive request');
        publish(ctx, { ...value, maintenance: { ...candidate, applied: true } }); return result;
      });

    pi.on('session_start', async (event, ctx) => {
      writerReady=false;
      try{await claimWriter(ctx,event.reason,event.previousSessionFile);writerReady=true;}
      catch(error){pi.events.emit('biomind:diagnostic',{code:'SESSION_WRITER_BINDING_FAILED'});setActiveTools([]);ctx.abort();ctx.ui.notify(error instanceof SessionWriterError?error.code:'SESSION_WRITER_CLAIM_FAILED','error');ctx.shutdown();return;}

      activeContext = ctx; restoreToolSelection(ctx); snapshotKnown = false; snapshotFailed = false; instructionsFailed = false;
      const id = sessionId(ctx);
      const initialization = ++backendEpoch;
      notifiedFailures.delete(id);
      updateBackend(ctx, { state: 'starting', stage: 'startup' });
      // Only an explicit reload retries a failed initial handshake. Accepted
      // requests and scientific jobs are never recreated or replayed here.
      const previous = clients.get(id);
      if (event.reason === 'reload' && previous?.startupFailed) {
        unobserveBackend(id); clients.delete(id);
        await previous.close({ disconnectOnly: true });
      }
      if (event.reason === 'resume' || event.reason === 'startup' &&
        (process.env.BIOMIND_PI_SESSION_EXISTING === '1' || ctx.sessionManager.getEntries().some(entry =>
          entry.type === 'message' || entry.type === 'custom' && entry.customType === REQUEST))) existingSessions.add(sessionId(ctx));
      {
        if (ctx.hasUI) ctx.ui.setStatus('biomind-execution', '正在初始化运行环境…');
        try {
          const connection = client(ctx);
          const live = asObject(await connection.hostCall('request.status', {}));
          if (initialization !== backendEpoch || id !== sessionId(ctx)) return;
          if (connection.lifecycle && connection.lifecycle.state !== 'ready') {
            // A status response and a pipe failure can arrive in the same turn.
            // The transport refuses this read-only call and waits for close to
            // collect the real failure; do not overwrite unavailable with ready.
            await connection.hostCall('request.status', {});
            throw connection.lifecycle.error ?? new Error('BACKEND_NOT_READY');
          }
          if (live.execution_profile && live.execution_profile !== profile) throw new Error('EXECUTION_PROFILE_RESTART_REQUIRED');
          readySessions.add(sessionId(ctx));
          updateBackend(ctx, { state: 'ready', stage: 'startup' });
        } catch (error) {
          if (initialization !== backendEpoch || id !== sessionId(ctx)) return;
          pi.events.emit('biomind:diagnostic', { code: 'EXECUTION_PROFILE_BINDING_FAILED',
            reason: String(error).match(/EXECUTION_PROFILE_[A-Z_]+/)?.[0], message: String(error) });
          setActiveTools([]); ctx.abort(); ctx.ui.setStatus('biomind-execution', `execution:unbound · requested:${profile}`);
          updateBackend(ctx, { state: 'failed', stage: 'startup', message: String(error) }); return;
        }
      }
      const epoch = ++welcomeEpoch;
      publishToolPolicy();
      setActiveTools(MAIN_TOOLS);
      ctx.ui.setStatus('biomind-execution', `execution:${profile}${process.env.BIOMIND_PI_BUILD_ID ? ' · build:'+process.env.BIOMIND_PI_BUILD_ID : ''}`);
      ctx.ui.setStatus('biomind-approval', native ? 'approval:N/A' : state(ctx) ? 'approval:unknown' : 'approval:manual');
      if (native && ctx.hasUI) ctx.ui.notify('execution:native · 普通工具使用当前账号权限，不提供额外目录沙箱。', 'info');
      if (ctx.mode !== 'tui' || !ctx.hasUI) return;
      if (event.reason === 'reload') { savedInstructions.clear(); savedGlobalInstructions.clear(); }
      // Consume Pi's loaded metadata once per lifecycle, not on every render.
      try {
        welcomeProject = inspectNativeWelcomeInstructions(ctx.getSystemPrompt(), ctx.cwd);
      } catch { welcomeProject = undefined; instructionsFailed = true; }
      welcomePreference = { status: 'checking' };
      const metadata = await inspectWelcomePreference(ctx.cwd, process.env);
      if (epoch !== welcomeEpoch) return;
      welcomePreference = metadata;
      ctx.ui.setStatus('biomind-welcome', undefined);
    });
    pi.registerCommand('biomind-task', {
      description: '当前任务：continue [指令] | revise <修订> | new <新任务>；继续不会恢复旧批准',
      getArgumentCompletions: argumentCompletions(['continue ', 'revise ', 'new ']),
      async handler(args, ctx) {
        const match = /^(continue|revise|new)(?:\s+([\s\S]*))?$/.exec(args.trim());
        if (!match || (match[1] !== 'continue' && !match[2]?.trim())) { ctx.ui.notify('用法：/biomind-task continue | revise <修订> | new <任务>', 'warning'); return; }
        if (!ctx.isIdle()) { ctx.ui.notify('先暂停当前模型推进；已有科学作业通过 /biomind-job 观察或取消。', 'warning'); return; }
        pi.sendUserMessage(match[1] === 'new' ? '[新任务] ' + match[2] : match[1] === 'continue' ? '继续当前任务。' + (match[2] ?? '') : match[2]!);
      },
    });
    pi.registerCommand('biomind-approval', {
      description: '审批与数据范围：status | paths | add <提案JSON> | revoke <grant_id> | manual | auto [--scope session]',
      getArgumentCompletions: argumentCompletions(['status', 'paths', 'add ', 'revoke ', 'manual', 'auto', 'auto --scope session']),
      async handler(args, ctx) {
        const input = args.trim() || 'status', value = state(ctx);
        if (native) { ctx.ui.notify('execution:native · auto/manual 不适用于普通工具。需要保护时在新进程使用 biomind --execution-profile protected；恢复其它模式会话需显式迁移。', 'info'); return; }
        const mode=input.split(/\s+/)[0]!;
        if (!ctx.isIdle()) { ctx.ui.notify('等待本轮结束或暂停后再更改审批模式。', 'warning'); return; }
        try {
          if (['status','paths'].includes(input)) { const status=await refreshApprovalStatus(ctx); if (!status) throw new Error('无法核验权限状态'); ctx.ui.notify(formatPermissionStatus(status),'info'); return; }
          if (!value) throw new Error('范围或模式修改需先提交任务；查看状态无需开始任务。');
          if (mode==='add') {
            const proposal=JSON.parse(input.slice(3).trim());
            if (value.status==='paused') throw new Error('任务已暂停，请先显式恢复。');
            publish(ctx,{...value,status:'active'});
            try {
              const result=await withApproval(ctx,'permissions.propose',{...proposal,call_id:'host-path-'+randomUUID()},'host-path');
              ctx.ui.notify(JSON.stringify(result),'info');
            } finally {
              if (state(ctx)?.requestId===value.requestId && state(ctx)?.status==='active') publish(ctx,value);
              await refreshApprovalStatus(ctx);
            }
            return;
          }
          if (mode==='revoke') {
            const listing=await observePermissions(ctx,{action:'status'}), grantId=input.slice(6).trim();
            await observePermissions(ctx,{action:'revoke',grant_id:grantId,request_scope:listing.request_scope,policy_revision:listing.policy_revision});
            ctx.ui.notify('已撤销此 grant 来源；其它覆盖来源及运行中作业见下方。\n'+formatPermissionStatus((await refreshApprovalStatus(ctx)) ?? {}),'info');return;
          }
          const parsed=/^(manual|auto)(?: --scope (request|session))?$/.exec(input);
          if (!parsed) throw new Error('用法：/biomind-approval status | paths | add <JSON> | revoke <grant_id> | manual | auto [--scope session]');
          const lifetime=parsed[2] ?? 'request';
          const live = asObject(await client(ctx).hostCall('request.status', {}, { requestId: value.requestId }));
          if (live.request_id && live.request_id !== value.requestId) throw new Error('BRANCH_REQUEST_MISMATCH');
          const paused = !!live.paused || value.status === 'paused';
          if (paused && !ctx.hasUI) throw new Error('REQUEST_PAUSED: host must explicitly resume before changing mode');
          if (mode === 'auto' && ctx.hasUI) {
            const action = (paused ? '恢复当前任务并开启' : '开启')+(lifetime==='session'?'当前持久会话':'当前任务')+'自动审批';
            const choice = await ctx.ui.select((paused ? '任务已暂停：此选择将明确恢复任务，但不会立即推进模型或启动作业。此前拒绝/取消的动作仍须新的人工决定。\n' : '') +
              '自动审批消费当前项目与已批准的数据范围，保持网络隔离。新增范围、升级权限、安装及策略修改仍需人工。拒绝/取消将暂停并关闭 auto。'+(lifetime==='session'?'当前会话包含恢复和后续业务，不含 /new 或 fork。':'新业务使本模式失效。'), ['取消', action]);
            if (choice !== action) return;
            if (paused) {
              await client(ctx).hostCall('request.resume', {}, { requestId: value.requestId });
              publish(ctx, { ...value, status: 'settled', pauseReason: undefined });
            }
          }
          const result = await client(ctx).hostCall('permissions.mode', { mode,lifetime }, { requestId: value.requestId });
          await refreshApprovalStatus(ctx);
          ctx.ui.notify(JSON.stringify(result), 'info');
        } catch (error) { ctx.ui.notify(String(error), 'error'); }
      },
    });
    pi.on('input', async (event, ctx) => {
      if(!writerReady){ctx.ui.notify('SESSION_WRITER_BINDING_FAILED','error');return {action:'handled'};}
      if (!readySessions.has(sessionId(ctx))) {
        pi.events.emit('biomind:diagnostic', { code: 'BACKEND_NOT_READY', sessionId: sessionId(ctx),
          state: backendStates.get(sessionId(ctx))?.state ?? 'starting' });
        return { action: 'handled' };
      }
      if (!ctx.isIdle()) {
        if (/^(?:\[新任务\]|新任务[：:])/.test(event.text.trim())) { ctx.ui.notify('当前模型运行中；请结束后明确开始新任务。', 'warning'); return { action: 'handled' }; }
        if (event.streamingBehavior) inputQueue.push({ text: event.text, kind: event.streamingBehavior });
        return { action: 'continue' };
      }
      try {
        let value = state(ctx);
        const newTask = /^(?:\[新任务\]|新任务[：:])/i.test(event.text.trim());
        const resume = /^(?:继续(?:当前任务)?[。.!！\s]*|resume\b|continue\b)/i.test(event.text.trim());
        const pureResume = /^(?:继续(?:当前任务)?|resume|continue)[。.!！\s]*$/i.test(event.text.trim());
        let live:Record<string,any>={};
        if (value && !newTask) {
          live=asObject(await client(ctx).hostCall('request.status',{}));
          if (!live.request_id) {
            if (value.ownerSessionId) throw new Error('AUTHORITATIVE_REQUEST_MISSING: 当前会话账本缺失，请检查存储或明确新任务；不会从聊天恢复批准');
            value=undefined; // Inherited legacy entry is history, not a new owner's request.
          }
        }
        if (!value || newTask) {
          if (!value && requestFromBranch(ctx.sessionManager.getBranch())) ctx.ui.notify('历史已带入，权限未继承；本次输入建立当前会话的新任务。','info');
          value = await begin(ctx, event.text);
        }
        else {
          // Load current backend policy before changing branch state. Never resurrect a grant.
          if (live.request_id && live.request_id !== value.requestId) throw new Error('BRANCH_REQUEST_MISMATCH: 请明确开始新任务，历史权限不自动迁移');
          if (live.requires_new_request) throw new Error('PROFILE_MIGRATION_REQUIRES_NEW_REQUEST: 模式迁移不恢复旧请求，请用 /biomind-task new 明确开始新任务。');
          if ((value.status === 'paused' || live.paused) && !resume) {
            ctx.ui.notify('任务已暂停。使用 /biomind-task continue 明确恢复，或 /biomind-task new 开始新任务。', 'warning'); return { action: 'handled' };
          }
          if (resume && (value.status === 'paused' || live.paused)) await client(ctx).hostCall('request.resume', {}, { requestId: value.requestId });
          // A selected historical Pi branch can lag the live request revision.
          // Advance from the host receipt without importing the abandoned branch.
          const revised = pureResume ? live : asObject(await client(ctx).hostCall('request.revise', {}, { requestId: value.requestId }));
          const revision = Number.isInteger(revised.revision) && revised.revision > 0 ? revised.revision
            : pureResume ? value.revision ?? 1 : (value.revision ?? 1) + 1;
          value = { ...value, status: 'active', pauseReason: undefined, revision,
            userBasis: [...(value.userBasis ?? []), ...(pureResume ? [] : [{ text: event.text, revision }])], maintenance: undefined };
          publish(ctx, value);
        }
        const entries = value.boundary ? boundaryDrafts(ctx.sessionManager.getBranch(), value.boundary) : [];
        if (value.boundary && (!value.boundary.committed || entries.length)) {
          value = { ...value, boundary: { ...value.boundary, committed: true } };
          entries.push({ type: 'custom', customType: REQUEST, data: value });
        }
        preparedInputRequestId = value.requestId; lastOutcome = 'completed';
        await refreshApprovalStatus(ctx);
        setActiveTools(MAIN_TOOLS);
        return { action: 'continue', ...(entries.length ? { biomindBoundary: { leafId: ctx.sessionManager.getLeafId(), entries } } : {}) };
      } catch (error) {
        preparedInputRequestId = undefined;
        pi.events.emit('biomind:diagnostic', { code: 'REQUEST_INITIALIZATION_FAILED', message: String(error),
          ...(error instanceof DomainHandshakeTimeoutError ? { reason: 'DOMAIN_HANDSHAKE_TIMEOUT', timeoutMs: error.timeoutMs } : {}) });
        ctx.ui.notify('本次输入未发送给模型：' + String(error), 'error'); return { action: 'handled' };
      }
    });
    pi.on('before_agent_start', async (event, ctx) => {
      clearScientificDeadline(); scientificBlock = undefined;
      if (!readySessions.has(sessionId(ctx))) { ctx.abort(); throw new Error('BACKEND_NOT_READY'); }
      event.systemPromptOptions.contextFiles = filterNativeContextFiles(event.systemPromptOptions.contextFiles, ctx.cwd, getAgentDir());
      if (state(ctx)?.status !== 'active') { ctx.abort(); throw new Error('REQUEST_NOT_ACTIVE'); }
      preparedInputRequestId = undefined; initialUserPending = true;
      setActiveTools(MAIN_TOOLS);
      event.systemPromptOptions.selectedTools = mainTools();
    });
    pi.on('message_start', async (event, ctx) => {
      if (event.message.role !== 'user') return;
      if (initialUserPending) { initialUserPending = false; return; }
      if (!inputQueue.length) return;
      const text = textOf(event.message);
      const index = inputQueue.findIndex(item => item.text === text);
      if (index < 0) { ctx.abort(); throw new Error('Cannot correlate queued user revision'); }
      inputQueue.splice(index, 1);
      const value = current(ctx);
      const revised = asObject(await client(ctx).hostCall('request.revise', {}, { requestId: value.requestId }));
      const revision = Number.isInteger(revised.revision) && revised.revision > 0 ? revised.revision : (value.revision ?? 1) + 1;
      publish(ctx, { ...value, revision, userBasis: [...(value.userBasis ?? []), { text, revision }] });
    });
    pi.on('turn_end', async (event, ctx) => {
      // turn_end follows Pi persistence of the complete tool group, so archive
      // identities are actual SessionManager entry IDs, never guessed offsets.
      await archiveTurn(ctx);
      lastOutcome = event.outcome;
      if (scientificBlock) { if (event.toolResults.length) { lastOutcome = 'aborted'; ctx.abort(); } return; }
      const value = state(ctx);
      if (value?.status === 'active' && (ctx.signal?.aborted || event.outcome === 'aborted')) {
        await pause(ctx, 'user_aborted'); return;
      }
      if (!value || value.status !== 'active' || event.outcome !== 'completed' || ctx.signal?.aborted) return;
      {
        // Ask while the active agent run still owns a cancellable signal. At
        // agent_before_settle that signal can already be gone in Pi 0.87.1.
        if (value.maintenance || event.toolResults.length || ctx.hasPendingMessages()
          || event.message.role !== 'assistant' || event.message.stopReason !== 'stop') return;
        try {
          const fresh = asObject(await client(ctx).call('context.snapshot', {}, { signal: ctx.signal })) as Snapshot;
          if (!fresh.history_enabled || fresh.memory_mode !== 'ask' || fresh.maintenance?.enabled !== true) return;
          publish(ctx, { ...value, maintenance: { consumed: true } });
          const answer = await ask(ctx, 'question', `maintenance-consent:${value.requestId}`,
            '是否将本轮可复用的公开事实保存到项目记忆？不保存不会影响本轮答案。', ctx.signal, undefined, ['不保存', '保存本次记忆']);
          if (!answer.cancelled && answer.answer === '保存本次记忆') publish(ctx, { ...value, maintenance: { consumed: false, consent: true } });
        } catch (error) {
          if (ctx.signal?.aborted) lastOutcome = 'aborted';
          else pi.events.emit('biomind:diagnostic', { code: 'MEMORY_CONSENT_UNAVAILABLE', message: String(error) });
        }
        return;
      }
    });
    pi.on('context', async (event, ctx) => {
      if (state(ctx)?.status !== 'active') { ctx.abort(); return { messages: [] }; }
      if (ctx.signal?.aborted) return { messages: normalizeLegacyHistory(event.messages) };
      try {
        const value = current(ctx);
        assertStageBoundary(ctx.sessionManager.getBranch(), value.boundary);
        await client(ctx).hostCall('request.begin', { request_id: value.requestId, role: 'main',
          ...(value.boundary ? { boundary_id: value.boundary.id } : {}), user_revision: value.revision ?? 1 }, { signal: ctx.signal });
        if (value.boundary && !value.boundary.activated) publish(ctx, { ...value, boundary: { ...value.boundary, activated: true } });
        const inspected = asObject(await client(ctx).hostCall('request.preflight', {
          user_text: (value.userBasis ?? []).map(item => item.text).join('\n'),
        }, { requestId: value.requestId }));
        if (inspected.status === 'blocked') {
          scientificBlock = inspected;
          // Reports and owned-job cleanup are already committed. Permit one
          // model answer, with a bounded settle window, never another tool loop.
          if (!scientificDeadline) scientificDeadline = setTimeout(() => { lastOutcome = 'aborted'; ctx.abort(); },
            Math.max(1, 30_000 - (Date.now() - Number(inspected.accepted_at) * 1000)));
          pi.events.emit('biomind:scientific_block', inspected);
        } else if (!['validated', 'not_applicable'].includes(inspected.status)) {
          throw new Error('SCIENTIFIC_PREFLIGHT_UNAVAILABLE');
        }
        ctx.ui.setStatus('biomind', `${value.role} · ${value.status}`);
        pi.events.emit('biomind:state', { ...value, sessionId: sessionId(ctx) });
      } catch (error) { ctx.abort(); pi.events.emit('biomind:diagnostic', { code: 'STAGE_BOUNDARY_INCOMPLETE', message: String(error) }); return { messages: [] }; }

      try {
        snapshot = asObject(await client(ctx).call('context.snapshot', {}, { signal: ctx.signal })) as Snapshot;
        if (typeof snapshot.history_enabled !== 'boolean') throw new Error('Domain context snapshot omitted history policy');
        if (snapshot.request_paused) { await pause(ctx, 'backend_paused'); return { messages: [] }; }
        snapshotKnown = true; snapshotFailed = false;
        // A provider-bound context refresh consumes global preferences. Native
        // AGENTS edits remain pending until Pi reloads its own resource snapshot.
        const preference = asObject(snapshot.preferences);
        if (['loaded', 'truncated', 'empty'].includes(preference.status)) {
          for (const filename of [preference.source_path, preference.preferred_path]) {
            if (typeof filename === 'string') savedGlobalInstructions.delete(path.resolve(filename));
          }
        }
      } catch (error) {
        // ExtensionRunner reports thrown hook errors and can keep the original
        // messages. Explicitly abort when current request state cannot be verified.
        snapshot = { history_enabled: false }; snapshotKnown = false; snapshotFailed = true; ctx.abort();
        if (!ctx.signal?.aborted) pi.events.emit('biomind:diagnostic', { code: 'CONTEXT_POLICY_UNAVAILABLE', message: String(error) });
        return { messages: normalizeLegacyHistory(event.messages) };
      }
      return { messages: normalizeLegacyHistory(event.messages) };
    });
    pi.on('context_with_system', (event, ctx) => {
      const value = state(ctx);
      const currentContext = { request_id: value?.requestId, plan: value?.plan,
        preferences: Object.fromEntries(Object.entries(asObject(snapshot.preferences)).filter(([key]) => key !== 'loaded_at')), user_revision: value?.revision, history_enabled: snapshot.history_enabled,
        authorization_refs: snapshot.authorization_refs ?? [], approval_mode: snapshot.approval_mode ?? { mode: 'manual' },
        revision: snapshot.revision, maintenance: snapshot.history_enabled && value?.maintenance?.outboxId && !value.maintenance.applied
          ? { prompt: value.maintenance.prompt, sources: value.maintenance.sources, instruction: '用户任务已答复。这是一次受限记忆维护；仅可提交有当前公开证据支持的patch或空operations，不运行新分析、不新增任务。' } : undefined };
      const messages = currentNativeProjectContext(event.messages).map(message => message.role === 'system' && message.sections?.['biomind-current']
        ? { ...message, sections: Object.fromEntries(Object.entries(message.sections).filter(([key]) => key !== 'biomind-current')) } : message);
      const rules = (value?.maintenance?.outboxId ? MAINTENANCE_RULES : `${COMMON_RULES}\n${native ? NATIVE_RULES : PROTECTED_RULES}`)
        + (scientificBlock ? '\n宿主已完成可信输入检查和本轮报告落盘；当前科学阶段 blocked。不要调用任何工具或另找执行入口；直接说明缺少的映射证据和下列报告路径并结束。可信回执：\n'
          + JSON.stringify({ reason_code: scientificBlock.reason_code, scope: scientificBlock.scope,
            checker: scientificBlock.checker, receipt_ref: scientificBlock.receipt_ref,
            blocked_operations: scientificBlock.blocked_operations, artifacts: scientificBlock.artifacts,
            delivery_status: scientificBlock.delivery_status }) : '');
      messages.push({ role: 'system', content: '', sections: { 'biomind-current': `${rules}\n当前有效状态与偏好：\n${JSON.stringify({ ...currentContext, execution_profile: profile, ...(native ? { approval_mode: 'not_applicable', authorization_refs: [] } : {}) })}` }, timestamp: Date.now() });
      const sections=messages.filter(message=>message.role==='system').map(message=>({content:message.content,sections:message.sections}));
      const projection=JSON.stringify(sections);
      pi.events.emit('biomind:prompt_snapshot',{requestId:value?.requestId,time:new Date().toISOString(),phase:'prepared',sent:null,
        systemPrompt:projection.length<=1024*1024?projection:projection.slice(0,1024*1024),truncated:projection.length>1024*1024,
        representation:'Exact ordered system-message projection from context_with_system; not a reassembled provider payload'});
      return { messages };
    });
    pi.on('session_before_compact', async event => {
      // Preserve Pi's material, previous summary and file references. The
      // compatibility bridge appends focus to Pi's own history/prefix templates.
      event.preparation.messagesToSummarize = normalizeLegacyHistory(event.preparation.messagesToSummarize);
      event.preparation.turnPrefixMessages = normalizeLegacyHistory(event.preparation.turnPrefixMessages);
      return { customInstructions: await loadCompactionInstructions(event.customInstructions) };
    });
    pi.on('agent_before_settle', async (event, ctx) => {
      if (scientificBlock) return;
      if (isCanaryMode()) return;
      const value = state(ctx);
      if (!value || value.status !== 'active' || value.maintenance?.consumed || event.outcome !== 'completed' || ctx.signal?.aborted) return;
      const fresh = asObject(await client(ctx).call('context.snapshot', {}, { signal: ctx.signal })) as Snapshot;
      // Auto is explicit policy; ask requires a real UI decision before any
      // maintenance provider request. Manual/disabled never arm automatically.
      if (!fresh.history_enabled || !['auto', 'ask'].includes(fresh.memory_mode ?? '') || fresh.maintenance?.enabled !== true) return;
      if (fresh.memory_mode === 'ask' && value.maintenance?.consent !== true) return;
      publish(ctx, { ...value, maintenance: { consumed: true } });
      try {
        const consent = value.maintenance?.consent === true;
        const branch = ctx.sessionManager.getBranch();
        const start = branch.findIndex(entry => entry.type === 'custom' && entry.customType === REQUEST && asObject(entry.data).requestId === value.requestId);
        const sources = branch.slice(start + 1).filter(entry => entry.type === 'message').filter(entry => entry.message.role === 'user'
          || (entry.message.role === 'toolResult' && !['memory_search', 'memory_read', 'maintenance_submit'].includes(entry.message.toolName)))
          .map(entry => ({ source_ref: `pi:${sessionId(ctx)}:${entry.id}`, kind: entry.message.role === 'user' ? 'user' : 'tool_result', content: textOf(entry.message) }));
        const prepared = asObject(await client(ctx).hostCall('memory.maintenance.prepare', { sources, ...(consent ? { consent: true } : {}) }, { signal: ctx.signal, requestId: value.requestId }));
        if (prepared.status !== 'ready' || typeof prepared.outbox_id !== 'string') return;
        const next: RequestState = { ...value, maintenance: { consumed: true, outboxId: prepared.outbox_id, prompt: String(prepared.prompt ?? ''), sources: prepared.sources, applied: false } };
        setActiveTools(['maintenance_submit']);
        return { entries: [{ type: 'custom' as const, customType: REQUEST, data: next },
          { type: 'custom_message' as const, customType: 'biomind.maintenance', content: '进行已启用的一次记忆维护。仅提交有证据的patch，无法支持时提交空operations。', display: false }], continue: true };
      } catch (error) {
        pi.events.emit('biomind:diagnostic', { code: 'MEMORY_MAINTENANCE_PREPARE_FAILED', message: String(error) });
      }
    });
    pi.on('agent_settled', (_event, ctx) => {
      clearScientificDeadline();
      const value = state(ctx); if (!value) return;
      if (scientificBlock) {
        publish(ctx, { ...value, status: 'blocked' });
        pi.events.emit('biomind:settled', { requestId: value.requestId, sessionId: sessionId(ctx), role: value.role,
          outcome: 'blocked', model_outcome: lastOutcome, scientificSuccess: false,
          scientific_block: scientificBlock, final_response: scientificBlock.final_response });
        return;
      }
      if (value.status === 'paused') {
        // The permission/request remains paused, but the host still needs the
        // terminal model-loop observation to report an aborted turn truthfully.
        pi.events.emit('biomind:settled', { requestId: value.requestId, sessionId: sessionId(ctx), role: value.role,
          outcome: 'aborted', reason: value.pauseReason, scientificSuccess: null });
        return;
      }
      publish(ctx, { ...value, status: lastOutcome === 'completed' ? 'settled' : lastOutcome });
      pi.events.emit('biomind:settled', { requestId: value.requestId, sessionId: sessionId(ctx), role: value.role, outcome: lastOutcome, scientificSuccess: null });
    });
    pi.on('tool_call', async (event, ctx) => {
      if (!readySessions.has(sessionId(ctx))) return { block: true, reason: 'BioMind backend is unavailable', terminate: true };
      const value = state(ctx);
      try {
        const admission = asObject(await client(ctx).hostCall('request.dispatch', { tool: event.toolName }, { requestId: value?.requestId }));
        if (admission.status !== 'allowed') {
          scientificBlock = asObject(admission.scientific_block);
          return { block: true, reason: 'SCIENTIFIC_STAGE_BLOCKED: ' + scientificBlock.reason_code, terminate: true };
        }
      } catch (error) { return { block: true, reason: 'Scientific admission unavailable: ' + String(error), terminate: true }; }
      if(!value?.maintenance?.outboxId && !mainTools().includes(event.toolName))
        return {block:true,reason:'Tool is not selected for the current BioMind session',terminate:true};
      if (native && (NATIVE_TOOLS as readonly string[]).includes(event.toolName) || native && event.toolName === 'file_stat') {
        if (!readySessions.has(sessionId(ctx)) || value?.status === 'paused' || value?.maintenance?.outboxId)
          return { block: true, reason: 'Session is paused or execution profile is not bound', terminate: true };
        return;
      }
      const allowed = value?.maintenance?.outboxId && !value.maintenance.applied ? toolNames(['maintenance_submit']) : mainTools();
      if (value?.status !== 'active' || !allowed.includes(event.toolName)) return { block: true, reason: 'This execution entry is not available for the current BioMind request', terminate: true };
    });
    if (!native) pi.on('user_bash', () => ({ result: { output: 'Shell 快捷入口在 BioMind protected 模式不可用；请通过领域 run_command 执行并保留授权与作业句柄。', exitCode: 1, cancelled: false, truncated: false } }));
    const guardLifecycle = async (ctx: ExtensionContext, action: string) => {
      if (!clients.has(sessionId(ctx))) return; // No domain owner/jobs exist before the first domain operation.
      try {
        const value = asObject(await client(ctx).call('session.lifecycle', { action }));
        if (typeof value.active_jobs !== 'boolean') throw new Error('Missing owner status');
        if (value.active_jobs) { ctx.ui.notify('当前仍有科学作业；请先观察或明确取消后再更换会话。', 'warning'); return { cancel: true }; }
      } catch { ctx.ui.notify('无法确认领域作业状态，已保留当前会话。', 'warning'); return { cancel: true }; }
    };
    pi.on('session_before_switch', async (event, ctx) => {
      if (event.reason === 'resume' && event.targetSessionFile) {
        try {
          const header = JSON.parse(readFileSync(event.targetSessionFile, 'utf8').split('\n')[0]!);
          const bound = sessionProfile(process.env.BIOMIND_PI_DATA_ROOT ?? path.join(ctx.cwd, '.biomind/pi-domain'), header.id);
          if (bound !== profile) { ctx.ui.notify(`会话绑定 execution:${bound}，当前进程 execution:${profile}；请以 biomind --execution-profile ${bound} --session ${JSON.stringify(event.targetSessionFile)} 启动新进程。`, 'warning'); return { cancel: true }; }
          const root = readTargetSessionRoot(event.targetSessionFile);
          if (await realpath(root) !== await realpath(ctx.cwd)) {
            ctx.ui.notify(`目标会话属于其它项目。当前会话与科学作业保留，请在系统终端重启：\n${restartProjectCommand(root, { ...restartOptions(ctx), session: path.resolve(event.targetSessionFile) })}`, 'warning');
            return { cancel: true };
          }
        } catch { ctx.ui.notify('目标会话目录无法确认；已保留当前会话。', 'warning'); return { cancel: true }; }
      }
      const blocked=await guardLifecycle(ctx,'switch');if(blocked)return blocked;
      if(event.reason==='resume'&&event.targetSessionFile){try{await claimWriter(ctx,'resume',undefined,event.targetSessionFile);}
        catch(error){ctx.ui.notify(error instanceof SessionWriterError?error.code:'SESSION_WRITER_CLAIM_FAILED','error');return {cancel:true};}}
    });
    pi.on('session_before_fork', (_event, ctx) => guardLifecycle(ctx, 'fork'));
    pi.on('session_before_tree', async (event, ctx) => {
      const blocked = await guardLifecycle(ctx, 'tree'); if (blocked) return blocked;
      const sourceState = state(ctx), targetState = requestFromBranch(ctx.sessionManager.getBranch(event.preparation.targetId));
      const omitted = new Set(sourceState?.boundary?.targets ?? []);
      if (event.preparation.userWantsSummary && (sourceState?.requestId !== targetState?.requestId || event.preparation.entriesToSummarize.some(entry => omitted.has(entry.id)))) {
        ctx.ui.notify('跨任务切换请关闭分支摘要；原始分支仍可查看，旧任务不会被带入目标请求。', 'warning'); return { cancel: true };
      }
      if (event.preparation.userWantsSummary) {
        // Preserve Pi's original replace && nonempty-custom condition: adding
        // default focus must not turn an empty replacement into a template override.
        return { customInstructions: await loadCompactionInstructions(event.preparation.customInstructions),
          replaceInstructions: Boolean(event.preparation.replaceInstructions && event.preparation.customInstructions) };
      }
    });
    pi.on('session_tree', async (_event, ctx) => {
      if (!readySessions.has(sessionId(ctx))) return;
      restoreToolSelection(ctx);publishToolPolicy();setActiveTools(MAIN_TOOLS);
      await refreshApprovalStatus(ctx);
    });
    pi.on('session_shutdown', async (event, ctx) => {
      clearScientificDeadline();
      welcomeEpoch++;
      backendEpoch++;
      unobserveBackend(sessionId(ctx));
      updateBackend(ctx, { state: 'closed' });
      rejectPending('Session connection closed; no approval was granted', sessionId(ctx));
      inputQueue.splice(0); initialUserPending = false; preparedInputRequestId = undefined; activeContext = undefined;
      if (event.reason === 'reload') {
        snapshot = { history_enabled: false }; snapshotKnown = false;
        pi.events.emit('biomind:lifecycle', { sessionId: sessionId(ctx), reason: 'reload', owner: 'connection_retained' });
        return;
      }
      // Disconnect-only: a session/UI shutdown must never cancel scientific jobs.
      const connection = clients.get(sessionId(ctx)); clients.delete(sessionId(ctx));
      await connection?.close({ disconnectOnly: true });
      pi.events.emit('biomind:disconnected', { sessionId: sessionId(ctx), reason: event.reason });
    });
  };
}

export default createBioMindExtension();
