# 多 Agent 测试 Workflow

面向单文件 Python 代码的测试工作流项目。测试生成 Agent 根据源码事实和行为规格规划并生成 pytest 候选，测试执行与评估 Agent 执行、采集覆盖率并保守归因，Workflow 统一调度二者并执行有限反馈，最后导出测试和报告。

**当前阶段：P6 已完成。** 两个生产 Agent、最多三轮反馈、历史候选选择、可追溯报告、只读检查与无模型 `ResultSummaryNode` 示例已实现。204 项 Workflow 回归和五个 P6 离线 CLI 案例通过，证据见 [日志/报告/扩展验收](evaluation/p6.md)。运行下面的离线命令即可查看协作与真实 pytest/coverage；P5 的三个真实模型案例独立保留在 [验收记录](evaluation/acceptance.md)。干净安装、演示和提交整理属于 P7。

## 环境与安装

使用 CPython 3.13，当前验证平台为 Windows x64。以下命令从本仓库根目录执行，无需原 hw1 工程。

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e vendor/agentdev
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
.\.venv\Scripts\python.exe -m pip check
```

锁定依赖直接沿用 AgentDev；`vendor/agentdev` 保持原包名 `testgen_agent`，新包名为 `test_workflow`。当前环境的安装与 `pip check` 已通过；完整干净目录交付验证属于 P7。

## 运行完整 Workflow

```powershell
# 正常首轮完成；输出默认保存到 outputs/workflow/<run_id>/
.\.venv\Scripts\python.exe -m test_workflow run --demo --source examples/calculation.py --spec examples/calculation.md
# 确定发生两轮反馈，执行与覆盖均为真实观察
.\.venv\Scripts\python.exe -m test_workflow run --demo --source examples/validation.py --spec examples/validation.md
# 明确缺陷：保留失败断言，输出 needs_review
.\.venv\Scripts\python.exe -m test_workflow run --demo --source examples/discount.py --spec examples/discount.md
```

`run --demo` 不读取 `.env` 或调用外部模型，状态为 `mode=offline`。默认候选随包提供，仅适用于对应样例；其他输入用重复的 `--fixture path.json` 指定每轮 `GeneratedTests` 响应。`run` 使用同一 Workflow 和两个真实节点；P1 的 `demo` 子命令只展示假节点交接。

`--max-rounds` 为 1–3，默认 3；`--timeout` 为单轮 pytest 超时 1–300 秒，默认 30。`--output` 指定**新的完整任务目录**，任何已存在路径都会被拒绝；省略时自动创建唯一目录。`run_id` 独立于目录名称。输出包含业务结论、停止原因、选中轮次与报告路径。退出码：0 为 `completed`，1 为 `partial` / `needs_review` / `failed` 或节点错误，2 为输入、配置或无法持久化产物；参数语法错误也为 2。

每次运行保存 `state.json`、节点 Context/Result、事件耗时、结构化消息、各轮原始测试与实测证据，以及 `summary.json` 和 `report.md`。可用候选按旧策略选择，原字节复制到 `final/test_subject.py`，同目录带 `subject.py`。报告分别列出选中与最新候选的通过状态；后轮失败不会被历史通过记录掩盖。无可用候选时没有 `final/`。

报告包含两个 Agent 的职责与调用/提交数、每轮收集/执行/通过/失败/错误/跳过计数、覆盖可用性与缺口、建议理由、消息和路由，以及对应原始证据的链接。停止原因、生成客户端调用、HTTP 请求和 pytest 启动分别记录；未知量保留 null。查看方式见 [报告说明](docs/reporting.md)。

## P6 扩展节点与只读查看

```powershell
# 在最终导出前注册无模型摘要节点，主循环共用原调度
.\.venv\Scripts\python.exe -m test_workflow run --demo --with-summary --source examples/validation.py --spec examples/validation.md
# 使用上条命令打印的任务目录，只读取已有状态
.\.venv\Scripts\python.exe -m test_workflow inspect <任务目录>
# 校验登记证据和最终副本，输出带逐轮详情的摘要；不改写文件
.\.venv\Scripts\python.exe -m test_workflow inspect <任务目录> --summary
# 五个新 CLI 案例、报告链接/只读验证及全量 Workflow 回归；不调用外部模型
.\.venv\Scripts\python.exe scripts/p6_acceptance.py
```

扩展节点不计作第三个 Agent，不调用模型或 pytest，不改变业务结论。产物在 `extensions/NN/result_summary.json`，返回统一 ArtifactRef 并经编排器校验；它记录的是导出前结论，最终完成状态看 `state.json`。实现、注册和定义示例见 [扩展指南](docs/extensions.md)。默认任务目录唯一；若显式指定 `--output`，复跑时请选择新目录。

| 常见情况 | 处理 |
|---|---|
| 输出路径已存在，退出 2 | 使用新目录或省略 `--output`，不会覆盖历史 |
| 非内置样例用离线模式，退出 2 | 提供与源码/规格匹配的 `--fixture` JSON；内置只支持 calculation/validation/discount |
| 输入/配置/产物读取或保存失败，退出 2 | 检查 UTF-8 非空规格、支持范围、文件权限和目录；不在日志打印密钥 |
| 有覆盖率但结论为 partial 或 needs_review | 查看停止原因、最新候选和逐轮评估，覆盖率不代表所有行为正确 |
| coverage unavailable 或测试超时 | 保留真实执行记录；不报告达标，不自动延长预算 |
| inspect --summary 校验失败 | 检查产物是否缺失或被改写；不通过重新执行覆盖原证据 |

任务工作区隔离不是执行安全沙箱；生成测试会在当前解释器环境运行。支持范围及归因边界见下方与 [docs/scope.md](docs/scope.md)。

真实模型模式去掉 `--demo`，使用现有 DeepSeek 配置适配器：

```powershell
.\.venv\Scripts\python.exe -m test_workflow run --source examples/calculation.py --spec examples/calculation.md --env-file .env --max-rounds 3 --timeout 30
```

该命令会向配置的服务发送源码、规格和反馈。P4 阶段只用 Mock 验证真实 CLI 接线；P5 已在获得三个必需案例的具体外传授权后运行 DeepSeek，正常和多分支样例完成，discount 输出疑似源码缺陷。完整实测见 [P5 验收](evaluation/acceptance.md)。

复现 P4 离线闭环与回归：

```powershell
.\.venv\Scripts\python.exe scripts/p4_baseline.py
```

脚本通过正式 CLI 运行 14 个确定案例，并保存 [evaluation/p4_results.json](evaluation/p4_results.json)、JUnit 与文本结果。详细路由和状态约定见 [docs/workflow.md](docs/workflow.md)。

## P5 系统与真实模型验收

```powershell
# 本地确定案例 + 系统回归，不读取 .env 或调用模型
.\.venv\Scripts\python.exe scripts/p5_acceptance.py --offline --tests
# 读取索引中的已有任务，重新校验文件、交接并更新报告；不重跑节点
.\.venv\Scripts\python.exe scripts/p5_acceptance.py --refresh
# 使用本地配置，重新运行三个必需真实案例；会向服务发送数据并产生模型费用
.\.venv\Scripts\python.exe scripts/p5_acceptance.py --real --env-file .env
```

本次三个真实案例：calculation 为 6 passed、行覆盖 100%；validation 为 15 passed、行/分支覆盖 100%/100%；discount 为 15 passed、2 failed，保留 `calculate_discount(100) == 90` 并输出 `needs_review/suspected_source_defect`。共发出 4 次真实模型请求，其中 validation 有一次客户端格式修复。两个成功案例都在首轮达标，自动反馈能力由独立离线案例证明。

[evaluation/acceptance.md](evaluation/acceptance.md) 和 [机器可读索引](evaluation/acceptance.json) 区分离线生成、真实测试、Mock/故障注入和真实模型流量，并关联每轮候选、消息、覆盖及实际 JUnit testcase。可选关系样例和重复稳定性测试本次未执行。P6 另行验证报告与正式扩展示例；干净安装仍留在 P7。

## 运行 P1 节点交接

```powershell
.\.venv\Scripts\python.exe -m test_workflow demo --source examples/calculation.py --spec examples/calculation.md
# 安装后的命令也可用：.\.venv\Scripts\test-workflow.exe demo --source examples/calculation.py --spec examples/calculation.md
.\.venv\Scripts\python.exe -m test_workflow inspect outputs/p1/<run_id>
```

`demo` 从随包提供的 calculation 确定性候选生成交接文件，第二个假节点读取并核对该文件、状态和测试代码摘要。输出为 `mode=demo workflow_status=finished task_status=unmeasured`。演示不读取 `.env`、不调用模型、不执行 pytest，也不产生测试通过或覆盖达标结论。使用其他样例时须通过 `--fixture` 指定匹配的 `GeneratedTests` JSON。

每次新建 `outputs/p1/<run_id>/`，保存 `run.json`、`state.json`、`events.jsonl`、`messages.jsonl` 和 `demo_artifacts/01/`。`agent_workspace/` 保持不存在，供 P2 首轮保存使用。`inspect` 校验并读取已有状态，不进行断点续跑。退出码：0 为演示完成或读取成功，1 为节点/交接失败，2 为输入或状态读取校验失败。

复现本次 P1 验收与契约样例：

```powershell
.\.venv\Scripts\python.exe scripts/p1_baseline.py
# 仅运行新 Workflow 自动测试
.\.venv\Scripts\python.exe -m pytest tests -q
```

验收脚本运行新 Workflow 测试并保存 `evaluation/p1_results.json`、JUnit 和文本结果；生成的上下文、结果、消息、状态及 JSON Schema 位于 `examples/contracts/p1/`。示例中的绝对路径对应生成时的实际任务，换机器仍可校验协议结构；重新执行脚本可生成本机引用。详见 [P1 验收记录](evaluation/p1.md) 和 [协议说明](docs/protocol.md)。

## 运行 P2 生成节点验收

```powershell
.\.venv\Scripts\python.exe scripts/p2_baseline.py
```

脚本用可注入的 `FixtureClient` 驱动真实 `TestGenerationAgent`，验证合法首轮、语法错误、合法修订、删除通过测试、弱化通过断言五个案例。每次节点调用只保存一个候选；生成期间拦截子进程启动。修订案例的首轮 pytest/coverage 由脚本在节点外执行，以提供真实通过记录和覆盖缺口；脚本手动提供反馈，不代表 P4 自动路由已经实现。

产物位于 `outputs/p2/<run_id>/`：内层 `agent_workspace/rounds/01|02/` 保存 vendor 原始计划、候选与客户端结果，外层 `handoffs/` 保存 Workflow 候选引用，`node_results/` 保存节点 Context/Result，状态由协调脚本通过 `WorkflowRunner.apply_result()` 校验后提交。状态保持 `running`、业务结论为空，表示单步生成证据尚待执行节点评估。

精简证据见 [evaluation/p2.md](evaluation/p2.md) 与 `evaluation/p2_results.json`；协议样例与更新后的 Schema 位于 `examples/contracts/p2/`。离线候选调用数和真实外部请求数分别记录，真实外部请求数为 0。需重建错误候选时运行 `scripts/build_p2_fixtures.py`。

生成节点通过 Python 接口注入已有 `GenerationClient`，支持本地 fixture 和 `DeepSeekClient`。P2 验收没有读取 `.env` 或使用真实模型；正式 CLI 和自动路由由 P4 接入。接口与错误约定见 [docs/generation.md](docs/generation.md)。

## 运行 P3 执行与评估验收

```powershell
.\.venv\Scripts\python.exe scripts/p3_baseline.py
.\.venv\Scripts\python.exe -m pytest tests -q
```

验收使用离线生成候选和真实执行节点，检查通过、低覆盖后的单步修订、非法语法、空测试、断言失败、名称/导入/收集错误、疑似缺陷、超时及 coverage 导出失败。每个有效候选只启动一次 pytest；无效候选只生成评估反馈。此 P3 脚本手动传递修订消息；P4 自动闭环使用新的 p4_baseline.py。

完整产物保存在 `outputs/p3/<run_id>/`，执行证据位于内层 `rounds/NN/executions/run-*/`，外层 `evaluations/NN/assessment.json` 给出动作建议、问题、目标覆盖和证据引用。索引见 [evaluation/p3_results.json](evaluation/p3_results.json)，协议样例在 `examples/contracts/p3/`。整个验收不读取 `.env`，外部模型请求数为 0。需重建故障候选时运行 `scripts/build_p3_fixtures.py`。

节点接口、建议与最终业务结论的区别见 [docs/execution.md](docs/execution.md)。默认执行器运行真实 pytest；故障注入与手工底层 no_tests 案例在验收记录中单独说明。

## 复现 P0 离线基线

```powershell
.\.venv\Scripts\python.exe scripts/p0_baseline.py
```

脚本不读取 `.env`、不访问模型服务，运行旧项目完整自动测试，检查四组输入，并使用确定的本地候选执行正常、补充覆盖、缺陷复核三个案例。pytest 和 coverage 实际执行。产物写入新建的 `outputs/p0/<时间-随机标识>/`；精简结果更新到 `evaluation/p0_results.json`。退出码 0 表示旧项目离线基线符合预期，1 表示检查或样例失败；这不等于新 Workflow 已验收。

候选 JSON 已随仓库提供。需要根据脚本重新构建时运行：

```powershell
.\.venv\Scripts\python.exe scripts/build_p0_fixtures.py
```

## 真实模型基线

真实生成会将样例源码、行为规格和模型输出契约发送至配置的 DeepSeek 服务。P0 已保存旧项目离线证据，旧项目真实基线本次未补跑；P5 已对新 Workflow 完成三个授权真实案例，两类记录分别保留。可在本地 `.env` 配置实际模型和密钥：

```powershell
Copy-Item .env.example .env
# 在 .env 中填写账户实际可用的模型名称和 Key
.\.venv\Scripts\python.exe -m testgen_agent --check-config
.\.venv\Scripts\python.exe scripts/p0_baseline.py --real --env-file .env
```

配置检查不会发送模型请求；真实基线只允许一轮 calculation 候选，但客户端内部可能有有限重试与格式修复。`.env`、虚拟环境和原始运行输出均由 Git 忽略，交付只包含占位配置。

## 工程文档

| 文档 | 内容 |
|---|---|
| [Design.md](Design.md) | 目标架构、流程、状态与实现决策 |
| [docs/scope.md](docs/scope.md) | 需求基线、范围与可核验验收项 |
| [docs/reuse.md](docs/reuse.md) | AgentDev 版本、真实签名、异常、快照与适配边界 |
| [docs/samples.md](docs/samples.md) | 输入样例、确定反馈候选、预期结果 |
| [docs/test-plan.md](docs/test-plan.md) | 后续测试矩阵与证据标准 |
| [docs/development.md](docs/development.md) | 开发阶段、风险、文档维护与下一步 |
| [evaluation/baseline.md](evaluation/baseline.md) | 本次实测记录及限制 |
| [docs/protocol.md](docs/protocol.md) | P1 接口、消息、状态所有权与持久化约定 |
| [evaluation/p1.md](evaluation/p1.md) | P1 离线阶段验收与边界 |
| [docs/generation.md](docs/generation.md) | P2 单步生成/修订接口与错误、计数约定 |
| [evaluation/p2.md](evaluation/p2.md) | P2 五组离线验收、测试与复用回归 |
| [docs/workflow.md](docs/workflow.md) | P4 自动路由、轮次预算、状态与结果选择 |
| [docs/reporting.md](docs/reporting.md) | 逐轮报告、事件语义、只读证据检查 |
| [docs/extensions.md](docs/extensions.md) | ResultSummaryNode 与新增节点方法 |
| [evaluation/p4.md](evaluation/p4.md) | P4 CLI 闭环、停止分支与回归证据 |
| [evaluation/acceptance.md](evaluation/acceptance.md) | P5 系统矩阵、真实模型结果与边界 |
| [evaluation/acceptance.json](evaluation/acceptance.json) | P5 逐轮指标、输入摘要及 JUnit 证据索引 |
| [evaluation/p6.md](evaluation/p6.md) | P6 日志、报告与正式扩展示例验收 |
| [evaluation/p6_results.json](evaluation/p6_results.json) | P6 CLI、链接、只读检查及回归结果 |
| [TASK_CHECKLIST.md](TASK_CHECKLIST.md) | 60–70 小时全阶段清单 |

## 当前目录

```text
docs/                       需求、复用、样例、测试计划、开发管理
evaluation/                 基线报告、结果索引、快照哈希清单
examples/                   四组源码与规格、离线候选 JSON
scripts/                    P0–P6 复现、验收与准备脚本
src/test_workflow/           两 Agent、适配器、协议、有限反馈编排、CLI 与导出
tests/                      新 Workflow 协议、节点、编排与 CLI 测试
vendor/agentdev/             原运行源码、依赖、文档、样例、测试
outputs/                    本地完整运行证据（Git 忽略）
```

输入边界为单个不超过 128 KiB、含 1–5 个顶层同步函数的 Python 文件和非空规格；不支持顶层类、异步语法、多文件工程、相对/通配符导入、第三方依赖。执行器会运行本机代码，独立目录和子进程不构成安全沙箱，使用可信输入。已有 CLI 的行为详见 [vendor/agentdev/README.md](vendor/agentdev/README.md)。
