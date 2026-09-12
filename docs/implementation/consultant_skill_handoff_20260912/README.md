# Consultant Skill 交接与旧停止记录恢复

本轮按用户消息中的明确实施要求完成。附件目录未发现本次同名规格文件，因此不声称核对了未提供的附件正文。基于当前分支 `refactor/consultant-controller-authority` 的实际 HEAD `86c26122e9ed17f72546e979c3261bccd27b77bf` 增量实施，没有回退。

## 交付与根因

- `d81937351bf6913bee9ebabc6d6673608ff9f82f`：Consultant 在共享 Skill 成功返回、协议解析拒绝、服务拒绝三处收口本轮状态与真实观察。文件：`bio_agents.py`、`agent_core/consultation_state.py`、`tests/export_consultant_handoff_fixture.py`、`tests/test_consultant_skill_handoff.py`。
- `050d21f5d943d9c957c53e39d1a179ccf24c49d9`：旧停止适用范围、生产继续入口、当前问题显示及新 provider 故障交接。文件：`bio_agents.py`、`agent_core/workflow/consultant_resume.py`、`agent_core/workflow/early_nodes.py`、`agent_core/workflow/managed_stop.py`、`tests/test_consultant_stop_resume.py`。

Consultant 原先在局部变量中创建并补充 `consultation_state`。原生 `SEARCH_SKILLS` 直接返回共享 `_handle_skill_node_decision` 的 delta；共享服务负责 Skill 状态和真实结果，返回契约不含 Consultant 生命周期。typed adapter 在 `await source` 返回后合并、重读 durable 并校验，因此第一次搜索的真实执行结果已经存在，生命周期却仍为空，触发 `CONSULTATION_LIFECYCLE_CONTRADICTION`。另外两个原生 Skill 分支和两种拒绝出口有相同边界风险。

修复由 Consultant 调用 `close_consultant_skill_turn`，使用本轮实际状态和真实 `skill_decision_result` 调用既有 `record_consultant_tool_observation`。仅返回服务 delta 与本轮咨询状态，没有返回全量旧图状态，没有让模型补内部字段。stale 返回不伪造已接纳观察；重复观察的原有无进展检查保留。共享 Skill 服务及 Planner/Generate 的生命周期没有改写，正式 prompt 没有修改，没有新增模型调用。

来源、取值时点及其他工具分支核对见 [source_matrix.tsv](source_matrix.tsv)。原有组件级结论升级为真实 Consultant source、共享 Skill 服务、typed adapter、phase loop 和最终 provider messages 的联合验证。

## 旧停止与“继续”

历史终端附件在第 94 行显示 `↪ Continuing`，随后有新的 triage/consultant 活动；前轮已提取的脱敏指标也记录了两轮调用。因此不能把第二次显示概括为完全没有重入。该文案来自 `go` 的用户提交续接路径；它本身不能证明走过 plain `continue` 控制入口（该入口使用 `resume=True`）。本轮没有恢复或重新加载原 session，没有再次读取原业务数据库。

实际代码中还有独立问题：plain continue 的恢复分类把 failed/blocked/cancelled 也视为 terminal，控制出口却统一称为 completed。新逻辑明确区分已完成与仍停止；只有当前停止确实来自旧 Consultant 空生命周期交接错误、存在 Consultant Skill 服务回执、尚无实体 Plan 或执行、没有其他业务等待/恢复事实时，才允许重新咨询。预览不改变状态，实际进入 Consultant source 才记录新 invocation 并重新打开该已证明可重试的任务状态。

新 invocation 只改变 Consultant 停止记录的当前适用范围。旧记录保留；新停止替换当前位置时，完整旧记录进入 `managed_run_stop_history`。其他 owner 的停止、取消、已有执行和其他错误不因“继续”被清空。旧报告文件沿用原有保护写入规则，未放开覆盖。当前真实用户问题与 checkpoint interrupt 优先显示；新 provider 异常以当前 invocation 绑定的 `CONSULTANT_PROVIDER_FAILED` 和实际错误详情通过 typed/phase 边界，不能借用旧诊断。

## 实际验收

所有 BioMind 导入均在 allowlist 导出的隔离目录中，由既有 guarded bootstrap 注入假配置、fake provider 和 scratch-only 状态。没有访问真实 API、原数据库或原 session，没有运行 WGS，没有读取或执行启动脚本、密钥或敏感配置。

| 验收链 | 实际 fake provider 调用 | 真实 Skill 服务调用 | 执行调用 |
|---|---:|---:|---:|
| 原生 SEARCH → 状态收口 → typed/phase → 最终下一轮消息 → planning_ready | Consultant 2 | search 1 | 0 |
| REQUEST_SKILL_CONTEXT 文档目录读取 → 同上 | Consultant 2 | context 1 | 0 |
| PROPOSE_SKILL_BINDING 未观察 snapshot 被服务拒绝 → 同上 | Consultant 2 | proposal 1 | 0 |
| SEARCH 缺字段被协议拒绝 → 同上 | Consultant 2 | 0 | 0 |
| 合成旧 checkpoint → 生产 interactive `continue` → SEARCH → 问用户 | Consultant 2；总结 0 | SEARCH 实际返回 | 0 |
| 合成旧 checkpoint → 生产 interactive `continue` → SEARCH → 注入新 provider 异常 | Consultant 2；既有停止总结 1 | SEARCH 实际返回 | 0 |

每个旧 checkpoint 都先通过真实 Consultant、真实共享搜索及 typed adapter，暂时禁用本轮两处修复，重现旧错误后持久化生成；这一步各使用 1 次额外 fake Consultant 调用，执行为 0。不是手填最终合法咨询状态。随后恢复真实修复，实际运行 `interactive_mode → _continue_control_state → _run_agent_interrupt_loop → go(resume=True) → 生产图`。正常分支停在真实待回答问题；故障分支生成新 stop_id 和实际新错误。两条分支再次输入 continue 均不额外调用模型：分别保持等待与停止。

首个提交的专用测试 **7 passed**。最终提交树的回归 **46 passed，0 failed，39.02 秒**。最终集合包括 Consultant/恢复专用测试、缺 prompt_toolkit fallback、既有 CLI fallback 和 Plan evidence/PURPOSE 回归。PURPOSE 回归另外运行了 3 个场景，各使用 fake Consultant 1、Planner 1、Generate 2，以及 2 个仅在 scratch 内读取小 TSV、写报告的无害进程（合计 6 个）；这些不属于上表 Consultant 链的 0 执行计数。

完整结果、解释器核对与原工作区保护见 [validation.json](validation.json)。四组原生 Skill 联合验收均在下一次最终 provider messages 中找到与服务实际返回**完整 JSON 相等**的观察，不只是命中文案关键词。两组继续回放保留完整模型请求、旧/新停止及调用数。证据均由合成任务产生，来源与 SHA-256 见 [evidence_manifest.json](evidence_manifest.json)。

```bash
cd /public/home/liusiqi/03.毕设/Biomind_v1
python tests/export_consultant_handoff_fixture.py /tmp/biomind-consultant-recheck --tree 050d21f5d943d9c957c53e39d1a179ccf24c49d9
python tests/cli_v2_support.py --export /tmp/biomind-consultant-recheck --results /tmp/biomind-consultant-recheck-results -q tests/test_consultant_stop_resume.py tests/test_consultant_skill_handoff.py tests/test_cli_v2_terminal_fallback.py tests/test_plan_results_evidence.py tests/test_plan_results_purpose.py
python docs/implementation/consultant_skill_handoff_20260912/verify_evidence.py
```

实际最终运行使用 `/tmp/biomind-resume-final` 和 `/tmp/biomind-resume-final-results`；导出树与第二个提交的 Git tree 相同，命令和解释器记录在 evidence/invocation.json。复跑需使用新的 `/tmp` 导出目录。

## 依赖、边界及未解决项

实际解释器 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python` 为 Python 3.12.12，其 site-packages 没有 prompt_toolkit 包或 dist-info。`requirements-cli.txt` 已声明 `prompt-toolkit==3.0.52`。生产 `run_terminal` 捕获 ImportError 后仅执行一次 plain runtime；新增隔离测试验证了这条路径。没有安装、升级或修改真实环境。动态终端是否恢复仍取决于实际运行解释器拥有此依赖，和 Consultant 业务修复分开。

两项旧 CLI 综合 TTY 测试在审批 fixture 的 `owner.modal` 断言处失败；同命令在修改前 HEAD 上也是 **2 failed / 6 passed**，带首个修复时为 **2 failed / 13 passed**（多出的 7 项是新增专用测试）。未改写这些旧测试，也不声称全仓库测试通过。PROPOSE 的成功绑定/审批全流程未在本轮新增端到端回放；本轮实际覆盖服务拒绝交接，并静态核对成功分支使用同一收口点。

原有 **437** 条 dirty/untracked 状态记录与开始时一致。`managed_stop.py` 已有的两处“任务状态/总结状态”文案修改保留在工作区，未夹带进提交或隔离验收树。未 push。真实科学任务及所有原 session 均未恢复，不能从这些离线结果推断其科学结果已成功。
