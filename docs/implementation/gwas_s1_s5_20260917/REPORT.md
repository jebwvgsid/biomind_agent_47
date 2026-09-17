# GWAS S1—S5 实施记录

依据审计提交 `3f71b90789c14b2662e717225cf826b1cf5ec3b3`、原 S1—S5 IMPLEMENTATION_PLAN 及用户 20260917 补充指令；未另起全面审计。本目录记录软件机制验收，不代表原 GWAS 已完成。

最终累计验收：四个独立解释器批次共 510 次测试执行（508 个不同用例，另 2 次新进程续跑）及 45 项子测试通过；36 项已确认基线失败显式排除。此前扩展单进程运行返回 -11，原因未定，保留原始证据。测试通过范围及异常见 [VALIDATION.md](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/VALIDATION.md)。

源码最终提交 `b82272283354bf299cea0000ba51af53d8b787d8`；六个源码逻辑提交、逐文件范围和受保护 overlay 复现方式见 [COMMITS.md](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/COMMITS.md)。最终候选 926 文件规范清单摘要为 `9b4ebef7e58cbfc7fcfeaf6da17161d2fad21f604e1ac5fadbac24406e38c1dc`，主工作区与经测候选一致。

## 基线与提交方式

实施起点 HEAD 为上述审计提交。生产前置为 `82c27867d862ef32d657fbc6162c70d4bc06492c` 的累计源码（含 `2e70f28658b8aee0d7cd9451f7f353f4b4121825`、`d2a35c4834c15373841c5e2969281c11c4db6da7`）及原受保护 overlay。对照审计的 905 文件清单未出现影响本轮实施的新差异。审计提交本身只有审计产物，单独 checkout 不构成完整测试基线。

原暂存区为空。每片使用备用 index 和三方增量合并 `HEAD + (实施前工作文件 → 经测试的工作文件)`，只提交本轮增量；随后只更新相应真实 index 路径，并保留原工作文件 overlay。未 reset、checkout、覆盖原 dirty/untracked、提交无关删除或 push。每片 receipt 同时记录测试完整文件 hash、index blob hash 和保留 overlay 的文件，二者不同是有意的保护分区。

全量既有基线只保存在已声明持久导出中；最终候选通过该基线加本轮完整变更文件复现，不重复提交整套无变化源码。精确文件与候选身份见最终清单和提交 receipts。

## 实施行为

- **S1**：从配置的 Conda 根和现有软件注册记录发现当前操作候选；默认环境缺失时允许仅支持当前 bcftools 样本检查的环境进入受控 Inspector。显式目标不换环境，候选路径存在不等于验证通过。设施失败为 UNKNOWN，不据此标坏环境或安装。
- **S2**：Consultant `skill_decisions.execution_capability_id` 沿既有 capability 协议触发实际检查，接受的作用域证据进入 ledger，新观察追加到 phase 的下一真实请求。Plan/Generate 消费当前证据，正常审批冻结后由原 runner 消费。新证据沿原 supersedes 更新；失效证据同步撤回 provider 的可用表述。只按 capability 表达就绪，不将局部成功升级为整个 Skill ready。
- **S3**：局部 helper 支持无表头、明确列选择和首样本保留；验证重复、缺失、错序与空值。全数值无 ID 的 Q 不因行数或指定首列而获得身份。明确 Q 行映射需锁定当前 Q/TFAM hash，完整、唯一地列明每行样本；来源说明不冒充独立生物学来源证明。Skill 区分无文件观察与显式样本文件提取，继续保留空必需产物拒收。
- **S4**：旧 seed 字符门退为兼容计量，完整预算负责准入；业务、分段和合并请求在最终适配器统一实际输出上限与预留。复用既有预算、coverage、分层压缩、维护额度、CAS、epoch 和持久重入。外置结果经正式 evidence.read 按当前作用域、版本、权限及 digest 回读，复用有限分页；保留响应和单对象大小保护。没有全面精简 seed 字段或清理继承材料。
- **S5**：优先保留既有具体 failure_code，正式核验失败仅在缺具体码时使用兼容分类。rc=0、核验拒收、未 Commit、Recovery 停止及文件交付分别展示；历史字符上限与新完整请求 token 预算按真实阶段和计量显示。

## 验收边界

自动链用确定性 provider `.ainvoke`、Inspector 最外 OS 叶子及审批决定替身。中间 discovery、SkillManager、ledger、phase、实际请求适配器、Plan、Generate、审批身份、核验、Commit 保持生产实现；微小 synthetic 脚本通过真实 runner 执行。替身给出的样本事实不是本任务正式科学结果。

审批到 launch 的一致性结论仅覆盖实测 argv、cwd、env、路径和现有身份字段；本轮没有增加检测所有同路径可执行文件内容替换的全局完整性机制。

真实主机另沿原 bwrap 生产入口只检查一次 `bio_gatk` 的 bcftools 版本和 query 帮助，候选路径存在，但 60 秒超时；结果 UNKNOWN、无绑定、无安装、未修改注册表。真实 OS 叶子未替换，也没有绕过隔离。现有 timeout 清理接口没有返回进程 rc 和部分 stdout/stderr，记录为未知，不能把历史 NETLINK 报错当作此次实际结果。详情见 evidence/host-controlled/。

正式输入只读 helper 观察：VCF 和无表头表型各 215 个样本，ID 集合和顺序一致；Q 为 215×3 且无 ID，映射未知。该事实允许报告初检，阻止依赖该 Q 对应关系的协变量准备与关联。不推断 Q 顺序、不改选科学模型、不声称 MLM/GWAS 完成。

未调用真实模型/API、安装升级、修改共享环境/注册表/主机 bwrap、恢复原 session、运行完整 GWAS/WGS、覆盖原 report.md/stage_status.tsv/reproduce.sh 或 push。留待后续：真实模型认证、主机受控检查设施、确需软件安装、Q 来源/映射和科学端到端执行，以及可选 seed 字段优化。
