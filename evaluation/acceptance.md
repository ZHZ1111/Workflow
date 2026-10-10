# P5 系统与真实模型验收

更新时间：2026-10-10T12:53:23.993398+08:00（北京时间）。
P5 完成状态：`True`；旧项目基线与 P1–P4 记录仍独立保留。

## 本次真实模型

配置服务为 https://api.deepseek.com，模型名称以请求与响应实测为准。
三个必需样例的源码、规格、契约和执行反馈已获用户明确外传授权；密钥仅用于认证，不进入产物。

| 样例 | 业务结论 | 停止原因 | 轮数 | 模型请求 | 耗时/秒 | 通过/失败 | 行/分支覆盖 |
|---|---|---|---|---|---|---|---|
| calculation | completed | coverage_target_met | 1 | 1 | 8.673 | 6/0 | 100.0/None |
| validation | completed | coverage_target_met | 1 | 2 | 18.673 | 15/0 | 100.0/100.0 |
| discount | needs_review | suspected_source_defect | 1 | 1 | 14.399 | 15/2 | 100.0/100.0 |

正常样例和多分支样例本次首轮达标；这不证明真实模型发生了反馈提升。受控离线反馈单独证明自动循环。
discount 保留 100→90 的规格断言，本次真实测试失败后进入 suspected_source_defect，未修改预期或原始源码。
validation 的请求数含客户端格式修复请求；模型请求数、格式修复与候选轮数分别记录。

可选同文件关系样例未获本次外传授权，因此未执行；正常样例重复稳定性测试也未执行，不作稳定性结论。

## 本次离线与系统回归

离线生成 + 真实执行的正式 CLI 案例：15 个；pytest 子进程 17 次；外部请求为 0。

| 回归 | 测试数 | 失败 | 错误 | 跳过 | 退出码 |
|---|---|---|---|---|---|
| workflow | 192 | 0 | 0 | 0 | 0 |
| vendor | 180 | 0 | 0 | 0 | 0 |

新增系统测试覆盖错误任务/轮次/节点/接收者、重复消息、文件摘要、错误修订保留历史、源码副本变更、持久化写入失败、阻塞规格疑问和中文/空格输入。
非法路径、空规格、不支持类和非法 UTF-8 均在生成调用前失败。磁盘持续写入失败时向调用方抛错并保留最后成功快照，不保证新 error 状态可以落盘。

空候选在生产门禁被拒；底层 no_tests 的真实观察使用单独受控工作区验证，未放宽生产门禁。API 错误、覆盖导出失败和持久化失败用本地 Mock/故障注入，不计为真实模型流量。

原始源码和规格在全部 CLI 案例中摘要一致；vendor 快照 57 文件未变：`True`。

## 核心矩阵

| ID | 检查 | 结果 |
|---|---|---|
| A01 | 正确简单函数 | passed |
| A02 | 多分支目标覆盖 | passed |
| A03 | 真实执行反馈补充 | passed |
| A04 | 可修复候选错误 | passed |
| A05 | 规格支持的边界缺陷 | passed |
| A06 | 不明确规格需复核 | passed |
| A07 | 真实超时有界停止 | passed |
| A08 | API/非法响应有界失败 | passed |
| A09 | 空测试禁止完成 | passed |
| A10 | 预算/无进展与选择 | passed |
| A11 | 错误任务或轮次拒绝 | passed |
| A12 | 节点注册/定义扩展 | passed |
| A13 | 已有输出不覆盖 | passed |
| A14 | 覆盖导出失败保留测试 | passed |
| A15 | 保护历史通过行为 | passed |
| W11 | 跨节点与修订归属 | passed |
| W12 | 重复消息不重复执行 | passed |
| W13 | 中文/空格输入输出 | passed |
| W14 | 非法输入零调用 | passed |
| W15 | 副本变更阻止执行 | passed |
| W16 | 持久化失败不伪称保存 | passed |
| W17 | 正式 ResultSummaryNode 示例 | delivery_pending_P6 |
| W18 | 干净目录安装 | pending_P7 |

A01–A15 对应 TASK_CHECKLIST 第 8 节；W11–W16 补充 docs/test-plan 的系统边界。W01–W10 对应 A01–A10/A14/A15 的运行证据。
A12 已由统一接口的尾部节点测试证明；正式 ResultSummaryNode 示例属于 P6，干净安装属于 P7。

## 复现与证据

```powershell
.\.venv\Scripts\python.exe scripts/p5_acceptance.py --offline --tests
# 只读取已有真实任务，不调用模型
.\.venv\Scripts\python.exe scripts/p5_acceptance.py --collect-real <calculation目录> <validation目录> <discount目录>
# 校验索引中已有的离线/真实任务并刷新报告，不调用模型
.\.venv\Scripts\python.exe scripts/p5_acceptance.py --refresh
# 使用本地配置重新发起三个有界真实案例
.\.venv\Scripts\python.exe scripts/p5_acceptance.py --real --env-file .env
```

[机器可读结果索引](acceptance.json) 保存任务目录、run_id、输入摘要、每轮模型用量/格式修复/耗时/执行/覆盖/动作，以及矩阵对应的实际 JUnit testcase。
每次独立目录保留 state.json、Context/Result、events/messages、assessment、原始 pytest/JUnit/coverage 和最终测试；完整 outputs/ 不提交，精简索引与验收证据提交。

- calculation：[run-7250affa4ffc4d1ebfda4d3c8b0947a5](../outputs/p5/real-calculation-20261010/report.md)，模型 `deepseek-flash`。
- validation：[run-62b0d5da105045c3b6aaf6fd7fb3f7c5](../outputs/p5/real-validation-20261010/report.md)，模型 `deepseek-flash`。
- discount：[run-0cfcd96fccf44740a7d6d79dbf180921](../outputs/p5/real-discount-20261010/report.md)，模型 `deepseek-flash`。
