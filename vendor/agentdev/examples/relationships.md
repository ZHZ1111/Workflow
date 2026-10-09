# 同文件直接调用：checkout 与 calculate_total

## 行为约定

- `unit_price` 和 `quantity` 均为非负整数且不包含 `bool`；域外输入行为未定义。
- `calculate_total(unit_price, quantity)` 返回两者乘积。
- `checkout(unit_price, quantity)` 是结账流程入口，必须使用同文件的 `calculate_total` 计算并返回相同总价。
- 任一参数为 0 时，两函数都返回 0；函数不修改输入，也不读写文件或访问网络。

## 函数关系与测试策略依据

- 源码中的 `checkout → calculate_total` 是可静态确认的直接调用。
- `calculate_total` 的乘法与零值边界适合独立单元测试。
- `checkout` 需要至少一个真实调用入口和真实依赖的协作测试，验证结账入口组合后的返回行为；无需 mock `calculate_total`。
- 该调用关系有额外可验证行为，但不要求为每组单元输入重复生成一条协作测试。

## 可核验场景

| 测试类型 | 调用 | 预期 |
|---|---|---:|
| 单元 | `calculate_total(12, 3)` | `36` |
| 单元边界 | `calculate_total(0, 5)` | `0` |
| 协作 | `checkout(12, 3)` | `36` |
| 协作边界 | `checkout(12, 0)` | `0` |

本样例只验证一个有明确依据的直接调用关系；动态调用、名称遮蔽和多层调用由分析器契约测试覆盖。
