# BioMind 定点运行时间优化实施回报 · 2026-09-11

IMPLEMENTATION STATUS：L1、L2、E1 已实施；离线结构验收通过。实测收益并非全部为正；原任务未恢复。E2 不在本轮范围。

## 版本与现场

- BRANCH：`refactor/consultant-controller-authority`。
- BASE HEAD：`0de3dfad80544c90112f929fa9e1e73f73c0bd8c`（上轮审计提交，源码对应既有 c975a681…）；没有切换分支。
- L1：`6ca2fea6ef4e8cf0a0dcce516d902c1b4e464f09`。
- L2：`75bf6e9245ab86006f8d9b41eb6eff9d0cda0b2a`。
- E1：`1655052862a9485d1356255367321cf923eaaa33`；transport 模板新鲜性补充：`8416fdb65995c0d662d7bb7bd25b39b54f18a157`。
- 性能 Before 对应 BASE；最终选用的 After 测量源码为 E1 提交 1655052…（其中 L1/L2 已完成），不是此前失败/被守卫判为源文件变化的那轮。随后增加的 transport 模板守卫单独做正确性测试，其完整调用成本未量化。
- FINAL HEAD 为包含本报告的交付提交，最终回复单列完整 hash，避免在自身正文嵌入不可成立的自引用 hash。
- PRE-EXISTING WORKTREE CHANGES PROTECTED：初始暂存区为空；104 条原有 porcelain 状态均保留，相交生产文件起初无差异。没有 reset/clean/stash，没有整体暂存。测试使用 HEAD 导出的安全源码并覆盖本次指定文件，不执行原工作区测试或配置启动逻辑；不把既有 dirty 测试混入提交。

## 实际修改

**PATCH-L1**：`_provenance_artifact_definitions` 两处深拷贝 step，只读调用不再污染 canonical step。首次回归发现直接删掉显式 artifact/dependency 回写会破坏历史来源关联，因此将这些已有 enrichment 作为单独返回的局部 updates，由 `ensure_provenance_state` 的显式变更边界应用；不把 normalizer 的旧语义字段写回。既有 artifact 身份、依赖和版本测试通过。这是保留原有字段更新，不新增语义 binder。

迁移在相关处理结束后比较最终内容及迁移元数据，load 的无变化抑制发生在 append_event 前；不全局抑制 `_atomic_write_plan_state`。真实清理只写一次，同名历史 migration 不会遮蔽新的实际修复。旧 schema 迁移仍保留原兼容分支；schema 10 的三项退休字段不会被 provenance 补回。批准、诊断、证据和 step 显式保存仍追加事件；不可变对象保持原内容。

**PATCH-L2**：主路径三个局部段合并读取，兼容投影也传入现有快照。校验和候选绑定收到隔离副本，正式提交仍读取最新状态并走 CAS。同次 `verify_chain` 只保留 latest_by_type 后续需要的事件正文；先在本次全链中确认成员，再检查类型，不再二次打开这些事件。断链/hash 错误、链外 latest、类型错、generation 错均拒绝；局部容器随调用销毁。

**PATCH-E1**：从现有 `load_approved_plan_snapshot(state=...)` 已获准快照按精确 step_id 取 canonical 声明，生成可直接放入批准脚本、仅依赖标准库 json 的 Python serializer。artifact 和 side-effect 分组、identity 固定；调用方必须提供按身份键控的实际 facts，artifact 要求实际 path，side-effect 要求批准字段；无事实、身份覆盖、未知身份/字段均拒绝。空声明不虚构输出或 observation。模板不是科学有效性证明，原 parser/证据投影/效果匹配函数保持不变，不由 rc=0 推进完成。

模板以稳定消息 ID 置于当前 inherited materials 首位，原失败排放明确仅供诊断。测试覆盖真实 episode 冻结、provider_messages 和 fake provider 接收，首次缺 ID 的失败已修复。transport 已有快照刷新之后再比较模板声明；不一致则拒绝旧模板，下一次按正常流程装配新版本；无额外 load，不引入新路由、审批循环或重试预算。

**轻量计时**：普通 `BioMind.Performance` logger + perf_counter_ns；覆盖状态读取/JSON、迁移、provenance、canonical projection、链校验、独立 conversation、锁等待、序列化、写/replace、文件/目录 fsync，以及 Generate context assembly、provider 调用、候选准备/绑定/提交、排放模板装配。已有 OutputRouter 显示真实阶段开始，普通日志记录完成/失败。日志失败不改变业务异常或结果，不写业务审计、不为计时额外加载/fsync。

时间口径：父子 span 不相加；context_assembly 截止 transport/cache 准备前，后续未覆盖间隔保持未归因；provider_wait 是 `_ainvoke_node_llm` 调用跨度（可能含既有 transport/retry 本地处理），精确 provider 等待仍以既有 provider manifest 为准。参数里的 TaskSpec 读取已移到 span 外。候选计时截止提交前，不吞并正式提交时间；早退没有对应 completed 记录，不能据此假报完成。没有新增轮询/全局/TTL 缓存。

## 读取与事件结构计数

| 调用段/实验 | Before | After | 边界 |
|---|---:|---:|---|
| `_validate_generate_context` | 2 loads | 1 load | 实际方法、隔离 canonical fixture |
| Generate 相邻 state/task 装配 | 2 loads | 1 load | 执行生产 AST 中原样相邻语句；不是复制实现 |
| `active_plan_state` → `_bind_pending_action(persist=False)` | 2 loads | 1 load | 直接调用图 + 执行实际调用表达式验证深拷贝；完整 binder 路径未单独计时 |
| 兼容 `_generate_task_spec_projection` | 3 loads | 1 load | Before 源码直接链；After 实际方法，现代 non-renderer 分支；不计历史收益 |
| 已接纳候选正式 CAS 提交片段 | 1 load | 1 load | 必要刷新保留；冲突测试为两次读取且第二次看到新 generation |
| 干净 fixture 连续10 loads | 0写 / 0新事件 | 0写 / 0新事件 | 持久 JSON、audit_index/head 不变 |
| 带旧默认字段 fixture 连续10 loads | 10写 / 10新事件 | 1写 / 1新事件 | After 唯一一次为真实清理；另测清理后10次为0/0 |
| 单次完整 load / verify_chain | 1 / 1 | 1 / 1 | 没有偷跳全链校验 |
| 128事件链，干净单次 load 的 read_event | 131 | 128 | 3个 latest 重复读取消除 |
| 带旧字段连续10 loads 的 read_event | 1365 | 1290 | 含真实 append 对链头的读取及不同增长后的链长度，不能当成同规模单次收益 |

保留刷新：skill access 持久化后；transport 前；模型返回后 recovery scope/claim；claim 保存后；批准后/Execute 前；正式候选提交与 CAS 冲突后。测试用 fake provider 改变 claim，实际 scope mismatch 函数识别新事实；CAS 测试注入并发保存，后次读取保留并发 evidence。两个合成 session 交错不串数据。审批快照更新/撤销及不可变对象读取由真实接口检查。

## 有限微基准

合成 fixture：16 steps、128 条有效事件链；干净 JSON 37149 bytes，带旧字段 JSON 38989 bytes。两版本每个单次样本从同一 seed 恢复到固定 `/tmp` 工作路径，恢复不计时；完整依赖均在该临时根目录，未复制/加载原 session。配对按相同 seed、相同规模，执行顺序为 Before 批次后 After 批次，非交错实验。普通文件系统缓存，无 cold-cache 宣称，无宿主缓存清理。

Python 3.12.12，pytest 9.1.1，langchain-core 1.2.7，langgraph 1.0.7；`/tmp` 为本机 ext4。无 profiler 参与下面计时；结构计数另一次执行，不把 mock/counter 开销混入耗时。每项5个样本，单位 ms，格式为 **中位数 [最小, 最大]**；原始样本、fixture SHA256 见 [benchmark.json](benchmark.json)。

| 指标 | Before | After |
|---|---:|---:|
| 干净起点单次 load | 13.908 [13.806, 18.089] | 16.390 [16.213, 59.662] |
| 带旧字段起点首次 load | 17.780 [17.671, 39.093] | 20.138 [19.945, 35.182] |
| Generate 局部入口校验 | 26.810 [26.697, 26.979] | 17.712 [17.570, 17.879] |
| Generate 局部 state + task 装配 | 30.524 [27.166, 33.186] | 57.527 [22.652, 96.252] |
| fake 返回后已接纳候选的 CAS 提交片段 | 29.844 [29.288, 72.124] | 36.107 [35.614, 85.985] |
| 局部 steps 深拷贝 | 0.893 [0.888, 0.944] | 0.793 [0.785, 0.843] |
| 候选 plan 深拷贝 | 2.040 [2.012, 2.072] | 1.832 [1.819, 1.849] |
| 100 个空性能日志 span | 1.429 [1.420, 37.158] | 1.401 [1.392, 22.656] |

结果：入口校验片段变快；干净单次 load、首次清理和 CAS 提交片段在此样本变慢。最终 state/task 装配片段也未测得改善，After 22.652–96.252ms 波动很大，原因 UNKNOWN；不选择此前较好的一轮替代最终结果。结构性删除重复读取已证明，但不能据此宣称所有阶段更快。新增最终内容比较与隔离复制存在成本，现有长链不会自动缩短。

连续增长实验的标准 JSON 序列化大小：Before 38989→40829 bytes、事件128→138；After 38989→37149 bytes、事件128→129。这与等规模单次 A/B 分开。复制成本为加载后局部对象，Before 含旧字段而 After 清理，不能当成同字节数复制算法比较。

100个空 span 的计时使用 INFO + NullHandler，含记录构造但不含实际终端/文件 handler I/O；生产日志 I/O 开销 UNKNOWN。原有 observation-only contract 中位数0.064ms，新 artifact/side-effect 模板装配0.539ms（5样本，代码2770字符）；这两者提供的功能不同，不能称作等功能提速。增加提示正文对真实 provider 延迟/纠错率的影响 UNKNOWN。

fake provider 验收/基准覆盖相关生产局部方法、消息冻结和 CAS 提交段。没有回放整个 `_generate_impl`/LangGraph 节点；完整“进入→请求准备好”和“模型返回→全部候选提交”的总耗时、真实收益、原运行收益均 **UNKNOWN**，没有用局部数字外推。

## 命令、结果与失败历史

隔离目录：`/tmp/biomind-runtime-opt-20260911/{before,candidate}`。可重现入口为已提交的 `tests/offline_runtime_evidence.py`；它安装安全审计守卫，禁止原工作区、启动/.env 读取、网络连接和外部命令。使用既有解释器，不安装依赖。

实际主要命令（历史运行还设置了 `HOME=/tmp`；后续新鲜性测试只保留 PATH）：

```bash
env -i PATH=/usr/bin:/bin HOME=/tmp /public/home/liusiqi/anaconda3/envs/biomind/bin/python /tmp/biomind-runtime-opt-20260911/candidate/tests/offline_runtime_evidence.py tests/test_runtime_optimization.py tests/test_execution_provenance.py tests/test_state_contract_audit_v2.py -q
# L1: 32 passed in 28.30s

env -i PATH=/usr/bin:/bin HOME=/tmp /public/home/liusiqi/anaconda3/envs/biomind/bin/python /tmp/biomind-runtime-opt-20260911/candidate/tests/offline_runtime_evidence.py tests/test_runtime_optimization.py tests/test_runtime_local_reads.py tests/test_state_contract_audit_v2.py -q
# L2: 24 passed in 32.44s；后续局部补强12 passed in 6.88s

env -i PATH=/usr/bin:/bin HOME=/tmp /public/home/liusiqi/anaconda3/envs/biomind/bin/python /tmp/biomind-runtime-opt-20260911/candidate/tests/offline_runtime_evidence.py tests/test_declared_effect_emission.py tests/test_runtime_optimization.py tests/test_runtime_local_reads.py tests/test_state_contract_audit_v2.py tests/test_execution_provenance.py -q
# E1 + 联合回归: 60 passed in 42.96s

env -i PATH=/usr/bin:/bin /public/home/liusiqi/anaconda3/envs/biomind/bin/python /tmp/biomind-runtime-opt-20260911/candidate/tests/offline_runtime_evidence.py tests/test_declared_effect_emission.py tests/test_runtime_local_reads.py -q
# transport 新鲜性补强：29 passed in 14.73s
```

基准命令同一 bootstrap，测试参数换成 `tests/runtime_optimization_benchmark.py -q`；环境依次设置 `OPT_BENCH_MODE=prepare`（candidate，只准备一次）、`before`（before）、`after`（candidate）、`template`（candidate）。每个最终基准运行1 passed，且 OFFLINE_CANDIDATE_UNCHANGED=True。运行日志路径、SHA256 和相关源码一致性见 [evidence.json](evidence.json)。

完整 pytest 失败正文及每次结论保存在 [validation-history.md](validation-history.md)，大份源码 inventory 仅留 /tmp 原日志：L1 首轮发现仍有显式投影回写，第二轮发现删 enrichment 破坏4项历史关联，均已修复；L2 首轮计时插入点缩进错误导致 AST/消费者检查失败，已修复；E1 真消息冻结发现缺 material ID，已修复；一轮基准期间测试支持文件变化，守卫明确报 UNCHANGED=False，该轮不作为最终验收/性能数据。没有隐藏失败或把旧测试通过当成新修复 PASS。

## 文件范围与明确未执行项

生产文件：`agent_core/plan_tracker.py`、`agent_core/phase_timing.py`、`agent_core/state_contract_store.py`、`bio_agents.py`、`agent_core/step_effects.py`。

新增测试/基准：`tests/test_runtime_optimization.py`、`tests/test_runtime_local_reads.py`、`tests/runtime_optimization_support.py`、`tests/runtime_optimization_benchmark.py`、`tests/test_declared_effect_emission.py`。交付文档仅本目录。每次提交均精确暂存、审阅 staged diff；禁用 git hooks，避免隐式额外命令。

REAL MODEL/API RUN: NOT RUN。REAL SCIENTIFIC TASK/WGS: NOT RUN。ORIGINAL SESSION: NOT RESUMED / NOT MODIFIED BY THIS WORK。HISTORICAL HOST PAUSE / LOADED REVISION: UNKNOWN。E2 FORMAL EVIDENCE REPAIR: NOT IMPLEMENTED。PUSH: NOT PERFORMED。没有读取/执行启动.sh、.env 或凭据文件，没有重建索引、删除历史事件或更改 Consultant/Controller 权限。
