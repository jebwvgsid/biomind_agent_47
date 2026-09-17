# 三个比较对象与证明边界

## A：修改前实际源码

- 完整 HEAD：`73e1f8fbc585c695530fba01d4e5394b99b4cbcc`；分支 `refactor/consultant-controller-authority`。
- 2026-09-17 18:34:42 +08 捕获的实际源码：`/public/home/liusiqi/03.毕设/biomind_audit_exports/environment_binding_unified_20260917/prerequisite_source`。
- 原清单：`/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/environment_binding_unified_20260917/evidence/baseline.json`。本轮逐项检查其已有哈希，全部匹配。
- `e90a70158142da04dcaf588517a3a1e2f64c2a02` 和 `73e1…` 是此前审计提交，均不能作为后来生产修复版本；A 含当时已有 dirty/untracked 代码，不能只用 git show A 代替。

## B：实际交付候选

- helper 前置提交 `078f7198e8c5c4447affefa6bfdbdf14e7de5152`。
- 生产主提交 `2e70f28658b8aee0d7cd9451f7f353f4b4121825`（19:37:40 +08）。
- 依赖准备补充提交 `d2a35c4834c15373841c5e2969281c11c4db6da7`（20:06:03 +08）。
- 文档交付提交 `82c27867d862ef32d657fbc6162c70d4bc06492c`（20:08:19 +08）。
- B 是这些增量加保留的前置 overlay；原记录也明确 standalone Git HEAD 不是被测候选。候选 `/tmp/biomind-unified-cumulative-release/candidate` 本轮仍存在，但不以其临时寿命作为唯一身份依据。
- 持久的 `final_source_manifest.json` 包含 905 个源码/Skill/测试文件；本轮与当前工作树逐一比较，**905/905 相同**。比较结果和 A 哈希复核保存为 `evidence/version_comparison.json`。

## C：目标 session 与本轮工作树

- session 根 `/public/home/liusiqi/03.毕设/benchmarks/biomind_agent_benchmark/runs/task_outputs/session_1789648505`。
- `/public/home/liusiqi/03.毕设/Biomind_v1/logs/biomind_3621046.log:5` 记录进程启动于 20:34:41，晚于上述交付提交；任务请求在 20:53，失败在 20:55。
- 本轮 22:54:30 +08 固定的工作树 HEAD 为 `82c27867d862ef32d657fbc6162c70d4bc06492c`，索引树 `4fa77f0b2612a1dd54e5e6224cbadec601828e58`，原 staged 为空。dirty/untracked 清单完整保存在 `evidence/status_before.txt`，不由本轮整理或提交。
- 本轮生产/Skill 冻结副本 `/public/home/liusiqi/03.毕设/biomind_audit_exports/gwas_env_recovery_20260917/current_source`；412 份相关源文件哈希在 `evidence/baseline_current.json`。只读/隔离见证以该副本或哈希一致的当前源为依据，不切换工作树历史版本。
- **限制：原进程没有保存足以证明每份导入源码的完整启动哈希。** 启动晚于交付、当前与交付候选一致、实际消息/脚本/错误与源码路径吻合，是强一致性证据，仍不等于能证明运行时所有模块与当前逐字相同。报告将“实际现场证据”和“当前/旧快照行为见证”分列。

## 增量归因方法

1. 先比较 A 的实际函数/Skill 与当前副本；逐字一致的函数不归为本轮新增。
2. 改动函数使用相关分支或同条件见证区分“代码变了”和“行为退化了”；Installer 的旧候选入口与当前入口独立判断，不能用两个科学需求不同的 session 代替前后对照。
3. 实際模型选择/输入解释没有同条件旧模型输出时，归为现场缺陷或覆盖不足，标注不能证明本轮回归。
4. 已有 WGS/binding/helper 测试只在源文件哈希和调用层次匹配时复用；不以旧通过总数代替 GWAS 通用发现或真实服务器受控检查的证据。

`witnesses/verify_boundaries.py` 另捕获并核对 905 份交付源码/测试、目标 session 全部文件、三份原报告和软件注册表；完成时 `protected_after.json` 必须无变化。本轮允许的差异仅为新审计目录与其逻辑提交。脚本不读取 `.env.api_keys`，不导入配置启动模块。
