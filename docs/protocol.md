# Agent 通信与共享状态协议

三个 Agent 通过类型化 Context/Result、消息及共享状态协作，定义以 [contracts.py](../src/test_workflow/contracts.py)、[state.py](../src/test_workflow/state.py) 和 [quality_contracts.py](../src/test_workflow/quality_contracts.py) 为准。

## 单写入交接

1. 编排器读取当前已提交状态，选出该节点尚未消费的合法消息，保存 Context。
2. 节点从快照和任务内证据读取输入，执行本职工作，返回 AgentResult 提案。
3. 编排器保存 Result，验证任务/节点/轮次/候选及产物摘要，验证补丁和消息语义。
4. 验证成功后提交新状态，标记 inbox 消息已消费，追加事件；失败则停止依赖步骤。

| 对象 | 内容与约束 |
|---|---|
| AgentContext | run_id、agent_id、轮次、工作区、状态快照及 inbox；不包含 API 密钥 |
| AgentResult | 状态、受限 StatePatch、消息、产物引用、评估与安全遥测；错误不得携带成功补丁 |
| ArtifactRef | 任务内相对路径、种类、轮次/候选和 SHA-256；防越界与文件替换 |
| WorkflowState | 原业务历史、节点记录、消息/消费集合、编排与业务状态、审查记录 |
| ExecutionAssessment | 一轮真实执行、coverage、问题及建议；未执行不能编造覆盖 |
| QualityAssessment | 当前候选的审查决策、引用核验、阻塞发现、遥测与保存证据 |

## 消息类型

| 类型 | 发送者 → 接收者 | 消费时机 |
|---|---|---|
| candidate_ready | test_generation → test_execution | 同轮，候选已校验并保存 |
| execution_feedback | test_execution → test_generation | 下一轮生成，携带执行/覆盖缺口和保护测试 |
| review_ready | test_execution → test_quality_review | 同轮，基础达标且开启审查 |
| quality_feedback | test_quality_review → test_generation | 下一轮补充，携带有规格依据的场景遗漏 |

消息校验任务标识、轮次、候选、发送与接收节点、允许的反馈边、负载与产物摘要。ID 唯一，重复或错投消息不得消费；下一轮反馈必须来自上一轮。生成候选无效或存在规格疑问时，执行节点可以只作评估而不要求正常 candidate_ready。

## 审查契约

ReviewDecision 的 verdict 仅为 approve/request_revision/request_review。approve 不允许未解决阻塞发现或疑问；request_revision 需要有依据且可执行的阻塞补充建议；request_review 需要具体问题和规格证据。引用核验检查函数/测试名、规格原文、测试片段是否真实存在，不替代语义评判。审查失败、缺响应或证据不符均不得默认批准。

批准与反馈绑定候选，只有当前有效批准才能支撑质量完成和导出。质量补充仍须通过原候选校验与历史通过测试保护。

## 记录与兼容

状态与 JSONL 各自落盘，节点 Context/Result 和证据可串联查看。摘要当前版本为 1.2，`model_usage` 区分 generation/review/total；未知请求量或 token 保留 null，不伪装成零。业务生成请求计数与总请求分别保留。

`inspect --summary` 验证并只读重建摘要；旧任务缺少质量字段时按默认关闭兼容。示例 [examples/contracts](../examples/contracts) 包含不同历史版本，供自动测试验证兼容性，不作为跨机器启动参数。当前协议以代码模型为准，不把旧 Schema 当成完整现行契约。

仅编排器保存状态，节点不直接提交共享状态；快照替换和日志不是跨文件事务，不支持回放续跑。状态中不保存配置密钥，但含用户源码和规格，原始运行目录属于本地证据。
