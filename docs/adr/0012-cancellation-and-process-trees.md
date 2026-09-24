# 0012. 可控中断语义与跨平台进程树回收

- 状态：Accepted
- 日期：2026-09-23
- 来源：`1e05fd2` Turn / Tool 生命周期与可控中断收尾

## 背景

用户在等待模型回答或命令执行时按 Ctrl+C，期望的是「停下来」，而不是「进程消失」。这两者差别很大：一次正在进行的文件写入无法安全中止，一条正在运行的命令可能已经产生了部分副作用。

如果直接终止进程，留下的是一份无法判断状态的存档——工具调用的记录停在 `running`，而实际发生了什么无人知晓。这与 [0010](0010-resume-contract.md) 的可恢复性要求直接冲突。

## 决策

**取消是协作式的，且收尾优先于终止。**

### 语义

- 第一次 Ctrl+C 请求取消当前回合；再次按下只显示收尾状态。
- 取消**停止后续的模型和工具调度**，不回滚已经发生的副作用。
- 按工具类型区分处理：

  | 工具类型 | 取消行为 |
  | --- | --- |
  | 文件工具（`read` / `write` / `edit`） | **一旦启动就等待真实结果**并保存。启动前已取消则抛 `ToolSkipped` |
  | 命令与检索（`bash` / `glob` / `grep`） | **停止受管理的进程树**，抛 `ToolCancelled` |

  这条区分由 `Tool.execute_cancellable` 的默认实现（`src/codehead/tools/base.py`）表达：它只在启动前检查标志，启动后直接等待。`bash` / `glob` / `grep` 覆盖该方法。UI 层用同一个白名单 `{"bash", "glob", "grep"}` 生成不同的等待提示（`src/codehead/agent/loop.py`），因为两类的等待时长直观感受完全不同。

- 尚未启动的调用记录为 `skipped`。共有 4 个记录点：工具基类的启动前检查、回合收尾时为未启动调用补记、进程层启动前检查、以及 Windows 绑定 Job 后但未获启动许可时。
- 收尾期间输入提示不会提前出现，收尾完成后才回到提示符，可开始新回合或正常退出。

### 收尾保证

- `TurnControl`（`src/codehead/agent/cancellation.py`）持有当前回合拥有的全部异步资源：拥有者任务、正在等待的迭代器、传给同步工具的取消事件、工具工作线程、当前执行记录、收尾任务。
- `settle` 循环 `asyncio.shield` 并吞掉外层 `CancelledError`，直到被拥有的任务**真实完成**为止。这是「文件工具等待真实结果」的实现基础：外部反复取消不会打断已经启动的工作。
- 即使存档已经失败，**也不放弃正在执行的线程**（`src/codehead/agent/loop.py` 中有明确注释）。孤立的线程比一次失败的存档更危险。

### 失败如何上报

- `StorageError` 与 `ToolCleanupError` **不转换为 `TurnFailed`**，而是继续向上抛出。
- CLI 据此报告收尾失败，并**拒绝宣称会话可恢复**。
- 数据库故障或无法确认工具停止时，会话保留为不可正常恢复的状态（见 [0010](0010-resume-contract.md)）。

### 跨平台进程回收

三层结构，职责分离：

- `src/codehead/tools/process.py`（`ManagedProcess`）— 共享生命周期门面：平台分支、看门狗线程、停止、等待、关闭。
- `src/codehead/tools/_windows_job.py`（`WindowsJob`）— **只定义 Job Object 机制**：`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`、`AssignProcessToJobObject`、`TerminateJobObject` 加轮询计数确认，5 秒未确认则升级为 `ToolCleanupError`。
- `src/codehead/tools/_process_launcher.py` — **Windows 专用的子进程侧启动门**：子进程启动后阻塞读取 stdin 的一个字节，只有收到父进程写入的 `b"G"` 才真正执行命令；启动失败以 127 退出。

**Windows 的启动许可链路**（两端协作）：

1. 父进程把命令包装成 `[python, _process_launcher.py, *command]` 并加 `CREATE_NO_WINDOW`。
2. `Popen` 之后立即把子进程加入 Job Object。此时子进程已在 Job 内，但因停在启动门上而**尚未执行任何命令**。
3. 绑定成功后**再检查一次**取消标志。若已取消则抛 `ToolSkipped`，子进程被 Job 回收，命令永不执行。
4. 通过检查后才写入 `b"G"` 释放启动门。

这个顺序保证了「取消发生在命令启动之前」是可靠的——不存在「已经跑了一半才发现该取消」的窗口。

**Unix 的终止序列**：进程组由 `start_new_session=True` 建立；取消时先 `SIGTERM`，轮询 1 秒探测存活，仍存活则 `SIGKILL`，然后 `wait(timeout=5)`，超时升级为 `ToolCleanupError`。

## 备选方案与取舍

| 备选 | 为什么不采用 |
| --- | --- |
| 所有工具在取消时立即强行停止 | 文件写入会在中途被打断且无法知道写到哪里，留下损坏文件 |
| 取消时不等待、直接丢弃结果 | 会产生「已执行但结果未知」的记录，破坏存档可恢复性这一前提 |
| 让同步文件工具支持中断 | 需要把每个文件操作改成可分块重入的异步流程，收益远小于复杂度 |
| Unix 也套一层启动门 | Unix 已有进程组信号机制覆盖启动后取消，为极窄的启动窗口再引入一个包装进程不划算 |
| 用 `asyncio.Task.cancel()` 直接取消工作线程 | 线程无法被安全中断，强行取消只会丢掉结果而线程仍在跑 |
| 取消后自动重试回合 | 取消是用户的明确意图，自动重试会与之冲突 |

## 后果

- **跨平台保证不完全一致**：Unix 路径不使用启动门，因此存在「`Popen` 完成到看门狗线程启动之间」的极窄窗口。Windows 通过 Job Object 把这个窗口消除，Unix 没有。这是已知缺口。
- 工具**不能保证**回收主动脱离进程组的 Unix 子进程（例如自行 `setsid` 的命令）。
- 取消可能留下部分副作用（半写的文件、部分执行的命令），这是被接受的代价。
- `skipped` 的 4 个记录点分散在不同层，新增工具执行路径时容易遗漏其中之一。
- 「即使存档失败也不放弃执行中的线程」这条规则反直觉，容易在重构时被当作冗余代码删除。

**重新评估的触发条件**：若需要在 Unix 上获得与 Windows 等价的「取消必定先于命令启动」保证，应补齐启动门；那时新增一篇 ADR 并说明代价。

## 代码与测试

- `src/codehead/agent/cancellation.py`（`settle`、`TurnControl`）— 收尾保证
- `src/codehead/agent/loop.py`（`request_cancel`、`_close_turn`、`_cleanup`、`cancellation_status`）— 取消编排与失败上报
- `src/codehead/tools/base.py`（`Tool.execute_cancellable`）— 文件工具等待真实结果
- `src/codehead/tools/process.py`、`_windows_job.py`、`_process_launcher.py` — 进程回收与启动许可
- `src/codehead/tools/bash.py`、`search.py` — 停止进程树的工具
- `tests/test_turn_lifecycle.py` — 取消时等待真实文件结果、取消第二个工具保留第一个结果、关闭后全部 `skipped`、迟到取消不覆盖最终答案
- `tests/test_process_lifecycle.py` — 启动前取消零执行、Job 绑定失败不得执行命令、Windows 回收子/孙进程、Unix 进程组回收、取消保留有界部分输出
