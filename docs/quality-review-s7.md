# 质量审查 Agent：S7 报告、运行图与统计

日期：2026-10-17。起始提交 `ee204f938a27b4ffda7d5125b6068466938fae71`，开始时工作区干净。S7 七项任务完成：质量证据、反馈消费、批准候选及用量分项进入摘要、报告与 Web；只读 inspect 同步验证质量引用。调度、三个生产节点的行为、候选保护和 vendor 不变。

## 报告与证据

`summary.json` 导出版本升级为 **1.2**。WorkflowState、AgentResult 及 AgentDev 模型版本不变；旧状态按已有缺省字段读取，inspect 生成当前版摘要，不覆盖旧报告。

| 字段 | 含义 |
|---|---|
| `quality_review_enabled/status/stop_reason` | 开关、当前质量状态及停止原因 |
| `quality_assessments` | 状态中已提交的逐轮审查，包含规格/测试引用、发现、建议与问题 |
| `round_details[].quality` | 该轮审查状态、实际原因、assessment/response 路径、遥测、证据核验及反馈 |
| `messages[].source_round/consumer_round/consumer_node` | 来源及已提交消费位置；尚未消费时接收位置为 null |
| `unresolved_findings` / `unresolved_findings_round` | 最近一次已提交审查的阻塞发现及来源；后轮未送审不会清空旧发现 |
| `selected/latest/exported_quality_approved` | 分别核对选中、最新、实际导出候选；关闭审查为 null，启用但未批准为 false，没有实际导出时导出项为 null |
| `quality_integrity_errors` | 普通汇总读取发现证据不一致的轮次，不能将其显示成批准 |

`report.md` 分开列出基础 ExecutionAssessment 与质量决定，展示 spec_quote、test_quote、目标函数、补充建议、人工问题、原始文件链接和反馈消费轮次。最终选择同时显示选中与最新候选的执行/批准情况，不能用较早可用候选掩盖最新未批准状态。disabled、基础未完成的 skipped、待送审 not_run、人工复核、预算/无进展、审查异常分别显示。

质量报告读取共用 `validate_quality_record()`：核对固定路径输出、ArtifactRef 摘要、规格/候选/计划/真实执行证据，以及已提交审查节点 Result 中的决定与遥测。普通构建遇到这些质量证据损坏时标记 error；`inspect --summary` 严格拒绝，并重新校验状态完成约束、反馈内容和唯一下一轮生成 Context。inspect 不加载模型、不重放节点、不写文件；测试比较全部文件摘要及修改时间。

## 请求与 token 口径

`model_usage.generation/review/total` 分别记录客户端调用、请求尝试、外部请求、prompt/completion/total token 及已知小计；`agents` 仍分别记录生成、执行、审查和摘要的职责、节点调用/提交次数。生成修复请求与审查修复请求不增加 Workflow 轮次。

1. `generation_external_requests`、`review_external_requests` 是分项；`total_external_requests` 和 `external_requests` 同为总量。**1.1 的 external_requests 是生成量，1.2 扩展为生成加审查**，消费者可据版本判断。
2. 根级 `token_usage` 同样改为总量，分项从 `model_usage` 读取。业务 `AgentState.api_calls/token_usage` 继续只归生成 Agent，本次不改写它们。
3. 按节点记录的遥测汇总，包含有匹配身份遥测的失败/被拒绝提案；没有 Result、遥测缺失或身份不匹配时保留未知。未知分项使对应总量为 null，`known_*` 仅表示已知小计。没有调用的分项明确为零，不补造缺失 token 或请求。
4. 固定客户端 external_requests 为零；计数测试用注入客户端返回已记录遥测验证非零汇总，未发送真实模型 HTTP 请求。demo 假节点没有实测模型用量。

## 页面

固定运行图增加第三个生产 Agent、执行→审查→摘要路径和质量反馈边，保留基础停止时的执行→摘要路径。高亮依据真实 node/route/message 事件及状态快照；新轮次不会沿用上一轮批准。审查关闭显示“未启用”，基础未达标显示未调用原因；缺少当前批准不能将结束节点显示为绿色。

Web POST 队列响应及任务快照增加 `quality_review_enabled`，使用已接受的严格布尔配置；state.json 尚未生成时也能正确显示等待审查，不会短暂显示为关闭。该字段不表示审查已经发生。页面展示生成/审查/总请求与 token、逐轮质量发现和消费位置；源码、规格与意见均用 textContent 输出。兼容旧摘要时，没有新总量字段的质量任务显示未知总请求。

本机浏览器检查使用生产服务/节点、固定生成和审查客户端，真实运行两轮 pytest/coverage：首轮要求补充，第 2 轮消费反馈、补充测试并获批准。Web 配置仍为 real，客户端由本机验证脚本注入；画面中的请求来自受控遥测，不代表实际模型费用。桌面实测 clientWidth 与 scrollWidth 均为 1265；另用宽 390 像素的只读 iframe 检查原页面的窄屏纵向布局。临时标签页和服务已关闭。

- [二轮完整任务](../outputs/s7-preview/705f7881b3f24cd982e732a9ae15cc76/outputs/web/2024e4996dec4871aaa3efa4b23f6edb/summary.json)
- [桌面运行图](../outputs/s7-preview/quality-graph.png)、[窄屏截图](../outputs/s7-preview/quality-narrow.png)

outputs 按既有规则忽略；这些是本机证据，源码测试可重新生成任务。

## 验证

新增 `tests/test_quality_reporting.py` 25 项和 `tests/web/quality-view.test.cjs` 9 项，覆盖逐轮证据、消费、当前批准、旧任务、禁用/跳过/预算/复核/错误、未知遥测、非零分项总量、只读与篡改检测，以及实际 DOM 文本渲染。既有 Web 入口测试补充队列和最终快照开关断言。

| 范围 | 本阶段结果 |
|---|---|
| 报告与质量编排：`tests/test_reporting.py tests/test_quality_workflow.py` | 39 passed，221.86 秒 |
| 报告、原编排、入口、Web：`tests/test_quality_reporting.py tests/test_reporting.py tests/test_workflow_integration.py tests/test_quality_entrypoints.py tests/test_workflow_web.py` | 103 passed，285.95 秒 |
| 契约、入口、系统兼容：`tests/test_quality_reporting.py tests/test_quality_entrypoints.py tests/test_quality_contracts.py tests/test_contract_examples.py tests/test_system.py` | 140 passed，143.75 秒 |
| 定向复验：质量报告与 Web 开关注册 | 27 passed，61.71 秒 |
| 最终摘要 1.2 / 总量别名：`tests/test_quality_reporting.py` | 25 passed，56.85 秒 |
| `node --test tests/web/*.test.cjs` | 25 passed |
| `pip check` | No broken requirements found |

这些命令存在交集，且早期运行时新增测试尚未全部加入，不能相加作为独立测试总数。完整 S8 矩阵、稳定后的全量 Workflow/vendor 回归及 S9 真实模型验收尚未执行；S5/S6 历史结果不冒充本阶段全量结果。

安装验证复用 `scripts/verify_quality_install.py`，离线构建两个 wheel，使用 Python -I 与非 editable --target 安装检查资源、质量 CLI、批准导出和严格 inspect；复用当前锁定第三方依赖，不宣称全新 venv。最终摘要 1.2 版本四项检查均通过，证据为 [results.json](../outputs/quality-install-verification/6f58f2823ea34e30b39a472e26325006/results.json)，相邻目录保存构建、安装和 CLI 日志及安装后的完整任务。

S7 没有真实模型调用，没有自动提交或推送代码。下一阶段为 S8。
