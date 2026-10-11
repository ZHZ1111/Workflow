# 复杂购物车结算规格

对应 `checkout.py`，所有金额单位为**分**，采用整数运算。共五个同步函数，无第三方依赖、文件或网络访问。测试应覆盖各函数单元边界，以及真实调用 `checkout` 后的协作结果。

## 商品与购物车

`item_total(unit_price, quantity)`：

- 两参数必须是 Python 内置 `int`，不接受 `bool`、浮点数、字符串或整数子类，否则抛 `TypeError`。
- 单价范围为 0–1,000,000（含端点），数量为 1–99（含端点）；范围不合法抛 `ValueError`。
- 先检查两参数类型，再检查单价范围，最后检查数量范围。返回单价乘数量。

`cart_subtotal(items)`：

- `items` 必须为 `list`（允许列表子类），否则抛 `TypeError`；购物车长度为 1–20，空购物车或超长抛 `ValueError`。
- 每一项必须为长度恰为 2 的列表或元组，顺序为 `(unit_price, quantity)`。项类型错误抛 `TypeError`，长度错误抛 `ValueError`。
- 按购物车顺序真实调用 `item_total` 并累加；商品参数异常原样传播，不跳过非法项。
- 商品可重复，零价格商品有效，输入购物车和内部列表均不得被修改。

## 会员与优惠券

`calculate_discount(subtotal, membership, coupon)` 返回总折扣，规则按以下顺序处理：

1. `subtotal` 必须为内置 `int`（排除 `bool` 和整数子类），否则 `TypeError`；负数抛 `ValueError`。
2. `membership` 必须为字符串，否则 `TypeError`；仅支持大小写敏感的 `regular`、`silver`、`gold`，其余抛 `ValueError`。
3. `coupon` 必须为 `None` 或字符串，否则 `TypeError`；仅支持 `None`、`SAVE500`、`PERCENT10`，其余（包括空串和小写名称）抛 `ValueError`。
4. 会员折扣：regular 为 0%，silver 为 5%，gold 为 10%；用 `subtotal * rate // 100` 向下取整。
5. `SAVE500` 要求**原价 subtotal ≥ 5000**，固定再减 500；门槛不足抛 `ValueError`。
6. `PERCENT10` 要求**原价 subtotal ≥ 20000**，在会员优惠后的金额上再减 10%，即 `(subtotal - 会员折扣) // 10`；门槛不足抛 `ValueError`。
7. 两部分折扣相加后，**总折扣封顶 3000**。封顶发生在两部分计算之后，不能分别封顶，也不能把优惠券比例应用于原价。

优惠券资格看原价；会员优惠不影响是否达到优惠券门槛。零原价无优惠券时折扣为 0。

| 参数 `(subtotal, membership, coupon)` | 预期折扣 |
|---|---:|
| `(101, "silver", None)` | 5 |
| `(101, "gold", None)` | 10 |
| `(4999, "regular", "SAVE500")` | ValueError |
| `(5000, "gold", "SAVE500")` | 1000 |
| `(19999, "regular", "PERCENT10")` | ValueError |
| `(20000, "silver", "PERCENT10")` | 2900 |
| `(20000, "gold", "PERCENT10")` | 3000（封顶） |
| `(30000, "gold", None)` | 3000 |
| `(40000, "gold", None)` | 3000（封顶） |

## 运费

`shipping_fee(payable, region, expedited=False)`：

- `payable` 为**优惠后货款**，必须为非负内置 `int`，类型错误（包括 bool）抛 `TypeError`，负数抛 `ValueError`。
- 区域必须为字符串，否则 `TypeError`；仅支持 `city` 和 `remote`，其他值抛 `ValueError`。
- `expedited` 必须为内置 `bool`，整数 0/1 等均抛 `TypeError`。
- 检查顺序：货款类型、货款范围、区域类型、区域值、加急类型。
- city：货款达到 15000 免基础运费，否则收 1200。
- remote：货款达到 20000 免基础运费，否则收 2500。
- 加急始终另收 800，基础运费为零也不能免除加急费。

重点覆盖 14999/15000 和 19999/20000 的门槛；免运费资格不能按折扣前原价判断。

## 结算入口与协作

`checkout(items, membership="regular", coupon=None, region="city", expedited=False)`：

- 依次真实调用 `cart_subtotal`、`calculate_discount`、`shipping_fee`，不替换依赖以掩盖真实组合行为。
- 返回仅含 `subtotal`、`discount`、`shipping`、`total` 四项的字典，各金额为整数。
- `total = subtotal - discount + shipping`；各阶段异常原样传播，错误时不返回部分账单。
- 不修改购物车；重复调用同一输入得到相同结果。

| items | 会员 / 券 / 区域 / 加急 | subtotal | discount | shipping | total |
|---|---|---:|---:|---:|---:|
| `[(2000, 3)]` | 默认参数 | 6000 | 0 | 1200 | 7200 |
| `[(2000, 3)]` | gold / SAVE500 / city / False | 6000 | 1100 | 1200 | 6100 |
| `[(1000, 2), [500, 3]]` | silver / None / city / False | 3500 | 175 | 1200 | 4525 |
| `[(15000, 1)]` | regular / None / city / False | 15000 | 0 | 0 | 15000 |
| `[(15000, 1)]` | silver / None / city / False | 15000 | 750 | 1200 | 15450 |
| `[(20000, 1)]` | silver / PERCENT10 / city / False | 20000 | 2900 | 0 | 17100 |
| `[(20000, 1)]` | gold / PERCENT10 / city / True | 20000 | 3000 | 800 | 17800 |
| `[(20000, 1)]` | gold / None / remote / False | 20000 | 2000 | 2500 | 20500 |
| `[(40000, 1)]` | silver / PERCENT10 / remote / True | 40000 | 3000 | 800 | 37800 |
| `[(0, 1)]` | regular / None / city / False | 0 | 0 | 1200 | 1200 |

至少保留一个优惠后跌破免运费门槛的协作场景，以及一个“会员 + 比例优惠券 + 封顶 + 加急”的场景。按表中明确预期建立断言，不需要为全部参数笛卡尔积重复生成测试，也不约束异常消息文本。

## 使用

Web：把 `checkout.py` 全文粘贴到“Python 源码”，把本文件全文粘贴到“行为规格”，点击运行。

CLI（调用本地配置的 DeepSeek，会发送本例源码与规格）：

```powershell
.\.venv\Scripts\python.exe -m test_workflow run --source examples/checkout.py --spec examples/checkout.md --env-file .env --with-summary
```
