# 质量审查 Agent：S4 Workflow 调度与路由

日期：2026-10-16。起始提交 `fd3ecf1aabfa89d3f8722d7cb87f7665cd5ccb1e`，开始时工作区干净。S4 将 S3 审查节点接入原有限循环，完成质量结果提交、唯一反馈路由、停止策略及批准候选选择。

## 流程定义与执行顺序

`definitions.py` 增加 QUALITY_WORKFLOW（生成、执行、审查）与 QUALITY_SUMMARY_WORKFLOW（再追加 ResultSummaryNode）。显式声明执行→审查依赖与审查→生成反馈边；运行前检查质量配置与定义一致，禁止开启配置但缺少节点或反馈边，也禁止关闭时使用质量定义。配置仍默认关闭。

原 TestExecutionAgent 在基础 completed 时发出唯一 review_ready，引用本轮真实 ExecutionAssessment；其他分支不发出该消息。Workflow 先对照原 choose_action 校验执行建议，再运行审查，批准前不提交 completed。执行反馈修复、覆盖不足、缺陷复核、超时及完整性错误仍沿用原处理方式。

第 N+1 轮生成的期望来源由第 N 轮已提交结论决定：质量补充要求 test_quality_review 的 quality_feedback，否则要求 test_execution 的 execution_feedback。inbox 必须只含一个未消费的期望反馈，来源轮次固定 N；不能混入其他来源、旧反馈或已消费消息。生成、执行、审查之间的交接也检查消息类型、当前候选及证据摘要。

## 提交校验与状态归属

Workflow 的 apply_result 重新校验输入状态与 AgentResult，不信任绕过校验构造的对象。原有唯一节点权限继续生效：质量审查只能提供质量记录及审查遥测，不能修改候选、执行观察或生成用量，StatePatch 必须为空。

新增 `quality_routing.py` 在提交时调用 S3 的 `load_review_inputs()`，再次核验源码、规格、候选、真实执行与历史证据。成功 Result 必须且只能登记本轮固定路径 `quality_reviews/NN/assessment.json` 和 `response.json`，文件摘要及内容须与 QualityAssessment、ReviewResponse、遥测和宿主输入一致，发现引用仍需通过来源校验。

质量反馈只引用该已提交 QualityAssessment，reason 必须等于决定摘要，嵌入的阻塞发现须与记录完全一致。消息、Result 或落盘文件被更改，或提供不匹配的规格摘要，均不能提交批准或标记交接消费。

## 质量决定与停止策略

| 决定或条件 | 质量状态 / 原因 | 业务结果与后续行为 |
|---|---|---|
| approve | passed / approved | completed / coverage_target_met，选中当前获批候选 |
| request_revision，有预算且有进展 | changes_requested / quality_gaps；进入下一轮后 not_run | Workflow 设置 ADD_MISSING_CASES，提交唯一质量反馈并继续 |
| request_revision，候选预算耗尽 | changes_requested / quality_budget_reached | partial / round_limit_reached，保留发现，不发送反馈 |
| request_revision，阻塞发现无减少或候选相同 | changes_requested / quality_no_progress | partial / no_progress，保留发现，不提交新的反馈 |
| request_review | needs_review / ambiguous_specification | needs_review / ambiguous_specification，停止并保留问题 |
| 审查失败、非法交接或输出不一致 | error / review_error | workflow_status=error、业务 partial 或 failed / unrecoverable_error，不默认批准 |
| 质量补充生成重复候选，被原门禁先行拒绝 | not_run / quality_no_progress | partial / no_progress，不再次审查，保留上一轮未解决发现 |

继续补充时不设置终止 stop_reason，原 ExecutionAssessment 的 FINISH 建议不变。候选总预算仍为 1–3，共用于执行修复与质量补充；最后一轮可批准，但不能发起第四轮。

无进展判定由宿主完成：先检查预算，再检查候选测试 SHA 是否重复；比较上一份补充决定与当前决定的阻塞发现指纹，若旧指纹集合仍全部存在则停止。指纹使用 category、排序后的 target_functions、spec_quote、test_code_quote 和 suggested_case；文本经过 NFKC 与空白规范化，忽略模型 finding_id、描述和测试名引用。它识别规范化后仍相同的请求，不提供语义相似度判断。若至少一个旧阻塞发现已消失，可继续处理剩余或新增发现。

重复候选可能被原生成门禁在再次执行/审查前拦截，因此状态约束增加一个有限例外：只有 partial/no_progress、旧轮次 request_revision 且当前未审查时，not_run 才能带 quality_no_progress。不会把旧质量决定标记为新候选的审查结果。

## 消息、事件、摘要与选择

Result 是节点提案。无进展时 S3 可能仍提出一条反馈，Workflow 保存原 Result 供审计，但仅将获准消息写入 state.messages 与 messages.jsonl。通过提交校验并写入消息日志后才记录消费，错误 Result、非法反馈和失败节点不消费输入。延续原单写者事件/状态保存机制，不承诺跨文件事务或断点恢复。

route_selected 增加 quality_approved、quality_revision_requested、quality_needs_review、quality_no_progress 的实际动作记录，节点归属为 test_quality_review；审查失败仍记录 node_failed/workflow_error。执行节点原有 FINISH 事件与 assessment 保留，不伪装成执行节点要求质量补充。

批准后 selected_round 和 AgentState.best_available_round 均指向当前候选，包括旧评分完全同分而偏向较早轮次的情况。尾部摘要再次提交时继续保持这个选择，最终测试按本轮原字节导出，selection_reason 明确说明当前候选已获质量批准。审查节点从尾部遍历中排除；正常业务结论确定后，ResultSummaryNode 只执行一次，错误路径延续原有跳过尾部节点的行为。

## 验证

新增 `tests/test_quality_workflow.py` **27 项**，组合受控生成、真实执行节点、S3 审查节点与固定审查响应。首轮使用生产 FixtureClient/生成节点，后续受控候选仍经过原 stage_revision_candidate 和候选适配器，执行真实 pytest/coverage；没有外部模型请求。

覆盖首次批准、质量反馈到第二轮并补充测试、同分选中获批轮次、第三轮批准与预算停止、规格复核、规范化发现无进展、重复候选前置停止、基础未完成不送审、默认关闭、摘要只执行一次、错误审查记录、摘要/文件篡改、错误身份与反馈、混合来源、过期及重复消费。

S3 单节点测试的准备流程改为使用执行节点实际发出的 review_ready；仅在专门验证基础不达标门禁时人为构造非法送审输入。保留 S3 的 58 项证据门禁测试。

已完成针对性验证：

- 原 runner / 集成回归：43 passed（45.53 秒）。
- 生成适配器与 S3 节点回归：89 passed（140.46 秒）。
- 最终新增质量循环、集成与报告回归：66 passed（143.70 秒）。
- `python -m pip check`：No broken requirements found。

最终全量回归命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests vendor/agentdev/tests -q
```

结果：**933 passed**（455.85 秒 / 7 分 35 秒），包含原 906 项回归及新增 27 项 S4 测试。`git diff --check` 通过；未修改 vendor、生产生成适配器、共享服务、CLI 或 Web。

详细计划中 S4 的 12 项任务已勾选，README、protocol、workflow 和本地主清单同步更新。

## 阶段边界

S4 证明编排器能将获准质量反馈交给下一轮受控生成节点，并通过真实执行再次送审。生产 TestGenerationAgent 仍会以 invalid_revision_feedback 拒绝质量反馈，此边界有专门回归断言；S5 将单独实现质量补充上下文与 Prompt，继续保留历史已通过测试保护。

本阶段没有开放服务、CLI 或 Web 开关（S6），未实现完整质量调用统计、报告与运行图（S7），也没有进行真实模型或真实业务质量验收（后续阶段）。生成请求数与审查请求数尚未在现有 summary 中合并；审查遥测仍独立保存在节点 Result 和 response.json，不能将现有汇总误读为全部模型请求统计。
