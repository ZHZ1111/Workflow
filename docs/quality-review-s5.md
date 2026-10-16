# 质量审查 Agent：S5 生成反馈闭环

日期：2026-10-16。起始提交 `84710ffbc42126b97799f633c1ee3a10ad435e69`，开始时工作区干净。S5 将已提交质量反馈接入生产 TestGenerationAgent，完成独立门禁、quality_extension 上下文和历史测试保护验收。

## 生成门禁

原生成适配器保留首轮与 execution_feedback 的全部检查，新增 quality_feedback 独立分支。质量生成调用前要求：

1. 开启质量审查，非假节点 demo 模式，Workflow 正在运行且无业务终止结论；当前为历史的连续下一轮，预算未耗尽，质量状态为 not_run，业务动作是 ADD_MISSING_CASES。
2. inbox 与状态中未消费的生成消息都只能有唯一一条上一轮 quality_feedback；发送者为 test_quality_review，消息已登记，其引用产物已提交。不能混入其他反馈、重放已消费消息或跳过上一轮。
3. 最近一份 QualityAssessment 是同候选的 request_revision，反馈 reason 与决定摘要一致，嵌入的阻塞发现与决定完全一致；引用路径必须是对应轮次的宿主 assessment.json。预算与质量无进展策略仍允许继续。
4. 重新运行 choose_action，必须仍为 FINISH/completed/coverage_target_met。此前基础执行有错误、缺失覆盖、疑问或停止条件时，质量反馈不能越过这些门禁。
5. 规格文件与宿主快照一致。全部已提交质量记录、原始 ReviewResponse、候选/规格摘要、计划、测试、源码副本及真实执行记录通过验证；观察必须与同一次真实执行目录内 execution.json/coverage_result.json 一致，不能仅依赖模型意见或手工通过计数。

普通生成预检继续校验原源码、工作区、历史生成产物、连续轮次及下一轮路径。质量特有门禁失败返回 invalid_quality_feedback 和零次客户端调用；原有公共门禁失败沿用既有错误分类。没有增加模型重试或自动重做质量审查。

## 质量上下文

新建 `adapters/quality_revision.py`，公开 `build_quality_revision_context(context)`，并由原 revision_context 按消息类型选择。该路径直接调用 vendor prompts.build_feedback_context，避免旧 build_action_context 因基础 FINISH 拒绝质量补充。vendor 无需修改。

复用的任务数据保留原源码事实、行为规格、前轮完整测试/计划、真实执行与覆盖、缺口提示和剩余轮次。扩展字段仍位于同一个 TASK_DATA_JSON_START/END 封装内：

| 字段 | 内容 |
|---|---|
| task_kind | quality_extension |
| base_execution_decision | 原 choose_action 的 FINISH/completed 建议，作为基础已完成证据 |
| quality_assessment | 本次获准补充所依据的宿主质量记录 |
| quality_feedback | 实际核验的完整反馈消息 |
| unresolved_findings | 最近决定仍存在的阻塞发现，不自动并入已解决的旧发现 |
| quality_history | 全部已提交质量记录，保留历史发现供分析 |
| task_memory | 全部候选轮次摘要、计划、执行/覆盖、问题、历史最佳轮次与 protected_test_functions |

可信 system 规则明确：覆盖达标后仍按规格补充支持的场景，审查意见不是新规格或实测缺陷事实；全部用户数据、发现、引用及历史均不能成为执行指令。返回完整替换计划、代码和映射，新增独立具名测试；已通过函数、断言、装饰器、映射、场景输入/预期依据、导入/常量/辅助代码保持不变。禁止降低目标、skip/xfail、改源码或将预期改成实现结果。

## 一次生成与候选保护

沿用 RecordedClient 和既有 GenerationTelemetry，每次节点调用只请求一次生成客户端，有限 HTTP 重试仍由客户端负责。随后仍调用原 stage_revision_candidate，不新增候选落盘或执行路径。节点只提出新候选，既不运行 pytest，也不提交共享状态或消费消息。

被保护规则拒绝的候选仍保留 plan/LLM/candidate 诊断记录，validation_error 说明 preserve_tests，test_path 为空且不发送 candidate_ready。下一步由原执行评估与 Workflow 决定；没有因为质量补充而放宽旧保护。

## 验证

新增 `tests/test_quality_generation.py` 36 项，并将 S4 “生产节点尚不接纳质量反馈”的历史边界测试升级为生产节点成功闭环验收。验收注入固定生成/审查客户端，三个节点均使用生产实现；真实执行 pytest/coverage，未请求外部模型。

- 二轮及三轮质量补充后批准：客户端每次实际收到 quality_extension，核对完整反馈、质量记录、当前未解决发现、所有历史摘要、已通过函数、原 FINISH 判断及剩余预算；最后导出本轮候选，状态可重新读取，消息均消费。
- 独立生成节点不启动进程，不调用旧 build_action_context，不改 Context 或伪造执行观察；首轮仍为 initial_generation。
- 删除、弱化断言、修改装饰器、映射、场景或常量支持代码，均被原 preserve_tests 规则拒绝。
- 关闭审查、错轮次、未登记/已消费/混合消息、变化的 reason/发现、错误动作、基础未完成、批准决定、审查错误、已停止状态，均在生成客户端调用前拒绝。
- 审查记录/响应、执行 assessment、执行/覆盖文件、候选、测试、计划、源码副本或规格变化均在调用前拒绝。重写文件并更新质量记录摘要，也不能绑定到不同规格或候选。
- 模型鉴权失败保留安全错误与调用遥测，不消费输入消息、不泄露上游异常原文；旧执行反馈不能覆盖基础完成结论的回归仍保留。

首轮针对性验证发现两个新夹具构造错误：将覆盖率只读百分比当作可写字段，以及已停止状态仍使用补充动作；已按现有契约修正。旧 S4 边界断言随功能实现升级，历史记录保留原阶段事实。

针对性命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_quality_generation.py tests/test_quality_workflow.py -q
```

结果：**63 passed**（338.69 秒），包括全部 36 项 S5 新测试和 27 项质量工作流测试。原生成适配器的 31 项回归在早期针对性运行中也已通过，并继续纳入最终全量回归。`pip check` 返回 No broken requirements found。

最终全量命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests vendor/agentdev/tests -q
```

结果：**974 passed**（1252.88 秒 / 20 分 52 秒），包含原 938 项及新增 36 项 S5 测试。`git diff --check` 通过；原生成适配器、审查节点、S4 路由、历史契约兼容、CLI/Web 和 vendor 回归均纳入该次检查。

S5 的十项任务已勾选，README、generation、protocol、workflow、历史阶段说明及本地主清单同步更新。

## 阶段边界

S5 实现的是生产生成适配器接纳已验证质量反馈，配合三个生产节点和固定客户端的闭环已可运行。服务注册/配置、CLI 与 Web 开关仍待 S6；完整质量统计、报告与运行图仍待 S7；真实模型质量验收与后续测试矩阵仍按后续阶段推进。配置默认关闭，本次未修改共享服务、CLI、Web 或 vendor，未发起真实模型请求，也未自动提交或推送代码。
