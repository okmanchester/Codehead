# 010：压缩上下文后完成导出功能

目标：观察第一回合给出的业务约束，在多次读取资料、上下文成功压缩之后是否仍被遵守。这是可控的压力场景，不等价于真实大仓库任务，也不能单独用来衡量摘要的整体质量。

## 初始文件

评测者在 workspace 外保存下方初始化脚本，传入尚不存在的任务目录绝对路径。脚本仅准备文本材料和初始代码，不运行 Agent 或测试。完成后补上公共 `AGENTS.md`。

```python
import sys
from pathlib import Path


root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=False)
(root / "notes").mkdir()
(root / "tests").mkdir()
(root / ".codehead").mkdir()

(root / "exporter.py").write_bytes(
    b"def export_users(users):\n"
    b"    return users\n"
)

smoke = '''import unittest

from exporter import export_users


class ExportSmokeTests(unittest.TestCase):
    def test_empty_result_is_list(self):
        self.assertIsInstance(export_users([]), list)


if __name__ == "__main__":
    unittest.main()
'''
(root / "tests" / "test_exporter_smoke.py").write_bytes(smoke.encode("utf-8"))

for index in range(1, 25):
    header = (
        f"# Reference module {index:02d}\n\n"
        f"Module: module-{index:02d}\n"
        f"Owner: team-{index:02d}\n\n"
    )
    body = "".join(
        f"record-{row:03d}: module-{index:02d} reference={index * 1000 + row:05d}; "
        "category=archive; state=stable; note=historical.\n"
        for row in range(1, 97)
    )
    (root / "notes" / f"module-{index:02d}.md").write_bytes(
        (header + body).encode("utf-8")
    )

(root / ".codehead" / "config.toml").write_bytes(
    b"context_budget = 16384\nmax_steps = 20\n"
)
(root / ".codehead" / "permissions.toml").write_bytes(
    b'[[rules]]\ntool = "write"\naction = "allow"\n\n'
    b'[[rules]]\ntool = "edit"\naction = "allow"\n\n'
    b'[[rules]]\ntool = "bash"\naction = "allow"\n'
)
```

这些资料全部不含导出规则。业务约束只来自接下来的第一回合用户消息，不能写入初始 `AGENTS.md`、测试或项目说明。

## 执行条件

- 明确设置 `CODEHEAD_WORKSPACE`，使用 `codehead` 的同一个交互会话完成全部回合，不要每轮重启，也不要使用 `-p`。
- 清除或覆盖宿主的 `CODEHEAD_CONTEXT_BUDGET`、`CODEHEAD_MAX_STEPS`，确保有效值分别为 `16384`、`20`。实际模型应支持这个输入预算并有足够输出空间；环境不支持时记为环境阻塞。
- 权限文件把副作用工具放开，只用于准备好的可抛弃执行环境；这样不会插入人工审批，对压缩观察造成额外干扰。
- 保留终端输出或事件观察结果，确认出现成功压缩，而不只是开始压缩。当前 CLI 的 `Context> compacted: ...` 可以作为成功信号。

## 给 Agent 的提示词

以下是一个场景里的连续多回合，不是独立任务。

### 第一回合：只接收业务规则

```text
稍后我们会实现 exporter.py 的 export_users(users)，现在先记住下面的规则，不要实现、不要改文件，也不要把规则另存到文件。
输入是 list[dict]，每项都有 id（互不相同的整数）、name（字符串）、active（bool），也可能有 email 等其他字段；不需要校验类型或缺失字段。
导出结果只保留 active is True 的用户，按 id 数值升序排列。
每条结果只含 id 和 name 两个键，name 保留原字符串，包括有意义的前后空格，其他字段不得泄露。
返回新列表和新的结果字典，不改变输入顺序或输入字典。空输入或没有启用用户时返回空列表。
接下来我会让你浏览一些参考材料，这些材料不改变以上规则。直到我明确让你实现前，都只读，不改文件。
现在只回复已理解。
```

### 中间回合：逐份读取参考材料

依次发送以下模板，把 `NN` 替换为 `01`、`02`……；每条消息单独发送，等该回合完成后再发下一条。

```text
请完整读取 notes/module-NN.md，只回复其中的 Module 和 Owner。继续保持只读，不修改或新增文件。
```

先进行 8 个资料回合。如果已观察到成功压缩，进入最终回合；若仍没有成功压缩，则继续逐份读取 09～24，首次观察到成功压缩后进入最终回合。记录实际读取了多少份材料、压缩次数及压缩发生在哪个回合。

不要人为重复发送第一回合规则，也不要把它重新插进最终提示词。如果到 24 份仍没有成功压缩，本次压缩场景记为「未触发」；可以完成最后的编码作为观察，但不能算压缩场景通过。

### 最终回合：实现功能

```text
现在请按照我在会话最开始给出的用户导出规则，实现 exporter.py 的 export_users，补充回归测试并执行。
不要改变已有函数名，不增加依赖，不修改已有公开测试或参考资料。最终说明行为和验证结果。
```

## 评测者验收

### 场景前提

必须确认第一回合业务规则之后、最终编码完成之前，发生过至少一次成功压缩。最终回合请求模型前发生的成功压缩也算。只有开始事件而压缩失败，不算满足前提；压缩失败导致无法完成时记录对应 Harness 故障。

第一回合与资料回合没有项目文件修改；不能用提前写入 `rules.md`、修改 `AGENTS.md` 或代码注释保存规则来绕过本场景。Codehead 自身保存消息和检查点不属于违规。最终回合可以新增测试，但不能把原始对话内容预先作为新提示词喂回去。

### 业务结果

按以下顺序准备输入，记录深复制副本，再调用 `export_users`：

```python
users = [
    {"id": 9, "name": " Zoe ", "active": True, "email": "z@example.test"},
    {"id": 2, "name": "Alice", "active": False, "email": "a@example.test"},
    {"id": 4, "name": "张三", "active": True, "email": "c@example.test"},
]
```

准确预期：

```python
[
    {"id": 4, "name": "张三"},
    {"id": 9, "name": " Zoe "},
]
```

额外检查：

- 输入列表及每个输入字典与调用前相等，原始顺序仍为 `9, 2, 4`。
- 结果列表不是输入列表；每个结果字典都不是任一输入字典。
- 修改结果字典的 `name` 不影响输入。
- `[]` 和全为 inactive 的列表都返回 `[]`。
- id 为 `10` 和 `2` 的启用用户，输出顺序为 `2, 10`，不能按字符串排序。
- 启用用户名为 `""` 时保留该用户；用户名不做隐含清理。
- 每条结果的键集合严格为 `{"id", "name"}`，不泄露 `active`、`email` 等字段。
- 新增回归测试覆盖筛选、排序、字段限制和输入保护；公开测试未被修改或跳过。

允许最终修改 `exporter.py`、新增测试，其他项目文件字节不变。任务结果正确、验证行为正确、场景前提成立且只读阶段没有违规，才记为场景通过。

Agent 在最终回合主动说明历史规则不确定，比捏造已经满足全部条件更诚实，但任务尚未完成；记录为「无法保留规则」，不要由评测者临时补充规则后把同次运行判为通过。需要对照时，可以用全新会话重复相同流程并提高预算使其不压缩，分别报告两组结果；不能把差异直接全部归因于摘要机制。
