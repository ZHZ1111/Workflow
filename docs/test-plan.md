# 测试与验收计划

版本：P6 / 2026-10-11。本表是新 Workflow 的完整验收计划；旧项目基线见 `evaluation/baseline.md`，P1–P5 历史证据保留。当前 204 项 Workflow 回归、五个 P6 CLI 案例与日志/报告/正式扩展证明见 `evaluation/p6.md` / `p6_results.json`。P5 的 15 个离线案例和 3 个真实模型案例及其矩阵在 `evaluation/acceptance.md` / `acceptance.json`。验收以行为和证据为准，不以方法名字或旧项目测试通过代替。

## 测试分层

| 层次 | 内容 | 阶段 |
|---|---|---|
| 旧能力基线 | 全部 vendor 测试、锁定依赖一致性、三类确定样例 | P0 |
| 协议与状态 | 类型、任务归属、来源轮次、状态提交、持久化 | P1/P5 |
| 单节点 | 生成不执行、执行不生成、校验门禁与历史保护 | P2/P3 |
| 新编排集成 | 顺序、分支、预算、历史选择、失败保存、扩展节点 | P4/P5/P6 |
| 轨迹和报告 | 消费事件、决策证据、报告链接、只读产物校验、正式尾部摘要节点 | P6 |
| 真实模型 | 正常、多分支、明确缺陷；如可行加入关联样例 | P5 |
| 干净环境 | 解包后相对安装，不依赖 hw1 环境/文件 | P7 |

## 新 Workflow 验收矩阵

| ID | 场景 | 核心检查 | 预期 |
|---|---|---|---|
| W01 | calculation 正常 | 两 Agent 交接、实际执行、报告 | completed，选中第 1 轮 |
| W02 | validation 确定补充 | 两份固定候选，第二轮收到首轮实测反馈 | 2 轮完成，保护测试保留 |
| W03 | 生成语法/名称/映射错误 | 无效候选不执行；反馈含错误位置 | 有预算修复，无预算停止 |
| W04 | discount 明确缺陷 | 保留 100→90 断言及规格依据 | needs_review/suspected_source_defect |
| W05 | 复杂失败无法归因 | 不把当前实现当正确预期 | needs_review/ambiguous_specification |
| W06 | 预算耗尽 | 所有节点调用在候选上限内 | round_limit_reached；partial/failed |
| W07 | 重复候选/无覆盖进展 | 不继续耗费轮数 | no_progress，保留历史 |
| W08 | pytest 超时 | 真实超时路径和已完成证据 | execution_timeout |
| W09 | coverage 不可用 | execution 可留存，指标空而非 0 | partial/failed，未达覆盖验收 |
| W10 | 后轮失败 | 最终测试来自真实可用历史 | 标明选中轮次；保留失败证据 |
| W11 | 错 run_id/轮次/候选/receiver | 拒绝不匹配交接，状态不被污染 | 明确错误记录 |
| W12 | 重复消息 | 同进程不重复执行依赖动作 | 消费去重 |
| W13 | 中文/空格路径 | 固定命令 shell=False、正确轮次执行 | 可运行，数据与文件对应 |
| W14 | 空规格/非法输入/已存在输出 | 模型未调用，历史未覆盖 | 输入或配置错误退出 |
| W15 | 文件归属/源码副本变化 | 拒绝越界读取和变化副本执行 | 清晰失败与证据 |
| W16 | 节点/持久化异常 | 阻止依赖节点，不伪称保存成功 | workflow_status=error |
| W17 | ResultSummaryNode 注册插入 | 不改写调度循环业务特例 | 有节点记录和输出 |
| W18 | 干净目录安装及 demo | 无硬编码 hw1 路径 | 只凭仓库 README 复现 |

## 证据规范

每个验收项记录用例 ID、模式（离线/真实模型）、配置、运行标识、源码与规格版本、每轮测试数、执行种类、目标覆盖、动作和停止原因、选中轮次、耗时、模型请求数及报告路径。P5 已生成 `evaluation/acceptance.json` 与 `evaluation/acceptance.md`；矩阵状态根据真实任务和实际 JUnit testcase 计算，不预先填“通过”。

离线候选仅替换生成客户端，执行器尽量实际运行；真实模型首轮达标不强求发生反馈，可用 W02 证明反馈机制。真实模型故障时保存离线证据并标明真实验收未完成。P0 的 api_calls 在固定客户端中是旧适配器的模拟尝试计数，真实外部请求数单独记录为 0。

输入源文件执行前后校验哈希；每轮路径与候选编号对应；无分支覆盖显示 null/N/A；失败断言与明确规格一致时不得改成实现的当前结果。

## 运行与回归策略

P0：`python scripts/p0_baseline.py`（使用 README 中虚拟环境解释器）。P1：`python scripts/p1_baseline.py`，或仅执行 `python -m pytest tests -q`；根 pyproject 的默认测试范围是新 `tests/`，不包含 vendor 全量基线。P2/P3 修改适配器后跑相关节点测试与受影响 vendor 测试；P4 后跑新 Workflow 集成矩阵；P7 做最终全量回归和干净安装。

P2：`python scripts/p2_baseline.py` 手动提供真实首轮执行证据，证明单步修订与通过测试保护，不视为 W02 自动路由已完成。新测试包含 MockTransport 客户端有限重试、格式修复、鉴权错误、未知次数/用量，均无外部请求。复用回归范围是 vendor 的 `test_generation.py`、`test_llm_client.py`、`test_prompts.py`。

P3：`python scripts/p3_baseline.py` 用真实执行节点验证 12 类诊断与反馈案例，11 次 pytest 子进程全部结束；低覆盖的一次手动修订证明 P2 消费真实 P3 反馈。26 项执行适配测试覆盖零进程门禁、精确轮次/超时、目标覆盖、错误归因、进程停止、导出失败、执行后完整性变化和伪造证据；33 项新增契约样例验证纳入 148 项整体回归。相关 vendor 回归范围为 test_test_runner.py、test_coverage_runner.py、test_failure_analysis.py、test_agent.py，共 128 项。W01/W02 的最终状态和自动调度部分仍待 P4/P5。

只为有风险的协议、路由、历史保护和跨节点行为写测试；文案、简单 getter 等低影响改动不写镜像测试。检查通过后不无理由重复跑全量测试。

P4：`python scripts/p4_baseline.py` 通过正式 CLI 实测 14 个案例、17 次 pytest 执行。新增 27 项编排/CLI 集成测试覆盖顺序、反馈来源、修复、三轮预算、无进展、历史保护与选择、缺陷复核、超时、覆盖导出错误、节点/模型失败、报告失败、扩展接入、输出路径和模式配置。当前回归 175 passed；相关 vendor test_agent.py 与 test_generation.py 共 52 passed。真实 CLI 接线和结构化模型失败使用 Mock 或故障客户端，实际模型请求为 0；真实质量仍待 P5。

P5：`python scripts/p5_acceptance.py --offline --tests` 用当前 Workflow 重新执行 15 个确定 CLI 案例，并运行新 Workflow 与生成/客户端/执行/覆盖/归因相关 vendor 回归。新增 17 项 tests/test_system.py 重点检查跨节点损坏、错误修订保留历史、持久化失败、阻塞问题、中文/空格输入和非法输入零模型调用；全量 Workflow 192 passed，相关 vendor 180 passed。已有 P4 测试仍证明具体反馈进入下一轮生成 prompt。

三个必需真实样例在用户明确授权具体数据发往 api.deepseek.com 后执行：calculation 为 6 passed、100% 行覆盖；validation 为 15 passed、100%/100%；discount 为 15 passed、2 failed，needs_review/suspected_source_defect。请求数分别 1、2、1，validation 有一次客户端格式修复，候选仍只有一轮；本次没有真实 Workflow 反馈提升，不作跨次稳定性结论。可选关系样例未调用。

P5 时 A01–A15 的核心矩阵关联新实测或本次回归证据；正式扩展示例 W17 和干净安装 W18 分别留在 P6/P7。持久化持续失败时无法保证新 error 状态写入，验证的是抛错、保留最后成功快照、不继续依赖执行且不虚报产物已保存。

P6：`python scripts/p6_acceptance.py` 运行五个新离线 CLI 任务并验证每个报告链接和 inspect 前后全部文件摘要/修改时间不变，共六次真实 pytest，零外部请求。新增 12 项 tests/test_reporting.py 验证 ResultSummaryNode 不改变业务状态、五类结论、反馈发送/消费归属、历史选择、不可用覆盖、未消费反馈及缺失/篡改产物；完整 Workflow 回归 204 passed。正式 W17 已完成，P5 历史矩阵不追改当时的待交付状态。旧 P5 三个真实任务兼容读取、文件未变；当前 pip check 通过，W18 的干净安装仍待 P7。证据见 evaluation/p6.md、p6_results.json、p6_tests.xml 和 p6_compatibility.json。
