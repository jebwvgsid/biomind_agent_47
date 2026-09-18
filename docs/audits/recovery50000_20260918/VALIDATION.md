# 验收范围与复现

使用 Python 3.12 `/public/home/liusiqi/anaconda3/envs/biomind/bin/python`。源码冻结在 `/tmp/biomind-r50k-20260918/`，最终累计源 manifest、精确调用、结果和必要回执永久导出到 `/public/home/liusiqi/03.毕设/biomind_audit_exports/recovery50000_20260918/`。每个累计组使用字节一致的独立副本，不共享测试产生的状态目录。A13 的最后补充仅增加一个独立测试文件；其生产/Skill/配置源码与该累计候选逐字节一致，新增文件及补验单独记入 final_source_test_overrides.json。全量生产/测试/Skill 配置源码 SHA 前后核对，未把测试产生的临时状态误算成源码变更。

## 命令与结果

逐组精确命令保存在 export 的 `final_evidence/<group>/invocation.sh`，目标 nodeid、完整 pytest 输出、逐用例结果及退出码同目录可查。通用调用形式为：

```bash
env TMPDIR="$TEST_ROOT" \
  BIOMIND_OFFLINE_RESULTS="$TEST_ROOT/results.json" \
  BIOMIND_CLI_PROGRESS_ARTIFACTS="$TEST_ROOT/cli-artifacts" \
  /public/home/liusiqi/anaconda3/envs/biomind/bin/python \
  "$CANDIDATE/tests/offline_managed_bootstrap.py" "$CANDIDATE" \
  -q -p no:cacheprovider <该组已记录的精确测试目标> --tb=short
```

`new` 组额外设置 `BIOMIND_REFERENCE_REAL_TOOLS=1`，只允许已登记的小型真实 helper 脚本。跨进程测试 prepare/resume 分别为全新 interpreter；两次使用同一明确的 restart store，pid 和 bootstrap root 不同；两进程累计仍只有三次物理请求，最多两次额外完整重交。

| 组 | pytest 结果 | 退出码 / 源码 |
|---|---|---|
| regression-graphs | 81 passed, 19 subtests passed in 101.11s (0:01:41) | 0 / unchanged |
| regression-runtime | 359 passed, 26 subtests passed in 90.54s (0:01:30) | 0 / unchanged |
| regression-recovery | 68 passed in 114.56s (0:01:54) | 0 / unchanged |
| new | 125 passed in 98.10s (0:01:38) | 0 / unchanged |
| plan-revision | 5 passed in 27.26s | 0 / unchanged |
| provider-terminal | 122 passed, 7 subtests passed in 151.57s (0:02:31) | 0 / unchanged |
| restart-prepare | 1 passed in 15.38s | 0 / unchanged |
| restart-resume | 1 passed in 14.45s | 0 / unchanged |
| local-repair | 1 passed in 16.45s | 0 / unchanged |

最终 9 组均 exit 0，无失败/跳过；共 815 条通过报告（含子测试和跨组重复），按 nodeid 去重为 755。这个计数不代表全仓所有测试。

P2–P5 首切片独立验收为 130 passed、1 skipped（该次未 opt-in 真实工具）；最终 `new` 组明确 opt-in，不把该 skip 当真实执行通过。P1 切片及累计验收完整结果见上表和 `COMMITS.json` 对应证据。

## 隔离与替身边界

- bootstrap 注入明确的合成配置，禁止网络、凭据/config 实际加载、主项目运行时读取和共享写入。单独解析/哈希源码配置不执行配置导入。没有真实模型 API 调用。
- 自动环境链回归保留候选发现、受控检查、新事实进入实际请求、审批与 runner 消费；检查叶子有明确替身，另验 bwrap 失败仍为 unverified。未把手工绑定的真实小参考工具测试当自动发现证明。
- Recovery 模型/摘要器叶子为受控返回，中间的预算、实际 Bio adapter、ToolDispatcher、引用读取、分段/多层合并、CAS 激活、持久状态、接纳和图路由为生产代码。压力测试的 usage 是明确合成计量，用于验证既有资源预留结算，不冒充 provider telemetry。
- 原生 SDK 测试使用实际安装的 langchain/OpenAI SDK 和生产 LLM factory，dummy key、offline URL，不发送 HTTP。DeepSeek wire JSON 的 `max_tokens=50000` 与原生 reasoning 回传已验证；线上接受能力为 NOT_RUN。其他 provider/Responses 的既有字段合同也回归。
- 真实基础设施测试用 384 bp、两 contig、中文/空格/%/# 路径，运行实际 BWA、samtools、Picard、Java。生产 Skill invocation、immutable runner、逐命令 journal、parser/Gate/Commit 保留；审批/Reviewer 是明示替身。未修改共享环境或安装工具，未运行原 benchmark 科学任务。
- 发布竞争、SIGKILL 风格返回码、Picard rc=1、发布边界 KeyboardInterrupt、坏 checkpoint、半 JSON/工具参数、超长响应均为可控故障注入。它们不证明主机真实 OOM、真实服务端 truncation 或真实掉电恢复。
- Plan B2 是冻结必需目录拒收与真实修订/审批机制的组合覆盖；不是声称原 GWAS 错误 Plan 已自主改正。未知 issue 到 Generate payload 与未知 issue 到终态是两个代表分支；另有真实 parser→Supervisor→因果记录→终态整链，不夸称所有未知错误都有同一完整图重放。

## 测试中遇到的问题及处理

第一轮将五组测试同时放在共享冻结副本运行：三组进程 exit -11，发生在异步全线程堆栈 dump 定时附近，原因未确证，不能作为通过结果。provider 组另有两个短时限用例在此负载下失败。部分旧测试在副本内生成 task_outputs，导致外层“任意新文件等于源码改变”的判定过宽。后续改用每组独立副本、串行重跑、核对完整固定源码清单；测试 bootstrap 的堆栈 dump 改为显式 opt-in。未放宽生产 timeout、取消、预算或必需终态。

P1 首次切片复制时误用全目录名排除规则，漏拷贝九个旧 S4 历史 fixture 数据文件，历史 seed 用例因此没有持久状态。补齐原字节 fixture 后重新执行；未修改历史任务或降低 seed 校验。该用例是基础设施回归，fixture 没有用于 GWAS/WGS 结果。

旧测试的完整 provider 回复缺 finish_reason，本轮按新必要终态合同补齐替身。六项 provider 旧事件/UI 断言及八项终态旧断言在起始 baseline 复现；修正过时测试期望，保留真实 DisplayModel 去重、完整详情、当前步 blocked/零 Commit 和可信复现脚本要求。非期限测试默认使用 2 秒并为每个代理分配独立临时目录；十项直接 watchdog/取消短时限参数保持不变。六项带完整 Bio 准备的故意停顿用例显式使用 0.2 秒首响应/idle 与 2 秒总准备窗口；backoff 用例先确认一次发送和进入等待，再验证同一 2 秒绝对期限取消 5 秒等待、无第二次发送，取消分支仍单独验证。真实未完成回复 UNKNOWN 仍拒收。

另一个额外选择的旧脚本块用例在原 baseline 同样先因错误选到 generic capability 而未到 Generate；复用现有 configure_protocol 后还暴露其旧 runner 替身不接收 display_context。没有改这个大类或生产来迎合旧替身；新窄集成用例使用现有正确前置配置与明确外部叶子，验证真实原错误→下一次 provider 修正→审批→执行边界，故障路径不进入 Recovery。旧失败和原基线对照均保留。

未声称全仓所有测试通过。下列五项此前隔离验证出现的无关既有失败，在原 baseline 复现，本轮未改生产绕过；精确对照见 export `support/diagnostics_delivery.json`：

1. `test_step_effect_contract.py::test_real_skill_operation_contract_is_the_generic_compiler_source`
2. `test_step_effect_runtime.py::RuntimeEffectEvidenceTests::test_declared_effect_without_evidence_and_typed_noop_fail_closed`
3. `test_step_effect_runtime.py::RuntimeEffectEvidenceTests::test_effect_evidence_from_a_foreign_attempt_is_ignored`
4. `test_step_effect_runtime.py::RuntimeEffectEvidenceTests::test_plain_stdout_and_success_status_do_not_fabricate_observation`
5. `test_cross_layer_delivery.py::test_F20_primary_execution_flag_keeps_original_scope`

原 S1–S5 累计回归清单仅取其已验证通过目标，并增加本轮必要新场景及已更新的 provider/终态用例；不是运行全部仓库测试后删除失败记录。初轮失败日志仍保存。

## NOT_RUN 与原候选

真实 provider 的 50,000 短请求、真实小 cap 截断再提交、大于原 8,192 的真实输出：NOT_RUN。当前可用进程环境与允许的配置源没有 API 凭据；受限凭据文件未读取。未更换 endpoint/model，未用离线 PASS 代替线上能力。

完整 GWAS/WGS、原 session 恢复、原报告覆盖、共享环境/注册表/bwrap 修改：NOT_RUN（本轮范围禁止）。WGS 原候选只读复核文件身份及完整内容的结论为 UR 需重生成，未 Commit；GWAS Q 映射仍 unknown。保护清单及源码保持状态见 export `final_protection.json`。
