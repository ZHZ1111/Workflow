# 质量审查 Agent：S3 单节点与证据保存

日期：2026-10-16。起始提交 `c8a9bdcf1b38a76a0f3c2b99a1b9030c667b62e4`，开始时工作区干净。本文记录 S3 结束时的状态：完成独立 TestQualityReviewAgent 及送审适配器，当时未接入 Workflow 自动循环、CLI 或 Web。后续调度接入见 [S4 记录](quality-review-s4.md)。

## 接口与归属

```python
from pathlib import Path
from test_workflow.agents.test_quality_review import TestQualityReviewAgent
from test_workflow.demo.quality_client import FixtureReviewClient

# 仓库根目录下选择固定响应样例。
client = FixtureReviewClient((Path("src/test_workflow/demo/data/quality_approve.json"),))
# client 也可替换为 S2 DeepSeekReviewClient；构造节点不发起请求。
agent = TestQualityReviewAgent(client)
# result = agent.run(context)  # context 为已登记当前 review_ready 的 AgentContext
```

节点实现现有同步 `Agent.run(AgentContext) -> AgentResult`，可按稳定 ID `test_quality_review` 注册。`adapters/quality.py` 负责送审门禁、证据核验、调用后的输入复核及宿主产物保存；节点只调用一次 ReviewClient，不执行 pytest、不调用执行 Agent、不改源码或候选，也不直接保存 WorkflowState、发送消息日志或提交消费标记。

## 调用前门禁

1. 重新校验 AgentContext 与其状态快照，要求节点、任务、当前轮次一致，workflow_status=running，开启 quality_review_enabled，模式为 offline/real，业务尚未停止，quality_status=not_run。
2. 当前候选必须已真实执行、全部通过、覆盖达标且无校验错误或生成疑问；重新调用旧 choose_action，必须仍建议 FINISH/completed/coverage_target_met。已有执行失败、覆盖缺口、缺陷复核或停止结论不能被审查推翻。
3. inbox 必须有唯一、已登记、未消费的当前轮 review_ready，发送者为 test_execution，指向当前唯一已登记 assessment；不允许选择性忽略状态中其他未消费审查消息。
4. 核验原始源码和规格文件与宿主快照一致，所有已登记生成产物摘要有效。当前 handoff 必须等于当前候选去掉执行/覆盖观察后的生成快照；测试文件路径/内容、计划内容及 subject 源码副本必须一致。
5. 执行 assessment 必须为 real completed，观察必须等于当前候选，且无完整性错误或反馈问题。执行证据必须登记在同候选、同次真实执行目录中，execution.json 和 coverage_result.json 必须与当前观察相等，覆盖目标为本任务 subject.py。
6. 历史审查必须具有已登记、摘要有效、内容相等的 QualityAssessment 与证据引用，且属于同一规格快照。共享 subject 仅复用本任务首轮引用，禁止未来轮次或跨候选证据；送审证据总数不超过 64。
7. 当前候选不得已有质量记录或 quality_reviews/NN 目录。完成所有输入校验后，先独占创建本轮目录，再调用客户端；目录创建失败不消耗请求。

ReviewContext 从这些真实宿主对象构造，传给客户端的是独立序列化副本。候选身份、测试摘要、实际送审的 UTF-8 规格快照摘要和引用均由宿主确定；模型不填写或选择这些值。

## 调用后与发现来源校验

客户端返回的必须是可重新校验的 ReviewResponse，原始业务响应与 decision 一致。节点检查其调用模式、调用次数、请求上限和最多一次格式修复；不因注入客户端返回不完整对象或越界遥测而采纳批准。

节点重新读取并核验送审输入，拒绝审查期间发生的源码、规格、测试、计划、执行证据变化及输出目录异常。发现中的 target_functions 必须存在于源码事实中；related_tests 必须是真实已有测试函数，缺失场景可以为空；spec_quote 和 test_code_quote（若提供）必须是实际送审文本中的原样片段。这些检查也适用于非阻塞建议。

片段存在仅证明引用来源，不证明模型的语义判断正确。节点不会把建议或问题转成“源码缺陷已实测确认”。客户端即使改动收到的请求副本，也不能修改节点使用的候选预算或共享状态。

## 产物与 Result

| 路径 | 格式与内容 | ArtifactRef.kind |
|---|---|---|
| `quality_reviews/NN/response.json` | ReviewResponse：有效 decision、原样保留的 raw_response 字符串与安全 telemetry | quality_response |
| `quality_reviews/NN/assessment.json` | QualityAssessment：宿主身份、候选/规格摘要、执行 assessment 引用、校验后的决定、登记证据与 UTC 时间 | quality_assessment |
| `quality_reviews/NN/failure.json` | 失败时的固定 error_code 与安全遥测；尽力保存，不包含上游异常原文 | 不作为成功产物提交，仅由 diagnostic_paths 指向本轮目录 |

成功写盘后重新读取并校验两份产物，要求内容等于准备返回的对象。所有引用使用任务内相对路径与文件 SHA-256；输入产物仅被引用，不重复注册。成功 Result 的 StatePatch 为空，assessment/generation 为空，quality_assessment 与 review 分别保存质量结论和审查遥测。

- approve：返回批准与证据，可保留有效的非阻塞建议，不生成反馈消息。
- request_revision：有剩余候选预算时提出一条 quality_feedback，来源轮次仍为 N，引用本轮 QualityAssessment，仅包含获准的阻塞发现，动作固定 ADD_MISSING_CASES。该消息是 Result 提案，未写入消息日志；S4 编排器还需决定进展、路由与是否提交。
- request_revision 且已为最后一轮：保留完整发现，不提出下一轮消息。
- request_review：保留问题与冲突证据，不提出生成反馈，不直接改变 TaskStatus。

## 失败与请求计数

客户端只调用一次，内部有界 HTTP 重试沿用 S2，节点不再套重试。合法 ReviewClientError 的实际请求、修复、用量和安全错误分类保留到 error Result。门禁/目录创建失败记录 client_calls=0、request_attempts=0；客户端抛出未提供可信遥测的未知异常时，记录调用一次，但请求数/修复数/用量保留 null，不能当作零请求。

error Result 不含成功补丁、QualityAssessment、消息或 ArtifactRef。写盘失败或内容被破坏时，即使留下 response.json 等诊断文件，也不能提交批准。创建过的本轮目录会阻止同候选再次请求，包括尚未提交或失败的调用；不会覆盖已有记录或自动删除后重试。诊断写入失败不隐藏原错误。

## 验证与阶段边界

新增 `tests/test_quality_agent.py` 共 **58 项**。测试先通过正式生成/执行节点取得真实 pytest/coverage 证据，再由测试宿主登记 review_ready 后调用审查节点；未伪造通过计数，也未发起外部模型请求。S3 独立节点本身的调用在禁用 subprocess.Popen 和执行工具的断言下通过，源码、候选、输入证据及 state.json 的摘要和修改时间保持不变。

覆盖批准、合法补充、缺失场景空测试名、最后一轮、规格冲突、非阻塞建议、假引用/未知名称、错身份/消息/轮次、摘要及内容伪造、基础覆盖不足/缺陷复核、重复调用、输入中途变化、写盘/回读失败、未知异常与 MockTransport 超时/鉴权/非法响应。另验证第二轮复用首轮源码引用，以及 S3 产物能满足 S1 的独立 completed 门禁。

针对性命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_quality_agent.py tests/test_quality_contracts.py tests/test_quality_client.py -q
```

结果：**194 passed**（105.22 秒）；之后补充未来轮次 subject 引用的送审前拒绝，单项验证 **1 passed**，并纳入最终全量回归。首次运行有 6 项测试夹具构造错误（错节点仍携带旧接收者 inbox、Mock 自动生成 safe_config），修正后上述验证通过。

最终全量回归：`.\.venv\Scripts\python.exe -m pytest tests vendor/agentdev/tests -q` → **906 passed**（385.25 秒），包含最终全部 58 项 S3 测试。`pip check` 和 `git diff --check` 通过；vendor、既有 WorkflowRunner、流程定义及服务未修改。

S3 九项任务均已勾选，README、protocol 和本地主清单同步更新。本阶段只使用固定审查响应及 MockTransport，未调用真实模型。

S3 结束时，S4 仍需让执行节点正式产生 review_ready、注册和调度审查、交叉核验 Result/文件/消息、提交质量状态和消息消费，并完成预算/无进展路由与批准候选选择。当时生产定义、主循环、服务和入口均未修改；独立节点测试不宣称已经完成自动三 Agent 闭环。
