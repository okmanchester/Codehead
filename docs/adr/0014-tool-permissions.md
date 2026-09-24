# 0014. 工具调用权限与会话授权

- 状态：Accepted
- 日期：2026-09-24
- 来源：工作区未提交变更（提交时补实际 commit hash 与 subject）

## 背景

[0006](0006-workspace-boundary-no-sandbox.md) 把工具文件访问限制在 workspace，并明确
`bash` 没有沙箱，同时决定暂不加入确认机制。现在模型可以直接启动写入和命令工具，
用户无法在副作用发生前审查具体调用。需要在不引入进程隔离的前提下增加调用级权限。

## 决策

AgentLoop 在 `start_tool` 之前按工具名和参数判断 `allow`、`ask`、`deny`。CLI 启动时从
workspace 的 `.codehead/permissions.toml` 读取可选规则并固定快照；嵌入宿主必须显式
传入策略，内置工具的 workspace 与策略不一致时构造失败。规则按文件顺序取首个
匹配项。文件工具按规范化的 workspace 相对路径匹配 glob，`bash` 按完整命令及工作
目录匹配。递归检索的 `ask` 和 `deny` 路径规则在搜索根可能覆盖受限路径时作用于
整次调用；无法确定字面目录前缀时保守拦截。`allow` 仍只匹配搜索根，排在前面的
`allow` 可覆盖后面的限制。未匹配时，读取和检索工具 `allow`，其余工具 `ask`。

`ask` 允许用户批准本次、批准当前进程中的同会话同工具同目标调用，或拒绝。文件工具
的目标是路径，`bash` 的目标是命令与工作目录；会话批准只覆盖 `ask`，重启或恢复后
失效。用户拒绝后，同一回合内同工具同目标不再询问，下一回合可重新询问。缺少批准
回调时拒绝执行；`ToolApprovalRequested` 只表示需要授权。拒绝不会启动工具，而是原子
提交 `denied` 工具结果，
让模型获得明确反馈并继续回合。等待批准时取消，沿用现有未启动工具的 `skipped`
收尾语义。底层 `ToolRuntime` 的直接调用是宿主程序的执行入口，不经 AgentLoop 权限门。

沿用 [0006](0006-workspace-boundary-no-sandbox.md) 的 workspace 文件边界、`bash` 无沙箱
及凭据清理决定；仅取代其中“无确认机制”的结论。此机制只控制调用启动：`grep` / `glob`
的路径规则不逐个检查结果；规则按工具分别配置，`bash` 可通过命令访问 workspace 之外。

## 备选方案与取舍

| 备选 | 为什么不采用 |
| --- | --- |
| 通过命令语义推断安全性 | Shell 语义复杂，简单分类易漏判；使用完整命令匹配并由用户批准 |
| 持久化会话批准 | 会引入规则版本与历史授权失效问题；当前进程内授权即可满足交互需要 |
| 把拒绝记为 `failed` 或 `skipped` | 会混淆权限拒绝、工具执行失败和取消前跳过 |
| 引入进程或文件沙箱 | 超出本次调用权限目标，并显著增加跨平台实现成本 |

## 后果

- `ToolResult.outcome` 与 SQLite `tool_executions.status` 增加 `denied`；旧表需事务内迁移。
- CLI 授权提示需要等待用户输入；Ctrl+C 必须按回合取消语义收尾。
- 配置位于 workspace 内，进程只在启动时读取它；修改配置不会改变当前进程的权限。
- 权限不是文件隔离；不能据此运行不受信任的 workspace 或工具命令。

**重新评估的触发条件**：需要保证禁止的文件不能经递归检索或 Shell 间接访问时，
应设计真正的文件与进程隔离，并重新界定权限规则的含义。

## 取代关系

本文取代 [0006](0006-workspace-boundary-no-sandbox.md) 中“无确认机制”的决定：写入和
命令等工具现在先经过规则判断，必要时等待用户批准。其余决定继续有效：文件工具保持
workspace 边界，`bash` 仍无沙箱并清理继承环境中的 `DEEPSEEK_API_KEY`。因此权限规则
仍不能保证文件隔离，也不改变直接调用 `ToolRuntime` 时宿主程序的责任。

## 代码与测试

- `src/codehead/permissions.py` — 规则校验、匹配及进程内会话授权
- `src/codehead/agent/loop.py`、`src/codehead/cli.py` — 执行前权限门与交互批准
- `src/codehead/session/store.py` — `denied` 状态及旧数据库迁移
- `tests/test_permissions.py` — 规则、拒绝、授权、取消及迁移验证
