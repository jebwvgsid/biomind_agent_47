# 隔离验证说明

仅在包含本次有效基线的临时副本或当前有效树运行。安装依赖不在这些命令内；不得用 npm install/Conda/R 安装补齐失败。所有新 Python 用例均使用临时 registry/data-root/软件夹具，先装载既有禁止 config/provider/dotenv 导入的测试保护。生产补登记脚本不是测试步骤。

从仓库根执行 Python 全部目标用例：

```bash
/public/home/liusiqi/anaconda3/envs/biomind/bin/python -B docs/changes/pi_unified_runtime_20261002/run_target_python.py
```

必须从真实文件启动该 runner：multiprocessing spawn 需要重新打开入口，`python - <<'PY'` 会因 `<stdin>` 不存在使并发 writer 夹具失败。旧文件 provenance 负例另由已有 `test_execution_provenance.py::test_preexisting_unchanged_file_is_not_attributed_to_new_attempt` 验证，先导入 `test_python_domain` 的离线保护再调用 pytest。

从 `integrations/pi_runtime` 执行当前已安装编译器：

```bash
.runtime/node/bin/node --max-old-space-size=768 node_modules/typescript/bin/tsc -p tsconfig.json
```

这是完整类型检查并生成 dist，没有 transpile-only 或依赖下载。既有 package build 使用 1024 MiB 堆上限，在本环境曾被杀死 rc137；同编译器/tsconfig 使用 768 MiB 成功。不同时启动多个编译器。

Node 测试使用独立 HOME/agent 目录。先将**现有** `/public/home/liusiqi/.pi/agent/bin/rg`、`fd` 复制到临时 agent/bin，或明确配置已有工具路径，禁止因隔离 PATH 漏配而联网下载。实际测试仅从这些临时路径调用现有工具；没有安装或修改真实软件。

本次路径：`/tmp/biomind_impl_20261002_baseline`。从 runtime 目录执行：

```bash
env HOME=/tmp/biomind_impl_20261002_baseline/home \
 PI_CODING_AGENT_DIR=/tmp/biomind_impl_20261002_baseline/agent \
 BIOMIND_PI_TEST_ROOT=/tmp/biomind_impl_20261002_baseline \
 BIOMIND_PI_TEST_EVIDENCE=/tmp/biomind_impl_20261002_baseline/C-final-evidence \
 BIOMIND_PI_GUARD_TRACE=/tmp/biomind_impl_20261002_baseline/C-final-guard.jsonl \
 BIOMIND_PI_WIRE_TRANSPORT=fetch-fixture \
 BIOMIND_PI_COMPOSITE_TRANSPORT=fetch-fixture PI_OFFLINE=1 PATH=/usr/bin:/bin \
 .runtime/node/bin/node --max-old-space-size=768 \
 --import ./testing/offline-guard.mjs --test \
 --experimental-test-isolation=none --test-concurrency=1 \
 dist/tests/unified-agent.test.js dist/tests/biomind-extension.test.js \
 dist/tests/native-workflow.test.js dist/tests/product-ui.test.js \
 dist/tests/audit-fixes.test.js dist/tests/handoff-wire.test.js \
 dist/tests/session-persistence.test.js dist/tests/audit-domain-stage.test.js \
 dist/tests/external-read-ui.test.js dist/tests/native-skills-read.test.js \
 dist/tests/native-discovery.test.js dist/tests/usage-boundary.test.js \
 dist/tests/external-access-wire.test.js dist/tests/wire.test.js \
 dist/tests/session-domain.test.js dist/tests/host.test.js \
 dist/tests/wire-composites.test.js dist/tests/native-mutation.test.js \
 dist/tests/runtime-binding.test.js
```

两个 `TRANSPORT` 字段属于不同既有夹具，都需要设置。当前 sandbox 不允许监听 loopback；本次通过同一 serializer 的 fetch fixture 验证序列化、工具配对、压缩和重试接线，**没有真实 HTTP socket/L4 或自然 provider 评测**。生产 Python Domain、PM 和无害临时子进程在对应集成用例中是真实的；并非所有测试都使用 FakeDomain。

`serializer.mjs <output.json>` 使用真实已安装 openai-completions serializer、固定假凭证与 fake fetch，将同一两请求链分 system/tools/history/tool outputs 统计字符/字节。修改前的 serializer-before.json 冻结；不要覆盖成新结果。真实历史只读验证用 `legacy_session_readonly.mjs`：仅复制审计明确指向的两个 JSONL 到临时目录并解码；不建立可执行 AgentSession/Domain，不重放旧工具。

`rna_fresh_reader.py` 仅对已授权登记运行禁止 Popen 的查询；不要将生产 registry 路径传给合成安装测试，不要再次执行 `supplement_rna.py --apply`。生产真实 R/fgsea/ORA/TAD 启动不在验证范围。

## 回滚边界

本轮源码回退以保存的 A/B/C 精确 delta 为单位，在无本轮之后改动冲突的临时副本检查后处理；不 reset 当前 dirty 树。dist 需用对应恢复后的源码重新完整构建。生产 metadata 用相关记录 CAS 回滚入口，软件副作用不能被说成已回滚。旧生产进程和未知运行 owner 不因代码部署/回滚自动停止或重放。
