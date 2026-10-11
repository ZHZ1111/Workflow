# 开发执行与工程管理

版本：P6 / 2026-10-11。总体预算 60 小时：P0–P7 功能阶段 52 小时，集成缓冲 8 小时；可选增强 6 小时，额外缓冲最多 4 小时，总量不超过 70 小时。预算是计划值，不虚报本次已经投入的人工工时。

## 阶段与文档维护

| 阶段 | 计划 | 主要工程产物 | 完成判据 |
|---|---:|---|---|
| P0 | 4h | scope、reuse、baseline、样例与独立环境 | 输入边界收敛，旧能力可复现 |
| P1 | 5h | 新包、contracts/state/registry/definitions/events | 两个假节点通过同接口交接并保存状态 |
| P2 | 7h | 生成节点与 adapter | 单轮首轮/修订有效，生成节点不执行 |
| P3 | 7h | 执行与评估节点 | 实测 pytest/coverage、保守归因与建议 |
| P4 | 8h | WorkflowRunner、CLI、demo 客户端 | 顺序/分支/有限反馈完整闭环 |
| P5 | 8h | 新 tests、acceptance.md/json | 离线矩阵和真实模型结果有独立证据 |
| P6 | 7h | 报告、消息、扩展示例、README/Design 完善 | 可追踪协作，可注册新节点 |
| P7 | 6h | 干净安装记录、演示和源码包 | 按 README 从交付目录复现 |

当前 P0 离线准备及 P1–P6 已完成。P6 Workflow 回归 204 passed，五个正式 CLI 案例通过；日志、报告、只读检查和 ResultSummaryNode 有独立证据，见 `evaluation/p6.md` / `p6_results.json`。P5 的 192 项 Workflow、180 项 vendor 回归、15 个离线案例及三个授权真实模型案例保留在 `evaluation/acceptance.md` / `acceptance.json`，P6 未新增外部请求。P0 旧项目真实基线未补跑，P1–P5 历史记录独立保留。P7 尚待实施；可选关联样例与正常样例稳定性复测未执行。每完成阶段，更新主清单对应复选框；暂未完成的条件子项单独标注。

## P1 已完成的基础

1. 已创建 `src/test_workflow`、pyproject、模块入口和 `test-workflow` 命令，安装后依赖检查通过。
2. 已实现 AgentContext/AgentResult/Message/WorkflowState，复用 CandidateRound 和 AgentState；执行/coverage 数据继续使用旧模型。
3. 已实现同步接口、稳定标识注册表和受约束的顺序流程定义。
4. 已实现外层工作区、原子状态保存、消息与递增事件日志；内层目录未创建。
5. 假生成与假执行节点交接同一候选，错标识、轮次、接收者、重复消息、文件变化和节点异常均有验证。
6. 未调用旧 run_agent/run_initial_step，未进行模型请求或候选 pytest 执行。

## P2 已完成的适配

已创建 `agents/test_generation.py` 与 `adapters/agentdev.py`，复用首轮、反馈上下文和修订保存入口。内层原始候选保持相对路径，外层候选转换成 Workflow 约定并带摘要交接。离线候选、MockTransport 重试/格式修复、语法门禁、历史保护与未知用量均有验证；实际调用次数与离线模拟明确区分。五个独立验收案例由 `scripts/p2_baseline.py` 复现，原 vendor 源码未改动。

## P3 已完成的实测节点

已创建 agents/test_execution.py 与 adapters/execution.py，核对任务、候选、摘要和工作区后调用一次 run_tests_with_coverage，复用保守归因和策略。assessment 和消息带真实执行、目标覆盖、缺口及历史通过保护；同轮不能重复执行。12 个独立案例和自动测试验证未执行、失败、超时、覆盖不可用不能得到 completed 建议；源码与测试执行后变化会停止，产物异常给出诊断目录。vendor 快照 57 文件摘要未变。

## P4 已完成的闭环

两个生产节点已接入 WorkflowRunner：无效候选只评估、真实执行反馈重入、三轮预算与无进展停止、人工复核、历史选择和基本报告导出。节点 Context/Result、事件耗时、route_selected 与最终状态均落盘；CLI run --demo 与真实模式只替换生成客户端。历史通过保护、后轮失败保留先前测试、节点错误、导出失败、中文/空格输出路径均已验证，未改 vendor 快照。

## P5 已完成的验证

新增 17 项系统测试检查生产节点之间的身份/轮次/接收者/重复消息/摘要损坏、历史保护、源码副本变更、持久化失败、规格疑问和中文/空格及非法输入。15 个全新离线 CLI 任务实测 17 次 pytest，零外部请求。真实 calculation 为 6 passed，validation 为 15 passed，discount 为 15 passed / 2 failed 并保留 100→90 断言，进入疑似源码缺陷复核；三项各一轮，共四次模型请求，其中 validation 含一次客户端格式修复。真实案例没有发生 Workflow 反馈循环，反馈消费与修订由受控离线任务验证。输入源码/规格及 vendor 57 文件摘要不变。

## P6 已完成的报告与扩展

事件显式记录输出保存、消息消费和业务停止；报告从已提交候选/评估/Context/Result 读取实测计数、覆盖、问题和原始证据链接。无模型 ResultSummaryNode 通过注册/定义插入导出前，不修改主循环业务路由或结论。inspect --summary 校验证据并只读重建摘要；五个 CLI 案例验证链接和全部文件摘要/修改时间不变，旧 P5 三个任务兼容读取且未改写。

新增 12 项报告测试纳入 204 项完整回归；初次 Windows 临时文件替换问题与零消息日志链接修复均留存说明。vendor 57 文件未改动，当前依赖检查通过。运行和维护见 docs/reporting.md、docs/extensions.md。

## 下一步：P7

从干净目录安装并按 README 复现，检查交付依赖和文件，准备正常及反馈/复核运行演示，整理源码版本和符合课程要求的备用压缩包。当前环境通过不代表任意干净环境已验证。

## 风险与处理

| 风险 | 影响 | 当前处理/后续措施 |
|---|---|---|
| 真实模型调用需要具体外传授权 | 请求范围与费用必须明确 | P5 三个必需案例已获授权并完成；可选关系案例及 P0 旧项目真实基线未运行 |
| HEAD 与实际工作文件不同 | 快照来源模糊 | 记录 6 项差异及逐文件哈希；后续适配修改继续记录 |
| 首轮目录预创建 | 校验/保存拒绝覆盖，可能浪费请求 | 外层/内层分开；请求前校验输出目录 |
| 数据轮次混用 | 新候选误用旧执行结果 | Message 验证来源与目标；显式 round_number |
| 保护测试被删除或弱化 | 伪造覆盖提升 | 复用 AST/映射/规格保护门禁 |
| 归因超出保守能力 | 错把生成错误判成源码缺陷 | 复杂断言转人工复核，报告边界 |
| 异常后仍称任务成功 | 提交误导性结论 | 编排状态与业务结论分离，保存真实停止原因 |
| 时间被 UI 挤占 | 核心协作验收不完整 | 先完成 CLI/协议/反馈，基础通过后才增强 |

## 配置、版本和交付纪律

只提交 `.env.example`；不要复制旧 `.env` 或将 API Key 放入文档/日志。公开配置使用 ModelInfo/safe_summary。真实运行明确标明模型模式、外部请求数、实际结论和时间，不挪用旧报告结果。

快照变动集中在 adapter 或 vendor 必要修改，并同步记录 docs/reuse.md；新功能集中在 test_workflow。P7 之前不把“使用当前环境已通过”描述为“任意干净环境均已验证”。

工程文档统一 Markdown，便于版本管理。scope 是需求基线，Design 是架构决策，reuse 是版本与适配依据，samples/test-plan 是验证资产，baseline/acceptance 是实际证据；避免一份文档兼任所有角色而互相矛盾。
