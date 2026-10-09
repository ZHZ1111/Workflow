# 同文件独立函数：rectangle_area 与 is_even

## 行为约定

- `rectangle_area(width, height)` 接受不包含 `bool` 的非负整数宽高，返回 `width * height`；任一参数为 0 时返回 0。
- `is_even(value)` 接受不包含 `bool` 的整数，偶数返回 `True`，奇数返回 `False`，负数按 Python 整数奇偶规则处理。
- 输入域均为调用方前置条件；域外行为未定义。
- 两个函数之间没有调用、数据传递或共享可变状态，不能仅因位于同一文件就编造协作流程。
- 两个函数均无外部副作用，不读写文件、不访问网络。

## 可核验场景

| 函数 | 场景 | 调用 | 预期 |
|---|---|---|---|
| `rectangle_area` | 正常 | `rectangle_area(4, 5)` | `20` |
| `rectangle_area` | 边界 | `rectangle_area(0, 5)` | `0` |
| `is_even` | 正常 | `is_even(4)` | `True` |
| `is_even` | 正常 | `is_even(3)` | `False` |
| `is_even` | 负数 | `is_even(-2)` | `True` |

本样例用于验证 Agent 会分别规划单元测试，而不会生成无依据的协作关系或组合断言。
