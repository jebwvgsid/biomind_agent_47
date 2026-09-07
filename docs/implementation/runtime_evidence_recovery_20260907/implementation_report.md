# BioMind runtime evidence recovery — 实施记录

STATUS: PARTIAL_WITH_EXPLICIT_REMAINDER

本轮先核对源码与Git，再沿RT-01、RT-02及其必需RT-03停止出口实施。未执行旧provenance增量迁移规格。RT-02与确定性RT-03收口作为完整逻辑补丁一起提交，避免产生无人消费的outcome或不能退出的恢复分支。

## 提交与历史边界

原始HEAD为b9a686445a1f4c9f3c7b94bcd55f70ea9998efa5，分支refactor/consultant-controller-authority；初始Git为71 modified /33 deleted /56 untracked状态条目，无staged。

上一轮mutation provenance补丁仍在原工作树，不能单独安全提交：注册reducer依赖未提交workspace_attestation框架和action.status=bound；Execute journal依赖prepared_mutation_intents与workspace admission；集成fixture还依赖未跟踪semantic_completion、step_effects和context_compaction。本轮未吸收这些依赖，也没有把dirty checkout作为已提交基线。E07/E08仍须在该独立框架能隔离验收后完成。

必要独立前置提交e4f01f9969425a07b0903abff1917c19f90ac784修复HEAD生产Planner仍用legacy FINALIZE gate拒绝schema-2 durable handoff的问题。仅接已有consultant_planning_readiness，5个实际生产gate测试通过。RT-01提交e07ab0a1dbf22f38868897f313394e3f9f805ccf修复完整audit传递、durable acceptance/reload与作用域消费，31个测试通过。两次提交均保留已有工作树改动。

## 真实生产链

BioAnalystAgent.schema2_app的Consultant真实load_skill_doc/select_skill/update_taskspec/submit_planning_handoff → _commit_consultant_update → PlanTracker CAS保存task_state.runtime_evidence → Planner持久化 → PlanReview审批 → TypedGenerate → _generate_impl的resolve_action_runtime_bindings。

仅required runtime fact缺失分支产生failure_scope=runtime_fact及UNKNOWN/UNAVAILABLE/AMBIGUOUS诊断。Generate在provider调用之前建立durable request/episode，返回GenerateOutcome.runtime_evidence_required → runtime_graph conditional edge → Schema2RecoveryNode → BioAnalystAgent._recover_runtime_evidence → RuntimeEvidenceRecovery。

服务先恢复本task同作用域有效证据；仍缺事实时读取RuntimeInspectionAdapter.describe中当前Skill唯一已声明且获准的检查。检查前CAS保存dispatch receipt → BubblewrapRuntimeProbe.run → 受限外部process factory → 验证输出、覆盖范围、原始时间及任务/plan/step/Skill/capability/environment/审批scope → CAS同时保存accepted evidence和completed receipt → 从磁盘重读resolver → RecoveryOutcome.runtime_evidence_ready → 同一步Generate重新组装prompt → 原审批/manifest/workspace检查 → 真实HITL → Execute原manifest/approval验证及新增实时路径复核 → Execute科学runner → Supervisor → CommitStep。

无法获得证据时，runtime_recovery_stopped经typed edge结束当前调用。finalize_runtime_recovery_result读取真实durable完成步骤、attempt及已验收产物，给出正文与局部resolution_disposition；只有可信完整负面事实才是dependency_blocked。UNKNOWN、超时、非零退出、输出不足、无producer、预算耗尽和内部存储错误保留各自原因。_terminal_status_from_snapshot与_interactive_run_result消费同作用域诊断，新实例也可读取；已恢复或旧scope诊断不能覆盖当前结果。

已有具体授权问题走持久pending_question → 原RecoveryOutcome.user_input_required → AskUser → Consultant解释用户答复。答复不直接成为runtime证据或执行批准。当前production adapter没有产生版本/环境多选的注册producer，不能把该分支的接口接线视作真实环境切换能力已验收。

## 本轮补齐的边界

- 每binding有UNKNOWN、AVAILABLE、UNAVAILABLE、AMBIGUOUS；inspection error是单独事实，旧摘要不能升格为可信ready。负面事实不会被Java HOME等原有路径派生覆盖。本轮未增加Java特判，未将required变optional。
- Runtime scope覆盖Skill文档/能力/检查契约、环境前缀身份、host、受准读取根与安全环境参数；报告与恢复使用相同审批scope定义。跨scope、晚到、过期或篡改结果拒收。
- 受限检查复用已有Skill健康检查声明及纯声明解析。无profile shell、空进程环境、显式只读挂载、独立网络/进程命名空间、输出与deadline限制；不安全语法、安装、下载、环境变更、无sandbox等停止并解释。实际bwrap可用性未运行探测，未安装依赖。
- 通用printf声明解析修复字面换行后的第二个RESOLVED_*漏收。
- durable episode最多3个动作、2次检查、1次有理由transient retry、每检查最多60秒、episode最多180秒；请求ID/时间戳变化不刷新预算，崩溃未知只允许声明可重复的受限重放。成功receipt和证据持久化后才恢复。
- missing-required分支不再做同输入deterministic Controller rebuild；其它非runtime prompt契约失败仍保留原行为，没有全局终态重写。
- Execute新增同一已批准值的实际路径/权限检查，不重算或洗白旧approval。第二次prelaunch检查失败时已登记但未启动attempt收口为FAILED，无伪造pid、returncode或执行成功。
- 恢复ready同CAS归档旧episode停止诊断；旧scope/已存在执行owner的停止处理不能清掉新pending action或把已开始状态写成False。
- 仅生成TaskSpec请求的report.md/reproduce.sh/stage_status.tsv，保护旧文件、软链和科学输入。reproduce只列真实完成attempt/script引用和未完成说明，不授权自动重放。报告失败仍返回正文，不给假路径。

## 验收与候选版本

FINAL_CANDIDATE_SOURCE_SHA256: f1eea7a12784567f6682d25254e6bc90d65b0f6a8494397dfff7884f5b8b7aa3
FINAL_TEST_RESULT: 152 passed, 139 subtests passed in 72.11s; candidate unchanged.
FINAL_CANDIDATE_BASE: e07ab0a1dbf22f38868897f313394e3f9f805ccf
FINAL_COMMIT: 由提交后终端结果关联，避免自引用追加提交。

最终测试仅在HEAD加本逻辑delta构建的安全候选树运行；未导入用户dirty工作树。使用既有/public/home/liusiqi/anaconda3/envs/biomind/bin/python及真实LangGraph/LangChain，无激活/安装/更换BioMind运行环境。guarded runner只替换dotenv/config.startup引导，拒绝原工作树、凭据及启动文件读取、真实子进程和网络；真实生产节点保留，仅外部provider/process/filesystem叶子受控替换。日志记录每个导入模块实际路径和源码SHA256、测试前后候选一致性。

9个生产用例包含一条完整18.1单例：真实Consultant→durable audit→无runtime prepare→真实CommitStep→需要两个facts的report→受控检查/accepted receipt→新BioAnalystAgent与新MemorySaver从磁盘重开→同plan/version/step→Generate/HITL/Execute/CommitStep。每步provider/科学runner各一次、probe总一次。独立checkpoint interrupt/resume用例另列，未将MemorySaver复用冒充冷重开。报告路径、UNKNOWN外层返回、两处prelaunch失效也通过真实生产链测试。

## 明确剩余项

1. E07/E08旧workspace malformed-candidate及mutation provenance完整集成回归需要未提交框架依赖，当前独立HEAD候选无法安全吸收。原dirty内容保留，未把这些项目标PASS。
2. C03正向compatible-cache adoption：当前全局缓存缺完整可信scope，adapter明确cache_restore_permitted=False；只恢复task自己的accepted records或运行当前声明检查。不把旧缓存digest/ready摘要改成新的observation。
3. D06及RT-03的runtime Recovery LLM策略循环未实现。当前只有单一获准检查，没有可选补充方法或可修参数producer；正常路径无需LLM。现有RecoveryAgentLoop还缺runtime source/tool/directive consumer和跨重入durable计费，不能仅包一层或伪造Execution FailurePacket声称完成。确定性获取与结果说明已落地；将来新增真实合法策略时需完整接线验收。
4. 无实际环境版本选择/审批改变后自动恢复的生产fixture；已有AskUser通道接线不被计作安装、换环境或用户授权自动接受。
5. 实际bwrap命名空间权限、真实模型/API、snpEff build和完整benchmark均NOT_RUN。当前工作树叠加历史dirty框架只做安全合并与语法核对；完整集成测试对象是隔离候选，不偷取那些未提交依赖。

## 安全与Git

未读取显式凭据或用户启动脚本，未改原session_1788759228证据，未调用真实模型/API或科学流程，未安装/下载/变更环境，未push。早期只读子agent命令使用过默认login shell并输出HPC系统profile banner，后来统一login:false；因此不能宣称整个会话从未执行过任何系统shell profile。应用测试均使用禁启动文件的guarded bootstrap。

提交按明确文件/精确patch写index，核对index字节等于已测candidate，hooks与GPG关闭，无整体add、reset、clean、stash或push。已有modified/deleted/untracked内容保留。最终Git状态通过终端及独立验收记录给出。

最终共享授权摘要与未保存请求回归已纳入统一验收；同问题已有持久答复但有效scope不变时返回unresolved，不重复问同题、不自动批准。
