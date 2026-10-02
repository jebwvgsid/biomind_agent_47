# C：统一主 agent、上下文和观察交付

实际删除新请求中的角色工具门槛、handoff 生产/消费、自动交接续轮和交接专属裁剪。主工具集合与公共规则统一；native/protected 原边界、真实新请求、用户原话和修订、停止条件、来源撤回、Pi 压缩及原 observer 保留。Main 的 Domain 准入采用宿主 work 请求，不假装 Generate；SQLite 归档 allowlist 增加 main，保留历史值。

测试还定位并修复了两项真实上下文问题：分支回退后的修订号采用 Domain 回执；旧 handoff 在撤回来源时仅保留匹配当前请求的宿主 userBasis，撤回其来源派生内容。拒绝后保持 paused，同时发出 aborted 观察结果；不新增续轮或取消活动作业。

诊断预算使用现有请求状态和 observer/executor，重复 call 不再扩大预算。相同请求/作用域、相同需求的选择可直接使用已接受绑定，回归禁止 registry.reload、候选枚举和 Popen。定点错误记录覆盖缺包、缺入口和 R 共享库路径；不使整个环境失效，权限/输入/取消/超时不误判。

Python 最终 88/88：锁与原子写、安装、候选、启动、登记、Main 准入、预算、科学停止/交付以及局部诊断。另有旧文件 provenance 负例通过。新科学测试全部为无害临时脚本；R/fgsea 检查失败或 TAD preflight 失败后，后续科学 marker 为零。合法空结果由原格式契约解释。

Node 第一轮 148/173，第二轮 166/173。首轮失败包括隔离 PATH 缺已有 rg/fd、遗漏 composite 的 fetch-fixture 配置，以及旧角色轮次/新请求假设；同时发现上述真实接线问题。最终完整 tsc 构建通过后，对发生变更的 native-workflow/runtime-binding/session-domain/wire-composites 复跑 **17/17**。旧会话来源、修订和压缩断言保留；真实 TAD/ORA 两份历史副本只读解码，原文件散列不变。七种旧状态矩阵使用合成 JSONL 副本，未接管生产未终态任务。

**未关闭问题：测试宿主间歇 SIGKILL。** host 普通流程和 memory-auto 曾在 provider 调用前被信号 9 终止，外层 exit 247；同一代码的普通流程有限重跑及无并发、384 MiB 父进程堆上限的 memory-auto 独立重跑通过（后者 3 次合成 provider）。relay 没有发送 SIGKILL，已记录的 cgroup OOM 计数未增加，但不能据此排除所有资源/外部原因。默认沙箱禁止 PTRACE_TRACEME，因此未取得信号发送方；未申请扩大权限，也未操作生产进程。这一未定因现象保留为 A02 PARTIAL，不能把成功重跑当成稳定性问题已解决。详细原始观察摘要和临时 raw 路径见 host-signal-observation.json。

同一 serializer、模型配置、两请求夹具的 UTF-8 字节总和如下。压缩/重试均为 0，history 均为 774 字节；工具输出和全部 body 已计入原始 JSON。

| profile | system 前→后 | tools 前→后 | body 前→后 |
|---|---:|---:|---:|
| native | 10,664 → 13,766 | 25,716 → 33,100 | 37,456 → 47,942 |
| protected | 18,038 → 14,728 | 25,300 → 37,278 | 44,414 → 53,082 |

protected 系统规则变短，但新增能力和统一可见工具使总请求变大。没有 token 计数器、真实模型成本或自然模型策略评估，不能声称节省 token。科学能力选择仍需由真实请求决定；离线 scripted provider 只证明代码接线。

C 的 37 个文件处置见 deltas/C-staging.json：18 个可独立 source hunk，19 个依赖原 dirty 树的精确 delta artifact。未混入旧改动。所有 A/B/C 补丁的临时重建逐文件匹配当前有效树；实际 dist 与 Pi 六个补丁文件核对见 build-C.json。未 push，未重启生产进程。
