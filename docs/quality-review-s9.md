# 质量审查 Agent：S9 文档、演示与真实验收准备

日期：2026-10-18。S8 的未提交改动和验收产物保留，本阶段未覆盖或改写其历史结果。S9 的文档、三个真实样例、受限验收脚本及 45 秒离线演示已准备；**真实模型验收等待外传授权，S9 尚未全部完成**。

## 当前结果

| 项目 | 状态 |
|---|---|
| README / Design / protocol / workflow / generation / reporting / 主清单 | 已同步开关、门禁、预算、报告字段与限制 |
| 三个真实模式样例 | calculation、quality_boundary、discount，输入及 SHA-256 已固定 |
| 请求边界与客户端回归 | 新增 8 项保护测试；连同既有客户端共 78 项通过（1.05 秒），全部固定数据或 MockTransport |
| 离线门槛边界闭环 | 真实执行两轮 pytest，2 → 3 项通过，两轮行/分支覆盖均 100% |
| 反馈消费与导出 | 第 1 轮 request_revision，第 2 轮消费并 approve，导出第 2 轮 |
| 短演示 | 45 秒录制证据回放，实际离线任务耗时 3.91 秒 |
| 真实 DeepSeek 调用 | 自动审批拒绝启动；本阶段发出 0 次请求，没有真实验收结论 |

离线反馈不能代替真实模型反馈。真实质量反馈数量为“未测”，不记为一次真实闭环，也不把尚未运行写成首轮 approve。后续真实模型若直接批准，会记录该事实，不重跑直到强行产生反馈。

## 可审阅的外传范围与预算

[范围清单](../evaluation/quality-review-s9-scope.json) 保存模型、目标地址、输入摘要和预算。使用当前配置的 `deepseek-flash`，目标仅为 `https://api.deepseek.com` 的 Chat Completions 接口。

| 样例 | 目的 | 候选上限 |
|---|---|---|
| `examples/calculation.py` / `.md` | 普通规格任务获得批准 | 2 |
| `examples/quality_boundary.py` / `.md` | 检查恰好 100 的门槛与向下取整；不强制遗漏或补充 | 3 |
| `examples/discount.py` / `.md` | 原有源码缺陷复核，保留 100→90 的失败断言 | 2 |

外传内容为以上样例的源码和行为规格，以及对应生成计划/测试、pytest/coverage 结果片段、执行/质量反馈、相关历史和任务元数据。Key 只用于配置服务认证，不写入范围清单、审计日志、状态或报告；不上传 `.env` 文件及其他工作区文件。

总 HTTP 派发尝试最多 **18 次**，每样例最多 **6 次**；每次请求体最多 128 KiB，输出 `max_tokens=8192`，请求超时 60 秒。生成和审查各最多 2 次尝试，允许一次格式修复，不做暂态重试；外层无自动重跑。实际可能产生模型费用，此处限制请求与 token 上限，不虚构货币费用估计。

`scripts/verify_quality_real.py` 默认仅准备，必须显式 `--execute` 才发出请求。共享传输门禁在派发前检查目的地、全局/单样例次数、请求体和输出预算；日志仅记录角色、样例、请求体摘要/大小、状态码和耗时，不记录认证头或请求正文。网络失败只记派发尝试，不能保证服务实际接收或计费。若客户端在门禁处被拒绝，其“尝试数”可能包含被挡住的尝试；独立 dispatches.json 记录实际交给网络传输层的数量。

2026-10-18 尝试执行时，自动审批明确拒绝：用户“开始 S9”未被视为允许将这些数据传向特定外部 DeepSeek 服务。未启动真实进程，没有绕过限制或改用其他通道。取得明确授权后才执行；授权未到时仅完成本地工作。

```powershell
# 默认只准备，不发送请求。
.\.venv\Scripts\python.exe scripts/verify_quality_real.py
# 取得对上述目的地、内容及预算的明确授权后，运行三条真实案例。
.\.venv\Scripts\python.exe scripts/verify_quality_real.py --execute
```

每次使用新的 outputs/quality-s9 目录，保留 manifest、dispatches、results 及三个完整任务。普通/边界任务要求 completed 且当前导出候选获批；discount 要求 needs_review/suspected_source_defect 和失败断言。错误、预算停止或人工问题按原样保存，不用 fixture 替换真实失败。

## 45 秒三 Agent 演示

打开 [交互回放](../evaluation/quality-review-demo.html)，点击“从头播放 45 秒”。页面明确标注固定生成/审查响应与真实 pytest 录制，不调用模型；可以暂停或按证据分段查看。浏览器已实际播放至 45/45 秒、片段 6/6，核对 approve、第 2 轮和导出摘要，截图保存在本机 outputs/quality-demo/s9-20261018/replay-proof.png，临时页面与服务已关闭。[演示讲稿](quality-review-demo.md) 提供 45 秒口播。

- [可提交的完整演示轨迹](../evaluation/quality-review-s9-demo.json)：两轮实测、规格引用、质量意见、消费消息、Context/Result 路径、差异及导出摘要。
- [本机原始报告](../outputs/quality-demo/s9-20261018/run/report.md) 与 [测试差异](../outputs/quality-demo/s9-20261018/test-diff.patch)。
- 固定候选为 `examples/fixtures/quality_boundary_round_01.json`、`quality_boundary_round_02.json`，包含完整计划和映射；审查响应复用包内 request_revision / approve。

```powershell
.\.venv\Scripts\python.exe scripts/prepare_quality_demo.py
```

此命令不读取 `.env`。它使用正式 CLI 与三个生产 Agent，真实执行两轮后严格 inspect，核对输入未变、反馈消费、第 2 轮批准和最终 SHA-256，再生成演示。网页的 45 秒时间轴是录制材料的讲解节奏，不是模型进度预测；原任务实测耗时另列。

## 产品边界

质量审查默认关闭，CLI `--with-quality-review` / Web 复选框开启；只在基础测试真实全通过且行覆盖 ≥90%、有分支时分支覆盖 ≥80% 时送审。审查与执行反馈共用最多三轮候选，当前批准仅对应同一规格、测试和真实执行证据；未批准不能 completed。

summary 1.2 的 external_requests/token_usage 为生成加审查总量，分项 model_usage 保留未知值及已知小计。业务 AgentState 继续只累计生成量。历史报告不改写，inspect 只读验证审查记录、当前批准和已提交消费 Context。

第一版不包含变异测试、属性测试、多文件项目、自动修复源码、任意 DAG/并行调度、断点续跑或服务重启后的 Web 历史列表恢复。pytest 工作目录隔离不构成执行安全沙箱，覆盖率与模型审查意见都不能证明源码完全正确。

S8 的 1029 项全量和 25 项前端结果仍是历史基线；本阶段按新验收脚本、离线演示及文档范围验证，没有冒充新一轮全量通过。未自动提交或推送代码。
