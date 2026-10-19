# 006：修复中文路径下的报告命令

目标：观察 Agent 能否把 CLI 行为、文件路径和编码作为完整契约处理，并用子进程验证。

## 初始文件

### `report.py`

```python
import argparse
import json
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    names = args.input.read_text().splitlines()
    target = args.output or Path("summary.json")
    target.write_text(json.dumps({"count": len(names), "names": names}))
    print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### `tests/test_report.py`

```python
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from report import main


class ReportTests(unittest.TestCase):
    def test_explicit_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "names.txt"
            target = Path(directory) / "out.json"
            source.write_text("Alice\nBob\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = main([str(source), "--output", str(target)])
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"count": 2, "names": ["Alice", "Bob"]},
            )


if __name__ == "__main__":
    unittest.main()
```

## 给 Agent 的提示词

```text
修复 report.py，补充包含真实子进程调用的回归测试。
输入是 UTF-8 文本，每行一个名字。每行先去掉两端空白，空行忽略；保留有效名字的原始顺序和重复项。
未传 --output 时，报告写到输入文件同目录，文件名通过 Path.with_suffix(".summary.json") 得到；例如 数据/名单.txt 对应 数据/名单.summary.json。
显式传 --output 时使用该路径，相对路径以调用进程的 cwd 为基准。输入和输出文件读写都显式使用 UTF-8；JSON 文本保留实际中文字符，不转成 \uXXXX。
JSON 结构固定为 {"count": 数量, "names": 名字数组}。成功时 stdout 只输出数量和换行，stderr 为空，退出码 0。
读写文件遇到 OSError 或输入不是合法 UTF-8 时，向 stderr 输出 Error: 开头的信息，stdout 为空，退出码 1，不显示 traceback。读取输入失败时不能创建或覆盖报告文件。
可以假设输出父目录已存在；不要求处理输入输出为同一路径、断电写入或跨文件事务。保留 argparse 的用法错误行为。
保持 main(argv=None) 接口，只用标准库，执行相关测试并说明结果。
```

## 评测者验收

评测者在 workspace 外准备下面的目录。`report.py` 始终指向任务 workspace 内的文件：

```text
<临时目录>/
  数据 集/
    名单.txt
  调用目录/
```

`名单.txt` 的 UTF-8 内容：

```text
 张三

李四
张三
```

| 场景 | 预期 |
| --- | --- |
| 在「调用目录」启动脚本，传输入的绝对路径，不传输出 | `数据 集/名单.summary.json` 存在；调用目录没有 `summary.json` |
| 同一次运行的报告 | 解析为 `{"count":3,"names":["张三","李四","张三"]}`；UTF-8 原文包含 `张三` |
| 标准流 | stdout 为 `3\n`，stderr 为空，退出码 `0` |
| 显式 `--output custom.json` | 写入调用目录的 `custom.json`；不额外生成默认报告 |
| 空文件或只有空白行 | `{"count":0,"names":[]}`，stdout 为 `0\n` |
| 输入 `archive.names.txt` | 默认文件为 `archive.names.summary.json` |
| 输入不存在，输出位置原本有哨兵文件 | 返回 `1`；stdout 为空，stderr 含 `Error:` 且无 `Traceback`；哨兵字节不变 |
| 输入字节为 `b'\xff\xfe'` | 同上，不生成默认报告 |
| 显式输出路径指向现有目录 | 返回 `1`；stdout 为空，stderr 含 `Error:` 且无 `Traceback` |

子进程调用使用参数列表而不是拼接 shell 字符串，并通过 `sys.executable` 选 Python。评测者固定子进程 `PYTHONIOENCODING=utf-8`，以 UTF-8 捕获标准流；这只固定标准流，不替代文件的显式编码。标准流比较允许平台把换行表示为 CRLF。

不要依靠 chmod 制造不可写目录：Windows 与 Unix 的权限行为不同。输出目标是目录这一场景足以提供可移植的写入失败样本。

允许修改 `report.py` 和补充测试，不修改原有公开断言。验收结束确认原输入文件字节不变。
