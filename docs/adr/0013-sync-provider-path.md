# 0013. 同步、非流式的 Provider 调用路径

- 状态：Superseded by [0004](0004-async-streaming-commit-after-validation.md)
- 日期：2026-09-22
- 来源：`afa3983` 初步实现四个基础工具，OpenAI Chat Completions适配器；`d300282` 完成内部数据模型初步搭建
- 被取代于：2026-09-23（`873ae9d`）

## 背景

建立最小可运行链路的阶段，目标是先跑通「CLI → Session → AgentLoop → Provider → ToolRegistry → ToolResult → 下一轮模型请求」这条完整路径。实时反馈不是当时的约束，而流式会同时引入异步、事件类型和取消三个新问题。

## 决策（当时）

- `LLMProvider.complete(request) -> LLMResponse` 是**唯一**抽象方法，同步返回完整响应。
- 回合内同步推进：拿到完整响应、校验，然后决定交付回答还是执行工具。
- 不引入流式事件。

`afa3983` 的更早形态是同步 `complete(messages, tools) -> Message`；`d300282` 在统一领域模型时把它改为 `complete(request) -> LLMResponse`。那是**模型形态的统一**（见 [0002](0002-unified-domain-models.md)），不是本决策本身的变更。

## 备选方案与取舍（当时）

| 备选 | 为什么不采用 |
| --- | --- |
| 一开始就实现流式 | 会同时引入异步、事件类型与取消三个问题，无法先验证最小链路是否正确 |

## 后果（当时）

- CLI 在长回答期间没有任何输出，工具执行状态不可见。
- 同步调用没有可中断的等待点，取消无从下手。

## 取代关系

**2026-09-23，`873ae9d` 引入 [0004](0004-async-streaming-commit-after-validation.md)。**

`stream(request) -> AsyncIterator[LLMStreamEvent]` 取代 `complete()` 成为唯一抽象方法；`complete(request)` 保留为**异步便捷方法**，内部消费同一条流。

取代原因：

- 长回答期间用户看不到任何反馈，工具执行状态也不可见。
- 取消需要在等待过程中打断，同步调用没有可中断的等待点。

哪些行为改变了：

- 调用方必须 `await`，并负责 `aclosing` / `aclose()` 生命周期。
- 回合的提交时机从「响应返回后」细化为「校验通过后」（见 [0004](0004-async-streaming-commit-after-validation.md)）。

哪些保持不变：

- Provider 仍然只暴露内部领域模型，不泄露任何协议类型。
- 结束原因的校验规则与零副作用保证完全不变（见 [0003](0003-strict-finish-reason.md)）。

**没有保留同步兼容层**——与 [0002](0002-unified-domain-models.md) 的「不保留兼容层」原则一致。

## 代码与测试

- `git show afa3983:src/codehead/llm/base.py`、`git show d300282:src/codehead/llm/base.py` — 当时的同步接口（本 ADR 的事实来源）
- `src/codehead/llm/base.py` — 当前接口
- `tests/test_openai_provider.py`、`tests/test_agent_loop.py` — 取代后的行为覆盖
