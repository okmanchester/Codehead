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
`ModelStarted`、`AssistantTextDelta`、`ModelCompleted`、`ToolStarted`、
`ToolCompleted` 和最终的 `TurnCompleted`，以及压缩期间的
`ContextCompactionStarted` / `ContextCompacted`。`step` 从 1 开始，表示本回合第几次
正常模型调用；摘要请求不占用 `max_steps`，摘要增量不作为助手答案展示。

文本增量仅用于即时展示。Loop 校验完整响应的 FinishReason 后才保存 assistant
消息、发送 `ModelCompleted` 和执行工具；工具结果保存后才发送 `ToolCompleted`。
CLI 不重复打印完成事件中的文本，也不默认展开工具输出。模型失败和步数耗尽仍通过
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

一个 Session 同时仅支持一个回合。同步工具在线程中按顺序执行；中断不能强制停止
已经启动的工具，也不能撤销其副作用，退出时可能需要等待该工具结束。首版中断后
退出 CLI，不支持恢复中断的 Session。事件未持久化，不支持重放。

## 上下文与会话存档

CLI 将原始消息保存到 workspace 下的 `.codehead/sessions.sqlite3`，每次启动创建
新的会话。`CODEHEAD_SESSION_DB` 可覆盖数据库位置；相对路径以 workspace 为基准。
数据库保存会话元数据、按序号追加的消息，以及摘要和保留消息序号组成的压缩检查点。
这里的原始消息指已通过响应校验的完整消息，以及经过现有输出限制处理的工具结果，
不包含流式片段或被工具输出治理截掉的内容。当前已支持存档，尚不支持恢复、续聊或
历史检索；数据库不会自动清理。

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
`edit` 仅替换唯一精确匹配；找不到文本时会提示重新用 `read` 或 `grep` 定位，
多处匹配时列出最多 5 个行列位置和短上下文供修正。相同的新旧文本会报错，
上述失败都不会写文件。`write` 与成功的 `edit` 仍返回简短摘要。
