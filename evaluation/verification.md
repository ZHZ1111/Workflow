# 工程整理与双入口验收

日期：2026-10-11（北京时间）。Windows x64、CPython 3.13.9。机器可读结果见 [verification.json](verification.json)。

本记录保留工程初验时的功能与实测结果。随后按用户要求，Web 改为仅使用 DeepSeek、空白源码/规格输入，移除页面及 API 的离线样例功能；CLI 离线功能保留，当前使用方式以 README 为准。

## 当前结果

- Workflow 与完整 vendor 回归：**645 passed，0 failures/errors**，耗时 221.77 秒；原始 JUnit 在 `outputs/verification-tests.xml`。
- CLI 和 HTTP 各通过 calculation、validation、discount 的离线和真实 DeepSeek 验收，共 **12 个成功符合预期的案例**。
- calculation、validation 的业务结论为 `completed/coverage_target_met`；discount 保留规格支持的失败断言，结论为 `needs_review/suspected_source_defect`。流程完成不等于所有被测源码没有缺陷。
- 离线 validation 在两个入口均实际运行两轮，消费执行反馈、补充测试后行/分支覆盖达到 100%。真实案例各在首轮结束；CLI validation 发生一次模型格式修复。
- 从本地构建的两个 wheel 安装到全新虚拟环境，`pip check`、包内样例/静态资源、安装后的 CLI、只读 inspect、安装后的 Web 两轮反馈均通过。运行目录位于仓库外的工作目录，包从新环境的 site-packages 导入。
- 浏览器实际操作确认离线两轮结果和下载链接；截图保存于 `outputs/web-verification.png`。当前工作台入口为 `http://127.0.0.1:8765`。
- 交付 Web 服务另从页面点击运行 DeepSeek calculation：6 passed、行覆盖 100%、无分支（不适用）、一次模型请求；任务目录 `outputs/web/0d914a1988de409f9888392f1610050d`，结果截图 `outputs/web-result.png`。

| 入口 | 生成模式 | calculation | validation | discount |
|---|---|---|---|---|
| CLI | 离线 | completed，1 轮 | completed，2 轮 | needs_review，1 轮 |
| Web HTTP | 离线 | completed，1 轮 | completed，2 轮 | needs_review，1 轮 |
| CLI | DeepSeek | completed，1 轮 | completed，1 轮 | needs_review，1 轮 |
| Web HTTP | DeepSeek | completed，1 轮（修复后复验） | completed，1 轮 | needs_review，1 轮 |

完整双入口运行索引：`outputs/verification/4b02ec07b5304fab916effbb0310a7b7/results.json`。其中首次 Web calculation 因 Windows pytest 检查点替换受阻失败，保留该失败记录；修复后的成功索引为 `outputs/verification/8dbbed80926845b3acad72e5b1366c3c/results.json`。精简结果合并成功案例，单独保留首次失败与复验来源。

最新 wheel 干净安装索引：`outputs/install-verification/7d473e7bafa842248cd767979c8a754b/results.json`。该次构建包含两个 Windows 文件占用修复。

## 修复与工程调整

1. 新增 `service.py`，统一 CLI 和 Web 的节点注册、正式流程定义、创建任务与调度。新 Web 不调用旧单 Agent 完整循环。
2. 新增 Workflow Web 页面与 `web` 子命令、`test-workflow-web` 入口，提供异步任务、轮询、逐轮结论、消息与测试/报告下载。只接受本机同源请求、限制请求长度、验证离线样例匹配、同一服务一次运行一个任务。
3. CLI 不再用固定参数默认值覆盖 `.env` 的轮次和执行超时；显式 CLI 参数仍优先。
4. Workflow 状态和 vendor pytest 检查点遇到 Windows 临时 PermissionError 时，最多八次、间隔 25 ms 重试原子替换；永久失败仍保留错误，不重跑模型或测试。共享占用有故障注入回归，频繁 HTTP 轮询有真实执行回归。
5. Web 资源与样例随 wheel 打包，新增 bootstrap、双入口验收、干净安装验收、编辑器配置和 GitHub Actions 离线回归配置。

vendor 仅修改 `src/testgen_agent/tools/_pytest_results.py`。原始来源哈希未覆盖，[补丁清单](vendor_patches.json) 单独记录当前哈希。旧阶段脚本区分“与原始快照一致”和“匹配原始快照加已记录补丁”，完整 vendor 回归纳入本次 645 项。

## 清理范围

删除 11 个重复 pytest 文本汇总和 14 个项目缓存/构建目录。保留历史 JUnit、验收 JSON、P0 首次异常日志、来源清单、测试使用的契约样例、依赖快照及已有运行证据。同步更新历史文档中指向已删除汇总的引用；原 `.env` 和运行环境保留。

## 实测边界

首次沙箱内联网被限制；自动审批最初要求具体数据外传授权，用户随后明确允许三个样例及其规格、生成测试、执行反馈发送至 `https://api.deepseek.com`，之后完成真实调用。失败的网络/pytest 尝试保存在独立任务中，不冒充成功。

本次证明 Windows 当前环境与新的 Windows 虚拟环境可运行。GitHub Actions 的 Windows/Linux 配置已提供，远端 CI 尚未触发；未将其记作实测成功。未执行可选 relationships 样例，也不保证任意模型输出都达标；服务失败、输入不支持及疑似源码缺陷仍返回真实业务状态。

## 后续 Web 输入调整

按用户要求移除样例和生成方式选择，源码及规格初始为空。Web API 固定使用 DeepSeek 并拒绝离线模式；bootstrap 不再返回样例。CLI 的离线模式保留。已重启工作台，浏览器验证两个输入框可编辑且交付时为空，截图为 `outputs/web-blank-form.png`。

本次相关 Web/CLI 回归 19 passed；新 checkout 例子规格校验 66 passed、行/分支覆盖 100%。最新干净安装记录为 `outputs/install-verification/21824a81a7404615bf9dc486ae507b2b/results.json`，Web 在安装验收中注入本地模型响应验证两轮反馈，不发出外部模型请求。
