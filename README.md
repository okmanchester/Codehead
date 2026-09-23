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
启动时若找不到 `rg` 会明确报错。`bash` 工具
以 workspace 为工作目录，但不提供进程沙箱；Windows 使用 PowerShell，Unix
使用 Bash。

## 流式事件

Provider 和 AgentLoop 使用异步流，事件类型统一从 `codehead.models` 导入。
Provider 输出 `LLMTextDelta`，最后输出一次 `LLMResponseCompleted`；Loop 输出
`ModelStarted`、`AssistantTextDelta`、`ModelCompleted`、`ToolStarted`、
`ToolCompleted` 和最终的 `TurnCompleted`。`step` 从 1 开始，表示本回合第几次模型调用。

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
60 秒超时。`write` 与 `edit` 仍返回简短摘要。
