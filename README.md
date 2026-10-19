# 多 Agent 测试 Workflow

为单个 Python 文件生成 pytest 测试、真实执行测试与 coverage，并在有限轮次内根据反馈修订。测试生成 Agent 复用 AgentDev，执行 Agent 负责实测与归因，可选质量审查 Agent 在基础达标后检查规格场景遗漏。CLI 与 Web 使用同一工作流服务。

课程必选项、推荐项和交付状态逐项见 [最终验收清单](evaluation/FINAL_CHECK.md)，架构与流程图见 [Design.md](Design.md)。离线演示使用固定模型响应，pytest 和覆盖率均真实运行；真实模式通过 DeepSeek 生成和审查。

## 安装

要求 **CPython 3.13**；仅前端开发测试需要 Node.js（CI 使用 24）。在仓库根目录执行：

```powershell
python scripts/bootstrap.py
.\.venv\Scripts\python.exe -m pip check
```

等价手工步骤：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
```

Linux/macOS 将 `.venv\Scripts\python.exe` 换为 `.venv/bin/python`。首次安装需要取得锁定依赖；提交包包含源代码，不包含 Python、虚拟环境或完整离线依赖库。只安装根项目这一个包，不另行安装旧 `testgen-agent`。

## 不需要密钥的快速验证

```powershell
# 一轮完成：生成 → 执行 → 导出
.\.venv\Scripts\python.exe -m test_workflow run --demo --source examples/calculation.py --spec examples/calculation.md
# 两轮反馈：执行 → 生成补充 → 再执行
.\.venv\Scripts\python.exe -m test_workflow run --demo --source examples/validation.py --spec examples/validation.md --with-summary
# 疑似源码缺陷：保留失败断言，needs_review，预期退出码为 1
.\.venv\Scripts\python.exe -m test_workflow run --demo --source examples/discount.py --spec examples/discount.md
# 三 Agent 二轮协作：首轮覆盖达标但遗漏边界 → 审查反馈 → 补充 → 批准
.\.venv\Scripts\python.exe scripts/prepare_quality_demo.py
```

最后一条命令同时生成 [45 秒证据回放](evaluation/quality-review-demo.html) 和新的本地运行证据。该 HTML 是离线回放，不是录制视频，也不证明真实模型的审查质量。CLI 的 `run --demo` 使用生产 Agent 与编排器，仅替换模型客户端；历史 `demo` 子命令是协议假节点示例。

## 真实模式与 Web

首次配置时复制 `.env.example` 为 `.env`，填写 `LLM_MODEL` 和 `DEEPSEEK_API_KEY`；已有 `.env` 时直接编辑，避免覆盖。

```powershell
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m test_workflow.agents.test_generation.core --check-config
.\.venv\Scripts\python.exe -m test_workflow run --source examples/calculation.py --spec examples/calculation.md --env-file .env --with-quality-review
.\.venv\Scripts\python.exe -m test_workflow web --port 8765
```

Web 打开 <http://127.0.0.1:8765>，粘贴源码和行为规格，按需勾选质量审查。较完整的输入见 [checkout.py](examples/checkout.py) 与 [checkout.md](examples/checkout.md)。页面展示固定运行图、节点与反馈状态、逐轮覆盖、审查证据、模型用量和产物下载。Web 只使用真实模型，一次执行一个任务；离线验证使用 CLI。

真实模式会把源码、规格、生成测试及必要执行反馈发送到配置的模型服务，并产生费用。密钥仅在服务端读取。离线模式不读取模型配置，不发送模型请求。配置优先级为显式 CLI 参数、进程环境、指定 `.env`、默认值。

| 参数 | 用途 |
|---|---|
| `--max-rounds 1..3` | 所有修复与质量补充共享的候选轮次预算，默认 3 |
| `--timeout 1..300` | 单轮 pytest 超时秒数，默认 60 |
| `--output 新目录` | 完整任务目录；拒绝覆盖已有目录 |
| `--with-quality-review` | 基础测试与覆盖达标后调用第三个 Agent，默认关闭 |
| `--with-summary` | 添加无模型摘要扩展节点，不计作第四个 Agent |
| `--fixture 文件.json` | 离线生成候选，可按轮重复指定 |
| `--review-fixture 文件.json` | 离线审查响应，可重复；要求 `--demo --with-quality-review` |

离线启用审查必须显式给出审查 fixture，缺失、耗尽或格式错误均不会默认批准。例如：

```powershell
.\.venv\Scripts\python.exe -m test_workflow run --demo --source examples/calculation.py --spec examples/calculation.md --with-quality-review --review-fixture src/test_workflow/demo/data/quality_approve.json
```

## 输出与结论

默认 CLI 输出为 `outputs/workflow/<run_id>/`，Web 输出为 `outputs/web/<job_id>/`。每次运行保存状态、消息、事件、节点 Context/Result、逐轮计划/测试/执行/覆盖/审查证据，以及 `summary.json`、`report.md`；可用候选导出到 `final/test_subject.py`，并附带 `subject.py`。

```powershell
.\.venv\Scripts\python.exe -m test_workflow inspect <任务目录> --summary
```

`inspect` 只读核验已有证据；服务重启后也可读取任务，但不会续跑。`completed` 表示完成，`partial` 表示未完全达标，`needs_review` 表示需要人工复核，`failed` 表示无可用结果。退出码分别为 0（completed）、1（其他业务结论或节点错误）、2（输入/配置/持久化错误）。疑似缺陷样例退出 1 是预期行为。

输入须包含单个不超过 128 KiB、含 1–5 个顶层同步函数的 `.py` 文件，以及非空 UTF-8 行为规格；仅有源码不足以确定期望行为。不支持顶层类、异步、多文件工程、相对/通配符导入或第三方依赖。默认目标为行覆盖 ≥90%、有分支时分支覆盖 ≥80%；这些是工程选择，并非课程硬性指标。生成测试在本机执行，使用可信输入；独立目录不构成恶意代码沙箱。

## 验证与提交准备

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
node --test tests/web/*.test.cjs
.\.venv\Scripts\python.exe scripts/verify_workflow.py
.\.venv\Scripts\python.exe scripts/verify_quality_acceptance.py
.\.venv\Scripts\python.exe scripts/verify_quality_install.py
```

wheel 验证离线构建并在隔离 `--target` 目录安装，用 `python -I` 检查真实 pytest/质量流程，第三方依赖复用当前锁定环境；不应称作全新依赖环境安装。`verify_install.py` 另提供全新虚拟环境验证，需要下载依赖。GitHub Actions 配置 Windows/Linux 回归，远端通过状态以实际 CI 记录为准。

最终回归结果、限制、清理内容及提交前检查见 [最终验收清单](evaluation/FINAL_CHECK.md)。按用户要求，本次不生成压缩包。交付源码时排除 `.env`、虚拟环境、缓存、构建和原始运行目录；保留 `.env.example`、锁定依赖和验收证据。

## 目录

```text
src/test_workflow/
  agents/
    test_generation/      生成 Agent；core/ 复用 AgentDev
    test_execution/       执行 Agent
    test_quality_review/  质量审查 Agent
  adapters/               节点与复用能力适配
  workflow.py             顺序编排、分支与有限反馈
  service.py              CLI/Web 共用服务
  contracts.py, state.py  类型化交接与共享状态
  quality_*.py            审查契约、客户端、证据与路由
  nodes/                  无模型摘要扩展
  web_static/, demo/      页面资源与离线客户端/样例
tests/                    Workflow 回归；agentdev/ 为原项目回归
examples/                 源码/规格/候选/协议样例（测试依赖，保留）
docs/                     当前协议、扩展、复用说明；agentdev/ 为来源元数据
evaluation/               最终验收、演示与来源审计；历史记录有明确标注
scripts/                  安装、验收与演示
```

进一步阅读：[设计](Design.md)、[Agent 目录](docs/agent-layout.md)、[通信协议](docs/protocol.md)、[扩展方法](docs/extensions.md)、[AgentDev 复用](docs/reuse.md)、[历史证据说明](evaluation/history.md)。
