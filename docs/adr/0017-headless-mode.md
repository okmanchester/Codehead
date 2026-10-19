# 0017. Headless 单回合执行与无人值守权限

- 状态：Accepted
- 日期：2026-10-13
- 来源：工作区未提交变更（提交时补实际 commit hash 与 subject）

## 背景

在此之前 Codehead 只有一条执行链路：交互式 REPL（`cli.py` 的 `run_repl`），每个回合
都必须由人在终端里逐轮驱动。这带来两类限制：

- 无法被其它程序驱动。评测框架、脚本和 CI 都没有入口把一条指令交给 Codehead 并取回
  结果，也无法把答案和进度分开解析。
- 即使有入口，无人值守也做不成事。[0014](0014-tool-permissions.md) 把
  `write` / `edit` / `bash` 默认设为 `ask`，而 `ask` 在没有批准回调时会被直接拒绝，
  于是任何需要写文件或跑命令的任务都会停在审批上。

促成决策的是先于设计出现的两个真实缺陷：

- 非 ASCII 指令经管道进入时崩溃。Windows 上 `sys.stdin` 用系统区域编码（本机
  cp936/GBK）加 `surrogateescape` 解码 UTF-8 字节，产生孤立代理字符，写回 SQLite 时
  以 `PydanticSerializationError` 中止回合。
- 标准输出与错误流同样是区域编码，重定向或经管道读取时非 ASCII 内容乱码。

本决策沿用 [0006](0006-workspace-boundary-no-sandbox.md)、
[0014](0014-tool-permissions.md) 关于「workspace 是文件边界而非沙箱」的结论，
不改变工具与权限的既有语义，只新增一条执行链路和两个显式开关。

## 决策

在 `cli.py` 增加一条与 `run_repl` 并列的非交互执行链路，并新增两个必须显式使用的开关。

**标准流固定为 UTF-8。** `main()` 启动时调用 `_configure_utf8_stdio()`，对
`sys.stdin` / `sys.stdout` / `sys.stderr` 执行
`reconfigure(encoding="utf-8", errors="replace")`，没有 `reconfigure` 的流（例如
`None` 或非文本流）被跳过。这样管道与重定向传入的文本按 UTF-8 解释，不再受系统区域
编码影响。

**`-p` / `--print [PROMPT]`。** 执行一个回合后退出，不进入 REPL。

- `PROMPT` 省略时读取整段 stdin；空白提示词报错并返回 1。
- stdout 只写最终答案（`TurnCompleted.answer` 加一个换行），不经 rich 渲染。
- 其余所有面向人的输出——会话 ID、工具进度、压缩提示、错误、一行
  `[codehead] usage input=.. output=.. total=..`——一律走 stderr。实现上只是把该模式下
  的 `Console` 换成 `Console(stderr=True)`，没有改动任何既有打印点。
- 与 `--list-sessions`、`--resume` 互斥。

**`--allow-all-tools`。** 跳过审批，把所有已知工具视为 `allow`。默认关闭，且必须与
`-p` 同用（否则 argparse 报错、退出码 2）。开启时**取代** `permissions.toml`，而不是
与其叠加。

**退出码。** `0` 拿到最终答案且 `session.finish()` 成功；`1` 启动或配置失败、模型失败、
步数耗尽（`AgentAbortedError`）、存储失败、被中断、退出标记写入失败；`2` 命令行用法
错误。print 模式下 `KeyboardInterrupt` 必须在 `run_print` 内捕获并返回 1——`main()`
现有的 `except KeyboardInterrupt: return 0` 会把中断误报为成功。

**复用而非重写。** 新增的 `run_print` / `_consume_print_turn` 与 `run_repl` /
`_consume_turn` 并列；`_main` 中原有的 session 构造与 resume 分支被抽成 `_open_session`
供两者共用；清理顺序保持 `session.finish()` → `store.close()` → `loop.aclose()` 不变；
`build_agent_loop` 仍是唯一装配点，只多一个默认 `False` 的 `allow_all_tools` 参数。

## 备选方案与取舍

| 备选 | 为什么不采用 |
| --- | --- |
| 用 approve 回调恒返回 `"session"` 跳过审批 | 每个副作用调用仍会发出 `ToolApprovalRequested`，事件流会谎称有人批准；还绕行了既有的 `grant` / `denial_key` 机制。 |
| 往 workspace 写一份全 `allow` 的 `.codehead/permissions.toml` | 零代码改动，但会在工作目录留下 `.codehead/`，污染被测目录（例如 `git status`、文件断言类检查）；而且"配置文件存在即视为同意"掩盖了无人值守这一事实，安全姿态不可审计。 |
| headless 默认放开权限 | [0014](0014-tool-permissions.md) 的全部意义就是副作用前需要有人同意；在最没人看管的时刻静默放开是最危险的时机。不放开时行为已定义且安全：`ask` 无回调即 `deny`，模型据此调整方案，失败可见可归因。 |
| 只修编码，不加 print 入口 | 修完仍然无法被程序驱动，也无法验证。 |
| 提示词与答案共用 stdout，不区分 stderr | 调用方无法把答案与进度分开解析，等于没有可编程契约。 |
| 提示词只从 argv 传入 | argv 有长度上限，且要额外处理引号与换行；同时支持 stdin 是免费且必要的。 |

## 后果

- `--allow-all-tools` 等价于使用者对每次 `ask` 都选择"本会话批准"，**不是沙箱**。
  它不改变 [0006](0006-workspace-boundary-no-sandbox.md) /
  [0014](0014-tool-permissions.md) 已确立的边界：`bash` 始终可以越过 workspace。
  因此本模式的隔离责任完全在调用方——只应在一次性、可抛弃的执行环境里使用。
- stdout/stderr 的分离是新契约而非行为变更：REPL 模式与既有事件语义完全不变。
- 强制 UTF-8 对已经是 UTF-8 的 Windows 控制台是空操作；对区域编码的管道，它把"崩溃"
  变成"按 UTF-8 解释，无法解码的字节替换为 U+FFFD"。
- 编码以运行时重配置实现，不引入新的依赖或环境变量约定。
- print 模式当前不支持"`--resume` 后单回合"的组合。

**重新评估的触发条件**：需要在不完全受信的宿主 workspace 上运行无人值守模式时——
那时必须先有真正的进程或文件隔离，而不是仅靠 `--allow-all-tools`；或调用方需要
"续聊 + 单回合"的组合时。

## 代码与测试

- `src/codehead/cli.py`（`_configure_utf8_stdio`、`run_print`、`_consume_print_turn`、
  `_add_usage`、`_open_session`、`build_agent_loop` 的 `allow_all_tools`、
  `_parse_args`）— 新执行链路、输出契约与两个开关。
- `src/codehead/permissions.py`（`PermissionPolicy.allow_all`）— 无人值守策略的唯一
  入口；不改变 `action()` 的默认分派。
- `tests/test_cli_print.py` — stdout/stderr 分离、stdin 取值、空提示词、模型错误与中断
  返回 1、`--allow-all-tools` 与 `-p` 的约束、UTF-8 重配置。
- `tests/test_resume_cli.py` — 既有 resume 用例的装配替身适配 `build_agent_loop` 新签名。
- `tests/test_permissions.py`、`tests/test_cli.py` — 既有默认策略与 REPL 行为保持不变
  的回归。
