# 验收边界与已有失败

本轮只判定 F1—F6 授权范围的生产修复与隔离框架机制。实际使用固定模型叶子和新建科学替身；固定非流式 AIMessage 不代表真实 SDK、网络、流式 token 回调或所有传输重试分支已验证。公开请求、真实传输包装、parser、审批、执行器、attester、Reviewer parser、gate、Commit、持久化及报告各有独立证据。正常图未触发的 hook 不因已注册而算异常路径已执行；对应异常由组合或独立正式构造验证，见 acceptance_matrix.tsv。

| 边界 | 本轮结论 |
|---|---|
| 真实模型/API 输出质量 | NOT_AUTHORIZED / NOT_RUN；没有网络请求 |
| 真实科学安装、版本、资源与 ANN 正确性 | NOT_AUTHORIZED / NOT_RUN；隔离六操作只证明机制与事实消费，不证明科学能力可用 |
| 原 session_1789388849、原批准脚本 | NOT_RESUMED / NOT_EXECUTED；原任务历史完整加载版本仍 UNKNOWN |
| Rscript、external_worker、宿主登录环境 | 条件性 NOT_RUN；未安装或启用它们，所有新增子进程用受控非登录执行 |
| 全部 OS/断电/清理窗口 | 未穷举；仅声称本轮明确注入窗口的结果；外层 watchdog 124 不作生产正确结束 |
| Recovery 结果 unknown 的远程语义 | 原 owner 的 at_least_once_without_provider_idempotency 保留；本地 scope/预算/target 可重载，不声称远程 exactly-once。已完成缓存可由正式 Generate 零 provider 调用消费 |
| 裸 commit/干净克隆部署 | NOT_VERIFIED；测试加载已登记保护 overlay 和原有安全未跟踪 fixture，加载 manifest 与提交父版本分开 |
| 原审计 237 文件及其他 Skill/能力 | 未变事实复用；本轮只回归受影响已知公共调用方，不称全部能力科学通过 |

旧九项缺 candidate scope 的 fixture 失败保留。当前公共回归中一项 BASE 已有的整字典形状断言失败也保留：四个布尔值满足旧断言，结果另含已有三项 provenance 字段；未删断言或改旧测试。具体函数字节等同性和实际失败见 regression/final03 与 regression/unified_final/preexisting_assertion.json。依赖未授权旧事故或其他 benchmark 输入的旧测试保持 NOT_RUN_SAFETY，逐项见 regression/preflight.json；安全直接接口由本轮正式新状态回归补充。

开发回执中的缺 fixture 依赖、错误测试预期、PTY 父进程输入时序、超时和 -11 保留原退出码。旧 scheduled traceback 与 -11 的相关性不等于已证实原因。最终测试只取消测试侧定时 traceback、保留 fatal handler 与外部 watchdog，真实 aclose 未替换。最终 F6 将固定回复下移到模型叶子；早期替换整个 node dispatch 的结果保留原边界，不计作最终完整传输包装验收。

本轮没有重启现场运行进程，没有自动接续其他规格，没有覆盖历史业务结果，没有 push。读取范围限安全源码、授权事故证据和本轮新合成产物；完整宿主环境、启动脚本、密钥、正式科学输入与业务库均未读取。
