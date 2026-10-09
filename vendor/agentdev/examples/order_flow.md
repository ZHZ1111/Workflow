# 复杂订单结算：`order_flow.py`

## 行为约定

所有金额均为非负整数元，计算结果不设上限；整数输入均不接受 `bool`。所有函数无文件、网络或全局状态副作用，不修改传入的购物车。不约束异常消息文本，只约束异常类型。

### 商品与购物车

- `validate_item(unit_price, quantity)`：单价必须是非负整数，数量必须是 1–20 的整数。合法时返回 `None`。类型不符抛 `TypeError`，数值越界抛 `ValueError`。先检查单价，再检查数量。
- `calculate_subtotal(items)`：`items` 必须是非空 `list`，其中每项是长度为 2 的 `tuple` 或 `list`，按 `(unit_price, quantity)` 表示商品。购物车类型不符抛 `TypeError`，空购物车或商品结构错误抛 `ValueError`；商品数值错误由 `validate_item` 原样传播。返回各项 `unit_price * quantity` 之和。接受零单价商品，不修改 `items` 或其元素。

### 折扣与运费

- `apply_discount(subtotal, member, coupon)`：`subtotal` 必须是非负整数，`member` 必须是布尔值。`coupon` 只能是 `None`、`"SAVE10"` 或 `"FREESHIP"`，其他值抛 `ValueError`。类型错误抛 `TypeError`，负小计抛 `ValueError`。
- 会员先按 `subtotal * 9 // 10` 得到折后商品金额，即打九折并向下取整；非会员金额不变。
- `"SAVE10"` 要求**原始** `subtotal >= 50`，否则抛 `ValueError`；符合时在会员折扣之后再减 10 元，结果最低为 0。`"FREESHIP"` 不改变商品金额。两种优惠券不可叠加。
- `calculate_shipping(discounted_subtotal, destination, coupon)`：金额必须是非负整数；目的地只能是 `"local"` 或 `"remote"`；优惠券取值与上面相同。非法金额类型抛 `TypeError`，负金额、非法目的地或非法优惠券抛 `ValueError`。`remote` 始终收 25 元，即使使用 `"FREESHIP"`；`local` 在**折后商品金额**达到 100 元或使用 `"FREESHIP"` 时免运费，否则收 12 元。

### 结账入口及函数关系

`checkout(items, member=False, coupon=None, destination="local")` 依次调用 `calculate_subtotal`、`apply_discount`、`calculate_shipping`，其中 `calculate_subtotal` 直接调用 `validate_item`。入口返回包含且仅包含下列四个整数键的字典：`subtotal`、`discounted_subtotal`、`shipping`、`total`；其中 `total = discounted_subtotal + shipping`。校验和异常按上述调用顺序传播。应验证入口的真实函数协作，无需 mock。

## 可核验场景

| 场景 | 调用 | 预期 |
|---|---|---|
| 数量上下界 | `validate_item(0, 1)`、`validate_item(5, 20)` | `None` |
| 商品类型错误 | `validate_item(True, 1)`、`validate_item(5, False)` | `TypeError` |
| 商品数值错误 | `validate_item(-1, 1)`、`validate_item(5, 0)`、`validate_item(5, 21)` | `ValueError` |
| 多商品小计 | `calculate_subtotal([(20, 2), [15, 3]])` | `85` |
| 空购物车与结构错误 | `calculate_subtotal([])`、`calculate_subtotal([(5,)])` | `ValueError` |
| 会员折扣取整 | `apply_discount(55, True, None)` | `49` |
| 折扣顺序 | `apply_discount(55, True, "SAVE10")` | `39` |
| 优惠券门槛 | `apply_discount(49, False, "SAVE10")` | `ValueError` |
| 本地运费边界 | `calculate_shipping(99, "local", None)`、`calculate_shipping(100, "local", None)` | `12`、`0` |
| 偏远地区不免运费 | `calculate_shipping(100, "remote", "FREESHIP")` | `25` |
| 普通结账 | `checkout([(20, 2), [15, 3]])` | `{"subtotal":85,"discounted_subtotal":85,"shipping":12,"total":97}` |
| 折扣后未达免运费线 | `checkout([(50, 2)], member=True)` | `{"subtotal":100,"discounted_subtotal":90,"shipping":12,"total":102}` |
| 优惠券本地免运费 | `checkout([(20, 2)], coupon="FREESHIP")` | `{"subtotal":40,"discounted_subtotal":40,"shipping":0,"total":40}` |
| 优惠券偏远地区 | `checkout([(20, 2)], coupon="FREESHIP", destination="remote")` | `{"subtotal":40,"discounted_subtotal":40,"shipping":25,"total":65}` |
| 无效商品从入口传播 | `checkout([(10, 0)])` | `ValueError` |

参考测试见 `test_order_flow.py`。它用于校验样例本身；让 Agent 生成测试时只传 `order_flow.py` 和本规格文件。
