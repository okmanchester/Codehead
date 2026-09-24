# Project Handoff

- 更新于：`1e05fd2`（Turn / Tool 生命周期与可控中断收尾）
- 上一版基线：工作区未提交版本记录的 HEAD 同为 `1e05fd2`，但其「已完成」区只保留了 `7faa835` 之后的条目，本版已按累积里程碑还原；再上一版提交版本（`7faa835`）记录的 HEAD 为 `ce3f600`，落后 3 个提交
- 分支：`main`

## 目标
- 增量实现可理解的 Python Coding Agent Harness，当前重点是会话上下文、存档、续聊及 Turn / Tool 生命周期一致性。
- Turn / Tool 可控中断收尾已完成并提交；尚无新的用户授权开发任务。

## 关键背景与决策
- 架构决策已迁出交接文档：新建 `docs/adr/`（13 篇，含 1 篇 Superseded + 索引 + 模板），`AGENTS.md` 的「文档」一节规定了何时必须新增 ADR、以及旧 ADR 只标取代不删改。handoff 此后只引用编号，不复述理由。
- 尚未写入 ADR 的约束：`docs/adr/README.md` 的规则正文优先于 `.agents/skills/adr/SKILL.md`，两者冲突时改后者。
- ADR：
  - ADR-0001 自实现 Harness，不引入 Agent Framework
  - ADR-0002 统一领域模型层 `codehead.models`
  - ADR-0003 FinishReason 严格语义与零副作用拒绝
  - ADR-0004 异步流式事件与“校验后提交”
  - ADR-0005 工具统一经 `ToolRuntime` 执行与分层输出上限
  - ADR-0006 workspace 文件边界、无沙箱与环境清理
  - ADR-0007 工具行为取舍：read 分页、edit 唯一精确匹配、检索依赖 ripgrep
  - ADR-0008 append-only SQLite 存档与活跃上下文分离
  - ADR-0009 估算驱动的阈值自动压缩
  - ADR-0010 恢复契约：只有正常退出的会话可恢复
  - ADR-0011 Turn / Tool 生命周期与原子提交边界
  - ADR-0012 可控中断语义与跨平台进程树回收
- 已取代：同步、非流式的 Provider 调用路径 —— 已由异步流式取代（ADR-0013 → ADR-0004，`873ae9d`）。

## 当前状态

### 已完成（累积里程碑，只增不删，可压缩）
- Turn / Tool 生命周期、原子提交边界与可控中断收尾 — `1e05fd2`
- 正常退出后的会话列表与按 ID 续聊（Session Resume）— `6273528`
- 上下文估算、自动压缩与 SQLite 原始历史存档 — `7faa835`
- `bash` 单次调用 `workdir` 与 `edit` 精确替换失败诊断 — `ce3f600`
- `read` 按行续读、`glob`/`grep` 检索与会话错误恢复 — `70af0ec`
- 工具输出治理：`ToolRuntime` 统一执行与 50 KiB 上限 — `0109042`
- 异步流式事件、Provider / AgentLoop 校验与提交时机 — `873ae9d`
- 统一领域模型（Request / Response / Message / Tool Calling）与 FinishReason 严格控制流 — `d300282`
- 四个基础工具、OpenAI Chat Completions 适配器与 workspace 边界 — `afa3983`
- 最小骨架 v0：CLI → Session → AgentLoop → Provider → ToolRegistry 链路打通 — `e2c3dee`

### 进行中
- 无。

### 阻塞 / 未确认
- `bash` 在 Windows 下对返回 PowerShell 对象的命令丢失输出，`pwd` 为最小复现（结转自 `ce3f600`；`1e05fd2` 时记为“原因未确认”，本次交接已定位根因）。
  - 根因：`_shell_command`（`src/codehead/tools/bash.py:129`）的包装脚本以 `exit` 结尾，PowerShell 在脚本结束前不刷新格式化器中的对象输出，`pwd` 返回的 `PathInfo` 因此被丢弃；字符串输出与原生命令 stdout 不受影响。
  - 最小复现：`powershell.exe -NoProfile -NonInteractive -Command "& { pwd }` 换行 `exit 0"` → 仅输出 `\r\n`，退出码 0。去掉结尾的 `exit` 即正常。
  - 可用绕过：`$PWD.Path`、`python -c "import os; print(os.getcwd())"` 均正常。
  - **影响范围未完整刻画**：`ls` / `Get-ChildItem` 仍能正常输出，因此并非所有 cmdlet 都受影响。未修改代码。
- 当前环境为 Windows；Unix 真实进程组测试在此平台跳过，Unix 行为尚未在对应平台实测。

### 已归档（本轮关闭）
- 「FinishReason 控制流修正只经 fake provider 验证、未重跑真实 API」→ 后续真实模型 smoke test 已覆盖该路径（`7faa835` 版本记录的构造样例会话）。
- 「若调整结束原因或异常恢复策略，优先检查 `agent/loop.py` 与 `tests/test_agent_loop.py`」→ 已由 ADR-0003 覆盖。

## 验证
- 本次交接最终代码 `D:\miniconda3\envs\codehead\python.exe -m pytest -q --tb=short` → 通过（296 passed，1 skipped；跳过 Unix 真实进程测试）。
- 本次交接 `python -m ruff check .` → 通过（All checks passed）。
- 本次交接 `python -m ruff format --check .` → **未通过**：`README.md` 与 `src/codehead/tools/write.py` 会被重排。两者本次均未触碰，属既有偏差，未修复。
- 本次改动仅限文档与 skill（`docs/`、`AGENTS.md`、`.agents/skills/`、`HANDOFF.md`），未触碰 `src/` 与 `tests/`。

## 待处理事项
- 当前没有已授权的后续开发任务；以下仅用于恢复上下文，不代表授权执行。
- 若处理上述 `bash` 输出丢失：先系统刻画受影响的命令范围，再改 `src/codehead/tools/bash.py` 的 `_shell_command`（避免以 `exit` 终止脚本），补充覆盖“返回对象的 cmdlet”的测试，并在 README 记录行为。
- 若需验证跨平台交付，在 Unix 上运行 `tests/test_process_lifecycle.py` 的真实进程组用例，再运行完整 `pytest`；Windows 已运行真实 Job Object 用例。
- `ruff format` 的两处既有偏差（`README.md`、`src/codehead/tools/write.py`）可择机修复。

## 关键文件
- `docs/adr/` — 新增。`README.md` 是 ADR 规则正文与索引；`template.md` 是骨架；`0001`–`0013` 为决策正文。
- `AGENTS.md` — 「文档」一节新增 ADR 长期维护约束；「架构」一节改为指向 `docs/adr/README.md`。
- `.agents/skills/adr/` — 新增 skill：判定、编号、写正文、处理取代、更新索引。
- `.agents/skills/project-handoff/SKILL.md` — 改为增量演进模式（先读旧文件、已完成累积不删、阻塞项必须结转）。
- `src/codehead/session/store.py`、`src/codehead/session/session.py` — SQLite schema、恢复、Turn / Tool 事务与活跃上下文。
- `src/codehead/agent/loop.py`、`src/codehead/agent/cancellation.py` — 流式回合、取消及收尾边界。
- `src/codehead/tools/bash.py`、`process.py`、`_windows_job.py`、`_process_launcher.py` — 上述阻塞项的所在处，以及进程树管理与 Windows 启动许可。
- `tests/test_turn_lifecycle.py`、`tests/test_process_lifecycle.py` — 生命周期与真实进程验证。

## 仓库状态与注意事项
- 工作区：有未提交修改。`AGENTS.md`、`.agents/skills/project-handoff/SKILL.md`、`HANDOFF.md` 为修改；`docs/`、`.agents/skills/adr/` 为新增。未自动 commit。
- `HANDOFF.md` 已被 Git 跟踪，且未被 `.gitignore` 忽略。
- 本次未修改业务代码，上一轮的测试结果对当前代码仍然有效。
