# 质量审查 Agent：S8 离线验收与完整回归

日期：2026-10-17。起始提交 `4d595d6`，开始时工作区干净。S8 七项任务全部完成，Q01–Q20 全部通过。按契约/客户端/单节点、跨节点集成、完整 Workflow/vendor 的顺序验证。生成和审查使用固定客户端或 MockTransport；执行节点使用真实 pytest/coverage，外部模型请求为零。

## 新增与修正

- 增加三个测试实例：CLI/Web 对相同注入响应的首轮批准和二轮补充直接比较节点顺序、路由、用量、批准对象与最终文件摘要；三个生产节点连续补充直到第 3 轮预算停止，验证无第 4 轮、末轮无生成反馈、未解决发现保留、摘要只执行一次。
- 新增 `scripts/verify_quality_acceptance.py`，保存十个独立 CLI 任务、固定输入、逐轮执行/审查证据、报告链接、消费位置与导出摘要；从实际 JUnit testcase 匹配 Q01–Q20，不预先把未运行项目记为通过。
- 安装验证补查质量报告/统计模块、第三节点页面资源和安装后摘要 1.2 的总量/批准字段。仍为离线 wheel + Python -I + 非 editable --target，复用锁定依赖。
- CI 原先只运行旧流程图测试，现固定 Node 24，运行全部三个前端测试文件，并保存 Windows/Linux Python JUnit。远端执行结果不由本机结果推断。

本阶段未修改生产 Python/浏览器代码或 vendor；测试验证既有保护，没有放宽消息、证据或预算约束。

## 十个 CLI 场景

完整本机证据位于 [scenarios.json](../outputs/quality-s8/7a082b295fa84d5a988e753bf6812bff/scenarios.json)，相邻目录保留各场景任务及日志。每个任务都严格 inspect，比较前后全部文件摘要/修改时间，并核对报告链接和输入文件未变化。

| 场景 | 实测结论 | 候选 / pytest / 审查调用 |
|---|---|---|
| 关闭质量审查 | completed / disabled | 1 / 1 / 0 |
| 首轮批准 | completed / passed，导出批准候选 | 1 / 1 / 1 |
| 质量补充后批准 | 首轮基础 completed 仍继续；反馈第 2 轮消费，导出第 2 轮 | 2 / 2 / 2 |
| 三轮仍有遗漏 | partial / round_limit_reached / quality_budget_reached，无第 4 轮 | 3 / 3 / 3 |
| 具体规格疑问 | needs_review，保留问题和规格引用 | 1 / 1 / 1 |
| 审查固定响应耗尽 | workflow error / quality error，不能默认为批准 | 2 / 2 / 2 |
| 先修复覆盖再送审 | 第 1 轮仅 execution_feedback，第 2 轮达标后批准 | 2 / 2 / 1 |
| 规格支持的源码缺陷 | needs_review / suspected_source_defect，保留失败断言，不调用审查 | 1 / 1 / 0 |
| 质量补充产生重复候选 | partial / quality_no_progress，第 2 轮不重复执行或审查 | 2 / 1 / 1 |
| 补充时删除原通过测试 | preserve_tests 拒绝候选，无新增执行或审查 | 2 / 1 / 1 |

十个场景共 **15 次真实 pytest**，外部请求均为零。审查调用是固定客户端调用，不能作为真实模型质量或费用结果。两个全覆盖候选在第 2 轮批准后选中第 2 轮；等覆盖选择还由 Q13 的专门测试核验。

## Q01–Q20 证据范围

完整匹配的参数化 testcase、报告摘要和安装记录保存在 [验收索引](../evaluation/quality-review-s8.json)，并归档 [完整 JUnit](../evaluation/quality-review-s8-tests.xml) 和 [二轮质量反馈轨迹](../evaluation/quality-review-s8-trace.json)。以下各项均已通过，具体参数化案例与证据摘要以索引为准。

| ID | 核验对象与证据 |
|---|---|
| Q01 | 禁用审查仍为旧双 Agent；无审查调用，disabled 场景 |
| Q02 | 首轮三个生产节点、唯一送审、当前候选批准与导出 |
| Q03 | 二/三轮生产闭环；生成 prompt 真正收到上一轮质量反馈，CLI 二轮任务 |
| Q04 | 覆盖不足不送审；execution_feedback 与 quality_feedback 不混用 |
| Q05 | 原源码缺陷 needs_review 与失败断言保留，审查零调用 |
| Q06 | 三个生产候选达到预算停止，最后发现保留，没有第 4 轮 |
| Q07 | approve 含阻塞发现及业务不一致响应拒绝，不能自动通过 |
| Q08 | 假规格引用、未知函数/测试、错误引用证据拒绝 |
| Q09 | 错任务/轮次/摘要、发送者或接收者、变化负载不能交接或再生成 |
| Q10 | 重复消费与冲突反馈拒绝，不再调用生成客户端 |
| Q11 | MockTransport 模拟超时、鉴权、非法 JSON；总 HTTP 尝试最多 3 次且包含修复，安全错误与未知用量保留 |
| Q12 | 删除/弱化通过断言、装饰器、映射、场景或常量支持代码仍被原保护拒绝 |
| Q13 | 同覆盖候选经第 2 轮批准后导出第 2 轮；不沿用较早未批准候选 |
| Q14 | 质量审查位于候选循环内，摘要仅在最后正常停止后执行一次 |
| Q15 | 具体规格问题为 needs_review，保留问题/引用，不声称缺陷已实测确认 |
| Q16 | 旧契约测试与三个实际旧任务只读 inspect；旧指标、报告与文件时间保持不变 |
| Q17 | 相同固定响应的 CLI/Web 节点顺序、路由、业务结果、批准与文件摘要一致；前端总量测试通过 |
| Q18 | 审查响应、Result 遥测、消费 Context、最终文件及登记产物变化被 inspect 拒绝 |
| Q19 | 重复发现或重复候选有界停止，保留未解决发现，覆盖达标不能绕过 |
| Q20 | 安装后的包内 fixture、独立模块、质量 CLI、批准导出和严格 inspect |

## 旧任务与安装

三个质量功能加入前的真实旧任务来自 `outputs/verification/ed83c3684006484c9958cda6b313c0a6/`，原摘要均为 1.1，原状态没有质量字段：calculation completed/第 1 轮，validation completed/第 2 轮，discount needs_review/第 1 轮。当前 inspect 生成 1.2 视图并缺省 disabled，原任务合计 **136 个文件**的字节与修改时间完全不变；原请求计数、轮次、选择与业务结论一致。

尝试另一个 P6 旧目录时，其 `.coverage` 产物拒绝访问，该目录没有计入兼容通过，也没有绕过核验或修改权限；本机索引记录为 legacy_excluded。通过项采用上述三份完整可读旧任务。

本阶段安装证据：[results.json](../outputs/quality-install-verification/410c846c2b2441d4b7f2775c949f0298/results.json)，四项检查均通过，无包索引访问。没有宣称新虚拟环境或远端 CI 已验证。

## 回归结果

| 范围 | 本阶段结果 |
|---|---|
| 契约、客户端、单节点 | 195 passed，385.77 秒 |
| 质量/原流程集成、报告与 HTTP | 172 passed，797.13 秒 |
| 完整 Workflow + vendor | **1029 passed，530.86 秒（8 分 50 秒），退出码 0** |
| 全量组成 | Workflow 606、vendor 423；失败/错误/跳过均为 0 |
| 前端三个测试文件 | 25 passed |
| Q01–Q20 矩阵 | 20/20，通过实际 JUnit 和安装证据匹配 |
| CLI 场景 | 10/10，15 次真实 pytest，零外部模型请求 |
| 依赖与差异检查 | pip check、git diff --check 均通过 |
| vendor 审计 | 57 个文件与原始清单及 6 个登记补丁一致 |

阶段测试存在交集，不能相加作为独立测试总数；最终全量为 1029 项，包含本阶段新增 3 项。历史 711 项及 S5 的 974 项保留为当时结果，不替代本次实测。重跑耗时受缓存与系统负载影响，不据此作性能提升结论。

第一轮全量运行随聊天中断停止，日志确认至 576 项通过，但没有完整 JUnit，未计入通过。保留 full-interrupted.log 和 interrupted.json 后重新运行完整集合，最终 1029 项通过；新增 --full 入口将日志、开始/结束时间和退出码写入 full-run.json，不依赖终端输出判断结果。

## 重现

从仓库根目录使用 README 的虚拟环境；所有命令仅使用离线响应。

```powershell
$taskEvidence = Join-Path (Get-Location) ('outputs/quality-s8/' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskEvidence | Out-Null
.\.venv\Scripts\python.exe -m pytest tests/test_quality_contracts.py tests/test_quality_client.py tests/test_quality_agent.py -q --junitxml "$taskEvidence/contracts-client-agent.xml"
.\.venv\Scripts\python.exe -m pytest tests/test_quality_workflow.py tests/test_quality_generation.py tests/test_quality_entrypoints.py tests/test_quality_reporting.py tests/test_workflow_integration.py tests/test_reporting.py tests/test_workflow_web.py -q --junitxml "$taskEvidence/integration.xml"
.\.venv\Scripts\python.exe scripts/verify_quality_acceptance.py --evidence $taskEvidence
.\.venv\Scripts\python.exe scripts/verify_quality_acceptance.py --evidence $taskEvidence --frontend
.\.venv\Scripts\python.exe scripts/verify_quality_install.py
.\.venv\Scripts\python.exe scripts/verify_quality_acceptance.py --evidence $taskEvidence --full
.\.venv\Scripts\python.exe -m pip check
```

最后使用 `--collect --installed <本次安装 results.json>`，并以可重复的 `--legacy-run <原始旧任务目录>` 指定待只读核验任务，生成本机 index.json 与可提交的 evaluation/quality-review-s8.json。原始任务、日志、固定候选与分阶段 JUnit 按 outputs 既有规则留在本机，完整 JUnit 与二轮轨迹另归档到 evaluation；脚本每次使用新目录，拒绝覆盖已有场景结果。首次准备验收时应保存质量功能加入前的完整任务目录以复现旧任务检查。

S9 真实模型案例与演示整理仍待完成。本阶段没有自动提交或推送代码。
