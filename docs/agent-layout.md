# 三个 Agent 的统一目录结构

2026-10-19 起，原 AgentDev 代码与当前生成、执行、质量审查节点整合为一个 Python 包。三个 Agent 在同一目录下并列组织，不再保留独立运行依赖 `vendor/agentdev` 或安装 `testgen-agent`。

```text
src/test_workflow/
  agents/
    base.py
    test_generation/
      __init__.py                  # 延迟加载公开 TestGenerationAgent
      agent.py                     # Workflow 生成节点
      core/                        # 复用的 AgentDev 运行实现
        agent.py                   # 原策略、上下文与旧循环（回归用）
        generation.py              # 生成、候选校验、历史测试保护
        llm_client.py              # 原测试生成模型客户端
        prompts.py
        config.py
        models.py
        failure_analysis.py
        workspace.py
        tracing.py
        reporting.py
        cli.py, web.py             # 原入口实现，供兼容回归
        tools/                     # 源码分析、pytest、coverage、结果插件
        web_static/                # 原 Web 资源，供原测试及兼容入口
    test_execution/
      __init__.py                  # 公开 TestExecutionAgent
      agent.py                     # 执行与评估节点
    test_quality_review/
      __init__.py                  # 公开 TestQualityReviewAgent
      agent.py                     # 质量审查节点
  adapters/                        # 与三个 Agent 配套的交接适配器
  quality_*.py                     # 审查协议、客户端、路由等共享能力
  workflow.py, service.py, ...      # 编排及公共服务
```

`core` 随生成 Agent 一起打包，是该项目内部代码，不是单独安装的第三方包。原 AgentDev 已有的执行工具与数据模型继续被三个节点共享，避免在迁移时复制出多个不兼容的结果模型。

## 导入方式

三个 Agent 的公开导入路径保持不变：

```python
from test_workflow.agents.test_generation import TestGenerationAgent
from test_workflow.agents.test_execution import TestExecutionAgent
from test_workflow.agents.test_quality_review import TestQualityReviewAgent
```

复用实现改为工程内部导入，例如：

```python
from test_workflow.agents.test_generation.core.models import CandidateRound
from test_workflow.agents.test_generation.core.tools import run_tests_with_coverage
```

Agent 的实际实现模块是各目录中的 `agent.py`。需要替换模块内工具的测试应 patch 该实现模块，例如 `test_workflow.agents.test_execution.agent.run_tests_with_coverage`。

公开包入口采用延迟导入，避免“共享模型导入生成包 → 生成节点导入 Workflow 协议 → 协议再次导入共享模型”的循环依赖。

## 安装与验证

根 `pyproject.toml` 直接声明 httpx、Pydantic、python-dotenv、pytest 和 coverage。只安装根项目：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest tests -q
node --test tests/web/*.test.cjs
```

也可以使用 `python scripts/bootstrap.py`。已存在的本机环境在本次迁移中已重装根项目并移除旧独立包；其他旧环境如仍保留 `testgen-agent`，可在安装根项目后自行卸载该旧包。

`scripts/verify_quality_install.py` 离线构建一个 wheel，在隔离目录中安装，检查三 Agent 文件、资源、实际 pytest 执行、质量流程和最终报告；不依赖旧包可导入。

pytest 子进程显式加载新的完整插件路径：

```text
test_workflow.agents.test_generation.core.tools._pytest_results
```

工具同时将实际 `src` 或安装目录加入受控子进程路径，避免只有父进程能够导入、子进程找不到插件的问题。

## 原测试、样例与来源记录

| 原位置 | 当前位置 |
|---|---|
| `vendor/agentdev/src/testgen_agent/` | `src/test_workflow/agents/test_generation/core/` |
| `vendor/agentdev/tests/` | `tests/agentdev/` |
| `vendor/agentdev/examples/` | `examples/agentdev/` |
| 原 README、Design、包配置等 | `docs/agentdev/`，仅作为来源文档和元数据保存 |

- `evaluation/reuse_manifest.json` 保留最初的 57 个来源文件及原始哈希。
- `evaluation/vendor_patches.json` 保留迁移前已登记的功能补丁。
- `evaluation/agentdev_layout.json` 记录每个来源文件的新位置、迁移前哈希、迁移后哈希及改动说明；同时记录 UTF-8/LF 规范化摘要，允许 Git 在不同系统转换换行而不误报源码修改。
- `scripts/vendor_audit.py` 继续为历史调用提供同名函数，现在能够验证整合后的路径及哈希。
- `scripts/prepare_p0.py` 改为检查当前来源审计；旧快照捕获脚本保存在 `docs/agentdev/prepare_snapshot.py`，不再用它重建独立 vendor 工程。

原测试仅调整包导入、替换工具的模块路径和样例位置，保留原测试行为。它们已经属于根 `tests` 树，不要再把 `tests/agentdev` 作为第二个全量参数重复收集。

历史阶段文档、JSON、JUnit 和输出产物保留当时的路径与运行事实，不为目录迁移改写过去的证据。阅读旧记录时按上表定位当前实现；当前安装与回归命令以 README 为准。

## 数据保留

旧 vendor 下忽略的缓存、构建文件和运行输出迁入 `outputs/agent-layout-backup/vendor/`。根目录旧构建缓存也保存到该备份区域，避免陈旧的 `.py` Agent 文件混入新 wheel。原始输入、现有 `.env` 和已有任务输出未修改。

目录整合不改变 Workflow 的顺序执行、反馈控制和质量开关；“并列放置”描述代码目录，不表示将三个 Agent 改为同时执行。

## 本次验证

- Python 全量回归 1037 passed，其中当前 Workflow 614 项、迁入的 AgentDev 423 项。
- 前端 25 项全部通过；当前环境依赖检查通过。
- 单个 wheel 的隔离安装、三个 Agent 文件与审查资源、安装后实际 pytest/质量流程及只读报告检查通过。
- 原始 57 个来源文件均可映射并通过迁移审计；换行转换可接受、实际内容修改会被拒绝。
- 未调用外部模型服务。精简记录见 [本次迁移验证](../evaluation/agent-layout-20261019.json)。
