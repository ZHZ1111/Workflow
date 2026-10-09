# TestGen Studio

TestGen Studio 是一个面向单文件 Python 代码的测试生成 Agent。它读取源码和明确的行为规格，调用 DeepSeek 生成 pytest 测试，在独立工作区中执行测试与覆盖率采集，并根据真实反馈最多迭代三轮。

项目提供本地 Web 工作台和命令行入口。两个入口都需要同时提供源码和行为规格。原始源码不会被修改；正常完成结果导出时会保留生成测试、执行结果、覆盖率、状态摘要和结构化轨迹。

## 主要能力

- 基于 AST 提取函数、分支、调用点和同文件直接调用关系；Agent 根据这些事实和规格判断关联，选择单元测试或协作测试，不能确认的调用会标明不确定性。
- 用 Pydantic 校验模型返回的测试计划、测试代码和依据。
- 在独立目录中运行 pytest 和 coverage，统计目标 `subject.py` 的行/分支覆盖率。
- 依据语法错误、执行失败、覆盖缺口和规格冲突选择修复、补充或停止。
- 发现规格支持的源码缺陷时保留失败断言，并标记为“需要复核”。
- 逐步保存 `run.json`、`trace.jsonl`、`state.json` 和 `summary.json`。

## 环境与安装

要求 CPython 3.13。锁定依赖已在 Windows x64 验证。下面的命令适用于 Windows PowerShell；在 Linux 上可将 `.\.venv\Scripts\python.exe` 换为 `.venv/bin/python`，并使用 `cp .env.example .env`，但本仓库未提供 Linux 验证记录。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
```

编辑 `.env`，将下面的模型占位值替换为账户可用的模型标识，并填写 API Key：

```dotenv
LLM_MODEL=your-deepseek-model
DEEPSEEK_API_KEY=your-real-key
LLM_MAX_TOKENS=8192
```

`.env` 已被 Git 忽略。程序不会把 Key 写入报告、轨迹或模型上下文。

先做本地配置检查：

```powershell
.\.venv\Scripts\python.exe -m testgen_agent --check-config
```

## Web 工作台

```powershell
.\.venv\Scripts\python.exe -m testgen_agent.web
```

打开 `http://127.0.0.1:8765`，粘贴 Python 源码和行为规格后开始生成。默认只监听本机回环地址。运行产物位于 `outputs/web/`，输入快照位于 `outputs/web-inputs/`。页面中的任务列表保存在服务进程内；重启后旧任务不会重新出现在列表中，仍可到磁盘目录查看其产物。

若浏览器提示无法连接，先确认启动命令所在终端仍在运行，并确保没有旧进程占用 8765 端口：

```powershell
Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue
```

## 命令行生成

```powershell
.\.venv\Scripts\python.exe -m testgen_agent `
  --source examples/calculation.py `
  --spec examples/calculation.md
```

也可以指定输出目录和预算：

```powershell
.\.venv\Scripts\python.exe -m testgen_agent `
  --source examples/relationships.py `
  --spec examples/relationships.md `
  --output outputs/my-run `
  --max-rounds 3 `
  --timeout 30
```

输出目录必须尚不存在，避免覆盖历史运行。命令完成后会打印状态、停止原因、结果目录、最终测试文件和摘要文件。`completed`/`partial` 返回 0，`needs_review`/`failed` 返回 1，输入或配置错误返回 2。

## 运行产物

每次任务的核心文件如下：

| 文件 | 内容 |
|---|---|
| `subject.py` | 被测源码的任务内副本；导出结果时恢复原始内容 |
| `rounds/NN/plan.json` | 本轮测试计划 |
| `rounds/NN/test_subject.py` | 本轮候选测试 |
| `rounds/NN/executions/` | pytest、JUnit、coverage 和日志 |
| `test_subject.py` | 选中的最终测试 |
| `state.json` | 完整 Agent 状态 |
| `summary.json` | 含结论、核心指标、函数关联与测试策略的机器可读摘要 |
| `report.md` | 面向用户的 Markdown 报告，包含结论、测试数据、覆盖率、关联依据、场景和限制 |
| `trace.jsonl` | 按顺序追加的执行轨迹 |

输入范围：单个不超过 128 KiB 的 Python 源文件，包含 1–5 个顶层同步函数；当前不支持顶层类、异步语法、相对/通配符导入和第三方依赖。关联分析主要确认同文件直接调用，并提供有限的数据流线索；动态调用不会被当作已证实的关联。生成的测试会在本机执行，工作区隔离不构成安全沙箱，因此只应提交可信的 Python 源码和规格。复杂断言或多个函数协作的失败通常需要人工复核，不能保证自动判定为源码缺陷。

## 示例与测试

`examples/` 包含正常计算、多分支校验、明确边界缺陷、同文件直接调用、同文件独立函数，以及较复杂的订单结算样例。订单样例由 `order_flow.py`、`order_flow.md` 和手写参考测试 `test_order_flow.py` 组成；参考测试可用 `python -m pytest -q examples/test_order_flow.py` 单独运行。

```powershell
.\.venv\Scripts\python.exe -m pytest -q -W error
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip check
```

核心实现位于 `src/testgen_agent/`，自动测试位于 `tests/`，精简后的验收结果位于 `evaluation/`。

## 项目结构

```text
src/testgen_agent/
  agent.py              控制循环与动作决策
  prompts.py            首轮/反馈 Prompt
  llm_client.py         DeepSeek 适配、重试与格式修复
  generation.py         候选校验与落盘
  failure_analysis.py   失败归因
  models.py             数据契约
  tracing.py            运行轨迹
  tools/                源码分析、pytest 与 coverage
  web.py                本地 Web 服务
  cli.py                命令行入口
  web_static/           无构建步骤的前端
```

实现约束、数据流与停止条件见 [Design.md](Design.md)。
