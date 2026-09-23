# Codehead

Codehead 是一个从零构建的 Coding Agent 项目。

项目目标：逐步实现现代 Coding Agent 背后的核心 Harness 能力

项目会保持增量开发：先实现最小可工作的 Agent，再根据真实遇到的问题逐步增加复杂度。

## Goals

Codehead 主要用于探索和实现 Coding Agent 的 Harness 能力
也包括理解：

* 为什么需要它们
* 它们解决什么工程问题
* 不同设计之间有什么取舍

## Design Principles

### Simple First

优先实现能够解决当前问题的最简单方案，再逐步迭代

不因为成熟 Coding Agent 使用了某种架构，就提前复制相同的复杂度。

### Understand the Harness

Codehead 的核心 Harness 应保持可理解。

关键执行逻辑不依赖大型 Agent Framework 隐藏实现细节。

### Evolve From Real Problems

新的抽象和机制应尽可能来源于实际遇到的问题，例如：

* 工具重复调用
* 工具执行失败
* 用户中断
* Shell 输出过大
* 上下文超过限制
* 危险操作需要确认

先遇到问题，再设计解决方案。

## Status

Codehead 正处于早期开发阶段。

项目结构和接口在这一阶段可能频繁变化，不保证向后兼容。

## Tech Stack

* Python 3.12+
* LLM API with native tool calling
* Pydantic
* pytest

其他依赖将在出现明确需求时引入。

## Quick Start

```powershell
conda activate codehead
python -m pip install -e . --no-build-isolation
# 另需安装 ripgrep，并确保 rg 可从 PATH 调用
$env:DEEPSEEK_API_KEY = "your-api-key"
codehead
```

Codehead 默认使用 `deepseek-flash` 和 `https://api.deepseek.com`。可通过
`CODEHEAD_MODEL`、`DEEPSEEK_BASE_URL` 和 `CODEHEAD_WORKSPACE` 覆盖默认值。

当前 `read`、`write`、`edit`、`glob` 和 `grep` 工具只能访问 workspace 内的文件。
`glob` 和 `grep` 依赖系统安装的 [ripgrep](https://github.com/BurntSushi/ripgrep)；
启动时若找不到 `rg` 会明确报错。`bash` 默认以 workspace 为工作目录，可通过
`workdir` 为单次调用指定 workspace 内的目录；它不提供进程沙箱。Windows 使用
PowerShell，Unix 使用 Bash。

## 流式事件

Provider 和 AgentLoop 使用异步流，事件类型统一从 `codehead.models` 导入。
Provider 输出 `LLMTextDelta`，最后输出一次 `LLMResponseCompleted`；Loop 输出
`TurnStarted`、`ModelStarted`、`AssistantTextDelta`、`ModelCompleted`、`ToolStarted`、
`ToolCompleted` 和最终的 `TurnCompleted`，以及失败或取消时的 `TurnFailed` / `TurnCancelled`、压缩期间的
`ContextCompactionStarted` / `ContextCompacted`。`step` 从 1 开始，表示本回合第几次
正常模型调用；摘要请求不占用 `max_steps`，摘要增量不作为助手答案展示。

文本增量仅用于即时展示。Loop 校验完整响应的 FinishReason 后才保存 assistant
消息、发送 `ModelCompleted` 和执行工具；工具结果保存后才发送 `ToolCompleted`。
CLI 不重复打印完成事件中的文本，也不默认展开工具输出。事件包含回合 ID，工具事件还包含执行记录 ID；
`ToolCompleted.result.outcome` 区分 `succeeded`、`failed`、`cancelled` 和 `skipped`。
模型失败和步数耗尽仍通过
异常报告；CLI 会显示回合错误并继续使用同一 Session，保留本轮 user 消息及此前已提交
的工具历史，不自动重试。工具业务失败通过 `ToolResult.is_error` 回传模型。

`complete(request)` 和 `run(session, user_input)` 现在必须 `await`，内部消费同一条
流式路径。直接消费事件时，使用 `contextlib.aclosing` 保证提前退出也能关闭流：

```python
from contextlib import aclosing

async def consume_turn(loop, session, user_input):
    async with aclosing(loop.stream(session, user_input)) as events:
        async for event in events:
            print(event)
```

所有回合结束后，调用方须在 `finally` 中 `await loop.aclose()`（直接使用 Provider
则调用 `provider.aclose()`）。CLI 已负责这一清理。外部注入的 SDK 客户端由注入方关闭。

一个 Session 同时仅支持一个回合。同步工具在线程中按顺序执行。执行期间第一次 Ctrl+C
请求取消当前回合；再次按下时仅显示收尾状态。文件工具一旦启动，会等待真实结果并保存；
`bash`、`glob` 和 `grep` 会停止受管理的进程树。尚未启动的调用记录为 `skipped`。
工具副作用不会回滚，已停止命令可能留下部分副作用。收尾完成后 CLI 回到输入提示，
可开始新回合或正常退出。收尾期间输入提示不会提前出现。数据库无法写入或无法确认
进程树已停止时，会报告收尾失败并拒绝宣称会话可恢复。事件未持久化，不支持重放。

嵌入调用取消 `AgentLoop.stream()` / `run()` 后，可继续使用已收尾的同一个 Session。
调用方应使用 `contextlib.aclosing` 关闭提前停止的流，并在有序退出前调用
`session.finish()`；`close()` 仅释放资源，不提交正常退出状态。取消整个 CLI REPL 时，
先等待当前回合收尾，再提交会话退出状态和关闭资源。

## 上下文与会话存档

CLI 将原始消息保存到 workspace 下的 `.codehead/sessions.sqlite3`，默认启动创建
新的会话。`CODEHEAD_SESSION_DB` 可覆盖数据库位置；相对路径以 workspace 为基准。
数据库保存会话元数据、按序号追加的消息、压缩检查点，以及新回合与工具执行状态。
这里的原始消息指已通过响应校验的完整消息，以及经过现有输出限制处理的工具结果，
不包含流式片段或被工具输出治理截掉的内容。支持正常退出后的会话续聊，尚不支持
历史检索、崩溃后自动恢复或已有遗留 `active` 会话的恢复；数据库不会自动清理。

`Session.messages` 现在表示活跃请求上下文。压缩成功后，Python 内存只保留摘要、
system prompt、最新用户输入和近期消息，旧消息继续保存在数据库中，不在请求路径
重新全量加载。工具调用和对应的全部结果始终作为一个整体保留或压缩。

`CODEHEAD_CONTEXT_BUDGET` 默认 32768，最小 1024，表示估算的**输入** token 预算，
不是模型窗口大小。需根据目标模型为输出预留空间，再配置输入预算。本地估算包含
消息、工具参数、工具定义和协议开销，使用 JSON 的 UTF-8 字节数除以 3，再加固定
开销，初始乘以 1.2；正常请求返回的 input usage 仅向上校准当前会话的系数。
没有 usage 时仍可估算。这不是精确 tokenizer，也不保证服务端一定不会超限。

每次正常模型请求前检查用量，达到预算 75% 时自动摘要，要求压缩后不超过 50%。
近期消息约占预算 25%，至少保留最后一个完整消息或工具交互单元，另外保留 system
prompt 和最新用户输入。摘要复用当前模型，以独立无工具请求处理旧历史；超出单次
输入预算时按完整单元分批合并，摘要本身上限为预算的 1/16。CLI 显示开始状态及
压缩前后估算用量。摘要属于可能有损的历史参考资料，不作为 system 指令。

摘要失败、取消或无法有效缩小时不会提交候选检查点，也不会丢弃原活跃消息。
压缩错误结束当前回合并回到输入提示，不自动重试；过大的最新输入、system prompt、
工具定义或不可拆工具交互也可能使压缩失败。消息和检查点均先提交 SQLite，再更新
内存或发送完成事件；数据库错误会终止会话并退出 CLI，已执行工具的副作用不能回滚。

嵌入使用时，通过 `Session(store=SQLiteSessionStore(path))` 注入磁盘存储，调用方
负责关闭 store。无参数 `Session()` 为兼容嵌入调用和测试使用临时的内存 SQLite，
不会落盘，应调用 `session.close()` 释放它；CLI 始终使用磁盘数据库并负责资源清理。

## 恢复会话

```text
codehead                        # 新建会话
codehead --list-sessions        # 列出当前 workspace 的会话
codehead --resume <完整会话ID>    # 继续一个正常关闭的会话
```

新建或恢复成功时会显示完整会话 ID。列表按最近活动时间倒序显示 ID、UTC 时间、
模型和退出状态；列表不要求 API key、有效的模型预算配置或 ripgrep，也不会创建
新会话。`--resume` 和 `--list-sessions` 不能同时使用。不支持 ID 前缀匹配、
自动选择最近会话或跨 workspace 恢复；指定 ID 无效时会报错，不会新建会话。

只有在输入提示处输入 `exit` / `quit`、收到 EOF 或按 Ctrl+C，且没有运行中回合、
已记录的工具调用都有对应结果时，才将会话标记为 `closed`。模型错误、压缩失败、
步数耗尽或已收尾的执行中断后返回
输入提示，仍可正常退出并在下次恢复；历史末尾可以是未获回答的用户输入或完整的
工具结果组。执行期间的可控中断先收尾回合，再由用户在输入提示处正常退出。
数据库故障、无法确认工具停止或进程异常退出不会写入正常退出标记，这些会话保留为
`active` 并拒绝恢复；不提供强制解锁。旧存档保持原样，新回合才产生 Turn / Tool 状态记录。

恢复会沿用原会话 ID 和消息序号。已有压缩检查点时，只加载最新摘要、保留消息及
检查点之后的新增消息，不重新加载被压缩的原文；无检查点则加载尚未压缩的历史。
检查点、消息格式或工具交互结构损坏时明确报错，不回退旧检查点。恢复成功后等待
新的用户输入，不自动继续上一轮、不自动重试模型请求，也不重新执行历史工具。
恢复占用与历史校验在同一事务内完成，同时恢复同一会话的两个进程只有一个能成功。

恢复沿用存档中的 system prompt（包括当时的 `AGENTS.md`），与当前指令不同时
会提示；要采用新指令，应新建会话。模型、端点、工具和输入预算使用当前配置。
模型名称和端点均相同时保留 token 校准系数，任一变化则提示并重置为 1.2。
每个新回合重新计算 `max_steps`。正常退出的空会话也可恢复，首次输入使用当前
system prompt。

旧数据库会自动、事务性地增加恢复所需字段，原始消息与检查点保持不变。功能上线前
的会话没有可靠的退出记录，标记为 `legacy`（退出状态未知），可以列出但不能恢复。

嵌入调用可使用 `Session.resume(id, store=store, workspace=..., model=...,
base_url=...)`。在调用方确认是正常退出、且没有正在执行的回合时调用
`session.finish()` 保存退出标记和 token 校准系数；`session.close()` / `store.close()`
仅释放资源，不写入正常退出标记。已 finish 的 Session 实例不能继续追加消息，应通过
恢复入口获得新实例；调用方仍负责关闭注入的 store。

## 工具输出

工具统一经 `ToolRuntime` 执行。Runtime 将工具异常转换为错误结果，并在返回模型、
发送 `ToolCompleted` 事件和写入 Session 前，将结果限制在 50 KiB UTF-8 字节以内，
截断时保留开头、末尾及明确标记。截断不会改变工具实际执行的成功或失败状态。

`read(path, offset=1, limit=200)` 按行读取并附行号，`offset` 从 1 开始，`limit`
最大 500。每次输出至多 40 KiB；单行只展示前 2 KiB，标注不完整。有后续行时返回
`next_offset` 和 `limit`，可用相同路径继续读取，不统计剩余总行数。文件不存在、目录、
二进制内容和无效 UTF-8 分别返回可区分的工具错误。

`glob(pattern, path=".", limit=100)` 查找文件；`grep(pattern, path=".", glob=None,
limit=100)` 使用区分大小写的逐行正则搜索并返回 `路径:行号:预览`。两者最多返回
200 条和 40 KiB，搜索 15 秒超时。搜索遵循 ripgrep 默认的忽略、隐藏文件、二进制文件
及符号链接规则；显式 glob 可能覆盖部分忽略规则。超限时需缩小路径或模式。

`bash` 持续排空 stdout 和 stderr，各保留最多 20 KiB 的头尾片段，等待真实退出码或
60 秒超时。`workdir` 只对本次调用生效，不会改变后续命令的默认目录。
`bash` 与检索工具使用同一进程管理层。Unix 使用独立进程组，取消时先发送终止信号，
1 秒后强制终止仍存活的进程；Windows 使用 Job Object 回收命令及子进程，命令在
成功加入 Job 后才获得启动许可。工具不能保证管理主动脱离进程组的 Unix 子进程。
`edit` 仅替换唯一精确匹配；找不到文本时会提示重新用 `read` 或 `grep` 定位，
多处匹配时列出最多 5 个行列位置和短上下文供修正。相同的新旧文本会报错，
上述失败都不会写文件。`write` 与成功的 `edit` 仍返回简短摘要。
