# Project Handoff

## 目标
- 增量构建可运行、易理解的 Python Coding Agent，探索 Harness 的核心工程机制。
- 最近一阶段已完成：`bash` 单次工作目录与 `edit` 精确替换失败诊断；尚无新的用户授权开发任务。

## 关键背景与决策
- 执行链路为异步 Provider → 异步 AgentLoop → CLI 消费事件。文本增量只用于展示；完整响应通过 FinishReason 校验后才写入 Session 或执行工具。`complete()`、`run()` 是异步便捷方法，不保留同步兼容层。
- 工具通过 `ToolRuntime` 顺序执行；同步工具在工作线程中运行。Runtime 将工具异常转成 `is_error=True` 的 `ToolResult`，并将每条结果限制在 50 KiB UTF-8 字节内。默认单 Session 同时只运行一个回合。
- `read` 按行续读并限制输出；`glob`、`grep` 依赖系统 `rg`，保留其默认忽略规则。CLI 遇到回合中的 `CodeheadError` 会显示错误并继续使用原 Session，保留已提交历史，不自动重试。
- `bash.workdir` 仅改变本次命令的启动目录，默认 workspace；路径必须留在 workspace 且是已存在的目录。`edit` 仍要求唯一精确匹配；相同新旧文本、零处或多处匹配均报工具错误且不写文件。多处匹配最多列出 5 个 1-based 行列位置及短上下文。
- 当前不提供 shell 沙箱、权限确认、持久化、中断恢复、工具并行或模糊替换。

## 当前状态

### 已完成
- 异步流事件、Provider/AgentLoop 校验与提交时机、CLI 实时文本及工具状态显示。
- `ToolRuntime` 输出治理；`read` 分页、行号与错误分类；`glob`/`grep` 检索；CLI 回合错误恢复。
- 最近提交 `ce3f600` 实现 `bash.workdir`、`edit` 错误诊断、README 说明和对应测试。

### 进行中
- 无。

### 阻塞 / 未确认
- 无开发阻塞。Windows 下 `bash(command='pwd')` 经现有 PowerShell 包装返回空白 stdout，原因未确认；显式执行 `python -c "import os; print(os.getcwd())"` 能正确输出指定 `workdir`。该现象未在最近一阶段修改。

## 验证
- 以下结果来自最近一次实现后的验证；本次 handoff 未重新运行测试，随后仅提交了对应改动。
- `conda run -n codehead python -m pytest -q` → 通过（169 passed）。
- `conda run -n codehead ruff check .` → 通过。
- `conda run -n codehead ruff format --check src/codehead/tools/bash.py src/codehead/tools/edit.py tests/test_tools.py` → 通过。
- `git diff --check` → 通过，仅有 Git 换行转换提示。
- 真实模型 smoke test 仅使用仓库外的构造样例：模型正确传入 `bash.workdir`，实际 cwd 与指定子目录一致；收到 `edit` 多处匹配错误后，模型读取文件并仅修改目标段落。此前 `read` 续读及 `glob`/`grep` 工具选择也通过构造样例验证。

## 待处理事项
- 尚无用户指定的下一项开发功能。以下仅供恢复上下文，不代表已授权执行。
- 若处理已观察到的 Windows `pwd` 输出问题，检查 `src/codehead/tools/bash.py` 的 PowerShell 命令包装；用仓库外临时 workspace 验证 `pwd`、普通原生命令、退出码和 stdout/stderr 行为，并运行相关测试。

## 关键文件
- `src/codehead/agent/loop.py`、`src/codehead/llm/openai.py` — 异步事件、响应校验和 Session 提交边界。
- `src/codehead/tools/runtime.py` — 统一工具执行、错误转换与 50 KiB 最终输出限制。
- `src/codehead/tools/bash.py`、`src/codehead/tools/edit.py` — 最近阶段的实现；对应测试在 `tests/test_tools.py`。
- `src/codehead/tools/read.py`、`src/codehead/tools/search.py` — 续读和检索能力。
- `src/codehead/cli.py` — 工具装配、事件显示与错误后继续 REPL。

## 仓库状态与注意事项
- 分支：`main`；HEAD：`ce3f600`（`bash 单次工作目录与 edit 错误诊断`）。
- 写入本交接文件前工作区 clean；写入后仅 `HANDOFF.md` 有未提交修改，未自动 commit。
- `HANDOFF.md` 已被 Git 跟踪，且未被 `.gitignore` 忽略；不要把旧版内容当作当前架构依据。
