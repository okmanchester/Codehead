# 0005. 工具统一经 `ToolRuntime` 执行与分层输出上限

- 状态：Superseded by [0015](0015-tool-system-v1-freeze.md)
- 日期：2026-09-23
- 来源：`0109042` 完成工具输出治理

## 背景

工具执行一开始散落在 AgentLoop 里：调用工具、捕获异常、格式化结果各写一遍。命令类工具的输出可以轻易达到数十 MB（例如误跑一次全仓库检索），这些内容会直接进入模型请求，既可能超出上下文预算，也让会话存档迅速膨胀。

同时，工具的失败有两种性质完全不同的情况：工具自己可预期的业务失败（文件不存在、匹配不唯一），和进程级的严重故障（无法确认子进程已停止）。前者应该回传给模型让它自己修正，后者继续执行只会掩盖问题。

## 决策

**所有工具调用统一经过 `ToolRuntime`（`src/codehead/tools/runtime.py`），它同时是执行、错误分类和输出尺寸的唯一边界。**

- **错误分类**：
  - `ToolCleanupError` **刻意不转换成结果**，向上抛出——无法确认进程停止是会话级故障，不能让模型以为只是一次普通工具失败。
  - `ToolCancelled` / `ToolSkipped` → `outcome` 为 `cancelled` / `skipped`。
  - `ToolExecutionError` → `failed`。
  - 其他 `Exception` → `failed`，并加上 `Tool '<name>' failed:` 前缀以便区分未预期异常。
  - 工具返回非 `str` → `ToolExecutionError`。
- **`outcome` 是唯一真源**：Runtime 只构造 `outcome=`，`is_error` 由 `ToolResult` 的校验器从 `outcome` 反推（`src/codehead/models/tool.py`）。两者同时给出且矛盾时构造失败。
- **分层输出上限**，越靠近数据源越严格：

  | 层 | 上限 | 位置 |
  | --- | --- | --- |
  | `bash` 单路 stdout / stderr | 各 20 KiB | `src/codehead/tools/bash.py`（`BASH_STREAM_MAX_BYTES`） |
  | `read` | 40 KiB，单行 2 KiB | `src/codehead/tools/read.py` |
  | `glob` / `grep` | 40 KiB、200 条、15 秒超时 | `src/codehead/tools/search.py` |
  | **最终出口** | **50 KiB** | `src/codehead/tools/runtime.py`（`max_output_bytes`） |

- **截断策略**：保留开头与末尾各一半，中间插入明确的截断标记（`src/codehead/tools/output.py`，`TRUNCATION_MARKER`）。保留尾部是因为命令的错误信息通常出现在最后。截断**不改变**工具实际的成功/失败状态。
- 上限以 **UTF-8 字节**计，不是字符数。
- 同步工具通过 `asyncio.to_thread` 在工作线程中执行，Runtime 返回一个被拥有的 `asyncio.Task`，调用方必须 shield 并 settle 它。

## 备选方案与取舍

| 备选 | 为什么不采用 |
| --- | --- |
| 只保留开头截断 | 会丢掉最关键的失败原因，模型拿到的信息反而误导 |
| 按行数而非字节数限制 | 单行可以极长（压缩产物、minified 代码），行数不能约束真实体积 |
| 让每个工具自己决定输出上限 | 没有兜底——新工具忘记限制会直接污染上下文；分层是「各自优化 + 统一兜底」 |
| 把 `ToolCleanupError` 也变成 `is_error` 结果 | 会让「进程没被确认停止」表现成一次普通失败，模型可能继续执行，而会话已不可信 |
| 用字符数计上限 | 与上下文估算（按字节）不一致，中文/代码混排时偏差很大 |

## 后果

- 被截断的内容**不进入存档**：存档保存的是已经过输出限制的结果，原文不会保留（见 [0008](0008-append-only-archive.md)）。需要完整输出时应缩小查询范围重跑。
- 模型可能因为看不到中间部分而做出错误判断；截断标记的作用是让模型意识到这一点。
- 50 KiB 是硬兜底，正常路径由各工具的分层上限先触发。

**重新评估的触发条件**：若引入需要完整输出的工具（例如返回结构化数据而非文本的工具），应重新考虑「结果只可能是字符串」这一前提，而不是继续放宽字节上限。

## 取代关系

本文的「`outcome` 是唯一真源」取代了 `7faa835` 引入的早期做法：当时 Runtime 在异常分支显式构造 `ToolResult(..., is_error=is_error)`，由布尔值表达失败。

**取代原因**：`is_error` 只能区分「成功」与「失败」，无法表达「被取消」和「未执行」——而这两者在中止语义（见 [0012](0012-cancellation-and-process-trees.md)）中必须与真正的执行失败区分开。继续保留 `is_error` 作为独立输入会让两个事实源发生漂移。

**哪些行为改变了**：Runtime 不再构造 `is_error`，只传 `outcome`；`is_error` 降级为派生字段。

**哪些保持不变**：异常的四个分类分支（`ToolCleanupError` 上抛、取消/跳过、已知工具错误、未预期异常）与 50 KiB 全局上限都未改变。

## 代码与测试

- `src/codehead/tools/runtime.py`（`ToolRuntime.execute` / `start` / `execute_async`）— 执行、错误分类、最终输出上限
- `src/codehead/tools/output.py`（`truncate_text`、`TRUNCATION_MARKER`）— 头尾保留的截断
- `src/codehead/models/tool.py`（`ToolResult.execution_outcome`）— `outcome` 与 `is_error` 的一致性约束
- `src/codehead/tools/bash.py`、`read.py`、`search.py` — 各层预算
- `tests/test_tool_runtime.py` — UTF-8 字节与头尾保留、已知/未知异常均成错误结果、进程级中断不被吞
- `tests/test_tools.py` — `bash` 大输出与 `read` 40 KiB 上限
