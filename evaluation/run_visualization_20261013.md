# 评审收尾与运行可视化验收（2026-10-13）

本次按 `review_20261013.md` 同步当前工程状态并实现固定运行图。原评审文档及 P0–P6 的日期、测试数、任务结论和初验补丁事实保留；不把历史记录改写为本次实测。

## 已更新

- 默认单轮 pytest 超时文档统一为 60 秒。
- Design、development、test-plan 和清单区分已完成的干净 wheel 安装、回归检查与待交付的演示、版本冻结、备用压缩包。
- verification 与 reuse 明确当前共有六项 vendor 登记补丁，同时标明初验时仅一项修改属于历史事实。
- 清单按既有证据更新核心完成标准、复用、消息约定、P7.1/P7.2 和 A.2–A.4。A.1 的服务重启后自动历史列表恢复、可选 B 实验及课程提交项保持未完成。
- 新增固定图、每轮节点高亮、反馈回路、可点击证据和交接消费卡片；CLI 与执行策略没有变更。

## 本次验证

| 检查 | 结果与证据 |
|---|---|
| 前端状态投影 | 13 项通过；`node --test tests/web/workflow-view.test.cjs`，覆盖等待、事件/快照进度差异、二轮重置、反馈消费、复核、部分完成、失败和导出 |
| Web/CLI 回归 | 19 passed；`outputs/run-visualization-tests.xml`，含新增 JS 资源 HTTP 访问和正式工作流回归 |
| 浏览器两轮反馈 | 采用注入固定模型响应，实际执行 pytest/coverage；第一轮 1 项通过、行/分支 60%/50%，第二轮 12 项通过、行/分支 100%/100%；26 个事件、3 条交接消息，其中反馈 1/1 已消费 |
| 刷新与下载 | 运行期间刷新恢复同一任务，输入框为空；报告下载成功；浏览器错误/警告日志为空 |
| 窄屏 | 390 像素视口下无横向溢出；恢复默认视口后结束检查 |
| 本地 wheel 打包 | 构建成功；包内包含 index.html、app.js、styles.css、workflow-view.js，入口引用新脚本；`outputs/visualization-20261013/wheels/` |
| 依赖与补丁审计 | pip check 通过；57 个 vendor 文件及 6 个登记补丁校验通过；本次未修改 vendor |

浏览器首个任务为 `outputs/web/8f17dbf396644b6b906f5bfa0be1b7b7/`。完成图截图：`outputs/visualization-20261013/completed.png`；窄屏截图：`outputs/visualization-20261013/mobile.png`。验收服务注入模型响应并添加等待时间用于观察高亮，生产 Web 仍仅使用 DeepSeek；本次没有外部模型请求，不能将这一任务计作新的真实 DeepSeek 质量验收。

刷新复验任务为 `outputs/web/9fcffc74217842c78c6a0b03c5752b14/`，两任务的 StateStore 与 inspect_summary 产物校验均通过。机器可读索引：`outputs/visualization-20261013/results.json`。

此前评审的 711 项全量回归与 12 个既有任务产物校验见 `review_20261013.md`。本次只运行受影响检查，没有重复全量回归或重新联网安装依赖。最新已有干净安装记录仍为 `outputs/install-verification/21824a81a7404615bf9dc486ae507b2b/results.json`；新增静态资源另做本地 wheel 打包检查，不冒充新的干净安装。

## 保留的范围

源码与非空规格仍为必填。图对应固定测试流程；服务重启后的 Web 历史自动恢复、源码单独输入、关系样例复测、对照实验、演示视频和课程提交凭据仍未完成。本次没有修改这些范围，也没有发布或提交仓库。
