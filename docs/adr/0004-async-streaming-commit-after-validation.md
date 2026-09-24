# 0004. 异步流式事件与"校验后提交"

- 状态：Accepted
- 日期：2026-09-23
- 来源：`873ae9d` 初步完成流式事件的抽象以及处理

## 背景

最初的 Provider 和 AgentLoop 都是同步的一次性调用：拿到完整响应才返回结果。这带来两个问题：用户在长回答期间看不到任何反馈；CLI 无法显示工具正在执行。为了实时反馈，需要流式。

但流式与 [0003](0003-strict-finish-reason.md) 的严格校验存在张力：如果增量一到达就写进历史，那么一旦最终响应被拒绝，历史已经被污染，零副作用的保证就失效了。

此外，如果调用方需要自己拼装 Provider 和 Loop 的返回值才能知道发生了什么，新增阶段（压缩、取消）就会不断改变调用方代码。

## 决策

**统一走异步流式路径，且提交严格晚于校验。**

- Provider 与 AgentLoop 都基于异步迭代器。`LLMProvider.complete()`（`src/codehead/llm/base.py`）和 `AgentLoop.run()`（`src/codehead/agent/loop.py`）是便捷方法，内部消费同一条流，不保留同步兼容层。
- 事件类型统一从 `codehead.models` 导入，按 `type` 判别。Provider 侧只有两个事件：`LLMTextDelta`（`src/codehead/llm/openai.py`）和 `LLMResponseCompleted`。Loop 侧在 `src/codehead/models/event.py` 定义回合、模型、工具、压缩各阶段的事件，并在发出前为每个事件注入 `turn_id`。
- **文本增量只用于展示**：`LLMTextDelta` 被单向转成 `AssistantTextDelta` 透传给调用方，**从不写入 Session**。只有通过校验的完整 `AssistantMessage` 才落库。
- **提交时机**：先校验完整响应，再保存 assistant 消息并发出 `ModelCompleted`，然后才执行工具；工具结果保存后才发出 `ToolCompleted`。因此「已经发出 `ToolStarted`」意味着这次调用一定已经被记录。
- `step` 从 1 开始，表示本回合第几次正常模型调用；上下文摘要请求不占用 `max_steps`，其增量不作为助手答案展示。

## 备选方案与取舍

| 备选 | 为什么不采用 |
| --- | --- |
| 保持同步接口，另加回调做实时展示 | 两套路径需要各自维护校验与提交时机，容易漂移；且回调难以表达取消 |
| 增量到达即累积进 Session | 与 [0003](0003-strict-finish-reason.md) 的零副作用保证直接冲突 |
| 由调用方自行拼装 Provider 与 Loop 的返回值 | 每新增一个阶段都要改调用方；事件流把这些阶段变成类型化数据 |
| 通过轮询共享状态获取进度 | 无法表达「工具执行中」这类瞬时状态，且引入同步原语 |

## 后果

- 调用方必须按流的方式使用：直接消费 `AgentLoop.stream()` 时应使用 `contextlib.aclosing` 保证提前退出也能关闭流；回合结束后调用方须在 `finally` 中 `await loop.aclose()`。
- 事件**不持久化**，不支持重放。崩溃后无法从事件恢复进度，只能依赖存档（见 [0008](0008-append-only-archive.md)）。
- CLI 需要主动避免重复显示：不重复打印完成事件中的文本，也不默认展开工具输出。

**重新评估的触发条件**：若需要支持多个并发回合（当前一个 Session 同时只允许一个回合），事件的 `turn_id` 注入机制和调用方的消费方式都需要重新设计。

## 取代关系

本文取代了 [0013 同步、非流式的 Provider 调用路径](0013-sync-provider-path.md)。该决定在最小链路阶段是刻意的第一步，因缺少实时反馈与可中断的等待点而被推翻；取代时**没有保留同步兼容层**。

## 代码与测试

- `src/codehead/models/event.py` — 事件类型定义与 `type` 判别联合
- `src/codehead/llm/base.py`（`LLMProvider.complete`）、`src/codehead/llm/openai.py` — Provider 侧流与增量累积
- `src/codehead/agent/loop.py`（`AgentLoop.stream` / `run` / `aclose`）— 回合事件序列与提交顺序
- `src/codehead/cli.py` — 事件消费与展示
- `tests/test_events.py` — 事件判别、非法 step / 空 delta 拒绝
- `tests/test_agent_loop.py` — 事件顺序与提交边界、提前关闭不提交增量
- `tests/test_openai_provider.py` — 先出文本增量后出完成事件
