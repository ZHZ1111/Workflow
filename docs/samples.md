# 验收样例与确定反馈候选

版本：P0 / 2026-10-09。源码与规格来自本次 AgentDev 工作内容，复制到新仓库 `examples/`。四组样例均经 AST 输入检查，原始哈希与函数事实见 `evaluation/p0_results.json`。

| 样例 | 目标函数 | 用途 | 预期业务结论 |
|---|---|---|---|
| calculation.py/.md | calculate_total | 正常链路、乘法与零值 | 有效候选实际执行达标后 completed；无分支显示不适用 |
| validation.py/.md | classify_score | 类型、范围、分类边界、多分支 | 完整测试通过达标；不充分候选先补充再 completed |
| discount.py/.md | calculate_discount | 预设边界缺陷 | 输入 100 实际 100，规格预期 90；保留失败，needs_review |
| relationships.py/.md | calculate_total、checkout | 同文件直接调用及协作策略 | AST 确认 checkout → calculate_total；有效单元/真实入口协作候选可达标 |

calculation/relationships 的域外行为未定义，不应强行要求负数或 bool 抛某种异常。validation/discount 对 bool、类型、范围有明确定义。discount 中的源码缺陷是测试资产，不能在开发时“顺手修复”。

## 离线模型响应资产

以下 JSON 是 `GeneratedTests` 1.1 数据，包含计划、代码、场景映射及预期依据，**不是本次真实模型输出**。可在未来 demo 客户端中按序读入并包装成 `LLMGenerationResult`。P0 基线脚本已使用同一资产测试旧 Agent 的真实 pytest/coverage 链路。

| 文件（examples/fixtures/） | 候选内容 | 预期观察 |
|---|---|---|
| calculation_complete.json | 正常乘法、零单价、零数量三个独立用例 | 3 个通过，目标行覆盖 100%，分支不适用 |
| validation_round_01.json | 只测 classify_score(60)==pass | 1 个通过，但行/分支覆盖不足，建议 add_missing_cases |
| validation_round_02.json | 保留首轮函数、导入、场景和映射；追加 11 个边界/异常用例 | 总计 12 个通过，行/分支达标，选中第二轮 |
| discount_defect.json | 独立单断言 calculate_discount(100)==90 | 真实断言失败，规格表支持疑似缺陷，needs_review |

第二轮保留首轮原测试，不能整体替换成参数化函数，否则会触发通过测试保护。候选构建脚本为 `scripts/build_p0_fixtures.py`，运行结果以 baseline 文档为准。

## 反馈验收时需看到的证据

1. 第一轮测试实际执行且通过，coverage 来自同一次运行，明确记录缺失行/分支。
2. 评估建议为 add_missing_cases，包含来源候选和执行路径。
3. 第二轮客户端收到真实上一轮的 controller feedback/history/protected tests，而非预写“失败了”的空文本。
4. 第二轮保存到 rounds/02，执行时明确 round_number=2；rounds/01 保留。
5. 第二轮覆盖有提升、预算内停止，原源码哈希不变，最终选中轮次正确。

P0 证明旧能力和候选资产有效；P4/P5 已用**新 Workflow**独立证明消息和跨节点协作，见 evaluation/p4.md 和 evaluation/acceptance.md，未直接沿用旧循环结论作为新验收。P5 三个必需真实样例均由模型生成并实际执行；其结果和确定候选明确区分。

## 复杂结算样例

[checkout.py](../examples/checkout.py) 与 [checkout.md](../examples/checkout.md) 包含五个同步函数：商品校验、购物车汇总、会员/优惠券折扣、配送费和结算入口。重点包括优惠券资格按原价判断、比例券按会员优惠后金额计算、总折扣封顶、优惠后跌破免运费门槛以及免基础运费仍收加急费。

Web 不自动填入任何样例；使用时把两个文件的全文分别粘贴到源码和行为规格输入框。源码符合当前单文件/最多五函数边界。本地规格校验为 66 passed，行/分支覆盖 100%；这组本地校验位于 `tests/test_checkout_example.py`。
