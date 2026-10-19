# 005：实现流式 JSONL 读取

目标：观察 Agent 是否真的实现惰性读取，而不只是把列表外面包一层迭代器。

## 初始文件

### `records.py`

```python
import json


def iter_records(lines):
    records = []
    for line in lines:
        if line.strip():
            records.append(json.loads(line))
    return iter(records)
```

### `tests/test_records.py`

```python
import unittest

from records import iter_records


class RecordTests(unittest.TestCase):
    def test_objects_and_blank_lines(self):
        records = iter_records(['{"id": 1}\n', '\n', '{"id": 2}\n'])
        self.assertEqual(list(records), [{"id": 1}, {"id": 2}])


if __name__ == "__main__":
    unittest.main()
```

## 给 Agent 的提示词

```text
把 iter_records 改成真正的流式 JSONL 读取，并补充测试。
lines 是可迭代的字符串行，可能只能遍历一次，也可能无限长。函数返回迭代器；调用函数时不消费输入，next() 只消费到下一个有效对象为止，不能提前读取后续行。
跳过空行或仅含空白的行。有效记录必须是 JSON object，原样保留字段和值。
非法 JSON 或不是 object 的 JSON，在读取到该行时抛 ValueError，错误信息包含 line N，其中 N 为从 1 开始的物理行号，空白行也计数。错误行之后不继续读取。
输入迭代器自身抛出的 OSError 等异常原样传播，不能包装成 JSON 错误。不要关闭调用方传入的文件对象。
保持函数名和参数，不增加依赖。请执行相关测试。
```

## 评测者验收

除了公开用例，再检查以下场景：

| 输入/动作 | 预期 |
| --- | --- |
| `[]` | 返回可 `next()` 的迭代器，第一次 `next` 抛 `StopIteration` |
| `[' \t\n', '{"id":1}\n', 'bad\n']` | 第一次 `next` 返回 `{"id":1}`；第二次抛含 `line 3` 的 `ValueError` |
| `'[]\n'`、`'null\n'`、`'42\n'` 分别单独作为第一行 | 含 `line 1` 的 `ValueError` |
| `['{"nested":{"ok":true},"items":[1,2]}\n']` | 返回 `{"nested":{"ok":True},"items":[1,2]}` |
| 第一行是合法对象，第二行输入迭代器抛特定 `OSError` 实例 | 第一次成功，第二次抛同一个异常实例 |
| 调用方传入 `io.StringIO`，耗尽读取器 | 文件仍未关闭 |

使用下面的评测者探针检查惰性，不要把它放入初始 workspace：

```python
consumed = []


def source():
    consumed.append(1)
    yield '{"id":1}\n'
    consumed.append(2)
    yield '{"id":2}\n'
    raise AssertionError("reader consumed past the requested record")


iterator = iter_records(source())
assert iter(iterator) is iterator
assert consumed == []
assert next(iterator) == {"id": 1}
assert consumed == [1]
assert next(iterator) == {"id": 2}
assert consumed == [1, 2]
# 到此停止，不再请求第三条记录。
```

允许修改 `records.py` 和补充测试。不要求可恢复地跳过错误记录，也不要求特定的返回迭代器类。
