# Ubuntu 契约样例回归修复（2026-10-16）

## 问题与原因

用户提供的 S4 GitHub Actions 日志为 **4 failed, 927 passed, 2 skipped**。四项失败均来自新增的 `tests/test_quality_contracts.py::test_saved_legacy_contracts_still_load`：

- `p1/state.json`
- `p3/passed_context.json`
- `p4/passed_state.json`
- `p4/revision_context.json`

这些历史 JSON 包含录制机器的 `E:\...` 绝对路径。Ubuntu 将其解析为 POSIX 路径，`WorkflowState` 的绝对目录校验因此报出 `run directory must be absolute`；上下文还会校验 `snapshot_json` 内的状态。

此前的路径重映射仅封装在 `test_examples.py` 的局部工具中，新增的质量兼容测试直接调用 `model_validate_json()`，未复用它。原有样例测试的修复仍有效，但没有共享入口及明确的复用规范，Windows 回归也无法发现重新引入的 Ubuntu 问题。本次日志没有此前的 `timeout=30` 断言错误。

## 修复

- 将历史样例加载提取到 `tests/contract_examples.py`，通过 `tests/conftest.py::contract_example` fixture 共享。
- 原样例测试与质量兼容测试均使用该入口；将盘符绝对路径统一映射至测试临时目录，同时递归处理序列化快照，保留上下文、状态和内部工作区的关系。
- 新增五项回归：POSIX/Windows 两种路径映射、两个真实历史上下文的路径关系、非法相对目录仍被拒绝。
- 在 `docs/test-plan.md` 明确新增历史契约测试的复用入口及双系统验证要求。

生产 `WorkflowState` 和 `AgentContext` 校验、vendor、历史样例文件和 CI 测试矩阵均未修改；没有跳过失败测试，也没有将错误的历史路径直接视为本机有效目录。

## 验证证据

修复前工作区基于 `e8a023abaccb36cd699167aa1a77a913bbcd4b46`，开始时干净。Windows 与 Ubuntu 均使用 Python 3.13.9；Ubuntu 24.04.3 / WSL 使用隔离工作副本与虚拟环境，依赖来自 `requirements.lock` 的本地 Linux wheel，项目按 CI 的 editable 安装命令安装。

Ubuntu 上直接以原始方式加载七份历史契约，准确重现同样四项错误，其余三份通过。记录：`outputs/ci-path-fix-20261016/linux-original-errors.json`。

针对性命令：

```text
python -m pytest tests/test_contract_examples.py tests/test_examples.py tests/test_quality_contracts.py -q
```

Windows **121 passed**；Ubuntu **121 passed**。Ubuntu JUnit：`outputs/ci-path-fix-20261016/linux-targeted.xml`。

全量命令与 CI 相同：`python -m pytest tests vendor/agentdev/tests -q`，额外指定 JUnit 输出以保存证据。

| 环境 | 全量结果 | 耗时 | JUnit |
|---|---|---|---|
| Windows / Python 3.13.9 | 938 passed | 479.10 秒 | `outputs/ci-path-fix-20261016/windows.xml` |
| Ubuntu 24.04.3 / WSL / Python 3.13.9 | 936 passed, 2 skipped | 156.01 秒 | `outputs/ci-path-fix-20261016/linux-full.xml` |

Ubuntu 两项跳过是原有的 Windows 文件占用重试测试（`test_execution_checkpoint.py` 与 `test_storage.py`），并非本次新增跳过。总用例数均为 938，比修复前的 933 增加五项路径回归。

两边 `pip check` 均通过，CI 后续 `python scripts/verify_workflow.py` 也均通过：calculation 首轮 completed，validation 两轮 completed，discount needs_review；均为离线 CLI 样例，模型请求数为 0。Windows 记录：`outputs/verification/faed6ad021ba4cd0ae46f9144c914a7e/results.json`。Ubuntu 运行标识：`6d2c69c69f684384b0ddf7fb6c0668c3`。

机器可读索引：`outputs/ci-path-fix-20261016/results.json`。Ubuntu 的原始错误记录与 JUnit 已保存到宿主目录；CLI 结果按成功退出会话的标准输出记录，WSL 临时运行目录已随会话退出而不可用，不将其视为仍可访问的产物链接。

`git diff --check` 通过。本次没有外部模型请求；下载工具与依赖未上传项目源码、规格或凭据。验证针对本地工作区及隔离 Ubuntu 副本，未触发新的 GitHub Actions；旧失败运行的记录不会因本地修复改变，需要提交推送这些文件后由新 CI 运行确认。
