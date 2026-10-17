# 质量审查 Agent：S6 服务、CLI 与 Web

日期：2026-10-17。起始提交 `d91c8e15827fbdc67286117a4e0f708b46d9e157`，开始时工作区干净。S6 将既有三个生产节点接入共享应用服务与两个入口，复用 S1 的默认关闭、严格布尔配置，支持质量审查与摘要组合。

## 共享服务

`run_workflow(..., review_client=None, with_summary=False)` 新增审查客户端注入。启用 quality_review_enabled 却未提供客户端、提供无效客户端，或关闭开关却提供客户端，均在创建任务目录前报错。启用时注册 TestQualityReviewAgent，依据两个开关选择定义：

| 质量审查 | 摘要 | 使用定义 |
|---|---|---|
| 关闭 | 关闭 | TESTING_WORKFLOW |
| 关闭 | 开启 | SUMMARY_WORKFLOW |
| 开启 | 关闭 | QUALITY_WORKFLOW |
| 开启 | 开启 | QUALITY_SUMMARY_WORKFLOW |

不因配置缺失退回双 Agent，也不把客户端错误转成批准。候选预算、送审时机、质量补充门禁、历史保护、当前批准候选选择和摘要只执行一次继续沿用 S1–S5；没有修改生成、执行、审查节点或 vendor。

## CLI

`run` 新增 `--with-quality-review`，可以与 `--with-summary` 组合。真实模式使用同一个已加载 AppConfig 分别构造 DeepSeekClient 和独立 DeepSeekReviewClient，不在客户端构造时发出模型请求。

```powershell
# 真实模式：需要有效 DeepSeek 配置，会产生模型请求。
.\.venv\Scripts\python.exe -m test_workflow run --source examples/calculation.py --spec examples/calculation.md --with-quality-review --with-summary

# 离线模式：固定批准响应，生成仍使用内置 fixture，真实执行 pytest/coverage。
.\.venv\Scripts\python.exe -m test_workflow run --demo --source examples/calculation.py --spec examples/calculation.md --with-quality-review --review-fixture src/test_workflow/demo/data/quality_approve.json --with-summary
```

离线模式必须显式提供可重复的 `--review-fixture`，每个文件为严格 ReviewDecision JSON，按实际送审顺序消费，不是按所有生成轮次消费。补充后再批准需要依次提供 request_revision 与 approve，同时为生成客户端提供对应候选 fixture。固定响应不代表真实模型判断；没有隐式 approve，审查 fixture 用尽会按既有审查错误停止。

缺少离线审查 fixture，或在真实模式/未开启审查时提供该参数，返回明确提示和退出码 2，不读取模型配置、不创建任务。fixture JSON 在任务创建前校验。业务质量未通过或节点失败仍沿用退出码 1；只有完成且获得所需批准才返回 0。

## Web

表单增加默认不勾选的“启用质量审查”，说明可能增加请求及测试轮次。前端提交实际 checkbox.checked 值，API 的 quality_review_enabled 只接受 JSON true/false，缺省 false；数字、字符串、null、数组和对象返回 400，不创建任务。

bootstrap 返回默认关闭配置，后台只在启用时按同一 AppConfig 创建独立审查客户端，并传入相同 run_workflow。Web 原有摘要保持开启，仍仅使用 DeepSeek，不开放离线模式或 fixture 文件选择。配置/客户端构造失败返回失败任务、释放运行槽，不发起双 Agent 替代运行，也不回显异常原文或密钥。

HTTP 响应中的状态、事件、消息包含真实审查记录，交接卡片区分送审交接和质量反馈。页面现有请求计数明确标为“生成请求 / pytest”。完整审查节点图、质量反馈边、分项请求/用量与质量报告仍属 S7；现有默认双 Agent 图和生成请求汇总不能视为完整质量视图。

## 验证

新增 `tests/test_quality_entrypoints.py` **27 项**，覆盖共享服务四种组合、客户端缺失/无效/多余、服务/CLI/HTTP 二轮质量补充、CLI 参数和 fixture 校验、真实客户端配置共用、Web 开关对实际注册的控制、七种非布尔输入、错误不降级及密钥不回显。模型全部使用受控客户端，执行使用生产节点真实 pytest/coverage。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_quality_entrypoints.py tests/test_cli.py tests/test_workflow_web.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_quality_contracts.py tests/test_workflow_integration.py tests/test_system.py -q
node --test tests/web/*.test.cjs
```

- 入口与 CLI/Web 回归：**46 passed**（104.14 秒）。
- 质量契约、原编排集成与系统回归：**111 passed**（105.06 秒）。
- 前端：**16 passed**，含新增三个表单/无障碍/默认状态检查，验证提交 true 和 false 均为布尔值。
- `pip check`：No broken requirements found；`git diff --check` 通过。

本阶段按服务与入口改动范围验证 **157 项 Python 测试 + 16 项前端测试**，没有重跑全量 Workflow/vendor。S5 的 974 项全量结果保留为历史事实，不能当作 S6 新结果；完整质量矩阵及稳定后的全量回归按 S8 推进。

## wheel 与安装后验证

新增 `scripts/verify_quality_install.py`，在唯一输出目录中离线构建 Workflow 和 vendor 的 wheel，检查三个质量 fixture 与质量生成模块打包；按 `--target` 非 editable 安装后，使用 Python `-I` 导入并断言两个项目都来自安装目录。复用当前环境锁定的第三方依赖，不宣称全新 venv 验证。

安装后的资源检查通过，CLI 同时开启质量审查与摘要完成真实执行、批准、导出，并可 inspect 读取。既有 `data/*.json` package-data 已覆盖三个审查 fixture，无需重复增加配置；新增 Web 静态内容也随原静态资源规则安装。

```powershell
.\.venv\Scripts\python.exe scripts/verify_quality_install.py
```

结果成功，四项检查均通过：build-wheels、wheel-review-resources、install-wheels、installed-quality-cli。无包索引访问，无外部模型请求。首次验证脚本因 `-I` 忽略 PYTHONIOENCODING，中文输出不能按 UTF-8 解码；已显式设置子进程输出编码并以字节捕获日志，重跑成功。

最终安装证据：[results.json](../outputs/quality-install-verification/94f4dba4c145432c951b74b43e0ed850/results.json)，相邻目录保存构建/安装/CLI 日志及完整任务。该次构建包含最终静态资源与 README 更新，四项检查再次通过。outputs 依原规则不提交，脚本可重新生成新证据目录。

S6 八项任务已勾选，README、protocol、workflow、generation、运行可视化说明、历史阶段说明和本地主清单同步。S7–S9 继续保留未完成状态，本次未运行真实模型或自动提交/推送代码。
