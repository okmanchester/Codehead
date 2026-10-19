# 007：为 TTL 缓存补回归测试

目标：观察 Agent 能否编写独立于实现细节、能识别真实缺陷的测试。本题生产代码初始即符合契约，只允许增加测试。

## 初始文件

### `cache.py`

```python
import time


class TTLCache:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._entries = {}

    def set(self, key, value, ttl):
        if ttl < 0:
            raise ValueError("ttl must not be negative")
        self._entries[key] = (value, self._clock() + ttl)

    def get(self, key, default=None):
        entry = self._entries.get(key)
        if entry is None:
            return default
        value, expires_at = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return default
        return value
```

### `tests/test_cache_smoke.py`

```python
import unittest

from cache import TTLCache


class CacheSmokeTests(unittest.TestCase):
    def test_set_and_get(self):
        cache = TTLCache(clock=lambda: 0.0)
        cache.set("a", "value", ttl=10)
        self.assertEqual(cache.get("a"), "value")


if __name__ == "__main__":
    unittest.main()
```

## 给 Agent 的提示词

```text
cache.py 当前实现应当是正确的，但公开测试太少。请只在 tests/ 下新增 unittest 测试，不修改 cache.py 或已有测试。
TTLCache 支持注入 clock；clock 返回单调不减的秒数。ttl 是非负 int 或 float，负数抛 ValueError，失败不能改动已有缓存值。
过期定义为 now >= 设置时间 + ttl；ttl=0 立即过期。get 不延长有效期，覆盖 set 则更新值和截止时间。
缺失或过期时返回本次 get 指定的 default（默认 None）；None、0、False、空字符串都可以作为正常缓存值。
不同 key 的值和截止时间相互独立。测试不要 sleep，不依赖墙上时钟，不读取 _entries 等私有属性，通过注入可控时钟验证公开行为。
请执行新增测试与现有测试，并说明覆盖了哪些边界。
```

## 评测者验收

先在原始生产代码上运行全部测试，必须通过；再确认 `cache.py`、公共 `AGENTS.md` 和原有测试字节不变。

把下列错误分别施加到生产代码的独立副本，每次只施加一个错误，再运行 Agent 新增的测试。变体只用于评测者验收，不应暴露给 Agent。测试框架不能因导入、语法或测试收集失败而假报「抓到缺陷」。

| 错误变体 | 应能识别它的公开行为 |
| --- | --- |
| 把过期比较从 `>=` 改成 `>` | 截止时刻恰好相等就已过期 |
| 缺失和过期时固定返回 `None` | 两类情况都须尊重调用者传入的非 None default |
| 用 `value or default` 返回尚未过期的值 | `0`、`False`、`""`、`None` 不能被当作缺失 |
| 删除负 ttl 校验 | 负数必须抛 `ValueError` |
| 先覆盖条目，再对负 ttl 抛错 | 原值仍必须可读取，失败没有副作用 |
| 覆盖已有 key 时保留旧截止时间 | 后一次 `set` 的 ttl 生效；例如 t=0 ttl=5，t=3 覆盖 ttl=10，t=6 仍可读取新值 |
| 每次成功 `get` 都把截止时间向后延长 | t=0 ttl=5，t=3 读取后，t=5 仍须过期 |
| 所有 key 共用最后一次设置的截止时间 | 在 t=0 设置 A ttl=3、B ttl=8；t=4 时 A 缺失但 B 仍存在 |

这些变体不要求使用特定变异测试工具，可手工构造。将每个变体是否被失败断言识别分别记录，不把它们合成含糊的「覆盖率」。另外人工确认存在 `ttl=0` 的行为断言。

若暂不执行变体检查，只能记录「测试在正确实现上通过，缺陷辨别能力未验证」，不能宣称测试质量已验收。检查测试中是否使用了真实等待、读取私有属性或仅复制实现算法。
