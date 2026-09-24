# 0011. Turn / Tool 生命周期与原子提交边界

- 状态：Accepted
- 日期：2026-09-23
- 来源：`1e05fd2` Turn / Tool 生命周期与可控中断收尾

## 背景

引入存档（见 [0008](0008-append-only-archive.md)）之后，一个进程可以随时死在任意位置：模型调用之后但消息落库之前、工具已启动但结果未记录、部分工具完成而其余未开始。恢复（见 [0010](0010-resume-contract.md)）需要能判断一份历史到底处在什么状态。

只靠「消息列表」无法表达这些状态——它看不出一个工具调用是「还没开始」还是「开始了但结果丢了」。因此需要显式的生命周期记录。

## 决策

**为回合和每次工具调用建立显式状态，并让每个状态变更对应一次独立的数据库事务。**

### 状态取值

运行时状态是**裸字符串**（`src/codehead/session/session.py` 中的普通 `@dataclass`，没有枚举类型），数据库侧有对应的 CHECK 约束：

| 对象 | 取值 | CHECK 位置 |
| --- | --- | --- |
| `Session` | `legacy` / `active` / `closed` | `sessions` 表（见 [0010](0010-resume-contract.md)） |
| `Turn` | `running` / `cancelling` / `completed` / `failed` / `cancelled` | `turns` 表 |
| `ToolExecution` | `pending` / `running`，终态取 `outcome` 的四值 | `tool_executions` 表 |

工具终态复用 `ToolResult.outcome`：`succeeded` / `failed` / `cancelled` / `skipped`（见 [0005](0005-tool-runtime-and-output-limits.md)）。

### 原子提交边界是「每个方法一个事务」，不是「每个回合一个事务」

`add`、`compact`、`begin_turn`、`accept_response`、`complete_tool` 各自是一个 SQLite 事务。回合级别的一致性**不靠一个大事务**，而靠三道写入前置断言：

- `_require_turn` — 目标回合必须处于允许的状态。
- `_require_tools_settled` — 回合内不得有 `pending` / `running` 的工具。
- `_assert_settled` — 整个会话不得有未收尾的回合或工具。

这样做的理由是：一个回合可能跨越多次模型调用和多次工具执行，把它们放在一个事务里会让事务长时间持有写锁，且崩溃时整个回合的进展全部丢失。分成小事务后，崩溃只会丢掉最后一步。

### 幂等收尾由两套独立机制保证

同一个工具结果可能被重复提交（例如取消与正常完成竞争）。防护在两处**分别**实现，互不依赖：

- 内存侧：`Session.complete_tool` 在 `tool.result is not None` 时直接返回。
- 数据库侧：`UPDATE ... WHERE status IN ('pending','running')` 要求 `rowcount == 1`，否则抛出 `ResumeError`。

回合终态同理：`Session.end_turn` 在已处于终态时直接返回，数据库侧由 `_require_turn` 把关。

### 一个会话同时只有一个活跃回合

由部分唯一索引 `one_live_turn`（`turns(session_id) WHERE status IN ('running','cancelling')`）在数据库层强制，而不是仅靠应用层判断。

### 一次回合的提交顺序

1. `begin_turn` — 同一事务写入回合记录与首批消息。
2. `accept_response` — 校验后的完整 assistant 消息落库；若无工具调用，回合立即转 `completed`。
3. `start_tool` — 工具标记为 `running`。
4. `complete_tool` — 工具结果作为消息落库，同时写入执行终态。
5. `end_turn` — 回合进入终态。`cancelling` 是唯一的中间态，其 `finished_at` 置 `NULL`。

## 备选方案与取舍

| 备选 | 为什么不采用 |
| --- | --- |
| 整个回合用一个事务 | 长事务持有写锁；崩溃会丢掉整个回合的全部进展 |
| 用 Python 枚举表达状态 | 数据库是状态的持久化真源，约束必须在数据库侧也成立；当前用字符串字面量 + CHECK 保持两侧一致，代价是缺少类型检查 |
| 只靠内存中的状态判断幂等 | 进程可能在提交后、更新内存前崩溃；数据库侧必须独立防护 |
| 只靠数据库 `rowcount` 判断幂等 | 正常路径下会引入不必要的异常开销；内存判断是常见路径的快路径 |
| 用应用层锁限制单活跃回合 | 无法跨进程生效，而恢复（见 [0010](0010-resume-contract.md)）本身就是跨进程场景 |

## 后果

- 状态取值在运行时（裸字符串）和持久层（CHECK 约束）**分别定义**，新增取值需要同时改两处；当前没有类型层面的强制。
- 消息与状态是**两次写入**（消息进 `messages`，终态进 `tool_executions`），但它们在同一个事务内，因此不存在只写一半的中间状态。
- 旧存档保持原样，新回合才产生 Turn / Tool 状态记录——因此 `legacy` 会话没有这些信息，无法判断是否收尾，也就无法恢复。

**重新评估的触发条件**：若需要表达更复杂的回合内阶段（例如模型重试、工具并行），当前「一个回合一条记录 + 每次工具一条记录」的模型需要扩展；那应新增一篇 ADR。

## 代码与测试

- `src/codehead/session/session.py`（`Turn`、`ToolExecution`、`begin_turn`、`accept_response`、`start_tool`、`complete_tool`、`end_turn`）— 状态迁移与幂等
- `src/codehead/session/store.py`（`one_live_turn`、`_require_turn`、`_require_tools_settled`、`_assert_settled`、各方法事务）— 持久化状态与前置断言
- `src/codehead/models/tool.py`（`ToolResult.execution_outcome`）— `outcome` 取值与 `is_error` 一致性
- `tests/test_turn_lifecycle.py` — 原子性、关闭后全部 `skipped`、存储失败保持回合未收尾、并发回合守卫、工具结果提交失败不发完成事件
- `tests/test_session_store.py` — 事务失败时内存与存档双向保留
