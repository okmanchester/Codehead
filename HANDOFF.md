# Project Handoff

- 更新于：`b6beb56`（优化开发流程，新增adr skill，记录重要adr，语义级增量演进handoff）
- 上一版基线：`1e05fd2`（落后 1 个提交）
- 分支：`main`

## 目标

- 增量实现可理解的 Python Coding Agent Harness。
- 当前子任务：权限 v1 已实现并通过相关测试，尚未提交；本轮只更新交接。

## 关键背景与决策

- 尚未写入 ADR 的约束：`docs/adr/README.md` 的规则正文优先于 `.agents/skills/adr/SKILL.md`，两者冲突时改后者。
- ADR：
  - ADR-0001 自实现 Harness，不引入 Agent Framework
  - ADR-0002 统一领域模型层 `codehead.models`
  - ADR-0003 FinishReason 严格语义与零副作用拒绝
  - ADR-0004 异步流式事件与“校验后提交”
  - ADR-0005 工具统一经 `ToolRuntime` 执行与分层输出上限
  - ADR-0006 workspace 文件边界、无沙箱与环境清理（由 ADR-0014 取代）
  - ADR-0007 工具行为取舍：read 分页、edit 唯一精确匹配、检索依赖 ripgrep
  - ADR-0008 append-only SQLite 存档与活跃上下文分离
  - ADR-0009 估算驱动的阈值自动压缩
  - ADR-0010 恢复契约：只有正常退出的会话可恢复
  - ADR-0011 Turn / Tool 生命周期与原子提交边界
  - ADR-0012 可控中断语义与跨平台进程树回收
  - ADR-0014 工具调用权限与会话授权（工作区未提交）
- 已取代：同步、非流式的 Provider 调用路径 —— 已由异步流式取代（ADR-0013 → ADR-0004，`873ae9d`）。
- 权限 v1 的本轮约束：用户要求不执行 `git add` 或提交；现有暂存状态来源未确认，不要据此推断用户已授权提交。

## 当前状态

### 已完成（累积里程碑，只增不删，可压缩）

- 权限 v1 — `AgentLoop` 增加调用权限、会话授权、拒绝归档与旧库迁移；后续收口递归检索、非法路径、嵌入入口和 workspace 校验 — 未提交
- ADR 体系与增量交接规范 — 建立 ADR 索引、技能及累积式 handoff — `b6beb56`
- Turn / Tool 生命周期、原子提交边界与可控中断收尾 — `1e05fd2`
- 正常退出后的会话列表与按 ID 续聊（Session Resume）— `6273528`
- 上下文估算、自动压缩与 SQLite 原始历史存档 — `7faa835`
- `bash` 单次调用 `workdir` 与 `edit` 精确替换失败诊断 — `ce3f600`
- `read` 按行续读、`glob`/`grep` 检索与会话错误恢复 — `70af0ec`
- 工具输出治理：`ToolRuntime` 统一执行与 50 KiB 上限 — `0109042`
- 异步流式事件、Provider / AgentLoop 校验与提交时机 — `873ae9d`
- 统一领域模型与 FinishReason 严格控制流 — `d300282`
- 四个基础工具、OpenAI Chat Completions 适配器与 workspace 边界 — `afa3983`
- 最小骨架 v0：CLI → Session → AgentLoop → Provider → ToolRegistry — `e2c3dee`

### 进行中

- 无。

### 阻塞 / 未确认

- `bash` 在 Windows 下对返回 PowerShell 对象的命令丢失输出，`pwd` 为最小复现（结转自 `ce3f600`）。`_shell_command` 末尾 `exit` 导致格式化器未刷新；`$PWD.Path` 可绕过。`ls` 仍正常，影响范围未完整刻画；代码未改。
- Unix 真实进程组测试在当前 Windows 环境跳过，Unix 行为尚未在对应平台实测（结转自 `1e05fd2`）。
- 当前 Git index 中 `.pytest_tmp/` 有 597 个暂存新增、工作区已删除的临时文件条目；来源未确认。本轮未调整暂存区。
- 最新鸭式 workspace 校验变更之后尚未重跑完整 `pytest`；最近的完整结果已过期。

### 已归档（本轮关闭）

- 上版「ADR 体系与 handoff 规范尚在工作区」→ 已由 `b6beb56` 提交；该事项已归入累积里程碑。
- 上版「`README.md` 有 Ruff 格式偏差」→ 本轮 `ruff format --check README.md` 通过；保留 `write.py` 的偏差待处理。
- 更早：「FinishReason 控制流只经 fake provider 验证」→ 真实模型 smoke test 已覆盖（上一版交接记录）。
- 更早：「结束原因或异常恢复策略需要专门提醒」→ 已由 ADR-0003 覆盖。

## 验证

- `python -m pytest tests/test_permissions.py -q --tb=short` → 通过（27 passed；最新 workspace 校验变更后）。
- `ruff check src/codehead/agent/loop.py tests/test_permissions.py` → 通过；`ruff format --check` 同两文件 → 通过。
- `ruff format --check README.md` → 通过（1 file already formatted）。
- 更早：完整 `pytest -q --tb=short` → 322 passed、1 skipped；在最新 workspace 校验变更前运行，结果已过期。
- 更早：`ruff check src tests` → 通过；在最新 workspace 校验变更前运行，结果已过期。
- 更早：`ruff format --check .` → 未通过；`src/codehead/tools/write.py` 有既有格式偏差，扫描 `.pytest_tmp/` 时还遇到访问权限问题。
- 更早：`pytest -q --tb=short` → 296 passed、1 skipped（`1e05fd2` 阶段，结果已过期）。

## 待处理事项

- 当前无新的已授权开发任务；以下只供恢复上下文，不代表授权执行。
- 核对权限 v1 的暂存差异与 `.pytest_tmp/` 的 597 个异常 index 条目；确认来源后再决定如何处理暂存、更新 ADR-0014 的实际提交来源及提交代码。
- 提交前在当前代码上重跑完整 `pytest`，并运行仓库 Ruff lint；区分已有 `write.py` 格式偏差与新问题。
- 若处理 `bash` 输出丢失：先刻画受影响命令，再修改 `src/codehead/tools/bash.py` 的 `_shell_command` 并补返回对象 cmdlet 的测试。
- 若验证跨平台交付，在 Unix 上运行真实进程组用例和完整 `pytest`。
- `src/codehead/tools/write.py` 的既有 Ruff 格式偏差可择机修复。

## 关键文件

- `src/codehead/permissions.py`、`src/codehead/agent/loop.py` — 权限规则、调用前检查与 workspace 对齐。
- `src/codehead/session/store.py`、`src/codehead/session/session.py`、`src/codehead/models/` — `denied` 结果、旧库迁移和生命周期存档。
- `tests/test_permissions.py` — 权限 v1 回归测试及最新 workspace 校验用例。
- `docs/adr/README.md`、`docs/adr/0014-tool-permissions.md` — 决策索引与权限 ADR；后者待补实际提交来源。
- `src/codehead/tools/bash.py`、`process.py`、`_windows_job.py`、`_process_launcher.py`、`tests/test_process_lifecycle.py` — 旧阻塞项所在处及跨平台验证入口。
- `AGENTS.md`、`.agents/skills/adr/`、`.agents/skills/project-handoff/SKILL.md` — ADR 与交接规范来源。

## 仓库状态与注意事项

- `HANDOFF.md` 已被 Git 跟踪，且未被 `.gitignore` 忽略。
