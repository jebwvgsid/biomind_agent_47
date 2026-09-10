# 状态延迟与产物证据：定点补审计（2026-09-10）

仅静态读取源码、原始 JSON/日志和既有 profile；没有调用任何业务加载器、构造器或验证接口。附件作为审计范围说明，执行授权以用户本轮要求为准。

源码基线：`refactor/consultant-controller-authority`，HEAD `c975a6818710dae5a23fc29c202c4e15ce0db25d`。下述核心源码与 HEAD 无工作区差异；仓库其他既有修改保留。历史进程实际加载 revision **UNKNOWN**，不由当前 HEAD 推定。文档版本由本目录的 Git 提交标识（最终回复给出完整 hash）；与源码基线分开。

定位约定：`R=/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789028444`；`S=R/plan_state.json`；`P=/tmp/biomind-latency-qo9unodf`。以下源码路径相对仓库，JSON pointer 相对所述文件。最小证据文件指纹见 [evidence.tsv](evidence.tsv)，不收录完整请求或运行数据。

## A. 暂停边界与重建风险

**CONFIRMED（持久记录）**：S 的 generation=103，前两步 passed，第三步 verification_pending，后续 13 步 pending。`/task_state/pending_action={}`、`current_attempt=""`、`active_execution={}`。最后成功命令 attempt 为 `attempt_a6a5d035647933379a47`（下称 A2），exit_status 记录 `2026-09-10T16:46:29+08:00` rc=0；尚无第三步 CommitStep。

最后接纳 directive：`recovery-directive-1715bc823a58c5edff6e9e47-01`。S `/task_state/recovery_state/active_recovery` 为 pending、target_route=generate；generation_claim 的 `generation-ce28af2229baf8afe99a` 于 16:53:27 claimed。`context_trace/events.jsonl` 最后记录 08:51:05Z `recovery_directive_admitted`；已有 Generate manifest 最后为 `generate-0017-35f0efa984be`，属于此前 A2，不能当作这轮新回复。可访问记录没有新纠错候选、新脚本批准或新 attempt。S 的 `resume_route_hint=supervisor` 与 active recovery 需共同解释，不能单凭 hint 推定从哪个节点恢复。

**CONFIRMED（指令内容）/ SUPPORTED（恢复风险）**：S `/task_state/typed_recovery_receipts/recovery-directive-1715bc823a58c5edff6e9e47-01/directive` 的 action 为 `retry_step`，instruction 开头是：

> Keep the existing scientific action (samtools faidx, bwa index, and a sequence dictionary) but correct the structured effect/binding emission

其 envelope 是 `PATCH_SCRIPT / PRIMARY / INLINE / FORMAL_OUTPUTS`。旧 A2 `approved.sh` 包含 `cp -f`、faidx、bwa index 和字典生成，无已有结果复用分支。故原样恢复很可能重新建索引；尚无新脚本，不能断言必然执行。未来须正式替换/收敛这条已接纳指令及对应 generation 授权为只读核验、补证据，不能期待附加提示自动改掉旧动作。

**UNKNOWN（宿主暂停状态）**：本次 ps 仅可见 sandbox PID namespace（PID 1 为 codex-linux-sandbox），无法核对宿主 executor 1631706、A2 记录 PID/PGID 3439882、启动信息或子进程。所读状态和 trace 未找到正式暂停/取消/checkpoint 回执；running 字段不证明仍运行，也不证明安全暂停。没有唤醒任务。首个 attempt 的 Killed/137 仅能确认日志现象，OOM/发送信号者仍 UNKNOWN；它不是最新完成门拒绝的直接原因。

## B. 重复规范化的具体触发条件

**CONFIRMED（静态因果链，既有 profile 支持）**：

1. `plan_tracker.py:7831` 保存 original_steps；`7924` 附近 `_backfill_step_contracts` 原地规范化。
2. 原 session 和 P 中现存副本均为 `compatibility.source_schema_version=10`，每个 step 持久含 `semantic_claims=[]`、`semantic_constraint_ids=[]`、`semantic_claims_validation={valid:true,errors:[]}`。
3. `7930–7937` 在 semantic source >= PLAN_SCHEMA_VERSION 时删除这些字段（也删除存在时的 invalid_raw）；`7946` 将删除后的 steps 与 original_steps 比较，因此第二次仍追加 `normalize_plan_step_schema` 到 **newly_applied**。`migrations_applied` 已含该名称并不抑制返回列表。
4. 紧接着 `7949` 的 `ensure_provenance_state` → `1864` `_provenance_artifact_definitions` → `1703/1803` 对原 step 调用 `normalize_plan_step` → `5746–5752` 把三个默认字段原地加回。比较点看到的是瞬时删除，实际持久化前又恢复。
5. `load_plan_state:10616` 调迁移，`10720–10722` 因 migrations 非空调 `_atomic_write_plan_state`；后者 `11701` 无条件 append_event。无 task/plan/approval 变化时事件类型为 `runtime_event`，不是单独的 migration 类型，不能误报审计事件 schema。

旧 `P/load.prof` 直接读取结果：两次 load、两次 migrate、两次 verify_chain；normalize 的 caller 包括 `_provenance_artifact_definitions`（128 次），证明既有加载路径实际经过该函数，非猜测。附件所述第二次持久内容仅 audit_index 变化与此吻合；P 没有两份独立 before/after 快照，故“全部业务内容逐字段零差异”仍是**复用的历史结论 SUPPORTED**，本轮未重新加载证明。三个字段删后加回则由源码和现存副本确认。

**最小修改建议，未实施**：先在 provenance 两处只读投影使用深拷贝 step，阻止其重建已退休字段；与迁移的 schema 分支保持一致，让真实旧字段清理只持久一次。再将规范化变化判定置于该阶段最终业务结果稳定之后，在 audit_index/事件生成之前判断真实差异；不要仅凭 transient changed 决定回写。不将所有 migrations 置 false，不按 generation 过滤事件，不大范围忽略字段。需保留其他真实迁移和独立批准/诊断/证据写入。

## C. 可合并读取与必须保留的刷新

**CONFIRMED（直接源码调用）；收益未量化**。仅跟踪 Generate 上下文/候选路径：

| 边界 | 最小建议 |
|---|---|
| `bio_agents.py:13784` `_validate_generate_context` 已 load，随后 `_task_revision_for_state:7081` → `_task_spec_for_state:7069` 再 load | 从同一已读 canonical task 取 revision，传只读快照；校验本身可能规范化，需隔离拷贝。 |
| `41197` 已 load，`41198` `_task_spec_for_state` 再 load | 同次上下文装配复用 canonical_task_spec_for_state(generate_plan_state)。 |
| `44436` 已 load，`44458` 附近调用 `_bind_pending_action(persist=False)` 未传 plan_state，后者 `12430` 再 load | 使用其现有 plan_state 参数，传局部拷贝给候选绑定，保留后续正式提交 CAS。 |
| `_generate_task_spec_projection:6951` → task helper、自身 load、`_renderer_action_protocol:11525` load | 若进入兼容/压缩分支，可传同一快照；未证明该分支在目标窗口实际触发，不计入历史收益。 |

必须保留：`41197→41238` 中间有 skill access 持久化，不能简单删后一次；`42462` transport 当前授权核对；模型返回后的 `42906` recovery scope/claim 核对；`42964` 附近 claim 保存后 `43051` 新鲜授权检查；`_commit_bound_action_candidate:12375`、审批后/Execute 前检查及 CAS 冲突后的重新读取。`_skill_context_projection:7527` 本来就接收快照，没有内部 load，不列为重复。

单次完整加载：旧 profile 为 2 loads / 2 verify_chain，不支持“每次重复跑两条全链”。`state_contract_store.py:280–300` 遍历链后，`305–311` 的 latest_by_type 又 read_event 已走过的少数事件，可在**同次校验内**复用已验证事件，同时保留类型与成员检查；不是跨加载缓存。conversation evidence 是另一条证据读取，不能算成重复校验同一链。

附件的 4分26秒/28.5秒/3分10秒作为旧统计保留边界：本次实际 A2 Generate manifest 时间是 08:35:32.393607Z→08:35:50.046361Z（约17.65秒，provider elapsed=17.49），action created=16:39:00。故不能把附件 28.5秒作为这份 manifest 的已核实请求耗时。原目录单次只读校验8.11秒是旧测量；禁止事件数乘8.11作历史归因。未来可在读取、规范化、校验、锁等待、写入/fsync 周围用普通轻量日志计时，不经状态审计链记录自身耗时；保留全链、锁、CAS、fsync，不设计 TTL/全局缓存。

## D. 正确身份实际送达，但输出模板仍有缺口

**CONFIRMED**：A2 对应 `context_trace/generate-0017-35f0efa984be/{request,response,manifest}.json`，manifest plan_revision=1，binding step 为 step-03-reference-indexes。request `/messages/1/content` 是 JSON 字符串；解码后：

- `/node_context/llm_payload/current_focus/current_step/step_effect/produces_artifacts/0` = `plan-artifact-7df4b9108825def8db5b2bcf`。
- 同 step_effect `/explicit_side_effects/0/identity` = `create_reference_indexes`，required_fields 为 `reference_resource_ref,index_artifacts`。
- `/node_context/llm_payload/current_focus/runtime/current_action_contract/step_effect` 重复上述正确分类；`/node_context/llm_payload/current_focus/runtime/current_action_artifact_bindings/1/expected_path` 为 `R/plan_artifacts/plan-artifact-7df4b9108825def8db5b2bcf`。
- `/node_context/observation_emission_contract` 有 prefix，但 approved_observation_ids/examples 都为空。正确身份和声明已提供；不能说完整 artifact/side-effect 发布示例也已提供。

批准定义对照：manifest binding approval_ref 指向 `objects/4f653349…601c.json` 的 approval_snapshot；approval_id=`approval-a9ba01f4b74936b72419`、revision=1。`objects/b97f9c49…8672.json` 同一 approval_id 钉住 `objects/b1479ab3…bc58.json` 的不可变 plan snapshot；该 snapshot 第三步、实际 request current_step 和 S `/provenance_state/attempts/A2/declared_step_effect` 的上述身份/字段一致。因此**该步效果定义一致 CONFIRMED**；binding 的可变 plan semantic hash 与批准 hash 不同，不能把两种对象 hash 宣称相同，也未证明整个历史上下文逐字同版。现有证据不支持“身份错误由批准版本切换造成”。

错误最早已见于该 call `/response.json#/content` 的 shell 原文，延续至 A2 `approved.sh:121` 和 `stdout.log:19`：顶层 `identity="create_reference_indexes"`，无 canonical 分组。`step_effects.py:3025–3060` 对未标 kind 的顶层 identity 按字段顺序投影为 produces_artifacts；`3168` 拒绝未声明身份。S `/provenance_state/attempts/A2/effect_evidence/parser_receipt` 确认 actual=副作用名、expected=正确产物名。本轮文本对比确认 response 的 shell 正文与 approved.sh 去首尾空白后完全一致，不是批准后适配器新造了错误机器身份；request `/messages/6/content` 还带着首次失败脚本同样的错误排放，构成重复错误的上下文来源（因果权重未量化）。

**最小 PATCH-E 方向，未实施**：复用 `load_approved_plan_snapshot(state=已核对快照)` 和 `canonical_step_effect` 的声明，扩展现有 emission contract/模板接口，分别逐字填充 `produces_artifacts` 与 `explicit_side_effects`，用真实路径/检查结果填值。当前公开 helper `observation_emission_contract/emit_observation:3108/3130` 只支持 observation，没有查到通用 artifact+side-effect emitter；不能声称已有全套模板。不要把副作用名映射成第一个 artifact，不放宽 parser，也不以 rc=0 代替 CommitStep。

## E. 复用已有索引：可检查，尚非现成补证据闭环

**CONFIRMED（记录）**：A2 approved.sh 指向批准 bundle 和原 reference，exit_status=0；stderr 给出 BWA 6.945秒。stdout 记录 fai=68B、dict=501B、amb=426B、ann=73B、bwt=16000072B、pac=4000002B、sa=8000056B。执行记录可关联 attempt、script、批准输出路径。**SUPPORTED（文件来源）**：同一脚本的复制/建索引及非空检查支持这些文件当时由 A2 生成；本轮未检查科学文件内容。**UNKNOWN**：当前完整性、参考原件与副本一致性、contig/序列对应、各索引有效性、是否被后来改动；日志非空和 rc=0 不能证明科学有效。

现成链：`workflow/inspection_evidence.py:191/293/448` 发布/读取真实执行、parser、validation 等不可变正文；`workflow/recovery_tools.py:600/647` artifact.inspect 只读检查已授权文件并在上限内算 hash，但目录返回 is_file=false/size=0，不递归核验本 bundle 的7个组成文件；artifact.read 的采样也不等于索引有效性。现有历史实现报告 `docs/implementation/inspection_evidence_terminal_20260907/implementation_report.md` 支持这些读取边界，不沿用它的旧测试作为本次 PASS。

**SUPPORTED（最小接口缺口）**：`completion_gate.py:600–638` 消费与成功/可完成 attempt 关联的正式 effect sources；只读 inspection observations 不会自动补出 create_reference_indexes 的正式效果。需要把“本次只读核验观察”作为新 producer 记录，明确引用 A2、批准 snapshot、输入和文件指纹，再通过现有正式证据/验证/CommitStep 链接纳；本次定点范围未找到可直接完成此跨来源补证据事务的现成入口。若用新的检查 attempt，必须显式标记核验既有产物、保留原创建 attempt 来源，并解决其与当前 prepare_reference capability/FORMAL_OUTPUTS 的合法绑定；不能默认 diagnostic probe 可完成主步骤。

后续获准时优先核验既有结果，必要时补组成文件授权/检查覆盖及上述正式来源接缝；本轮不执行检查、不声称可直接续跑。原失败 receipt/事件/stdout 保留，新增证据不可伪称原脚本已正确输出。字典 fallback 和原件/副本路径一致性需纳入核验，不能靠修改历史记录消除。

## 交付状态

五组静态/既有证据补审计完成，UNKNOWN 均保留；PATCH-L（幂等性与有限读取合并）、PATCH-E（正式模板与复核证据接缝）仅建议。生产修复 **NOT IMPLEMENTED**；新测试/微基准/性能验证 **NOT RUN**；真实模型/API纠错 **NOT RUN**；任务 **NOT RESUMED**；当前 run 完整 WGS **未完成**。只提交本目录报告与最小证据索引，不 push，不改历史 session、索引、生产代码或既有修改。
