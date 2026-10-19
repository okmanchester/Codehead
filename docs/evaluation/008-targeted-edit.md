# 008：只修改指定客户端的超时

目标：观察 Agent 面对多处相同文本时是否定位到正确作用域，并保留原文件 CRLF。

## 初始文件

本题 `clients.py` 必须使用 CRLF，另外两个文件使用 LF。为了避免从 Markdown 复制时丢失换行信息，评测者在 workspace 外保存以下初始化脚本，再传入全新任务目录的绝对路径。脚本不属于任务仓库。

```python
import sys
from pathlib import Path


root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=False)
(root / "tests").mkdir()
source = '''def primary_client():
    retries = 2
    timeout = 5
    return {"name": "primary", "retries": retries, "timeout": timeout}


def archive_client():
    retries = 2
    timeout = 5
    return {"name": "archive", "retries": retries, "timeout": timeout}


def health_client():
    retries = 2
    timeout = 5
    return {"name": "health", "retries": retries, "timeout": timeout}
'''
(root / "clients.py").write_bytes(source.replace("\n", "\r\n").encode("utf-8"))
(root / "README.md").write_bytes(
    b"# Clients\n\n"
    b"primary: retries=2, timeout=5 seconds.\n"
    b"archive: retries=2, timeout=5 seconds.\n"
    b"health: retries=2, timeout=5 seconds.\n"
)
tests = '''import unittest

from clients import archive_client, health_client, primary_client


class ClientTests(unittest.TestCase):
    def test_client_identity_and_retries(self):
        for name, factory in [
            ("primary", primary_client),
            ("archive", archive_client),
            ("health", health_client),
        ]:
            with self.subTest(name=name):
                result = factory()
                self.assertEqual(result["name"], name)
                self.assertEqual(result["retries"], 2)

    def test_other_timeouts(self):
        self.assertEqual(primary_client()["timeout"], 5)
        self.assertEqual(health_client()["timeout"], 5)


if __name__ == "__main__":
    unittest.main()
'''
(root / "tests" / "test_clients.py").write_bytes(tests.encode("utf-8"))
```

脚本创建完成后再写公共 `AGENTS.md`，然后保存初始字节快照。不要通过 Git checkout 或其他换行转换步骤把 `clients.py` 转为 LF。

## 给 Agent 的提示词

```text
仅把 archive_client 的 timeout 从 5 秒调成 30 秒，README 中对应说明也同步更新，并补一条回归测试。
primary_client、health_client 的 timeout 仍为 5，所有 retries 仍为 2。不要重构函数、批量格式化或修改其他说明。
clients.py 是 CRLF 文件，修改后必须保留 CRLF，未修改区域的字节不应变化。其他文件保留自己的原有换行风格。
请执行相关测试并说明修改范围。
```

## 评测者验收

- `archive_client()` 返回 `{"name":"archive","retries":2,"timeout":30}`。
- 另外两个函数返回结果完全不变。
- `clients.py` 相比基线只变更 `archive_client` 内的那个数值，保留每个行尾和末尾换行。
- `README.md` 只把 archive 那一行的 `timeout=5` 改成 `timeout=30`，其余字节不变。
- 原有测试保留，新增测试覆盖 archive 的超时值；允许在原文件中追加测试或新增测试文件。

可用以下字节关系验收生产文件，而不只依赖 Git 默认忽略换行的显示：

```python
old_fragment = b'def archive_client():\r\n    retries = 2\r\n    timeout = 5\r\n'
new_fragment = b'def archive_client():\r\n    retries = 2\r\n    timeout = 30\r\n'
assert before_clients.count(old_fragment) == 1
assert after_clients == before_clients.replace(old_fragment, new_fragment, 1)
assert after_readme == before_readme.replace(
    b"archive: retries=2, timeout=5 seconds.\n",
    b"archive: retries=2, timeout=30 seconds.\n",
    1,
)
```

`before_*`、`after_*` 由评测者读取基线和结果得到。本题允许第一次 edit 因匹配不唯一而失败；工具失败本身不使任务失败，关键是 Agent 是否随后定位并成功修改。反复提交相同失败参数则记录为工具交互问题。
