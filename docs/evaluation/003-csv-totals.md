# 003：修复 CSV 金额汇总

目标：观察 Agent 是否根据数据格式选择正确的标准库，并避免用浮点数处理金额。

## 初始文件

### `totals.py`

```python
def totals_by_customer(text: str) -> dict[str, str]:
    totals = {}
    for line in text.strip().splitlines()[1:]:
        customer, amount = line.split(",")
        totals[customer] = totals.get(customer, 0.0) + float(amount)
    return {customer: str(amount) for customer, amount in totals.items()}
```

### `tests/test_totals.py`

```python
import unittest

from totals import totals_by_customer


class TotalsTests(unittest.TestCase):
    def test_quoted_customer_and_exact_cents(self):
        text = 'customer,amount\n"Acme, Inc",0.10\n"Acme, Inc",0.20\n'
        self.assertEqual(totals_by_customer(text), {"Acme, Inc": "0.30"})


if __name__ == "__main__":
    unittest.main()
```

## 给 Agent 的提示词

```text
修复 totals_by_customer，并补充测试。
输入是 CSV 文本，第一行固定为 customer,amount；字段遵循 CSV 引号规则，客户名可以包含逗号或引号，但不包含换行。
只跳过没有任何字段的空记录；不得 strip 客户名，有意义的前后空格要保留。
金额允许正数、负数、零，格式为可选正负号加十进制定点数，最多两位小数；不含指数、NaN、Infinity、千分位或金额前后空格。
使用十进制精确求和，按客户名分组，结果值固定两位小数。合计为零时统一输出 0.00，不输出 -0.00。
输入只有表头时返回空字典。可以假设 CSV、表头和金额格式合法，不需要增加异常格式处理；每个金额和每个客户合计的有效数字不超过 18 位。
保持函数签名，只用标准库。请执行相关测试并说明结果。
```

## 评测者验收

下表中 `\n` 表示实际换行。每个输入均应加上 `customer,amount\n` 表头。

| 数据行 | 预期字典 |
| --- | --- |
| `"Acme, Inc",0.10\n"Acme, Inc",0.20\n` | `{"Acme, Inc": "0.30"}` |
| `A,1.25\nB,2\nA,-0.25\n` | `{"A": "1.00", "B": "2.00"}` |
| `A,-0.00\n` | `{"A": "0.00"}` |
| `A,0.10\nA,-0.10\n` | `{"A": "0.00"}` |
| `"He said ""Hi""",1.20\n` | `{'He said "Hi"': '1.20'}` |
| `" A ",1\nA,2\n` | `{" A ": "1.00", "A": "2.00"}` |
| `\nA,1\n\nA,2\n` | `{"A": "3.00"}` |
| 没有数据行 | `{}` |
| `A,999999999999.99\nA,0.01\n` | `{"A": "1000000000000.00"}` |

额外把同一合法输入的行尾换成 CRLF，预期不变；最后一行没有行尾也应成功。字典的插入顺序不作为验收条件。

允许修改 `totals.py` 和补充 `tests/`，不修改原有公开断言。无需新增金额校验框架或支持题目明确排除的格式。
