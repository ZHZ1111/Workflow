# P0 环境与旧 Agent 基线记录

验证日期：2026-10-09（北京时间）。本记录为新 Workflow 开发前对 AgentDev **实际源码快照**的本次验证，不引用旧文档的通过数字作为本次结果。原始运行目录为 `outputs/p0/20261009-103023-93744e/`；精简索引为 [p0_results.json](p0_results.json)。

## 结论

**离线基线可用。** 首次完整执行 423 项测试，其中 422 通过、1 项因 Windows 结果文件替换 PermissionError 失败；对唯一失败用例进行定向复验后通过。没有修改原工程、快照源码或测试预期来获得通过。三个离线端到端案例均满足预期，原输入字节保持不变。

真实模型基线**尚未执行**。自动审批拒绝了只读使用 AgentDev `.env` 并调用 DeepSeek 的尝试，理由为尚未明确授权向外部服务发送样例源码和规格。没有读取凭据或发出模型请求；需用户授权后完成一次 calculation 真实生成。离线结果不能替代真实模型验收。

## 来源与环境

| 项目 | 本次记录 |
|---|---|
| 来源 HEAD | a347f85385547d90e9e2ff429665ae59a56ca298 |
| 快照 | vendor/agentdev，57 个文件；6 个实际工作文件与 HEAD 不同 |
| 来源证据 | reuse_manifest.json 中逐文件 SHA-256 / HEAD blob |
| 平台 | Windows 11 x64，build 26200 |
| Python | CPython 3.13.9 |
| 虚拟环境 | 本仓库新建 `.venv`，未使用旧 AgentDev 的虚拟环境 |
| httpx | 0.28.1 |
| pydantic | 2.13.5 |
| python-dotenv | 1.2.3 |
| pytest | 9.1.1 |
| coverage | 7.16.1 |
| setuptools / wheel | 84.0.0 / 0.48.0 |
| 锁定依赖 | requirements.lock，沿用本次 AgentDev 实际文件 |

依赖使用已配置的 PyPI 镜像下载并在新环境安装，vendor 使用无构建隔离、无再次解析依赖的 editable 安装。解释器宿主路径解析、依赖安装和离线子进程验证需要经过环境审批；没有为此改动源码逻辑。

## 命令与结果

所有命令从仓库根目录执行，完整安装步骤见 README。

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q vendor/agentdev/src vendor/agentdev/tests scripts
.\.venv\Scripts\python.exe scripts/p0_baseline.py
```

| 检查 | 首次结果 | 证据 |
|---|---|---|
| pip check | 退出码 0，No broken requirements found | p0_results.json/checks |
| compileall | 退出码 0 | p0_results.json/checks |
| 旧完整测试 | 422 passed、1 failed，137.23s；基线脚本首次因此退出 1 | p0_initial_tests.txt |
| 唯一失败复验 | 1 passed，4.40s，退出码 0 | p0_recheck.xml |

失败用例为 `test_agent_loop.py::test_duplicate_with_comment_changes_stops_before_second_execution`。原预期 no_progress，实际因 pytest 插件保存结果时 `pytest_result.tmp → pytest_result.json` 发生 `PermissionError [WinError 5]`，使首轮执行被归因为需复核。定位证据来自该轮 stdout 与 summary。定向复验命令：

```powershell
.\.venv\Scripts\python.exe -m pytest vendor/agentdev/tests/test_agent_loop.py::test_duplicate_with_comment_changes_stops_before_second_execution -q -W error --basetemp outputs/p0/recheck-duplicate-20261009 --junitxml evaluation/p0_recheck.xml -o cache_dir=outputs/p0/recheck-cache
```

复验通过说明该失败未稳定复现；不能由此宣称已经修复文件锁问题，或说首次全量“423 全部通过”。后续若频繁出现同一文件替换错误，应调查本机文件占用并评估结果插件处理策略。原始失败日志已保留。

## 本次真实执行的离线案例

“离线”指生成响应来自固定候选，而 pytest、coverage、归因、选择与报告均调用实际工具。固定客户端在旧 api_calls 字段产生模拟计数，下面另列真实外部请求数，避免误读。

| 案例 | 各轮通过/失败 | 行覆盖 | 分支覆盖 | 结论/停止原因 | 选中轮次 | 外部请求 |
|---|---|---|---|---|---:|---:|
| calculation 正常 | 3/0 | 100% | N/A | completed / coverage_target_met | 1 | 0 |
| validation 第一轮 | 1/0 | 60% | 50% | 请求补充，实际进入第二轮 | — | 0 |
| validation 第二轮 | 12/0 | 100% | 100% | completed / coverage_target_met | 2 | 0 |
| discount 缺陷 | 0/1 | 62.5% | 50% | needs_review / suspected_source_defect | 1 | 0 |

本次正常、反馈、缺陷案例分别约 1.817s、4.940s、1.395s。反馈客户端第二次输入包含 CONTROLLER_FEEDBACK_DATA，历史和已通过测试被保留；两个候选均通过宿主门禁。discount 保留 `calculate_discount(100)==90` 的失败断言，没有修改原源码或预期。

原始报告：

- `outputs/p0/20261009-103023-93744e/offline_normal/report.md`
- `outputs/p0/20261009-103023-93744e/offline_feedback/report.md`
- `outputs/p0/20261009-103023-93744e/offline_defect/report.md`

三个目录同时包含 state、summary、trace、候选计划和逐轮执行证据。outputs 由 Git 忽略；精简索引及首次/复验日志随工程保留，完整证据可通过基线脚本重建。

## 输入与后续限制

calculation（341 字节、1 函数）、validation（664 字节、1 函数）、discount（638 字节、1 函数）、relationships（406 字节、2 函数）均通过源码分析并有非空规格。relationships 静态确认 checkout → calculate_total；本次单独端到端案例为前三组，关联场景另由旧测试覆盖，后续新 Workflow 验收需再执行。

本次没有实现新 Workflow、验证两新 Agent 消息协议或完成 P7 干净解包安装。新 Workflow 的真实模型、多 Agent 协作、预算和扩展验证按 docs/test-plan.md 执行；这些不能被本次旧能力基线代替。
