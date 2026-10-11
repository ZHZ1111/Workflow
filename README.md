# 多 Agent 测试 Workflow

为单文件 Python 源码生成并验证 pytest 测试。生成 Agent 负责规划与修订，执行 Agent 负责 pytest、覆盖率和评估，Workflow 负责交接、最多三轮反馈、历史候选选择和报告导出。

**CLI 和 Web 共用同一工作流服务。** Web 仅使用 DeepSeek，用户自行输入源码与行为规格；CLI 另外保留离线固定候选，仍真实执行 pytest/coverage。工程初验结果和修复记录见 [工程验收](evaluation/verification.md)。

## 安装

需要 **CPython 3.13**，从仓库根目录运行：

```powershell
python scripts/bootstrap.py
.\.venv\Scripts\python.exe -m pip check
```

脚本创建 `.venv`，安装锁定依赖和两个本地包。无需原 hw1 工程，也无需从包索引寻找 `testgen-agent`。等价手工安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e ./vendor/agentdev -e .
```

Linux/macOS 使用 `.venv/bin/python`。包包含离线候选、样例及 Web 静态资源，wheel 安装后不依赖仓库目录。

## DeepSeek 配置

首次配置复制 `.env.example` 为 `.env`，填写实际模型和密钥。已有 `.env` 时直接编辑，避免覆盖：

```powershell
Copy-Item .env.example .env
# 编辑 LLM_MODEL 与 DEEPSEEK_API_KEY
.\.venv\Scripts\python.exe -m testgen_agent --check-config
```

配置优先级：显式 CLI 参数 > 进程环境 > 指定 `.env` > 默认值。未指定轮次或超时时采用 `AGENT_MAX_ROUNDS` 和 `TEST_EXECUTION_TIMEOUT_SECONDS`；Web 初始表单也读取这些默认值。`--env-file` 可指定配置，密钥只在服务器端读取，不返回页面或写入工作流状态。

真实模式发送源码、规格、生成测试和反馈，会产生模型费用。离线模式不读取模型配置、不发出外部请求。

## CLI 运行

```powershell
# DeepSeek：正常完成
.\.venv\Scripts\python.exe -m test_workflow run --source examples/calculation.py --spec examples/calculation.md --env-file .env
# 离线：真实执行两轮测试，消费反馈并补足覆盖
.\.venv\Scripts\python.exe -m test_workflow run --demo --source examples/validation.py --spec examples/validation.md
# 缺陷复核：保留失败断言，needs_review，退出码 1
.\.venv\Scripts\python.exe -m test_workflow run --demo --source examples/discount.py --spec examples/discount.md
```

安装后的 `test-workflow` 命令与模块入口等价。

| 参数 | 行为 |
|---|---|
| `--max-rounds 1..3` | 最大轮次，默认来自配置，离线为 3 |
| `--timeout 1..300` | 单轮 pytest 超时秒数，默认 60 秒，可通过配置或此参数覆盖 |
| `--output 新目录` | 指定完整任务目录；已存在目录被拒绝 |
| `--demo` | 固定候选生成 + 真实测试执行 |
| `--fixture 文件.json` | 离线候选，可按轮次重复指定 |
| `--with-summary` | 注册无模型摘要扩展 |

默认输出：`outputs/workflow/<run_id>/`。退出码 0 表示 `completed`；1 表示 `partial`、`needs_review`、`failed` 或节点错误；2 表示输入、配置、参数或持久化错误。`demo` 子命令保留为历史假节点示例，完整离线运行使用 `run --demo`。

## Web 运行

```powershell
.\.venv\Scripts\python.exe -m test_workflow web --port 8765
# 等价独立入口
.\.venv\Scripts\test-workflow-web.exe --port 8765
```

打开 [本机工作台](http://127.0.0.1:8765)。源码和行为规格初始为空，填写后点击“运行工作流”，由 DeepSeek 生成测试。没有样例或生成方式选择；未配置 DeepSeek 时不能提交。页面显示结论、停止原因、选中轮次、模型请求/pytest 启动数、逐轮覆盖和反馈，提供测试与报告下载。

复杂例子见 [checkout.py](examples/checkout.py) 与 [checkout.md](examples/checkout.md)：包含购物车校验、会员折扣、优惠券门槛/叠加/封顶、优惠后的运费门槛和加急费。把两个文件全文分别粘贴到页面即可使用。

默认读取当前目录的 `.env`，可用 `--project-root` 指定输出项目目录、`--env-file` 指定配置。输出保存在 `outputs/web/<job_id>/`，输入保存在 `outputs/web-inputs/<job_id>/`。仅监听本机，一次执行一个任务，拒绝跨站提交。刷新页面可继续查看当前任务；服务重启后用 CLI `inspect` 读取持久化产物。

旧 `testgen-agent-web` 仍运行 vendor 的单 Agent；本项目使用 `test-workflow web` 或 `test-workflow-web`。

## 产物与检查

任务保存 `state.json`、`events.jsonl`、`messages.jsonl`、节点 Context/Result、逐轮测试及实测证据、`summary.json` 和 `report.md`。可用候选原字节导出到 `final/test_subject.py`，附带 `subject.py`；没有可用候选时不产生最终测试。

```powershell
.\.venv\Scripts\python.exe -m test_workflow inspect <任务目录> --summary
```

检查核对证据摘要及最终副本，仅读取已有任务。报告区分覆盖达标与业务正确、选中候选与最新候选。Windows 状态/pytest 检查点遇到临时文件占用时执行有限重试，不重复调用模型或执行测试。

## 验证与维护

```powershell
# 全量 Workflow + vendor 回归
.\.venv\Scripts\python.exe -m pytest tests vendor/agentdev/tests -q
# CLI 正常、反馈、缺陷复核：三个离线任务
.\.venv\Scripts\python.exe scripts/verify_workflow.py
# 三个 CLI 离线 + 六个 CLI/Web DeepSeek 任务，发送样例及反馈并产生费用
.\.venv\Scripts\python.exe scripts/verify_workflow.py --real --env-file .env
# 新环境、wheel 安装、CLI 两轮反馈与 Web 注入模型响应的两轮验证
.\.venv\Scripts\python.exe scripts/verify_install.py
```

每次验收保存新的 `outputs/verification/` 或 `outputs/install-verification/` 目录。可用 `--real --skip-offline --interfaces web --samples calculation` 只复验指定案例。GitHub Actions 配置 Windows/Linux 离线回归，远端是否成功以实际运行记录为准。

不提交 `.env`、虚拟环境、缓存、构建目录或原始运行输出。删除历史重复 pytest 文本汇总，保留 JUnit、结构化索引、异常初验日志和协议样例。旧阶段脚本可重建证据。vendor 原始哈希保留在 `evaluation/reuse_manifest.json`，后续补丁独立记录在 `evaluation/vendor_patches.json`。

```text
src/test_workflow/   协议、两个 Agent、编排、共享服务、CLI、Web、报告
tests/              Workflow 回归与 HTTP 集成验证
vendor/agentdev/     本地依赖、原测试与来源文档
examples/           源码/规格、离线候选、协议样例
scripts/            安装、当前验收、历史复现
docs/               设计、范围、协议、扩展、开发说明
evaluation/         精简验收、JUnit、来源和补丁哈希
outputs/            本地证据及构建产物（Git 忽略）
```

支持单个不超过 128 KiB、含 1–5 个顶层同步函数的 Python 文件和非空行为规格。不支持顶层类、异步、多文件工程、相对/通配符导入或第三方依赖。独立目录不构成安全沙箱；生成测试在本机运行，使用可信输入。

详细说明：[总体设计](Design.md)、[范围](docs/scope.md)、[路由](docs/workflow.md)、[协议](docs/protocol.md)、[报告](docs/reporting.md)、[扩展](docs/extensions.md)、[复用审计](docs/reuse.md)、[历史验收](evaluation/acceptance.md)。
