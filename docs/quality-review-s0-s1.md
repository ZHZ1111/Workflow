# 质量审查 Agent：S0–S1 实施记录

日期：2026-10-16。本记录保存 S0–S1 完成时的协议与状态基础；后续客户端进展见 [S2 实施记录](quality-review-s2.md)。

## S0：基线与范围冻结

- 起始提交：`b2920aabce408a64841f1cd3eabe97d4c3ea8c76`。实施步骤文档中的 `97fde32` 是编写时参考，实际实施以本条为准。
- 开始修改前 `git status --porcelain=v1` 为空。`docs/upgrade-roadmap.md` 和 `docs/quality-review-agent-plan.md` 均已跟踪，保留原文件及其后续阶段计划。
- 现有 Python 验证命令：`.\.venv\Scripts\python.exe -m pytest tests vendor/agentdev/tests -q`；基线实测 **711 passed**（254.34 秒）。
- 现有前端验证命令：`node --test tests/web/workflow-view.test.cjs`；基线实测 **13 passed**。
- 依赖检查：`.\.venv\Scripts\python.exe -m pip check`，基线实测 `No broken requirements found.`。

冻结的第一版约定：

1. 仅在原执行评估准备以 `completed/coverage_target_met` 完成时审查。基础失败、修复、覆盖不足和人工复核分支保留原规则。
2. 候选总预算仍为 `max_rounds=1–3`；不额外创建质量循环预算。每候选最多提交一份审查；未来客户端每次审查最多 3 次 HTTP 尝试，重试与格式修复共用该预算。
3. 开关固定为 `WorkflowConfig.quality_review_enabled: StrictBool = False`。S1 仅增加配置契约，后续 S6 才开放 CLI/Web 启用入口。
4. 输入继续是单文件 Python 源码与非空行为规格；不增加变异测试、属性测试、多文件、自动源码修复、通用 DAG 或恢复执行。
5. 审查只提出意见，不改候选、执行观察或生成用量。具体补充必须有规格证据；重复、风格建议不阻塞；规格不明确转人工复核。

现有读取链路与后续修改归属：

| 现有位置 | 已有行为 | 后续阶段归属 |
|---|---|---|
| `definitions.py` / `workflow.py` | 生成、执行，以及可选尾部摘要；执行反馈在候选循环内 | S4 增加循环内审查，避免被 `nodes[2:]` 再次调用 |
| `events.jsonl` / `messages.jsonl` / `state.json` | 保存节点、消息、消费、路由、停止和状态 | S4 增加真实质量转换与消费记录 |
| `web.py` 的 `/api/runs/<id>` | 同一任务 HTTP 快照提供 state、summary、events、messages | S6/S7 传递配置与新增审查数据 |
| `web_static/workflow-view.js` / `app.js` | 已有运行图和节点详情，由上述快照投影；调用数目前读取 summary.external_requests | S7 扩展现有图与生成/审查分项，不重建界面 |
| `reporting.py` | 导出 summary.json、report.md；inspect 只读核验证据 | S7 展示和校验所选轮次的质量结论 |

完整后续文件分工继续沿用 [实施步骤第 6 节](quality-review-agent-plan.md#6-文件修改清单)。S2/S3 负责 Prompt、客户端、证据适配与单节点，S4/S5 负责路由和质量反馈生成，S6/S7 负责入口和展示，S8/S9 负责闭环验收与真实模型记录。本次不改 vendor。

## S1：冻结的协议

`quality_contracts.py` 仅依赖标准库、Pydantic 与 vendor 共有模型，不导入 state/contracts。所有新增模型继承冻结、拒绝额外字段的 ContractModel。

| 契约 | 约束 |
|---|---|
| `Finding` | 六种 category；严格 bool；1–5 个目标函数，最多 50 个已有测试引用；同一列表不重复 |
| `ReviewDecision` | summary/description/suggested_case/question 各最多 2000 字符，引用各最多 4000 字符；最多 20 个发现、10 个问题；发现 ID 唯一 |
| `approve` | 无阻塞发现、问题或规格歧义；可含非阻塞建议 |
| `request_revision` | 至少一条阻塞发现；每条都有规格引用与非空建议；无规格歧义或未解决问题；redundancy 不能阻塞 |
| `request_review` | 必须有非空具体问题和带规格引用的发现；不把冲突变为源码缺陷结论 |
| `ReviewTelemetry` | 客户端调用 0–1；HTTP 尝试 0–3；格式修复 0–2 且包含在同一次 HTTP 预算；real/mock/offline 分开，未知请求和 token 用量保留 null |
| `QualityAssessment` | 宿主填写任务/轮次、测试文件 SHA-256、UTF-8 规格快照 SHA-256、执行 assessment 引用、业务 decision、证据和 UTC 时间 |
| `QualityFeedback` | 仅带已获准的阻塞发现与匹配的 quality_assessment 引用；动作固定 ADD_MISSING_CASES，不占用 ExecutionFeedback |

`QualityAssessment.evidence` 为 1–64 个唯一文件引用，必须含恰好一个 tests 引用且摘要等于 candidate_sha256。允许 candidate/tests/plan/subject/execution/coverage/execution_evidence；execution_assessment 单独引用 kind=assessment。所有证据属于当前任务及候选；仅共享 subject 可以复用本任务较早轮次的已登记引用，匹配现有源码副本只在首轮登记的行为。该结构限制不替代 S3 的送审前文件核验。

`validate_review_evidence()` 检查目标函数、真实已有测试名、规格片段与测试片段是否属于送审输入；缺失场景允许 related_tests 为空。该函数验证引用来源，不能证明模型意见的语义正确。跨轮发现规范化及无进展判断在 S4/S5 接入，不依赖模型复用 finding_id。

新增消息保持 schema_version=1.0：

- `review_ready`：test_execution → test_quality_review；来源轮次；assessment 引用；不同时含执行反馈或质量反馈。
- `quality_feedback`：test_quality_review → test_generation；来源轮次；严格的 QualityFeedback；下一轮目标由编排器指定。

新增 `ArtifactRef.kind=quality_assessment/quality_response`，继续使用相对路径与 SHA-256 约束。AgentResult 新增可空 quality_assessment/review；只有 test_quality_review 可以提交质量产物、结论、遥测或质量反馈。其 StatePatch 必须为空，不能夹带 generation/执行 assessment。消息中的阻塞发现和 assessment 引用必须与 Result 一致。error Result 不可携带成功结论、产物、消息或补丁，但可保留安全 review 遥测和诊断路径。落盘内容与 Result 的交叉提交核验在 S3/S4 实现。

## S1：状态与完成门禁

WorkflowState 增加 quality_status、quality_stop_reason 和最多三份、按轮次递增且唯一的 quality_assessments。旧输入缺省解释为 disabled；开启配置且未提供质量状态时初始化为 not_run。状态和原因必须按实施步骤第 4.4 节配对，passed/changes_requested/needs_review 必须对应当前轮的真实同任务 assessment。开始下一轮时须显式重置 not_run 并清空 quality_stop_reason，历史 assessment 保留。

独立函数 `quality_state.validate_quality_completion()` 已接入测量模式的完成终态校验：

1. 开启质量审查时，只有 passed/approved 可以 completed，选中的必须是当前唯一 approve 候选，并与 business.best_available_round 一致。
2. 所选候选的测试路径、内容、SHA-256、规格摘要与批准记录一致；送审计划与源码副本必须存在，且与当前候选计划、源码事实快照一致。
3. quality assessment、执行 assessment 和全部引用证据必须已登记，落盘文件摘要有效，保存的 quality assessment 与状态对象相等。
4. 真实执行 assessment 必须给出 completed 建议，执行/覆盖结果与所选候选一致；继续保留原有测试通过和覆盖达标门禁。
5. 重新读取状态或 StateStore.save 时同样核验；关闭开关的旧状态不会增加文件读取。

这只是终态约束，尚未实现审查节点调度。因此现在不能通过配置开关获得三 Agent 闭环；后续 S4/S6 将实现正式启用路径。旧 TaskStatus、StopReason、生成请求统计和 summary 版本不变。

业务 JSON 样例见 [approve](../examples/contracts/quality/approve.json)、[request_revision](../examples/contracts/quality/request_revision.json)、[request_review](../examples/contracts/quality/request_review.json)。它们是协议样例，不是实际模型审查记录；第三份使用一段有冲突的独立规格。

## 验证记录

新增 `tests/test_quality_contracts.py` 检查错误 verdict、无建议的 revision、证据范围/来源、错候选、消息与 Result 不一致、节点越权、错误结果、请求预算、严格开关、P1/P3/P4/P6 历史样例、批准对象与导出候选一致、落盘篡改和下一轮状态重置。终态门禁测试用固定生成客户端与两轮真实 pytest/coverage 获取基础执行证据，再构造宿主批准记录；未调用外部模型，也不宣称已完成生产审查闭环。

针对性验证：`.\.venv\Scripts\python.exe -m pytest tests/test_quality_contracts.py tests/test_contracts.py -q` → 初次通过 83 项；补充计划/源码绑定与篡改用例后，最终 **86 passed**（4.13 秒，其中新增质量测试 67 项）。新测试首次因引用不存在的历史样例文件名失败，已改为实际的 `p1/state.json` 后通过。

`node --test tests/web/workflow-view.test.cjs` → **13 passed**；`.\.venv\Scripts\python.exe -m pip check` → `No broken requirements found.`；`git diff --check` 通过。

`.\.venv\Scripts\python.exe scripts/verify_workflow.py` → 既有三个 CLI 离线案例通过：calculation 一轮 completed、validation 两轮 completed、discount 一轮 needs_review。实际执行 pytest/coverage，外部请求均为 0；记录在 `outputs/verification/46b713bc884440d3884b44d139e90872/results.json`。此脚本的 offline 模式不运行 Web 入口；Web 回归由全量测试承担。

全量回归：`.\.venv\Scripts\python.exe -m pytest tests vendor/agentdev/tests -q` → **775 passed**（261.04 秒）。该轮启动后补充了 3 项计划/源码绑定及篡改用例，并加强对应门禁；这些最终变动已由上述 **86 项针对性验证**覆盖，未把它们计入本轮 775 项的结果。两组验证有重叠，数量不相加。

S0/S1 清单已勾选；README、protocol 和本地主清单已同步。vendor 与 `docs/upgrade-roadmap.md` 无改动。整个阶段未发起外部模型请求，S2–S9 保持未完成。
