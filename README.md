# 多 Agent 测试 Workflow

面向单文件 Python 代码的测试工作流项目。计划由测试生成 Agent 规划并生成 pytest 候选，测试执行与评估 Agent 执行、采集覆盖率和归因，Workflow 统一调度二者并执行有限反馈，最后导出测试和报告。

**当前阶段：P2 已完成。** 在 P1 协议与顺序交接基础上，已实现 `TestGenerationAgent`：复用 AgentDev 单步首轮/修订、候选门禁、历史通过测试保护、错误分类和调用计数。当前 Workflow 测试 89 项通过，相关 vendor 回归 74 项通过；执行与评估 Agent、完整反馈路由将在 P3/P4 接入。`testgen_agent` 仍是复用项目的基线入口。

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

生成节点通过 Python 接口注入已有 `GenerationClient`，支持本地 fixture 和 `DeepSeekClient`。P2 验收没有读取 `.env` 或使用真实模型；正式模型 CLI 和完整路由留到后续阶段。接口与错误约定见 [docs/generation.md](docs/generation.md)。

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

真实生成会将样例源码、行为规格和模型输出契约发送至配置的 DeepSeek 服务。当前会话的这一步尚待用户授权；P0 已保存离线证据。获得授权后，可自行配置本地 `.env`：

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
| [TASK_CHECKLIST.md](TASK_CHECKLIST.md) | 60–70 小时全阶段清单 |

## 当前目录

```text
docs/                       需求、复用、样例、测试计划、开发管理
evaluation/                 基线报告、结果索引、快照哈希清单
examples/                   四组源码与规格、离线候选 JSON
scripts/                    P0/P1/P2 复现与准备脚本
src/test_workflow/           协议、状态、顺序编排、生成适配器与假节点 CLI
tests/                      新 Workflow 的协议、编排与生成节点测试
vendor/agentdev/             原运行源码、依赖、文档、样例、测试
outputs/                    本地完整运行证据（Git 忽略）
```

输入边界为单个不超过 128 KiB、含 1–5 个顶层同步函数的 Python 文件和非空规格；不支持顶层类、异步语法、多文件工程、相对/通配符导入、第三方依赖。执行器会运行本机代码，独立目录和子进程不构成安全沙箱，使用可信输入。已有 CLI 的行为详见 [vendor/agentdev/README.md](vendor/agentdev/README.md)。
