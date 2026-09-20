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