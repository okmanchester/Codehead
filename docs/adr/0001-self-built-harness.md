# 0001. 自实现 Harness，不引入 Agent Framework

- 状态：Accepted
- 日期：2026-09-21
- 来源：`e2c3dee` 最小骨架v0

## 背景

Codehead 的目标不是交付一个能用的 Coding Agent，而是理解现代 Coding Agent Harness 背后的核心工程机制：为什么需要这些机制、它们解决什么工程问题、不同设计之间有什么取舍。

如果核心执行链路由现成框架提供，上述问题就被框架的实现细节遮蔽了——可以调用出来，但无法解释为什么是这样。

## 决策

核心执行链路全部自实现，不依赖任何 Agent Framework：

- 回合驱动：`AgentLoop`（`src/codehead/agent/loop.py`）
- Provider 抽象与适配：`LLMProvider`（`src/codehead/llm/base.py`）、`OpenAIProvider`（`src/codehead/llm/openai.py`）
- 工具注册与执行：`ToolRegistry`（`src/codehead/tools/registry.py`）、`ToolRuntime`（`src/codehead/tools/runtime.py`）
- 会话与存档：`Session`（`src/codehead/session/session.py`）

Python 运行时依赖限定为 4 个通用库（`pyproject.toml:10-15`）：`openai`（仅作为 HTTP/SSE 协议客户端）、`pydantic`、`python-dotenv`、`rich`。其中没有一个是 Agent 框架。

「框架」的边界按职责划分：`openai` SDK 只是协议适配层，不承担回合调度、工具分派、上下文管理或持久化；这些全部在 `src/codehead/` 内实现。

## 备选方案与取舍

| 备选 | 为什么不采用 |
| --- | --- |
| 基于 LangChain / LlamaIndex 等框架构建 | 框架会隐藏关键执行逻辑，与「理解 Harness」的项目目标直接冲突 |
| 基于厂商官方 Agent SDK | 同上，且会把设计空间绑定到单一厂商的抽象 |
| 引入通用工作流/状态机库来编排回合 | 当前回合编排复杂度不足以证明新依赖的必要性 |

## 后果

- 代价：进程管理、异步并发、事务边界、跨平台差异这些底层问题必须自己解决并自己承担正确性（见 [0011](0011-turn-tool-lifecycle.md)、[0012](0012-cancellation-and-process-trees.md)）。
- 代价：没有框架生态提供的现成集成，每接一个新能力都要自研。
- 收益：关键执行逻辑保持可读、可调试、可解释，这是本项目的核心产出。

**重新评估的触发条件**：当某个机制的自研成本已经明显超过它的学习价值，且引入现成实现不会遮蔽需要理解的部分时，可以按机制单独引入——但应为此写一篇新 ADR 说明边界，而不是整体转向框架。

## 代码与测试

- `pyproject.toml:10-15` — 依赖清单，是这条决策最直接的证据
- `src/codehead/agent/loop.py`、`src/codehead/llm/base.py`、`src/codehead/tools/runtime.py` — 自实现的核心链路
- `src/codehead/cli.py`（`build_agent_loop`）— 依赖装配点，可看出组件全部是本地实现
