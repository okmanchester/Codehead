# 0006. workspace 文件边界、无沙箱与环境清理

- 状态：Accepted
- 日期：2026-09-22
- 来源：`afa3983` 初步实现四个基础工具，OpenAI Chat Completions适配器；`1e05fd2` Turn / Tool 生命周期与可控中断收尾

## 背景

模型可以驱动文件读写和命令执行。没有任何边界的 Coding Agent 会直接改写 workspace 之外的文件，或把凭据泄露给子进程。同时，真正的隔离（容器、namespace、用户降权）会显著提高实现与使用成本，与本项目「最小可理解实现」的取向冲突。

## 决策

**划定一条明确但有限的边界，并如实声明它不构成安全隔离。**

- **文件访问限制在 workspace 内**：共享自由函数 `resolve_workspace_path`（`src/codehead/tools/base.py`）解析路径（相对路径基于 workspace 展开）后判定 `is_relative_to`，越界抛 `ToolExecutionError`。
  - 它是**自由函数而非基类方法**：`Tool` 基类不含路径逻辑，由 `read` / `write` / `edit` / `glob` / `grep` / `bash` 各自显式调用。
  - `bash` 的 `workdir` 在此基础上额外校验非空、存在、是目录，且**只对本次调用生效**，不改变后续命令的默认目录。
  - 检索工具对 ripgrep 返回的路径做**二次防线**校验（`src/codehead/tools/search.py`），因为进程输出不完全可信。
- **`bash` 不提供沙箱**：只做工作目录和环境变量处理，没有 chroot、namespace 或用户降权。这一限制在 `README.md` 中明确声明。
- **凭据清理**：`bash` 在继承父进程环境后移除 `DEEPSEEK_API_KEY`（`src/codehead/tools/bash.py`）。
- **无确认机制**：不实现危险操作的权限确认。

## 备选方案与取舍

| 备选 | 为什么不采用 |
| --- | --- |
| 用容器 / namespace 实现真正的沙箱 | 显著提高部署与实现成本，且会把「进程隔离」这一学习目标之外的工程问题引入项目 |
| 把路径校验做成 `Tool` 基类方法 | 会让不涉及路径的工具也被迫携带该概念；自由函数更直白，调用点即证据 |
| 依赖模型自觉遵守路径约束 | 约束必须由代码保证，提示词只是辅助 |
| 每次都弹确认对话 | 交互成本高，且当前没有区分「危险」与「普通」的可靠判据 |
| 清空子进程的整个环境 | 会让 PATH、临时目录等基础能力失效，命令无法运行 |

## 后果

- **这只是边界，不是隔离**：`bash` 内的一条命令完全可以读写 workspace 之外的文件。文件工具的路径校验只约束这六个工具自身。
- **环境清理不对称**：清理只发生在 `bash`（`src/codehead/tools/bash.py`）。`glob` / `grep` 启动 `rg` 时直接继承父进程环境（`src/codehead/tools/search.py`），**不清理凭据**。当前 `rg` 不会外发网络请求，因此风险有限，但这是一处真实的不一致。
- 越界路径返回的是工具错误而非会话级错误，模型看到后可以自行改用 workspace 内路径。

**重新评估的触发条件**：若要处理不受信任的 workspace，或在多租户环境运行，则必须引入真正的隔离机制（容器、低权限用户），此时本 ADR 应被取代而不是修订。同时，若新增任何会执行外部命令的工具，必须一并决定其凭据清理策略，以消除当前的不对称。

## 代码与测试

- `src/codehead/tools/base.py`（`resolve_workspace_path`）— 唯一的路径边界实现
- `src/codehead/tools/bash.py` — `workdir` 校验与环境变量清理
- `src/codehead/tools/search.py` — 检索结果的二次路径校验
- `tests/test_tools.py` — 五个文件工具的越界拒绝、非法 `workdir` 在启动前拒绝、平台 shell 选择与 API key 移除
- `tests/test_search.py` — 检索工具越界路径拒绝
