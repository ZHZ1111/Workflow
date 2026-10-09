# 开发执行与工程管理

版本：P1 / 2026-10-09。总体预算 60 小时：P0–P7 功能阶段 52 小时，集成缓冲 8 小时；可选增强 6 小时，额外缓冲最多 4 小时，总量不超过 70 小时。预算是计划值，不虚报本次已经投入的人工工时。

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

当前 P0 离线工程准备与 P1 已完成，真实模型基线因此前外传审批待授权。P1 48 项测试通过，证据见 `evaluation/p1.md` 与 `evaluation/p1_results.json`。P2–P7 尚待实施；P1 的假节点不代表真实 Agent 或端到端测试工作流已完成。每完成阶段，更新主清单对应复选框；暂未完成的条件子项单独标注。

## P1 已完成的基础

1. 已创建 `src/test_workflow`、pyproject、模块入口和 `test-workflow` 命令，安装后依赖检查通过。
2. 已实现 AgentContext/AgentResult/Message/WorkflowState，复用 CandidateRound 和 AgentState；执行/coverage 数据继续使用旧模型。
3. 已实现同步接口、稳定标识注册表和受约束的顺序流程定义。
4. 已实现外层工作区、原子状态保存、消息与递增事件日志；内层目录未创建。
5. 假生成与假执行节点交接同一候选，错标识、轮次、接收者、重复消息、文件变化和节点异常均有验证。
6. 未调用旧 run_agent/run_initial_step，未进行模型请求或候选 pytest 执行。

## 下一步：P2

创建生成 Agent 与 `adapters/agentdev.py`，复用 `generate_initial_candidate()` 和 `stage_revision_candidate()`。保持一次调用只产生一轮候选；校验失败仍作为有证据的候选返回；不得在生成节点执行 pytest。真实 Agent 产物应放入内层 AgentDev 工作区，转换为外层任务相对引用并返回受限 StatePatch。P4 再扩充当前顺序 runner 的反馈路由与完整预算策略。

## 风险与处理

| 风险 | 影响 | 当前处理/后续措施 |
|---|---|---|
| 真实模型调用需要外传授权 | 无法完成本次真实旧 Agent 基线 | 离线证据已保存；仅一次 calculation 实际调用待授权；P5 真实验收仍待执行 |
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
