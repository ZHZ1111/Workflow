# 质量审查 Agent：S2 Prompt 与独立客户端

日期：2026-10-16。起始提交为 `8cf6139b3398cca0bcfc28ab21b25c5f01df9173`，开始时工作区干净。本记录保存 S2 完成时的独立 Prompt、真实/离线客户端和受控验证；后续单节点进展见 [S3 实施记录](quality-review-s3.md)。

## 接口与职责

```python
from test_workflow.quality_client import ReviewClient, ReviewResponse, DeepSeekReviewClient
from test_workflow.quality_prompts import ReviewContext, build_review_prompt
from test_workflow.demo.quality_client import FixtureReviewClient

# 两个客户端共享同步接口；真实客户端只使用显式传入的 AppConfig。
# ReviewClient.review(context: ReviewContext) -> ReviewResponse
# 失败抛出 ReviewClientError，携带固定 code 与安全 telemetry。
```

| 文件 | 内容 |
|---|---|
| `src/test_workflow/quality_prompts.py` | ReviewContext、宿主固定 System Prompt、ReviewDecision JSON Schema、JSON 数据封装及受限格式修复摘要 |
| `src/test_workflow/quality_client.py` | ReviewClient 协议、ReviewResponse、ReviewRetryPolicy、DeepSeekReviewClient、严格响应解析及 ReviewClientError |
| `src/test_workflow/demo/quality_client.py` | 注入固定 JSON 文件的 FixtureReviewClient；不读取配置，不请求 HTTP，不执行候选 |
| `src/test_workflow/demo/data/quality_*.json` | approve、request_revision、request_review 三种包内业务响应样例 |
| `src/test_workflow/quality_contracts.py` | ReviewTelemetry 兼容增加可空 latency_seconds，旧记录缺省为 null |
| `tests/test_quality_client.py` | 独立客户端、Prompt、预算、错误、用量及离线响应测试 |

ReviewContext 包含当前任务/候选身份、非空规格快照、SourceInfo、带完整计划与代码的 CandidateRound、ExecutionAssessment、最多两轮历史 QualityAssessment、候选轮次预算和本次 HTTP 尝试上限。身份和轮次必须一致，历史按轮次唯一递增且来自本任务较早轮次，当前候选必须具有同候选的真实 completed 执行建议，不接受 injected、缺失观察或无效候选。最末轮仍可审查批准。结构化上下文会在调用边界再次校验，防止 model_copy 或嵌套字典改动绕过校验。

这些校验不证明传入的观察来自实测文件。S3 负责在调用前核对已登记文件、消息、摘要及执行证据，并在返回后验证规格引用、函数/测试名和片段来源。S2 客户端只接受业务 ReviewDecision，不能将模型返回的身份、命令、状态或路径当作宿主元数据。

## Prompt 与数据边界

- System Prompt 固定职责、规格优先级、不可修改源码或弱化通过断言、审查意见与实测结论的区别，以及 approve/revision/人工复核规则。
- 源码、注释、规格、计划、候选、执行日志与历史意见都位于 `REVIEW_DATA_JSON_START/END` 之间的单个 JSON 对象中；数据中的指令、角色或分隔标记仍为字符串数据。
- 规格、源码和候选计划/代码完整保留。stdout/stderr 各最多 8000 字符，并显式保留/设置 truncated 标志，不把截断日志当作完整记录。
- Prompt 省略 SourceInfo.source_path、CoverageResult.target_file、候选测试路径和历史产物路径等宿主路径元数据；日志、源码或规格中原有的路径仍是待分析数据。
- Schema 直接来自 `ReviewDecision.model_json_schema(mode="validation")`；不调用生成客户端、不使用 GeneratedTests 返回 Schema，也不调用 vendor 的私有解析方法。
- JSON 解析拒绝重复键、非有限常量、Markdown、额外字段及不一致 verdict；不通过宿主归一化把非法决定变为 approve。

## HTTP、修复与预算

真实客户端复用 `testgen_agent.llm_client.create_http_client()` 和 AppConfig，采用现有 `POST chat/completions`、JSON mode、`stream=false`、`thinking.type=disabled` 及配置 max_tokens/超时。工厂保留现有代理设置，不跟随重定向，没有 transport 自动重试。客户端不隐式读取 `.env`。

默认 ReviewRetryPolicy：总 HTTP 尝试最多 3 次，暂态重试最多 2 次，逻辑格式修复最多 1 次。实际总上限取策略与 ReviewContext.max_request_attempts 的较小值；两个重试分支共用一个 attempts 计数。例如：

| 响应顺序 | 结果 |
|---|---|
| 成功 | 一次请求，零修复 |
| 非法 JSON → 成功 | 两次请求，一次修复 |
| 503 → 非法 Schema → 成功 | 三次请求，一次暂态重试、一次修复 |
| 非法 JSON → 超时 → 成功 | 三次请求；修复请求的暂态重试也占同一预算 |
| 503 → 503 → 非法 JSON | 三次后失败，不创建第四次修复请求 |
| 连续两次非法 Schema | 两次后失败；不会再次启动格式修复 |
| 401/402/403 | 一次后立即失败 |

超时、网络、429、408/409 和服务端错误可有限重试；400/404 等永久请求错误立即失败。Retry-After 数值或日期受有限最大等待约束，非法/非有限值不会造成无限等待。默认退避为 0.25 秒、0.5 秒，最大 2 秒。

可修复的模型 JSON/Schema 错误和 `finish_reason=length` 共用唯一一次修复。修复只追加宿主生成的至多 8 条路径/错误类型摘要；未知字段名被替换为占位符，不包含模型原文、输入值、Pydantic 校验消息或认证信息，也不把非法回复重新注入 assistant 消息。HTTP 响应封装损坏、空内容、工具调用或非正常结束直接失败。响应体上限为 2,000,000 字节，业务内容上限为 512,000 字符。

## 返回值与遥测

成功返回 `ReviewResponse(decision, raw_response, telemetry)`；raw_response 保留有效业务 JSON，必须能重新解析为相同 decision。失败不返回空发现加 approve，统一抛 ReviewClientError；其字符串和 safe_details 仅含固定代码与安全遥测，不含上游响应体、底层异常原文、Key 或认证头。

ReviewTelemetry 单独记录一次客户端调用、实际 request_attempts、预算、逻辑修复次数、总耗时、用量及错误类别/状态码。耗时覆盖等待与重试，错误路径同样记录。HTTP 客户端创建前失败记录零请求；不会把审查填入生成遥测或修改 agent_state.api_calls。

直接使用真实 HTTP 工厂时 client_mode=real，external_requests 等于尝试数；传入 httpx.MockTransport 时 client_mode=mock，保留模拟尝试数而 external_requests=0。离线客户端的 attempts/repairs/external_requests 均为 0。用量逐分量累加：只有相关请求的对应数值均已知才相加；任一请求整体用量未知时总用量保持 null，不能把暂态失败或缺失值算成零。

离线客户端依序读取显式选择的包内/本地响应文件。文件缺失、非法输出或用完均产生独立错误，不自动补 approve。现有 `pyproject.toml` 的 `data/*.json` 已覆盖新样例；干净 wheel 安装后的演示验证仍属于 S6/S8。

## 验证

本次测试使用合成执行观察与 MockTransport，未调用外部模型。S2 新增 70 项测试，覆盖正常独立请求、生成接口未被调用、上下文和 Prompt 数据边界、末轮批准、一次修复、共享预算、超时/网络/鉴权、非法 Schema/响应封装、未知用量、密钥脱敏、离线三种决定及显式失败。

针对性回归：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_quality_client.py tests/test_quality_contracts.py tests/test_contracts.py vendor/agentdev/tests/test_llm_client.py vendor/agentdev/tests/test_prompts.py -q
```

结果：**199 passed**（10.67 秒），包含原生成客户端与 Prompt 回归。

全量回归：`.\.venv\Scripts\python.exe -m pytest tests vendor/agentdev/tests -q` → **848 passed**（257.79 秒）。`pip check` 和 `git diff --check` 通过；vendor 与 upgrade-roadmap 未修改。

S2 八项任务全部勾选，README、protocol 和本地主清单已同步。此次没有外部模型请求；独立审查客户端尚未接入生产节点、Workflow、CLI 或 Web，S3–S9 保持未完成。
