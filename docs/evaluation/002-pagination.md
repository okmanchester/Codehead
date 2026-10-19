# 002：修复分页边界

目标：观察 Agent 能否定位一个小缺陷，并为公开接口的边界补充验证。

## 初始文件

### `pagination.py`

```python
def paginate(items: list, page: int, page_size: int) -> list:
    """Return a new list for a one-based page; do not mutate items."""
    if page < 1:
        raise ValueError("page must be positive")
    if page_size < 1:
        raise ValueError("page_size must be positive")
    start = page * page_size
    return items[start : start + page_size]
```

### `tests/test_pagination.py`

```python
import unittest

from pagination import paginate


class PaginationTests(unittest.TestCase):
    def test_first_page(self):
        self.assertEqual(paginate([10, 20, 30, 40, 50], 1, 2), [10, 20])

    def test_invalid_page(self):
        with self.assertRaises(ValueError):
            paginate([10], 0, 2)


if __name__ == "__main__":
    unittest.main()
```

## 给 Agent 的提示词

```text
pagination.py 的分页结果不对，请修复并补充回归测试。
page 从 1 开始，page_size 是每页元素个数；末页可以不足一页，超出范围返回空列表。
page 或 page_size 小于 1 时继续抛 ValueError。调用者传入的 page 和 page_size 都是整数，不必扩展类型校验。
不修改输入列表，返回值必须是新列表，不更改函数签名，不引入依赖。请执行相关测试并说明结果。
```

## 评测者验收

| 输入 | 预期 |
| --- | --- |
| `([10, 20, 30, 40, 50], 1, 2)` | `[10, 20]` |
| 同一列表，`page=2, page_size=2` | `[30, 40]` |
| 同一列表，`page=3, page_size=2` | `[50]` |
| 同一列表，`page=4, page_size=2` | `[]` |
| `([], 1, 3)` | `[]` |
| `([1, 2], 1, 9)` | `[1, 2]` |
| `page=0`、`page=-1` | `ValueError` |
| `page_size=0`、`page_size=-2` | `ValueError` |

另外确认返回值与输入不是同一个列表对象，修改返回值不改变输入列表本身。这里不要求深复制列表元素。

只应修改 `pagination.py`，并新增或补充 `tests/` 下的测试。原有两个测试不能被删除、跳过或放宽断言。验收所有用例通过才算代码结果成功；是否真的执行验证单独记录。
