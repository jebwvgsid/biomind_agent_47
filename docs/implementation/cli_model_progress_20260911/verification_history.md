# CLI 实施：隔离集成验证记录

本文件仅记录本轮新增 exporter、离线 runner 及实际节点集成测试。最终验收结果以 `cli3-final148-exact` / `cli3-final148-tree` 为准；中间结果不累计成“最终全通过”。

## 隔离边界

- 显式白名单导出安全 `.py` 和一个既有 local-blast Skill 文档；未导出配置、环境文件、原 session、数据库、业务数据、启动脚本。
- 测试子进程只收到合成环境；原 config 被合成模块替代；不调用真实 provider；网络和非登记 subprocess 被拒绝。唯一执行脚本是新建 scratch 目录中、校验 SHA 后显式登记的无害脚本，其实际行为为检查新建输入文件存在并打印结构化观察回执。
- 文件打开和 SQLite guard 限定在导出源码、合成结果/scratch、已知解释器/依赖和必要系统库；`/proc` 被拒绝并触发现有指标 unknown 路径。系统目录祖先仅允许 O_DIRECTORY 遍历/fsync，不能由此读取原仓库文件。
- 使用真实 BioAnalystAgent 节点、实际 Recovery loop、原 Plan 合同与状态机；所有状态加载操作仅作用于新建合成 scratch store，不恢复任务或读取原 session。
- 实际 TTY 使用 prompt_toolkit Terminal/Application 的 writer/flush；CLI3 的业务 async loop 位于单独 runtime 线程。测试环境中的定时 callback 只服务受限环境的跨线程 Future 唤醒，不写伪时间、不手动确认 ticket、不延迟业务给人阅读。

## 中间失败和修复

1. 初建 runner guard 阻止自己新建的结果根目录，随后将该已知结果根加入允许集。
2. 下一次导入指标收集器尝试读取 `/proc/<pid>/stat`，guard 以 PermissionError 拒绝，使用原指标 unknown fallback，未准许原宿主进程读取。
3. 原 CAS 路径解析需要从 `/` 开始 O_DIRECTORY 遍历；只增加允许根祖先的目录描述符访问，仍不准许任意文件读取。
4. 隔离旧 GER graph smoke 随后通过，`integration-smoke4` 的实际图含 fake provider、tiny processes、Recovery 和 CommitStep，SOURCE_UNCHANGED True。
5. CLI1 初版显示测试发现 plain flush 的 deadline race，并出现 TTY 在受限环境跨线程通知下挂起；runner 以 180 秒上限终止。显示代理修复真实完成时间判定，并用 test-only callback 服务通知；未伪造 writer/flush。最终 CLI1 exact/candidate 各 34 passed。
6. CLI2 初版 exact 61 passed，而 candidate 60 passed / 1 failed。唯一失败来自既有旧 fixture 缺少 step effect 声明，触发原 PLAN_STEP_EFFECT_UNSPECIFIED 纠错；exact 中该旧测试恰有本轮之外 dirty 改动。只在本轮新 CP fixture 显式声明合成 observation，未纳入或修改旧测试。candidate 新 fixture 与新增原 parser 目标各通过；CLI3 最终全套重新覆盖此项。
7. CLI3 首次实际图及适配器回归 91 passed，后续追加真实冲突外层与 Skill 时序目标通过。review 再收紧 script envelope 不能包裹原生 Skill/solution；开发中的 `cli3-strict-exact` 导出恰已包含最终 inner+public+error 行为，测试尚保留临时“非法内层不公开”的预期，产生 4 passed / 2 failed，仅差 public 数量 6 vs 5。已对齐最终规则：已知唯一 inner 的原解析错误保持 inner 身份并可公开当轮说明；外层冲突/缺 payload 和已知 incomplete 不公开。最终结果不引用这个中间快照作为验收。

## 实际覆盖

- CLI1 Recovery：三次 fake provider 调用、两次实际 loop dispatch、当前公开文本去重；原 AI wire 和工具结果不因显示添加；plain/off/timeout/broken 的指令、调用账本、预算相同。
- CLI2 C/P：实际 Consultant inspect_resource 和 Planner Skill headings 工具；TTY/plain/off C2/P2/G1 完全相同；每个工具前对应 public 的 writer/flush 真实完成；工具结果和原 AI wire 在下一请求各一次；public 不进入 Plan 和审批记录。
- CLI3 主链：C2/P2/G2；第一次非法脚本包装触发原候选纠错；第二次原 payload 经真实语法检查、fake 审批、真实 tiny process、观察验证和 CommitStep。新 TTY/plain/off 与 legacy off 的 Plan semantic hash、获批脚本字节/hash、实际 stdout 原始回执/hash、一次审批/执行/commit 和一次纠错账本相同。
- CLI3 特殊入口：原生 Skill decision 顶层 public 在真实工具前 flush；把同一 Skill JSON 塞进 script payload 必须被原表示检查拒绝，不能派出额外 Skill 工具。合法完整 JSON 但 metadata 为 length 也只走原候选纠错；冲突外层内藏 shell/execute 标签不能落入任何 fallback 执行。
- 所有 fake 模型都只有原生产语义调用，不增加 narration 请求，不把展示段添加为独立 assistant 历史。

8. CLI3 首次完整最终快照 exact 145 passed / candidate 144 passed + 1 failed。唯一失败为本轮 T23 测试硬编码了工作树已有 dirty managed_stop 的中文展示标签，独立 candidate 保留旧格式。调用 one-shot/返回合同/真实图其余 144 项通过；仅放宽新测试到两者共用的状态及 terminal result 不变量，未修改旧生产 formatter。最终增量复验记录见后续候选结果。

9. 最后补真实 Bash 语法失败场景时，新测试首先假设 provider feedback 保留 `failure_stage`/`$.code`/`syntax_check`，实际原 script-artifact projection 会把所有已解析脚本的违例转成 artifact 字段提示，丢失部分原语法细节。该既有行为未修改。最终测试保留原反馈记录，断言实际仍传递的错误 code/repair stage/source；用真实 checker receipt 独立确认 Bash -n 返回非零且 execution_started=False，并比较新 envelope plain 与 legacy off 的完整反馈记录、业务账本和获批脚本字节相同。`cli3-syntax-final` 此目标通过。

语法反馈的范围补充：`exact_error` 实际保留了 Bash stderr、GENERATE_SCRIPT_SYNTAX_INVALID 和 script_body_validation，`contract_ref.execution_started=False` 也保留；信息损失仅涉及 exact_violations 的字段路径/附属 checker receipt 与错误地改成 artifact 的说明，并非整个语法诊断丢失。

## 最终单次验收

- 最终 exact 工作树白名单快照：148 passed，92.14 秒；source_signature `7edd37f7660d267320dc6c5a0dc3bc7cbdcd17152fcafe4c551931ee186aa0d9`。
- 最终候选树 `7029aa3f6cd4e96003142d6495382c54cc9f6c80` 白名单快照：148 passed，93.21 秒；source_signature `2ab6ba79fd9f1675474bdaadbc2edc0068b040705c676d0f1b3920335f07f48a`。
- 每个快照均导出 146 个文件，SOURCE_UNCHANGED True；测试后逐文件 SHA-256 与 manifest 二次核对，drift=[]。
- 单次 148 组成：public adapter/实际 prompt 80，完整性缓存 19，实际 Generate 图 7，原最终回答/summary 4，显示 15，Recovery/工具 15，主接线 6，实际 Recovery loop 1，实际 C/P 图 1。
- T23 新测试改为校验实际 terminal result 的 status/reason 值后在最终独立候选树通过，未改原 formatter；真实语法反馈新旧一致的目标也包含在两边最终 148 中。
- 完整时序和账本汇总：`cli3-final148-summary.json`，包括 20 个 exact/tree 实际图 case；每个原始结果根保留 `invocation.json`、`pytest-results.json`、`stdout.txt`、真实渲染 writer/flush 和 fake provider 请求/响应。
