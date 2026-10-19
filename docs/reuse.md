# AgentDev 复用与审计

来源为用户既有 AgentDev / TestGen Studio 项目，记录的仓库地址为 `https://github.com/ZHZ1111/AgentDev`（来自本地 Git 配置，未在本次核验远端）。来源 HEAD 为 `a347f85385547d90e9e2ff429665ae59a56ca298`。未推断或新增来源许可证。

## 复用范围

复用原测试生成、提示词与客户端、候选校验、历史通过测试保护、pytest/coverage、失败归因、工作区及模型。Workflow 以单步生成和单轮执行接入，不调用原 `run_agent()` 完整闭环，因而生成与执行是独立节点，协作由新的 WorkflowRunner 管理。

| 旧来源路径 | 当前路径 |
|---|---|
| src/testgen_agent/ | src/test_workflow/agents/test_generation/core/ |
| tests/ | tests/agentdev/ |
| examples/ | examples/agentdev/ |
| README、Design、包配置、锁文件、占位配置 | docs/agentdev/ |

三个 Agent 在同一 `agents/` 目录并列组织；core 是生成 Agent 内部代码，执行与审查也复用其中工具/模型。当前仅安装根 test-workflow 包，无单独 vendor 或 testgen-agent 依赖。

## 可核验记录

- [reuse_manifest.json](../evaluation/reuse_manifest.json)：57 个来源文件的初始内容摘要、Git blob 与来源 HEAD 对比。来源工作区有 6 个文件与当时 HEAD 不同，故不是纯 HEAD 导出。
- [vendor_patches.json](../evaluation/vendor_patches.json)：接入期间登记的 6 项补丁及修改前后摘要。
- [agentdev_layout.json](../evaluation/agentdev_layout.json)：57 个文件的当前位置、迁移前后摘要和 UTF-8/LF 规范化摘要。
- [vendor_audit.py](../scripts/vendor_audit.py)：验证来源→登记补丁→当前位置的摘要链；兼容 Windows CRLF/LF，不接受实质源码变化。

```powershell
.\.venv\Scripts\python.exe -c "import sys; from pathlib import Path; sys.path.insert(0, 'scripts'); from vendor_audit import audit_vendor; r=audit_vendor(Path.cwd()); print(r); assert r['verified']"
```

迁移主要改变模块导入；pytest 工具另调整结果插件全限定名及子进程 src/安装根路径，防止父进程可导入而测试子进程不可导入。当前插件为 `test_workflow.agents.test_generation.core.tools._pytest_results`。原测试迁入 tests/agentdev 并更新导入与样例路径，保留回归意图。

原 CLI/Web 与静态文件保留在 core 供兼容回归，来源 README/Design 是历史材料，不是当前操作入口。统一使用根 [README](../README.md) 的 test-workflow 命令。清理不删除这 57 个审计目标，不删除原项目回归和协议样例。
