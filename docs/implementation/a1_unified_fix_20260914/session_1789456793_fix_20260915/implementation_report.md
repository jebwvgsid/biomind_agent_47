# 连续故障修复回执

旧坏候选在后续候选通过完整准入、真实 action 绑定并持久化的同一 CAS 中退出当前纠错来源；旧 rejection、计数、receipt 与预算全部留存。Recovery 后产生的新拒收成为当前 previous_candidate，保留已接纳 Recovery 指令中的配置、布局、日志与真实完成条件，不再拿已解决的 A 约束 C。

建库外层 rc0、内层 255、predictor 缺失仍不能通过 attester/gate/Commit。失败与 STOP 精确归属当前 build，包括 verification_pending；已 Commit 的 contig/prepare 保留，未执行 annotation/summary/finalize 保持 pending。未执行候选的 attempt 明确为空，已有建库 attempt 只作验证过的因果证据；匹配还核对当前候选记录、原 scope、审批权威、新 attempt、取消及版本，空 attempt 不表示通配。正式 writer 已在 G2/G3 写出三份报告并保持重复收尾幂等。

## 生产修改与关闭证据

| 工作包 | 已实现及实际见证 |
|---|---|
| A 纠错生命周期 | candidate_repair 的 resolved_by 只撤销当前纠错权威；Generate action admission CAS 与正常/Recovery 共用。G1/G2/G3 实际第三次 build 请求无 A repair，第四次精确引用 C；A 账本仍有 ordinal1/remaining3。 |
| B 载体及 marker | EffectHelperError 投影 declared/real carrier、source、marker 数及行号/作用域、template available/SHA。UNKNOWN 声明、纯 Python、Bash heredoc、native argv-only、双 marker、无 marker、模板不可用及冲突组件通过；缺声明与双 marker 的实际下一请求通过。无 marker 的合法独立代码保持合法，不引入字符串猜测。 |
| C failure log | 仅所选 snpEff build 的 frozen predictor 后置条件定位同目录 build.log；可靠 attester 快照、授权读取、无 symlink、哈希一致、1MiB 上限。持久 failure_log 对象经 evidence.read 正式分页；先用 step_inspect 错工具得到 KIND_MISMATCH/正确工具，再读取第二页的 Command ann / Unknown option -gtf22。跨任务/步骤/attempt、伪造 ref、文件改写、路径逃逸及 symlink 均拒。日志不是成功事实。 |
| D 专用构造及配置 | 既有 build 构造本来已有正确顺序；本轮统一显式 snpeff_argv，renderer 实际调用并补显式 ann，保留独立 -version。launcher/JAR 两形态的 build/ann 参数序通过；G1实际受控 Python 小进程 argv 由该生产构造函数产生。Skill 投影顺序与 Recovery 日志判读，不改写自由代码。prepare 的独立 config 产物与 mutable database 是不同消费对象，保留正式资源引用，未增加重复填写字段。 |
| E 精确步骤 | record_preaction_contract_failure 接收精确 step_id/expected_scope，缺失/跨步/陈旧/完成状态拒绝；不扫描“第一个可失败步骤”。重复登记不改写原记录；图中 build 正确 blocked/failed，annotation 无 attempt 且 pending。 |
| F STOP/report | 复用既有 candidate/claim/generation/provenance 身份计算候选匹配；原 verification attempt 单独保留 causal_scope。G2/G3 真正 END→CAS→writer。stage_status 用 process_returned_zero 表示外层 rc，正式 committed=False 不被掩盖；旧 scope/新候选/新 attempt/取消仍不能发布。 |

## 连续图

| 图 | 实際候选、审批/执行、Commit、预算与收尾 |
|---|---|
| G1 | A 双 marker拒收 → B Python argv-only接纳、重新审批、执行(外0/内255/无predictor) → 真实Recovery日志工具/分页/retry_step → C不同双marker拒收 → D保留Recovery检查、重新审批成功。正式六种操作按依赖执行，7次执行/7次代码审批、6次Commit，任务completed。build修正账本remaining 3→2，未重置。 |
| G2 | 相同 A/B/Recovery/C 后模型正式blocked。6次Generate、4次Recovery叶子调用（错工具/两页读取/指令），3次执行、2次Commit。build blocked；后三步pending。当前候选attempt为空、原因果build attempt独立；report.md/stage_status.tsv/reproduce.sh均created。重复finalize不新增provider、不改报告字节。 |
| G3 真重复 | C被精确重复，现有无进展保护有界停止；3次执行、2次Commit，build failed、后三步pending；预算仍余2，三份真实报告created。不同C首次出现没有被误杀。 |
| G3 最后机会/耗尽 | 新完整Plan第一步依次修 env数组→非字符串值→双marker→语法，原limit4的remaining为3/2/1/0。最后合法响应被接纳并走完六操作/6Commit；最后仍非法则0执行/0Commit，正式BUDGET_EXHAUSTED并生成报告。 |
| 附加缺载体图 | 第一候选缺声明，实际下一请求带 UNKNOWN/marker1/templateavailable；合法纠正后串联同一A/B/Recovery/C/D与六操作完成。 |

固定provider只替模型叶子，科学能力是新Python替身。上述成功不证明真实模型能稳定生成代码，也不证明真实snpEff建库/ANN有效。小进程真实执行并由真实attester读取其新输出；没有预填步骤approved/ready/succeeded或注入观测。

## 验收分层与版本

180 个唯一 pytest ID PASS（其中 PTY child 由真实专用父进程运行；普通 pytest 的该入口 skip 没有计绿）；另列7场内核 PTY/plain矩阵。组件104、协议/取消/AskUser/hook22、V2组件37，以及新连续图6、旧审批图3、False/partial/报告冲突3、终态摘要异常/取消2、V2通用正常/恢复2；同一PTY child ID去重为1。新图、后段保护、PTY与V2图使用 current07；前述组件在 current06，所有生产/Skill及其测试字节与 current07 完全相同，仅新图选择真实构造函数与加载记录器两测试文件不同，[版本账本](version_ledger.json)给逐文件证据。

最终460文件 source signature：`e4052102ae2e605901594cb0269836ed77510d20f05d17b80449663f1accde6b`。实际加载函数/路径/SHA见各run loaded_manifest.json；图中专用renderer callable亦被记录。BASE `adf299e661a4996e6405eefe815b22d99bec0562`＋保护overlay。裸commit独立部署未验证，原V2 protected planner_context 的PARTIAL提交边界仍保留。

修复前 captured-before witness 成功复现缺陷（测试本身应FAIL）：剩3提前no-progress、build verification_pending、未执行annotation failed、MANAGED_STOP_STALE_SCOPE、writer未进入。此witness不能计FIX PASS。

开发失败均留在verification/development：早期合成Plan/载体声明/漏读输入不合法、Bash-only测试执行器不支持Python argv、局部target变量覆盖已修；长图组合触发外部360s watchdog、并发PTY触发70s父限时（rc=-15）均保留，改为同字节独立/顺序验收后通过，生产预算不变。旧A1缺scope与V2 WGS版本断言失败不删除、不借本轮PASS抹去。旧直接shell执行测试未绕过现有guard运行，所选生产constructor +实际受控小进程另验。

## 第13节回执

```text
IMPLEMENTATION_STATUS: COMPLETE（本单限定框架机制）
BASE_HEAD_AND_PROTECTED_OVERLAY: adf299e661a4996e6405eefe815b22d99bec0562 + protection.json
PRODUCTION_COMMITS: 2ee4397d20006c1b427ef4904265be18d13bbbc6
AUDIT_ARTIFACT_COMMITS: 3bfab57ea5dd2a041aac151e854e046f46c89f09；原始字节校正/最终回执提交另见交付hash
FINAL_TEST_SOURCE_SIGNATURE: e4052102ae2e605901594cb0269836ed77510d20f05d17b80449663f1accde6b
REPAIR_REFERENCE_LIFECYCLE_RESULT: FIX PASS；退场但不删账/不重置
RECOVERY_DIRECTIVE_AND_ACTUAL_MESSAGES_RESULT: PASS；实际第三/四次build请求及工具回执
BUDGET_LAST_CHANCE_RELOAD_IDEMPOTENCY_RESULT: PASS；limit4保留；3/2/1/0；独立重载/去重/最后合法机会
CARRIER_MARKER_DIAGNOSTICS_RESULT: PASS；组件矩阵+真实下一请求+完整链
FAILURE_LOG_REF_READ_AND_PAGINATION_RESULT: PASS；正式ref/错工具反馈/分页/作用域反例
SNPEFF_COMMAND_CONSTRUCTION_PATH_AND_LIMIT: 显式所选constructor机制PASS；自由生成/真实命令NOT_RUN
EXPLICIT_CONFIG_INTERFACE_OR_DUPLICATE_RESULT: 独立config产物引用保留；不同于mutable workspace；未加重复源
EXACT_FAILURE_STEP_OWNERSHIP_RESULT: FIX PASS；build终态，annotation未执行/pending
STOP_CANDIDATE_VS_CAUSAL_ATTEMPT_RESULT: FIX PASS；空候选attempt不通配；因果attempt核验
TERMINAL_WRITER_AND_STAGE_STATUS_RESULT: FIX PASS；真实created、冲突保护及幂等
G1_CORRECT_RECOVERY_CONTINUES_FULL_GRAPH: FIX PASS；7执行/6Commit
G2_CURRENT_STOP_WITH_CAUSAL_ATTEMPT_REPORT_GRAPH: FIX PASS；3执行/2Commit/三报告
G3_TRUE_NOPROGRESS_OR_BUDGET_STOP_GRAPH: FIX PASS；真重复与预算末次合法/非法均有图
FALSE_SUCCESS_AUTHORITY_STALE_APPROVAL_CANCEL_NEGATIVES: PASS（对应范围见verification）
V2_AND_AFFECTED_F1_F6_REGRESSIONS: PASS（受影响字节及列明用例）
EXISTING_FAILURES_AND_DEVELOPMENT_FAILURES: 保留上述旧账本与development；不计成功
ORIGINAL_SESSION_MODIFIED_OR_RESUMED: NO
REAL_MODEL_API: NOT_RUN / NOT_AUTHORIZED
REAL_SNPEFF_CORRECTED_COMMAND: NOT_RUN / NOT_AUTHORIZED
SCIENTIFIC_E2E: NOT_RUN / NOT_AUTHORIZED
ORIGINAL_F1_F6_AND_V2: 保持原实施事实与原限定机制范围
PROTECTED_WORKTREE_AND_INDEX_PRESERVED: YES；独立index、HEAD CAS、仅同步拥有文件
PUSH: NOT_PERFORMED
REMAINING_LIMITS: 非全项目审计/非裸commit部署/非真实科学成功；日志上限及固定路径规则明确
```

净生产/测试提交为9个生产或Skill文件＋3个新测试文件，925增行/32删行。相交managed_stop两处旧文案hunk留在工作树，其余安全dirty哈希与其他index逐条相同。材料仅本分册和原主单两入口追加；原事故审计目录保持untracked未收编。大块JSON/终端回放的收集副本按[压缩索引](compressed_artifacts.json)无损gzip，原隔离结果未覆盖；实际审批脚本、wrapper、exit、执行回执、状态与报告存于evidence的preserved目录。

材料提交完整性：初始材料提交 `3bfab57ea5dd2a041aac151e854e046f46c89f09` 的三份未压缩plain PTY流受到Git换行归一化；后续材料提交通过局部 `.gitattributes` 保留原始字节并重存原回执，不改测试源或隔离结果。最终逐blob核对见 `artifact_integrity.json` 和交付回执。
