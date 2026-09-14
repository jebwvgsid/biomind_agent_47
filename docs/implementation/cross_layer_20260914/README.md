# 跨层故障链统一实施回执

本轮从 `session_1789312388_failure_chain` 已完成审计进入生产修复。原审计 T01–T18 的 witness 保留在历史中；本回执的 FIX 结果来自新建隔离任务及修复候选，没有恢复事故 session。

## 状态与版本

- IMPLEMENTATION STATUS：COMPLETE
- REPOSITORY：`/public/home/liusiqi/03.毕设/Biomind_v1`
- BRANCH：`refactor/consultant-controller-authority`
- IMPLEMENTATION_BASE：`e09b39f8df0afdd87321464e17e7e2c8f3b2c67d`
- FINAL_CODE_HEAD：`a500d2cacb37bc11636271aa9e24a132fb977df5`；本报告独立提交，其完整 hash 见交付消息。
- FINAL_TESTED_TREE：`052a3d1c0f254a2af7710b90b05da3ce40ffcbc2`
- AUDIT_BASE_REFERENCE：`60db1e4b8961d9eed7d36c312e79a75885e33dfa`；审计测试 `5f89f1d633ce02d6cac5ac0b13e285168462ac9a`。未 checkout 旧树。

| 状态项 | 本轮结果 |
|---|---|
| PRODUCTION_CODE_STATUS | COMPLETE：S1、S2、S3及领域增量已实现；范围限定于公共机制 |
| OFFLINE_MECHANISM_ACCEPTANCE | PASS（20组全部正常退出；保留开发失败与历史限制）；见 verification.json 和 F01-F38.md |
| CURRENT_CANDIDATE_LOAD_VERIFIED | 裸候选、五个显式生产 overlay 的新进程模块/Skill hash 分别登记 |
| BARE_COMMIT_REPRODUCIBLE | 裸生产源码验收；五份既有安全测试 fixture 单独登记，不冒充已提交源码 |
| REAL_ENVIRONMENT_CAPABILITY_VERIFIED | NOT_RUN |
| REAL_MODEL_BEHAVIOR_VERIFIED | NOT_RUN |
| SCIENTIFIC_END_TO_END_VERIFIED | NOT_RUN |
| ORIGINAL_SESSION_RESUMED | NO |

## 生产修改与提交

完整父提交和逐文件范围见 [commits.json](commits.json)。所有提交使用单独 index，仅接纳本轮文件/hunks；未 push。

| 切片 | Commit | 修改及验收 |
|---|---|---|
| S1 | `718adbcf7bb1537a7265ac4732bf15432d06e620` | 明确事实发布用途允许已声明子集；提供项仍严查身份/required_fields/path。冻结 helper 在审批前展开；普通与 Recovery 共用；合法 blocked 到真实 parser/Recovery，科学 Commit=0。 |
| S2 Reviewer | `ff7c2b4e82439655483c92eb58084282b4068a29` | 精确 schema 路径反馈；原层最多2次协议纠正；持久 assessment 请求回执排除临时包装；合法否决立即结束协议纠正；耗尽自动收尾。 |
| S2 Generate | `48f1fbe5b5c859f6762f2210fe999b75d65e53a2` | 候选 ledger 冻结同源总额；记录分类和完成元信息；有界独立 Python compile 子进程，不执行代码；安全输出策略投影。 |
| S3 | `5d32783141944a9320dec46e09b1dc72ecbeda61` | 当前未执行候选与已消费历史因果分离；共享报告发布 journal、原子不覆盖、丢回执的归属证明恢复；指定报告交付与明确授权的内部归档分开。 |
| 领域及共享调用方 | `889ca966b16741afe6c73b7662bc22f42ea4bd67` | 未决倍性经现有真实问询/用户证据确认；三个 Skill 文档；原生 Generate 同 helper；问询公开投影不泄露 answer_turn_ref；目录 reader 返回真实边界和已登记可用引用。 |
| S2 实际反馈收口 | `68bf745395ca2cce6ea0a91fe2981742985a079f` | 预算由已校验 receipt 投影到实际下一请求；语法错误不再经过产物路径提示改写。实际 wire 断言覆盖末次允许成功和耗尽。 |

S2反馈收口由最终请求复查发现：早期组合虽已通过执行/预算收口，但 wire 中缺预算且语法错误有错误路径建议。它们不是旧 witness PASS；新增实际消息断言失败后完成补丁并重新冻结全部最终验收树。失败轨迹保存在 failure-ledger.json。

S3冻结正文收口另提交 `a500d2cacb37bc11636271aa9e24a132fb977df5`（父 `68bf745395ca2cce6ea0a91fe2981742985a079f`）：将无发布回执显示为not_attempted，并写明冻结时点；当前终端仍使用最终真实回执。新增用例及最终组合检查正文，没有改归属、覆盖保护或业务路由。

## 边界与策略

**事实发布不授予科学完成权。** `emit_declared_effects(..., publication=True)` 仅由冻结模板选择；默认严格全量语义不变。缺产物时省略 artifact，不能伪造 path。空事实不输出假 observation；空列表与 missing/null 不等价；非法一项整次拒绝。publisher 不选路由，原 rc 来自执行 receipt。物理、完整性、provenance、CompletionGate 与 CommitStep 仍决定完成；result_resolution 的成功接纳保持严格。

**Reviewer**：`semantic_completion.REVIEWER_PROTOCOL_CORRECTIONS=2`，首次+2纠正，实际最多3次。持久 scope 来自当前 task/Plan版本/step/action/attempt、真实证据字节及批准义务；临时 report、inspection、state_generation 不另发额度。请求先预留再发送；重复/重载不增加请求，合法否决不“重试直到赞同”。真实新事实可以形成新 assessment。持续无效保留 rc0，自动停止；C′ 后显式重投 native revalidate 接纳后仍不再调 Reviewer。

**Generate**：`candidate_repair_policy.DEFAULT_CANDIDATE_REPAIRS=4`，现有 task budget 可提供同源策略，首次进入时冻结在当前 task revision / Plan revision / step 的既有 ledger。4指额外候选调用，首个失败候选不算额外调用；最后一个可用机会仍可以成功。representation / response_completion / static_syntax / candidate_contract 共用总额，invocation/inspection 不能重置。旧 ledger 的 recorded cap 不被默默升级。不同请求同文本照计并受无进展停止；同事件重投不扣两次。实际反馈明确当前序号与本次之后剩余额度，没有公开内部 receipt ID。

**输出策略**：安全 SDK 替身捕获使用真实已安装 ChatOpenAI 适配器。示例构造请求2048，bind覆盖为3072，实际传输3072；只代替最终 SDK transport，不接网。远端生效上限 UNKNOWN，不把 incident 的8192认定为本机源码设置。

**语法覆盖**：固定 Python compile 驱动，独立解释器 `-I -S`；有输入大小、时间、CPU/地址空间界限，不 eval/exec。覆盖完整 Python 与明确静态引号 heredoc 的 Python stdin；动态片段、R等未覆盖情况明确 unavailable/uncovered，不声称全面 Shell 语义验证。

**Recovery 因果**：只沿当前 generation → 已接纳 execution envelope → typed receipt → 已消费 packet → 实际 attempt 的同源引用读取。不是“取最后一个 consumed”。两个失败父节点能关联，其他 step/session 和输入评审不混入；active packet 不复活。候选终止时 attempt为空、executed=false，另列 causal attempt/rc/stderr，主执行事实不被预执行失败覆盖。summary使用当前冻结step，摘要模型有效或失败都不能改变原错误。

**报告**：三个指定文件各有真实 receipt 与 partial/failed/complete状态。不可覆写无归属旧文件；同内容/mtime/intent都不构成归属。已有 PlanState journal 固定 owner、授权引用、路径和body hash，写前持久化准备证明；真正发布用原子 hardlink。最终 receipt 丢失后，新进程只能凭已持久化 inode+hash+proof恢复；最终回执持久化后可安全退休proof。尚未写入的冻结正文显示not_attempted，明确其时点，最终交付以持久receipt为准，不把未尝试算成失败。并发两个进程只允许一个创建。内部诊断只使用既有批准的明确诊断目录输出，没有该授权不生成 fallback 路径。task内部存储位置不等于外部报告授权。

**领域调用方**：字典入口与后续 MarkDuplicates 能力分开；不自动换 aligner 或推断科学环境。原生 action 的明确 env 在实际批准/执行路径落实；科学能力空检查显示 UNKNOWN。未确认倍性只走已有 unresolved/ask_user，Plan批准不冒充生物学确认；收到“四倍体，明确使用4”后才形成相应候选。目录 inspect/read 都准确返回不支持，另列已登记且实际可打开的文件引用，文本可解析性仍 NOT_CHECKED，不自动切工具。

## 组合与调用计数

下表均为实际生产状态、审批、执行器、gate、Commit和文件存储接线；模型决策来自固定 fake provider。表中审批指脚本批准，不混入 Plan 展示/审批中断。检查进程和小 stub 是真实新进程；科学命令0。每条的 Consultant=1、Planner=1；F34单独为 Consultant=2、Planner=1、Generate=1、calling=0。

| 链 | Generate | Reviewer | Recovery | 脚本审批/Execute | rc | CommitStep | 结果 |
|---|---:|---:|---:|---:|---|---:|---|
| A′ | 2 | 2 | 1 | 2/2 | 0,1 | 1 | 输入只执行一次；后序 blocked；依赖步骤0；三报告独立写成 |
| A′ 最后评审机会 | 2 | 3 | 1 | 2/2 | 0,1 | 1 | 第三次合法评审仍可提交 |
| B′ | 4 | 1 | 1 | 2/2 | 7,0 | 1 | 格式/length纠正不新增Recovery |
| B′ 最后候选机会 | 6 | 1 | 1 | 2/2 | 7,0 | 1 | 第四个额外候选成功；包含静态语法拒收 |
| B″ 无内部授权 | 6 | 0 | 1 | 1/1 | 7 | 0 | 一旧报告保护，两新报告成功；指定partial、内部not_authorized |
| B″ 有内部授权 | 6 | 0 | 1 | 1/1 | 7 | 0 | 三旧文件保持，指定failed、内部3文件complete |
| C′ | 1 | 3 | 0自动＋1显式重投 | 1/1 | 0 | 0 | 再次Supervisor仍Reviewer=3；rc0未被改成执行失败 |
| D′ GATK / snpEff各自 | 1 | 1 | 0 | 1/1 | 0 | 1 | 真实registry、同publisher/parser/gate、正常成功 |
| D′ 合法否决 | 1 | 1 | 1 | 1/1 | 0 | 0 | 有效否决按原路由，无协议反复评审 |

B″ 的两个 summary 场景各调用现有摘要接口1次：无授权场景注入摘要失败、有授权场景合法摘要；重复stop不新增调用。均无新增旁白模型。Recovery/Reviewer/summary也均为fake，不是远端API调用。

## 验收证据与复跑

最终裸生产树173个不同用例通过：本轮新增正向及组合36项，既有边界回归137项。五个明确生产overlay另跑34项，均通过，重复项不加进173。20组exit code均为0；可复跑CLI入口另实际执行6个重复transport用例，返回0，同一导出字节。A′、B′、B″、C′、D′全通过。

- [verification.json](verification.json)：逐组实际argv、返回码、distinct nodeids、模式/树和计数；不累加重复用例。
- [F01-F38.md](F01-F38.md)：要求→测试→证据映射；科学未知项单列。
- [evidence.json](evidence.json)：新流程计数、精确反馈、候选/因果/交付投影、文件hash。
- `sources/`：最终裸树与overlay的export manifest、新进程加载身份；五份原有fixture分别hash登记。
- `execution/`：清理前保留的新批准/执行脚本、原始stdout/stderr、退出receipt、三个实际报告。没有原事故输入、原批准脚本或原数据库。
- [failure-ledger.json](failure-ledger.json)、`failures/`：开发失败、异常退出与未完成测试。没有改capture、跳断言或改清理来消除失败。`execution/**`的目录内Git whitespace属性仅保留原始脚本/日志/报告字节，不格式化证据、不影响测试断言或生产文件。

复跑单组（每次使用新 `/tmp` 根，生产解释器和已安装CLI依赖；保持双guard、fd capture、持久fatal）：

```bash
cd /public/home/liusiqi/03.毕设/Biomind_v1
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 \
  /public/home/liusiqi/anaconda3/envs/biomind/bin/python \
  docs/implementation/cross_layer_20260914/replay.py \
  --tree a500d2cacb37bc11636271aa9e24a132fb977df5 --group B-stop \
  --root /tmp/biomind-cross-layer-new-replay
```

`--group` 支持 core、AC、B-success、B-stop、D、last-veto、names、writer、gates、preflight-recovery、cli、approval-input、planner、protocol、transport；`--overlay` 只显式加载五个当前保护文件并记录digest。复跑时overlay若发生变化，必须按新manifest判读，不能沿用此回执。transport仅额外导出同一候选树的已提交安全测试文件。replay不加载业务session，不执行复制的reproduce.sh。

## 保留现场与限制

开工已有437项状态（36 modified、33 deleted、368 untracked）完整保留，原index为空。五个相交文件原增删行分别20/21/76/59/6行，与开工快照逐项相等；新生产hunks已独立提交。其原始及当前sha、提交sha见preservation.json；未清理、暂存或提交无关内容。

历史SIGSEGV根因仍UNKNOWN。本轮 `s2-generate-1-overlay-results` 在7项通过后、B_exhausted开始后异常退出（原启动回执 -11）。持久fatal文件只留下出口头，没有fatal栈；同树单例和后续完整组合通过不能宣称崩溃修复。另有S3旧inspection测试在一批组合之后输出捕获断言失败，实际文件/receipt已先通过；原capture单独运行2项通过。保留该顺序相关限制，不改业务清理/执行权威。

真实Picard/GATK/Java/samtools能力、真实倍性/物种/参考、历史loaded_revision及外部shim字节、远端8192来源、旧报告的准确owner、科学质量与吞吐均未核实。未做主机断电验证，fork故障注入只证明该协议边界。不安装依赖；本轮不重启/恢复用户任务。

REAL MODEL/API = 0；REAL SCIENTIFIC COMMANDS = 0；ORIGINAL SESSION RESUMED = NO；PUSH = NO。
