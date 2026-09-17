# 提交、精确范围与复现依赖

以下按实际提交顺序列出。所有生产共享文件由 root 集成；协作者仅提供各自隔离切片。没有 push。

| 切片 | 完整 commit | 文件数 | 内容 | 逐文件回执 |
| --- | --- | ---: | --- | --- |
| S5 | `7d62508ec9d7fda14164da1523488d93184ed0ee` | 7 | 执行/核验/Commit/Recovery/交付的独立事实与同作用域具体失败码 | [receipt](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/s5_commit.json) |
| S1 | `9693df595ce6181393e4f6383c513deb08aaa560` | 7 | 按当前 capability 发现环境、受控检查与设施失败 UNKNOWN | [receipt](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/s1_commit.json) |
| S2 | `e53d1328e209c8a4e0cf33ced223a635e0e0e34b` | 4 | 自动发现到 ledger/phase/请求，再到审批及 runner 的生产接线 | [receipt](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/s2_commit.json) |
| S3 | `af49f24c5801407b9127ce1b69b546ab67abd9b1` | 8 | 无表头样本与无 ID Q 的局部 Skill/helper 合约 | [receipt](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/s3_commit.json) |
| S3 集成 | `41d166b8766ae2a251c9451dc59733fe3022b1a4` | 4 | helper 结构化观察进入下一实际请求；未知 Q 拒绝执行 | [receipt](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/s3_observation_commit.json) |
| S4 与交叉收口 | `b82272283354bf299cea0000ba51af53d8b787d8` | 26 | 旧 seed 字符门、最终派发输出预算、正式归档回读；S5 预算显示和兼容测试 | [receipt](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/s4_commit.json) |

每个文件的完整路径、提交内 SHA-256、该片经测工作文件 SHA-256、最终累计工作文件 SHA-256 见 [COMMIT_FILES.tsv](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/COMMIT_FILES.tsv)。六个源码提交涉及 46 个不同文件；各片计数含同一文件的连续修改，不能相加为不同文件数。S4 的 26 文件包括 10 份限定的历史回放 fixture；它们不作为新科学结果，也不恢复原 session。

## 前置及累计候选

审计基点 `3f71b90789c14b2662e717225cf826b1cf5ec3b3` 只有审计产物。生产前置为 `82c27867d862ef32d657fbc6162c70d4bc06492c` 的累计实现（包含 `2e70f28658b8aee0d7cd9451f7f353f4b4121825` 和 `d2a35c4834c15373841c5e2969281c11c4db6da7`）加原受保护 overlay。S5、S1 独立推进；S2 依赖 S1；S3 的观察集成依赖 S2/既有观察机制；S4 最后串行集成共享文件和 S5 预算显示。

最终源码提交：`b82272283354bf299cea0000ba51af53d8b787d8`。经测累计候选含 926 个源码/Skill/fixture 文件，规范 JSON 清单摘要为 `9b4ebef7e58cbfc7fcfeaf6da17161d2fad21f604e1ac5fadbac24406e38c1dc`（排序键、compact separators 的 JSON 摘要，不是排版后清单文件自身的字节摘要）。所有最终测试批次使用同一清单，主工作区 926 文件逐项相同。身份见 [final_identity.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/final_identity.json)；全文件 hash 见 [final_source_manifest.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/final_source_manifest.json)。文档提交不改变这套源码。

完整复现输入已持久保存在 `/public/home/liusiqi/03.毕设/biomind_audit_exports/gwas_s1_s5_20260917`：`baseline_source/` 对应原 905 文件，`baseline_manifest.json` 锁定其内容；`final_changes/` 提供本轮 46 个变化/新文件。以这些文件重建最终清单，而不是仅 checkout HEAD。原 overlay 未强行纳入本轮源码提交，不能把纯 Git 树的测试范围写成完整工作区验收。

## 旧 overlay 的保留

原 staged 为空；备用 index 只接纳本轮增量，真实 index 只更新明确路径。源码测试结果来自完整候选，提交内 blob 刻意不包含无关的原有工作区改动。原其他 dirty、untracked、删除及原 session/三份报告/注册表保持原状，见 [final_protection.json](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/final_protection.json)。

`tests/test_recovery_schema2_node.py` 的旧 overlay 已含多个 HEAD 中不存在的 helper/测试。本轮在那些旧定义内的少量 provider-cap 接口修改无法用自动三方合并直接独立归入 HEAD；自动提交在写主工作区前停止。人工分区只将 HEAD 原有 `_Model` 和 node_runs 定义的本轮改动放进源码提交，四份 HEAD/base/tested/index hash 经核对。旧 overlay 内的全部本轮增量另作为 [精确 patch](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/s4_preserved_overlay_test_increment.patch) 随文档提交，补丁使用零上下文格式，针对 hash 一致的 base 执行 `git apply --unidiff-zero` 已验证逐字节等于 tested 文件；完整经测文件亦保存在 final_changes。详见 [分区回执](/public/home/liusiqi/03.毕设/Biomind_v1/docs/implementation/gwas_s1_s5_20260917/evidence/s4_manual_partition.json)。没有删除旧 overlay，也没有把其旧新增代码冒记为本轮新增。

源码提交后的验收回执是本目录记录的候选；文档提交完整 hash 由最终用户回执及持久导出的 post_commit_receipt.json 给出，避免文档自包含自身 Git hash。
