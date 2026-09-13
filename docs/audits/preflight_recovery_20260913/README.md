# session_1789266028 预检连续失败与 Skill 续查定点审计

本轮仅审计，未修改生产代码、正式 prompt、原 Plan、原批准脚本或既有测试。唯一现场为 `session_1789266028`；原 session 仅静态读取安全 JSON/日志/脚本，未恢复、未调用业务状态加载器或数据库。没有读取原 FASTQ/FASTA、启动脚本、敏感配置或环境值，没有真实模型/API/WGS调用。

## 结论与三次 attempt

第三次 rc=2 是**新错误**：四个 FASTQ 前缀检查已返回 passed，参考 FASTA 被送入同一个 FASTQ 专用 reader，读取其长行时触发 `READ_LINE_LIMIT`。不是第一次 argparse 错误复发，也不能据此判定原 FASTA 损坏。

三次均为 `plan_ece29181c77a978618ee` / `plan_v1` / `step-00-input-preflight`。下列时间为原退出记录的北京时间，时长为 execution record；不是整个模型/人工等待耗时。

| attempt / action | 完成 / 时长 / rc | 原始失败、真正退出阶段 | 输出与副作用 |
|---|---|---|---|
| `attempt_78354904d83db1c42d6f` / `action_e247d4d249aa0727` | 10:34:27 / 6.222s / 2 | `argument --item: expected 2 arguments`；五项 LABEL/PATH 各合为一个引号词；argparse 未进入逐项检查 | stdout 空、parser emission 0；调用方已 mkdir 最终证据叶目录 |
| `attempt_ba5be193655765231699` / `recovery_action_35e94a3a5ca4c321` | 10:36:04 / 6.134s / 1 | 参数已分开；`execution/preflight.py:46` 的 `directory.mkdir(parents=True, exist_ok=False)` 抛 `FileExistsError` | 同旧目录，脚本仍先 mkdir；无逐项开始、无 observation |
| `attempt_05babb68ee8a678c96dd` / `recovery_action_cdacf04377dd1f62` | 10:37:19 / 6.727s / 2 | 参数正确，删除 mkdir，目录创建成功；第 5 项 `reference_fasta` 内层 rc=2 / `READ_LINE_LIMIT` | 四项 passed，第五项失败；外层仍有 1 条合法 `validation_observation`，status=incomplete、returned=5、passed=4、unstarted=0、scope=fastq_prefix_only、full_file_validated=false |

第三次的直接原件是 attempt 下 `stderr.log` / `stdout.log` / `executed.sh` / `exit_status.json` 与执行记录，以及 observation 明确引用的 `item-005.stderr.log`。第五项 stderr 记录 operation=fastq_prefix、representation=plain、checked_records=0、returned_lines=1、scanned_input_bytes=decoded_bytes=16391、line_bytes=16384、detail=`record representation exceeds limit`。外层 parser status=parsed、issues=[]；不能把“进程失败”写成“没有任何真实证据”。

本次三条记录证明执行过三次预检，未提供 BWA/GATK 主计算执行证据。最后 Recovery provider 取消，不能把 provider 的 CLEANUP_CONFIRMED 外推为整个任务/工具全部清理完成。

## 版本、来源与字节身份

审计起点 HEAD：`2e6eb7a09a61ce14b5304763215faa4e0b7e3624`，tree `7fc420b2ae3ca865c529ac1caf9d7da4cfd8745d`，分支 `refactor/consultant-controller-authority`。index 起始为空；原 437 条 dirty/untracked 状态原样保护。

隔离导出使用该提交树，再明确覆盖当前五个安全 dirty 文件：`prompts/manager.py`、`agent_core/node_context.py`、`agent_core/planner_context.py`、`agent_core/provider_message_safety.py`、`agent_core/workflow/managed_stop.py`。其余既有 dirty 测试未纳入；这不是整个工作区验收。`verification.json` 记录这些文件的保留校验和本轮导出来源 hash。

现场 runtime identity 的 disk_head_at_capture 与起点 HEAD 相同，但 **loaded_revision=UNKNOWN**。已捕获源码中的 bio_agents.py、prompts/manager.py、recovery_agent.py、inspection_readers.py 与当前安全文件 SHA 相同。generate_handoff.py、execution/preflight.py 没有该快照的逐文件字节证明，历史版本一致性仍 UNKNOWN；后者的原 traceback 与当前真实入口行为是独立证据。

三次资源绑定一致：`genomics/wgs-short-read-mapping#scripts/inspect_fastq_batch.py`，环境句柄 `BIOMIND_SKILL_ECD588FCA5ADDFE0`，resource snapshot `skill-resource-86030299f95e1a46`，绑定内容 SHA256 `86030299f95e1a464699480278f179d2b19463256681f64c95357329a9483160`。未发现换成另一 helper 快照的证据。Skill 版本 1.1.0，snapshot `skill-snapshot-db7850875a317dbdd050`。

| attempt | generated = approved SHA256（原件逐字节相同） | executed wrapper SHA256 |
|---|---|---|
| 1 | `1ef088c54b727b574f48b32f2422b68c1eeb546b0035775a67aadb0c25ce770e` | `de7253830ef4dec6b9b366fbd2e7ab6682ea8efe2708868aa78791cf81c01707` |
| 2 | `42ccb7349320e60508c1b586921b4eb398627489dd90dfe6dae2edce5a821c1d` | `0cc863571ef2fc44537441663a3f6c8a72bc364ca6ef06547b66d54ad891a57b` |
| 3 | `3717b1199a961e147c699b1bac88bd1ac3a6feb143325ce7a9fde4a4ca529856` | `2bdd0f7a100308bfbcf5b022f4f270a142d677de63a0ef4a7f6d136d6a81f0f7` |

首次 Generate0009 使用现有 `biomind_generate_display_v1` envelope，解出 payload 后与第一次批准脚本对应；其后0012/0013/0016为 shell 块。generated 的原始 provider 响应、剥离块边缘空白后的 body 对应、批准脚本与实际 wrapper 分开记录在 `session-projection.json`。UI 正文 hash 不充当 executed hash。原件记录了外层 `bash executed.sh` 命令，但没有独立捕获内层 batch 的 OS argv；历史参数数量依据批准 shell 形式、真实 parser 和逐项回执交叉验证，不对任意 shell 人造 split 后冒充 argv。新隔离驱动自己的显式 argv 则完整留存。

## Recovery → Generate 修正传递矩阵

| 约束 | 第一次正式 directive / 接纳 / Generate | 格式纠正后与第二次脚本 | 第二次正式 directive / 接纳 / Generate | 第三次脚本 |
|---|---|---|---|---|
| `--item LABEL PATH` 两参数 | 明确修正；admitted / candidate_repair_required；Generate0012 message1 含完整 instruction | Generate0013 message1 仍有；脚本落实 | 明确保留；Generate0016 message1 含完整 instruction | 保留 |
| 不预创建最终叶目录 | 第一条反而要求 evidence-dir 等保持不变，未修 mkdir | 保留 mkdir，仍冲突 | 明确去 mkdir；admitted / candidate_repair_required | 落实 |
| 不复用旧叶目录 | 未修，复用第一次路径 | 复用 | 要求新未存在叶目录 | 实际新建成功，但叶名用了上次 attempt ID，不能当作本次正式身份分配 |
| 原输入绑定 | 保留，且仍将 reference_fasta 放 batch | 五个路径未被省略或改写 | 要求保持 | 保留，但错误的 FASTA→FASTQ 调用选择也被保留 |
| 授权输出根 | 原用户 relative root 在 Generate 请求可见，但错误旧 evidence 路径被要求保留 | 仍少 `remaining/` | 新 instruction 限定 approved logs；正确绝对根在 recovery explanation 中出现 | 本次落到正确根下 |
| 按输入类型选择检查器 | 第一步 success_criteria 已分别定义 FASTQ 与 FASTA；Recovery 第一条仍要求五项进 FASTQ batch | 未修语义 | 本轮仅修目录，未修异构输入 | 第五项技术失败 |

第一条 directive：`recovery-directive-765758077c3b3cd34f230d28-1`，Recovery0011 原生提交。Generate0012 (`generate-0012-8d977fac31bc`) 在 shell 外输出说明，真实错误为 `GENERATE_SCRIPT_BLOCK_INVALID: prose outside the executable block is not allowed`；这不是第四次进程执行。Generate0013 (`generate-0013-ace12cccc970`) 请求前 7 条与0012完全相等，再追加坏响应与纠正反馈，总计9条。已接纳修正没有在格式纠正时丢失。

第二条 directive：`recovery-directive-a868a6f0d66f1f4fd19dcad6-1`。Recovery0014 缺 schema_version，被 `STAGE_FIELD_REQUIRED` 拒收；0015补齐后才接纳。Generate0016 (`generate-0016-bb42c9ce9c75`) message1 含完整当前 instruction，message6 含真实失败脚本/执行证据。旧第一条 instruction 没有全文重塞，但当前 instruction 重述需继续保留的参数修正，实际脚本亦保留。**无证据支持把本次失败归因于 Recovery→Generate 上下文丢失。**

用户正式输出根：`/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/remaining/BM-REMAIN-WGS-005/medium`。前两次 evidence-dir 为 `.../runs/BM-REMAIN-WGS-005/medium/00.input_check`，不在该根内；第三次为正确根下 `logs/00.input_check.attempt_ba5be193655765231699`。脚本获得代码审批是事实，但不能据此声称已有结构化授权根校验；execution policy 本次未提供该 leaf 与 TaskSpec log-root 的结构化绑定证明。也不据此推断其他宿主级写权限。

## Skill 查询与长说明来源

Recovery0010 原生调用 `skill_search`，query=`inspect_fastq_batch --item LABEL BOUND_PATH`，skill_id=`genomics/wgs-short-read-mapping`，tool_call_id=`call_01_XltYnURTQVANoLENbYLC7164`。started/completed 事件 owner=Recovery；结果 cache_hit=true、decision_result=SKILL_CONTEXT_RESULT，给出已选 Skill 的137–150行（包含正确二参数接口）。Recovery0011 最终 provider request 的 ToolMessage 收到该结果。它是查已选资源用法；不是 Consultant 重新选 Skill，也不是 Planner 重入。失败后的 trace 只有 Recovery/Generate，两个 admitted outcome 都是 candidate_repair_required，Plan 保持 plan_v1。

可优化为当前已绑定资源版本的局部文档/入口查询；本次搜索已限定 skill_id，不能把它描述为广域搜索，更不应禁用 Recovery 搜索。两项全 Plan selected skills 与当前预检步骤的单项 Skill 要区分。

长英文段在 Recovery0014：content 长度9803，provider annotation reasoning_char_count=0。`recovery_response_received.explanation` 与 content 逐字符相同，source_message_id=`recovery-seed-a868a6f0d66f1f4fd19dcad6:assistant:001`，status=not_accepted，error=STAGE_FIELD_REQUIRED。这是 native content 在协议失败分支的完整公开投影证据，不能称已证实 reasoning 字段泄露，也不代表多次 provider 调用。

`agent_core/workflow/recovery_agent.py` 的响应收口（约1180–1293行）先保存 content；只有 candidate_validated/native_tool 分支以 directive.summary 替换公开 explanation。缺字段的拒收分支保留整段 content，继而由 `bio_agents.py` 的 `_schema2_recovery_event_sink`（约3484行）公开显示。有效补交时才显示短 summary。Generate message5 还接收 unverified recovery_explanations；应把诊断证据与公开投影责任分清，不凭这点任意删除历史。这里仅导出长度、hash、字段和事件，原长文没有进入审计产物。

## 真实入口差异与最小补丁建议（本轮未实施）

| 优先项 | 文件/函数 | 最小建议及不应做的改动 |
|---|---|---|
| 输入类型与接口示例 | `skills/genomics/wgs-short-read-mapping/SKILL.md` batch 段；`scripts/inspect_fastq_batch.py:main` | 写明 FASTQ-only、重复 `--item LABEL "BOUND_PATH"`、不接收 FASTA。用当前 Plan 已有 FASTA success_criteria 指导新候选调用独立有界 FASTA 头/可读性检查；现有 reader 没有 fasta_prefix，若增设入口须显式声明语义与覆盖。不提高行限来掩盖类型错配，不把短 FASTA 的 rc0 当通过。 |
| 目录唯一创建与重试 | 同一 Skill 示例、batch `--evidence-dir` help、`execution/preflight.py:run_checks`；`bio_agents.py:execute_logic_impl` 的已有 attempt/资源绑定层（正式身份在审批执行边界形成，需把候选日志 leaf 与该层衔接） | 明确唯一创建者是 helper，调用方只提供获准父目录下未存在的 leaf。复用既有绑定/attempt 分配机制提供本次新 leaf；同候选再次批准也不能重用旧叶。保持 exist_ok=False，不删除旧证据，不 mkdir/mktemp -d 同一叶，不要求模型编造正式 attempt ID。 |
| 生成交接 | `bio_agents.py` Generate 最终装配（约41351、42015–42053行）与已有格式纠正 | 本次全 instruction 和真实失败证据已送达，暂不改交接架构或堆叠历史 token。先修调用文档/类型语义与确定的目录绑定空缺；没有支持新增 context-loss 补丁的证据。 |
| 公开摘要失败分支 | `recovery_agent.py` 当前响应投影，`bio_agents.py:_schema2_recovery_event_sink` | 对未接纳 native 候选沿用已有安全公开摘要语义，明确“尚未接纳”和真实字段错误；禁止把整段未筛选 content 自动当公开说明。不要用英文关键词过滤或新增旁白模型；私有内容也不能放入详情。 |
| Skill 续查 | Recovery 的既有 scoped Skill 接口 | 有已绑定快照时可直接局部查该版本的参数/help。无需换 Skill、禁搜索或 revise_plan。 |

文档已经正确写出 nargs=2 形式，问题不是文档要求单参数；“new evidence-dir”目前未充分说明未存在叶目录及唯一创建方。batch 源码所有条目都执行 inspect_fastq.py，scope固定 fastq_prefix_only。正式 Plan 的 FASTA 成功条件已经存在且预检 outputs=[]/有 observation，保留单 Plan、无文件检查和 gate。

## 离线验收与可复现命令

使用当前安全导出、既有双 guard、fake provider、目标 biomind 解释器、原 pytest capture 与持久 fatal 出口；无依赖安装、无原 session fixture。测试只读本轮新建的小 gzip FASTQ / 短与17000碱基单行合成 FASTA。

```bash
cd '/public/home/liusiqi/03.毕设/Biomind_v1'
python tests/export_preflight_recovery_audit.py /tmp/biomind-preflight-audit-20260913/export-6 --tree 2e6eb7a09a61ce14b5304763215faa4e0b7e3624
python - <<'PY'
from pathlib import Path
from tests.cli_model_progress_support import launch
raise SystemExit(launch(Path('/tmp/biomind-preflight-audit-20260913/export-6'),
    Path('/tmp/biomind-preflight-audit-20260913/final-run'),
    ['-q', 'tests/test_preflight_recovery_audit.py'],
    fatal_diagnostics=True, installed_cli=True))
PY
python docs/audits/preflight_recovery_20260913/project_session.py /tmp/biomind-preflight-audit-20260913/projection-3
```

这些是本轮实际路径；重跑须换新的 export/results/projection 目录，避免覆盖证据。目标子进程为 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python`；父级 python 只执行安全导出/启动器。

| 真实 batch 反例 | 结果 |
|---|---|
| 五个合并参数 | rc2 / expected 2 arguments，无 observation |
| 正确参数+mkdir；改名仍mkdir；去mkdir仍复用旧叶 | 均 rc1 / FileExistsError，无逐项检查 |
| 正确参数+新未存在 leaf；再次另新 leaf | 均 rc0，四个新 FASTQ 前缀通过，旧证据文件 hash 不变 |
| 四 FASTQ + 长合成 FASTA | rc2 / READ_LINE_LIMIT，4/5 passed，仍有可解析 incomplete observation |
| 四 FASTQ + 短合成 FASTA | 外层 rc0，但仅4/5 passed，FASTA prefix无效；反证“rc0=科学检查成功” |

**最终整份新驱动：3 passed，退出码0**（`verification.json` 保留 setup/call/teardown 完整结果）。batch 单项为8次真实 batch、18次真实 reader 子进程、模型调用0。生产图交接每个 compact/off 用例：真实 Consultant/Planner/Generate 装配 + fake 响应，初次真实参数失败 → native Recovery 缺字段拒收 → 补齐实际接纳 → 下一次 Generate 最终请求 → 块外文字拒收 → 格式纠正 → 新批准字节经真实 executor 执行。每模式 Consultant1/Planner1/Generate3/Recovery2，审批2，执行2（rc=[2,0]），未进入 Supervisor/后续科学步骤。没有手工预填合法最终状态，没有全 mock 工具成功。

显示对照比较调用数、节点序列、审批/执行身份对应和实际 observation；两个独立合成任务的临时路径和正式 attempt 身份自然不同，逐案保留原始批准/执行 hash，跨模式代码只在明确替换合成根路径后比较，不能把规范化 hash 冒充原件相同。off 不产生终端正文。此为 plain compact/off 定点对照，不声称完成新的生产 TTY 部署验收。生产图沿用既有合成 local-blast Skill 目录 fixture，实际 batch 通过导出的绝对路径执行；它验证修正交接及真实工具接口，不单独证明现场 mapping Skill 绑定链。现场三次绑定由原 policy/快照静态证明。最终全套总计12次 batch、20次 reader，fake provider14次（C2/P2/G6/R4），真实模型与旁白调用0。

`verification.json` 保留全部先前失败运行的退出码、pytest phase和摘要：最初四项 fixture 的 argparse 报文与现场五项不同；首次交接 fixture 未声明 observation，阻在 Planner；随后 fixture 错将批准正文 hash 与执行 wrapper hash 作相等比较。修正均只在本轮新驱动，未放松生产保护或跳过断言。最终验证使用分别核对的批准正文及 wrapper。新通过结果不等于此前未解决 SIGSEGV 已修复；本轮没有重跑该旧矩阵。

## 产物与限制

`session-projection.json` 是55个静态来源的紧凑投影（完整路径+字节数+SHA），包括三份批准脚本、执行身份、修正/admission/请求字段矩阵、精确 Skill 结果、第三次 nested error。`verification.json` 是调用/执行/失败账本/来源签名，不复制原 provider 全文、原 state、敏感环境、原始数据或大型回放包。

未解决：生产接口/显示修补留待后续实施；历史内层 argv、未捕获文件的运行字节、loaded revision、最终全任务清理状态仍 UNKNOWN。当前证据不支持清空 stop、修改 Plan、放松 gate、重跑主计算或禁止 Skill 搜索。

新隔离驱动提交：`b88826fdff68c1487caf1755f4a6c6a03a1cee6c`（仅两个新增 tests 文件）；本报告与两个 JSON 投影及静态投影脚本另行提交。无 push。
