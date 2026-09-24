# 0008. append-only SQLite 存档与活跃上下文分离

- 状态：Accepted
- 日期：2026-09-23
- 来源：`7faa835` 上下文自动压缩与 SQLite 历史存档

## 背景

在引入压缩之前，`Session` 就是一个内存里的消息列表，进程退出即消失。压缩（见 [0009](0009-estimated-context-compaction.md)）要求丢弃旧消息，但如果丢掉的是唯一副本，历史就永久不可恢复；后续的会话续聊（见 [0010](0010-resume-contract.md)）也要求历史能跨进程存活。

## 决策

**把「模型这次请求看到什么」和「磁盘上保存了什么」彻底分开。**

- **`Session.messages` 是派生视图，不是存储**（`src/codehead/session/session.py`）：它由原始记录加当前摘要实时渲染（`context_messages`），摘要被渲染成一条参考资料性质的 `UserMessage`。**全量原文只存在于数据库的 `messages` 表**。
- **存档是 append-only SQLite**（`src/codehead/session/store.py`），默认位于 workspace 下的 `.codehead/sessions.sqlite3`，可用 `CODEHEAD_SESSION_DB` 覆盖。五张表：
  - `sessions` — 会话元数据与退出状态
  - `messages` — 按 `(session_id, sequence)` 追加的原始消息
  - `checkpoints` — 压缩检查点（摘要 + 保留消息序号）
  - `turns` — 回合记录
  - `tool_executions` — 工具执行记录
- **先提交数据库，再更新内存**：`add`、`compact`、`begin_turn`、`accept_response`、`complete_tool` 每一处都遵循这个顺序。数据库提交成功后才允许改变内存状态或发出完成事件。
- **存档保存的是经过治理的结果**：即已通过响应校验的完整消息（见 [0003](0003-strict-finish-reason.md)），以及经过输出限制处理的工具结果（见 [0005](0005-tool-runtime-and-output-limits.md)）。流式片段和被截断掉的内容不保留。
- **数据库故障是会话级故障**：`_transaction` 在出错时把 store 标记为失败，后续操作抛出 `StorageError`，会话不可继续、也不写入正常退出标记。
- **对象生命周期**：无参数 `Session()` 使用临时的内存 SQLite，供嵌入调用与测试使用，不落盘；CLI 始终使用磁盘数据库并负责资源清理。

## 备选方案与取舍

| 备选 | 为什么不采用 |
| --- | --- |
| 把压缩后的活跃上下文直接当存档 | 压缩是有损的，原文会永久丢失，历史不可追溯也不可能恢复 |
| 把摘要和保留消息直接存进 `Session` 对象并整体序列化 | 每次压缩都要重写整个会话，且无法只加载检查点之后的增量 |
| 用 JSON 文件而非 SQLite | 需要自己实现追加、并发占用、部分写入的回滚；SQLite 事务直接给出这些保证 |
| 把原文放在内存 + 定期落盘 | 崩溃会丢失最近的完整消息，且内存占用随会话增长 |
| 保存流式片段以便完整重放 | 片段本身不是可信内容（见 [0004](0004-async-streaming-commit-after-validation.md)），保留只会放大存档体积 |

## 后果

- 数据库**不会自动清理**，长期使用会持续增长。
- 磁盘写入在关键路径上：每次追加消息都包含一次事务提交。当前规模下不构成瓶颈。
- 存档格式成为需要长期兼容的契约——升级旧库时只能**追加列**（`_migrate`），原始消息与检查点保持不变。
- 压缩后不重新加载原文，因此恢复时只加载最新检查点、保留消息及检查点之后的新增消息。

**重新评估的触发条件**：若会话规模增长到单库成为瓶颈，需要按会话分片或引入清理策略；那会改变存档的生命周期语义，应新增一篇 ADR 并取代本篇。

## 代码与测试

- `src/codehead/session/store.py`（`SQLiteSessionStore`、`_transaction`、`_migrate`）— schema、事务边界、迁移
- `src/codehead/session/session.py`（`Session.messages`、`context_messages`）— 活跃上下文作为派生视图
- `src/codehead/session/history.py`（`validate_history`）— 载入前的结构校验
- `tests/test_session_store.py` — 原文往返与隔离、事务失败时内存与存档双向保留、打开失败为领域错误
- `tests/test_context.py` — 检查点提交失败不发成功事件、取消时保留旧检查点
