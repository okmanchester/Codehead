# 004：增加环境变量配置

目标：观察 Agent 是否能在加载器和 CLI 之间保持一致的优先级与错误语义。

## 初始文件

### `settings.py`

```python
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    retries: int = 2
    timeout: int = 5


def load_settings(path: Path) -> Settings:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Settings(
        retries=raw.get("retries", 2),
        timeout=raw.get("timeout", 5),
    )
```

### `app.py`

```python
import argparse
from pathlib import Path

from settings import load_settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    print(f"retries={settings.retries}, timeout={settings.timeout}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### `settings.json`

```json
{"retries": 4, "timeout": 8}
```

### `tests/test_settings.py`

```python
import tempfile
import unittest
from pathlib import Path

from settings import load_settings


class SettingsTests(unittest.TestCase):
    def test_file_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"retries": 4, "timeout": 8}', encoding="utf-8")
            self.assertEqual(load_settings(path).retries, 4)
            self.assertEqual(load_settings(path).timeout, 8)


if __name__ == "__main__":
    unittest.main()
```

## 给 Agent 的提示词

```text
为这个小应用增加环境变量覆盖，并补充加载器和 CLI 的测试。
load_settings 扩展为 load_settings(path, env=None)，env 可以是字符串映射；仅 env is None 时读取 os.environ，显式传 {} 就是空环境。
retries 对应 DEMO_RETRIES，timeout 对应 DEMO_TIMEOUT。优先级为默认值→JSON 文件→环境变量；空串或仅含空白的环境值不覆盖文件，非空值先去掉两端空白。
文件路径保证存在，内容保证是 JSON 对象，不需要处理 JSON 语法或文件不存在的情况。忽略不认识的字段和环境变量。
先合并再校验：最终 retries、timeout 都须为正整数；文件值只能是非 bool 的整数，环境值只接受由 ASCII 数字 0～9 组成的字符串，转换后必须大于 0。不接受 +3、1.5、true 等写法。
无效最终值抛 ValueError，错误文本包含对应字段名。CLI 捕获这类错误，向 stderr 输出 Error: 开头的信息，返回 2；stdout 为空，不显示 traceback。成功输出保持原格式，返回 0。
不要修改传入映射或全局 os.environ，不增加依赖。更新 README.md 说明新行为，执行相关测试。
```

## 评测者验收

每行独立调用，文件可以由评测者替换为指定 JSON。只有「未传 env」场景才使用受控的进程环境。

| 文件 | env | 预期 |
| --- | --- | --- |
| `{}` | `{}` | `retries=2, timeout=5` |
| `{"retries":4,"timeout":8}` | `{}` | `4, 8` |
| 同上 | `{"DEMO_RETRIES":"9"}` | `9, 8` |
| 同上 | `{"DEMO_RETRIES":" 6 ","DEMO_TIMEOUT":"12"}` | `6, 12` |
| 同上 | `{"DEMO_RETRIES":"","DEMO_TIMEOUT":" \t "}` | `4, 8` |
| `{"retries":4}` | `{"UNRELATED":"999"}` | `4, 5` |
| `{"retries":false}` | `{}` | 含 `retries` 的 `ValueError` |
| `{}` | `DEMO_TIMEOUT` 分别为 `0`、`-1`、`1.5`、`true`、`+3`、`３` | 含 `timeout` 的 `ValueError` |
| `{"retries":"bad","timeout":8}` | `{"DEMO_RETRIES":"7"}` | `7, 8`，被覆盖的文件值不阻塞加载 |

额外检查：

- 用 `unittest.mock.patch.dict` 把宿主 `DEMO_RETRIES` 设为 `99`，显式传 `env={}` 仍取文件值；省略 `env` 则取 `99`。
- 传入 `types.MappingProxyType` 也可读取；调用前后传入映射和 `os.environ` 均不变。
- `app.main([config_path])` 在受控环境中验证成功与失败的 stdout、stderr、返回值；子进程验证失败时没有 traceback。
- 原公开测试在清除了 `DEMO_RETRIES`、`DEMO_TIMEOUT` 的环境中执行，避免宿主设置干扰题目基线。

允许修改 `settings.py`、`app.py`，新增 `README.md` 并补充测试。无需支持多层配置、`.env`、热重载或重写配置文件。
