# Codehead

Codehead 是一个从零构建的 Coding Agent 项目。

项目目标：逐步实现现代 Coding Agent 背后的核心 Harness 能力

项目会保持增量开发：先实现最小可工作的 Agent，再根据真实遇到的问题逐步增加复杂度。

## Goals

Codehead 主要用于探索和实现 Coding Agent 的 Harness 能力
也包括理解：

* 为什么需要它们
* 它们解决什么工程问题
* 不同设计之间有什么取舍

## Design Principles

### Simple First

优先实现能够解决当前问题的最简单方案，再逐步迭代

不因为成熟 Coding Agent 使用了某种架构，就提前复制相同的复杂度。

### Understand the Harness

Codehead 的核心 Harness 应保持可理解。

关键执行逻辑不依赖大型 Agent Framework 隐藏实现细节。

### Evolve From Real Problems

新的抽象和机制应尽可能来源于实际遇到的问题，例如：

* 工具重复调用
* 工具执行失败
* 用户中断
* Shell 输出过大
* 上下文超过限制
* 危险操作需要确认

先遇到问题，再设计解决方案。

## Status

Codehead 正处于早期开发阶段。

项目结构和接口在这一阶段可能频繁变化，不保证向后兼容。

## Tech Stack

* Python 3.12+
* LLM API with native tool calling
* Pydantic
* pytest

其他依赖将在出现明确需求时引入。

## Quick Start

```powershell
conda activate codehead
python -m pip install -e . --no-build-isolation
# 另需安装 ripgrep，并确保 rg 可从 PATH 调用
$env:DEEPSEEK_API_KEY = "your-api-key"
codehead
```

Codehead 默认使用 `deepseek-flash` 和 `https://api.deepseek.com`。运行参数可通过
项目 TOML 或环境变量配置，见下方「运行时配置」。

当前 `read`、`write`、`edit`、`glob` 和 `grep` 工具只能访问 workspace 内的文件。
`glob` 和 `grep` 依赖系统安装的 [ripgrep](https://github.com/BurntSushi/ripgrep)；
启动时若找不到 `rg` 会明确报错。`bash` 默认以 workspace 为工作目录，可通过
`workdir` 为单次调用指定 workspace 内的目录；它不提供进程沙箱。Windows 使用
PowerShell，Unix 使用 Bash。

## 运行时配置

启动时先按现有方式加载 `.env`，再由 `CODEHEAD_WORKSPACE` 或当前目录确定 workspace，
读取可选的 `workspace/.codehead/config.toml`。文件使用平铺字段，不包含 workspace
或 API key；例如：

```toml
model = "deepseek-flash"
base_url = "https://api.deepseek.com"
session_db = ".codehead/sessions.sqlite3"
context_budget = 65536
max_steps = 20
```

| 字段 | 默认值 / 约束 | 环境覆盖 |
| --- | --- | --- |
| `model` | `deepseek-flash`，非空字符串 | `CODEHEAD_MODEL` |
| `base_url` | `https://api.deepseek.com`，非空字符串 | `DEEPSEEK_BASE_URL` |
| `session_db` | `.codehead/sessions.sqlite3`，相对路径以 workspace 为基准 | `CODEHEAD_SESSION_DB` |
| `context_budget` | `65536`，整数且不小于 `1024` | `CODEHEAD_CONTEXT_BUDGET` |
| `max_steps` | `20`，整数且不小于 `1`；每回合的正常模型调用上限 | `CODEHEAD_MAX_STEPS` |

优先级为默认值 → TOML → `.env` → 进程环境；进程环境不会被 `.env` 覆盖，空环境变量
视为未设置。合并后的参数在启动时统一校验，TOML 中的整数不得写成字符串或布尔值。
文件不存在时使用默认值；未知字段、读取或解析错误会明确报错，不静默回退。
密钥继续通过 `DEEPSEEK_API_KEY` 设置。配置在启动时固定，修改文件需重启。

嵌入使用时，`LocalConfig` 和 `RuntimeConfig` 是不可变值对象，构造时不读取配置来源。
原 `Config` 和 `from_env()` 已移除；使用独立加载函数：

```python
from codehead.config_loader import load_local_config, load_runtime_config

config = load_runtime_config()  # 完整运行参数，要求 API key
local = load_local_config()  # 仅路径配置，不要求 API key 或有效的模型参数
```

`--list-sessions` 使用本地路径加载器；它仍检查配置文件语法和未知字段，但不校验模型
参数。初版只读取项目配置，不支持用户级配置、profiles、热重载或 `--config` 参数。
权限继续由独立的 `permissions.toml` 管理，工具上限和模型思考开关保持现有行为。

## 工具权限

AgentLoop 在启动工具前检查权限。默认 `read`、`glob`、`grep` 为 `allow`；
`write`、`edit`、`bash` 及其他工具为 `ask`。CLI 的 `ask` 提示会展示完整调用参数，
可选择仅本次批准、本会话批准或拒绝；直接调用 AgentLoop 且没有批准回调时，
`ask` 会被拒绝。`deny` 和用户拒绝都会产生 `denied` 工具结果，模型可以据此调整方案。

可在 workspace 的 `.codehead/permissions.toml` 放置启动时读取的规则；文件不存在时
使用上述默认值。例如：

```toml
[[rules]]
tool = "write"
action = "deny"
path = "private/*"

[[rules]]
tool = "grep"
action = "deny"
path = "private/*"

[[rules]]
tool = "bash"
action = "allow"
command = "git status --short"
workdir = "."
```

规则按文件顺序取首个匹配项。每条规则必须包含 `tool` 和 `action`（`allow`、`ask`、
`deny`）。文件工具 `read`、`write`、`edit`、`glob`、`grep` 可加 `path`，匹配解析后的
workspace 相对路径；`bash` 可加完整命令字符串 `command` 和工作目录 `workdir`。
路径模式使用 glob，`*` 可跨目录；多个条件须全部匹配。规则按工具分别配置，
`read` 的拒绝规则不会自动限制 `grep`。对 `grep` / `glob` 的 `ask`、`deny` 路径规则，
只要搜索根可能覆盖规则对应的路径，就对整次检索询问或拒绝。例如 `path="private/*"`
会拦住从 `.` 或 `private` 发起的检索；无法确定目录前缀时会保守拦截，可能误拦。
`allow` 路径规则仍只匹配搜索根，排在前面的 `allow` 可以覆盖后面的限制规则。
规则在启动时校验并固定，修改配置需重新启动。`.codehead/` 默认不纳入 Git。

本会话批准仅复用于同工具、同目标：文件工具为同一路径，`bash` 为同一命令及工作目录。
内容等其他参数可变化。批准只在当前进程有效，`--resume` 后重新询问，且不能覆盖
配置中的 `deny`。用户拒绝后，同一回合内同工具同目标的重试直接拒绝；下一回合重新询问。
`ToolApprovalRequested` 表示调用需要授权，即使没有批准回调、随后会直接拒绝，也会发出该事件。
权限判断只针对调用参数，`glob` / `grep` 不逐项检查搜索结果；`bash` 仍无沙箱，
可能访问 workspace 外的文件，也能绕过文件工具的路径规则。底层 `ToolRuntime` 的直接调用
由宿主程序自行负责授权。

CLI 在启动时加载权限文件；嵌入调用必须显式传入策略，且策略 workspace 须与内置工具一致：

```python
from codehead.agent import AgentLoop
from codehead.permissions import PermissionPolicy

policy = PermissionPolicy.from_file(
    workspace, (tool.name for tool in agent.tools.definitions())
)
loop = AgentLoop(agent, permission_policy=policy)
```

`PermissionPolicy.allow_all(workspace, names)` 是不经审批、把所有已知工具视为
`allow` 的入口；CLI 仅在 `-p` 搭配 `--allow-all-tools` 时使用它（见「单回合执行」）。
它不改变底层的默认分派：未显式放开的工具仍按其规则求解，无批准回调时 `ask` 变
`deny`。

## 流式事件

Provider 和 AgentLoop 使用异步流，事件类型统一从 `codehead.models` 导入。
Provider 输出 `LLMTextDelta`，最后输出一次 `LLMResponseCompleted`；Loop 输出
`TurnStarted`、`ModelStarted`、`AssistantTextDelta`、`ModelCompleted`、`ToolStarted`、
`ToolCompleted` 和最终的 `TurnCompleted`，以及等待授权时的 `ToolApprovalRequested`、失败或取消时的 `TurnFailed` / `TurnCancelled`、压缩期间的
`ContextCompactionStarted` / `ContextCompacted`。`step` 从 1 开始，表示本回合第几次
正常模型调用；摘要请求不占用 `max_steps`，摘要增量不作为助手答案展示。

文本增量仅用于即时展示。Loop 校验完整响应的 FinishReason 后才保存 assistant
消息、发送 `ModelCompleted` 和执行工具；工具结果保存后才发送 `ToolCompleted`。
CLI 不重复打印完成事件中的文本，也不默认展开工具输出。事件包含回合 ID，工具事件还包含执行记录 ID；
`ToolCompleted.result.outcome` 区分 `succeeded`、`failed`、`cancelled`、`skipped` 和 `denied`。
模型失败和步数耗尽仍通过
异常报告；CLI 会显示回合错误并继续使用同一 Session，保留本轮 user 消息及此前已提交
的工具历史，不自动重试。工具业务失败通过 `ToolResult.is_error` 回传模型。
工具内部缺陷会中止回合，不发送该调用的 `ToolCompleted`，并以 `internal_error`
记录工具终态；其余未启动调用记为 `skipped`。收尾成功后会话仍可继续或正常恢复。

`complete(request)` 和 `run(session, user_input)` 现在必须 `await`，内部消费同一条
流式路径。直接消费事件时，使用 `contextlib.aclosing` 保证提前退出也能关闭流：

```python
from contextlib import aclosing


async def consume_turn(loop, session, user_input):
    async with aclosing(loop.stream(session, user_input)) as events:
        async for event in events:
            print(event)
```

所有回合结束后，调用方须在 `finally` 中 `await loop.aclose()`（直接使用 Provider
则调用 `provider.aclose()`）。CLI 已负责这一清理。外部注入的 SDK 客户端由注入方关闭。

一个 Session 同时仅支持一个回合。同步工具在线程中按顺序执行。执行期间第一次 Ctrl+C
请求取消当前回合；再次按下时仅显示收尾状态。文件工具一旦启动，会等待真实结果并保存；
`bash`、`glob` 和 `grep` 会停止受管理的进程树。尚未启动的调用记录为 `skipped`。
工具副作用不会回滚，已停止命令可能留下部分副作用。收尾完成后 CLI 回到输入提示，
可开始新回合或正常退出。收尾期间输入提示不会提前出现。数据库无法写入或无法确认
进程树已停止时，会报告收尾失败并拒绝宣称会话可恢复。事件未持久化，不支持重放。

嵌入调用取消 `AgentLoop.stream()` / `run()` 后，可继续使用已收尾的同一个 Session。
调用方应使用 `contextlib.aclosing` 关闭提前停止的流，并在有序退出前调用
`session.finish()`；`close()` 仅释放资源，不提交正常退出状态。取消整个 CLI REPL 时，
先等待当前回合收尾，再提交会话退出状态和关闭资源。

## 上下文与会话存档

CLI 将原始消息保存到 workspace 下的 `.codehead/sessions.sqlite3`，默认启动创建
新的会话。`CODEHEAD_SESSION_DB` 可覆盖数据库位置；相对路径以 workspace 为基准。
数据库保存会话元数据、按序号追加的消息、压缩检查点，以及新回合与工具执行状态。
这里的原始消息指已通过响应校验的完整消息，以及经过现有输出限制处理的工具结果，
不包含流式片段或被工具输出治理截掉的内容。支持正常退出后的会话续聊，尚不支持
历史检索、崩溃后自动恢复或已有遗留 `active` 会话的恢复；数据库不会自动清理。

`Session.messages` 现在表示活跃请求上下文。压缩成功后，Python 内存只保留摘要、
system prompt、最新用户输入和近期消息，旧消息继续保存在数据库中，不在请求路径
重新全量加载。工具调用和对应的全部结果始终作为一个整体保留或压缩。

`CODEHEAD_CONTEXT_BUDGET` 默认 65536，最小 1024，表示估算的**输入** token 预算，
不是模型窗口大小。需根据目标模型为输出预留空间，再配置输入预算。本地估算包含
消息、工具参数、工具定义和协议开销，使用 JSON 的 UTF-8 字节数除以 3，再加固定
开销，初始乘以 1.2；正常请求返回的 input usage 仅向上校准当前会话的系数。
没有 usage 时仍可估算。这不是精确 tokenizer，也不保证服务端一定不会超限。

每次正常模型请求前检查用量，达到预算 75% 时自动摘要，要求压缩后不超过 50%。
默认配置对应 49152 个估算 token 触发、压缩后不超过 32768；近期消息预算约 16384，
摘要上限为 4096。需要更早观察压缩时，可显式设置较小预算，例如压缩评测使用的 16384；
历史评测记录中的显式预算不随默认值变化。
近期消息约占预算 25%，至少保留最后一个完整消息或工具交互单元，另外保留 system
prompt 和最新用户输入。摘要复用当前模型，以独立无工具请求处理旧历史；超出单次
输入预算时按完整单元分批合并，摘要本身上限为预算的 1/16。CLI 显示开始状态及
压缩前后估算用量。摘要属于可能有损的历史参考资料，不作为 system 指令。

摘要失败、取消或无法有效缩小时不会提交候选检查点，也不会丢弃原活跃消息。
压缩错误结束当前回合并回到输入提示，不自动重试；过大的最新输入、system prompt、
工具定义或不可拆工具交互也可能使压缩失败。消息和检查点均先提交 SQLite，再更新
内存或发送完成事件；数据库错误会终止会话并退出 CLI，已执行工具的副作用不能回滚。

嵌入使用时，通过 `Session(store=SQLiteSessionStore(path))` 注入磁盘存储，调用方
负责关闭 store。无参数 `Session()` 为兼容嵌入调用和测试使用临时的内存 SQLite，
不会落盘，应调用 `session.close()` 释放它；CLI 始终使用磁盘数据库并负责资源清理。

## 恢复会话

```text
codehead                        # 新建会话
codehead --list-sessions        # 列出当前 workspace 的会话
codehead --resume <完整会话ID>    # 继续一个正常关闭的会话
```

新建或恢复成功时会显示完整会话 ID。列表按最近活动时间倒序显示 ID、UTC 时间、
模型和退出状态；列表不要求 API key、有效的模型预算配置或 ripgrep，也不会创建
新会话。`--resume` 和 `--list-sessions` 不能同时使用。不支持 ID 前缀匹配、
自动选择最近会话或跨 workspace 恢复；指定 ID 无效时会报错，不会新建会话。

只有在输入提示处输入 `exit` / `quit`、收到 EOF 或按 Ctrl+C，且没有运行中回合、
已记录的工具调用都有对应结果时，才将会话标记为 `closed`。模型错误、压缩失败、
步数耗尽或已收尾的执行中断后返回
输入提示，仍可正常退出并在下次恢复；历史末尾可以是未获回答的用户输入或完整的
工具结果组。执行期间的可控中断先收尾回合，再由用户在输入提示处正常退出。
数据库故障、无法确认工具停止或进程异常退出不会写入正常退出标记，这些会话保留为
`active` 并拒绝恢复；不提供强制解锁。旧存档保持原样，新回合才产生 Turn / Tool 状态记录。

恢复会沿用原会话 ID 和消息序号。已有压缩检查点时，只加载最新摘要、保留消息及
检查点之后的新增消息，不重新加载被压缩的原文；无检查点则加载尚未压缩的历史。
检查点、消息格式或工具交互结构损坏时明确报错，不回退旧检查点。恢复成功后等待
新的用户输入，不自动继续上一轮、不自动重试模型请求，也不重新执行历史工具。
恢复占用与历史校验在同一事务内完成，同时恢复同一会话的两个进程只有一个能成功。

恢复沿用存档中的 system prompt（包括当时的 `AGENTS.md`），与当前指令不同时
会提示；要采用新指令，应新建会话。模型、端点、工具和输入预算使用当前配置。
模型名称和端点均相同时保留 token 校准系数，任一变化则提示并重置为 1.2。
每个新回合重新计算 `max_steps`。正常退出的空会话也可恢复，首次输入使用当前
system prompt。

旧数据库会自动、事务性地增加恢复所需字段，原始消息与检查点保持不变。功能上线前
的会话没有可靠的退出记录，标记为 `legacy`（退出状态未知），可以列出但不能恢复。

嵌入调用可使用 `Session.resume(id, store=store, workspace=..., model=...,
base_url=...)`。在调用方确认是正常退出、且没有正在执行的回合时调用
`session.finish()` 保存退出标记和 token 校准系数；`session.close()` / `store.close()`
仅释放资源，不写入正常退出标记。已 finish 的 Session 实例不能继续追加消息，应通过
恢复入口获得新实例；调用方仍负责关闭注入的 store。

## 单回合执行

```text
codehead -p "总结这个仓库的结构"                      # 单回合，答案写 stdout
echo "修复这个 bug" | codehead -p                     # 省略 PROMPT 时从 stdin 读取
codehead -p "跑一遍测试并修好失败的用例" --allow-all-tools   # 无人值守：跳过审批
```

`-p` / `--print` 执行一个回合后退出，不进入 REPL。省略 `PROMPT` 时读取整段 stdin，
空白提示词报错并返回 1。该模式下 **stdout 只包含最终答案**（外加一个换行），
其余输出——会话 ID、工具进度、压缩提示、错误，以及一行
`[codehead] usage input=.. output=.. total=..` 用量摘要——全部写 stderr，
便于调用方把答案与进度分别解析。

退出码：`0` 表示拿到最终答案且会话正常收尾；`1` 表示启动或配置失败、模型失败、
步数耗尽、存储失败、被中断或退出标记写入失败；`2` 表示命令行用法错误。
print 模式下 Ctrl+C 返回 1。`-p` 与 `--list-sessions`、`--resume` 互斥，会话仍会
照常落盘并带上正常退出标记。

`--allow-all-tools` 跳过权限审批，把所有已知工具视为 `allow`，并**取代**
`permissions.toml` 而与之不叠加；它必须与 `-p` 同时使用，默认关闭。它**不是沙箱**：
不改变「workspace 只是文件边界、`bash` 仍可越界」这一既有事实（见「工具权限」），
只应在一次性、可抛弃的执行环境里使用。不开启时行为与交互模式一致：`ask` 因无人
批准而被拒绝，模型会拿到 `denied` 结果并据此调整方案。

启动时标准输入、输出、错误流被固定为 UTF-8，因此管道与重定向传入的非 ASCII 文本
按 UTF-8 解释，不受系统区域编码影响。

## 工具输出

工具统一经 `ToolRuntime` 执行。注册时固定并检查工具的 Draft 2020-12 JSON Schema，
模型看到的定义和 Runtime 校验使用同一份快照。参数不符合 schema 时，工具不会执行，
Runtime 返回 `failed` 结果；工具本身仍检查路径存在性等业务条件。预期的工具失败
转为错误结果，未预期的异常则中止当前回合。Runtime 在返回模型、
发送 `ToolCompleted` 事件和写入 Session 前，将结果限制在 50 KiB UTF-8 字节以内，
截断时保留开头、末尾及明确标记。截断不会改变工具实际执行的成功或失败状态。

`read(path, offset=1, limit=200)` 按行读取并附行号，`offset` 从 1 开始，`limit`
最大 500。每次输出至多 40 KiB；单行只展示前 2 KiB，标注不完整。有后续行时返回
`next_offset` 和 `limit`，可用相同路径继续读取，不统计剩余总行数。文件不存在、目录、
二进制内容和无效 UTF-8 分别返回可区分的工具错误。

`glob(pattern, path=".", limit=100)` 查找文件；`grep(pattern, path=".", glob=None,
limit=100)` 使用区分大小写的逐行正则搜索并返回 `路径:行号:预览`。两者最多返回
200 条和 40 KiB，搜索 15 秒超时。搜索遵循 ripgrep 默认的忽略、隐藏文件、二进制文件
及符号链接规则；显式 glob 可能覆盖部分忽略规则。超限时需缩小路径或模式。

`bash` 持续排空 stdout 和 stderr，各保留最多 20 KiB 的头尾片段，等待真实退出码或
60 秒超时。`workdir` 只对本次调用生效，不会改变后续命令的默认目录。
Windows 下的 PowerShell 会在退出前冲刷对象输出，并保留命令失败状态。
Windows Shell 使用 Windows PowerShell，启动时移除子进程继承的 `PSModulePath`，
由它重建默认模块路径，避免 PowerShell 7 的同名模块遮蔽系统模块。宿主自定义模块路径
不会继承；需要时可在单次命令内显式导入模块。控制台输入、输出和原生命令管道编码
固定为无 BOM 的 UTF-8，stdout/stderr 继续按 UTF-8 解码。自行输出其他编码的外部程序
需在命令内配置编码，工具不自动识别或转码。
`bash` 与检索工具使用同一进程管理层。Unix 使用独立进程组，取消时先发送终止信号，
1 秒后强制终止仍存活的进程；Windows 使用 Job Object 回收命令及子进程，命令在
成功加入 Job 后才获得启动许可。工具不能保证管理主动脱离进程组的 Unix 子进程。
`edit` 仅替换唯一精确匹配；找不到文本时会提示重新用 `read` 或 `grep` 定位，
多处匹配时列出最多 5 个行列位置和短上下文供修正。相同的新旧文本会报错，
上述失败都不会写文件。`edit` 的 LF 多行片段可匹配 CRLF 文件，未修改区域保留原始
换行；新片段中的普通 LF 沿用匹配处的换行风格。`write` 保留传入内容的换行。
两者先写同目录临时文件，再原子替换目标；覆盖现有文件时保留权限位。
`write` 与成功的 `edit` 仍返回简短摘要。
