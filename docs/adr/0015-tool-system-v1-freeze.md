# 0015. 工具系统 v1 冻结：参数契约、文件提交与错误边界

- 状态：Accepted
- 日期：2026-09-24
- 来源：工作区未提交变更（提交时补实际 commit hash 与 subject）

## 背景

[0005](0005-tool-runtime-and-output-limits.md) 已统一工具执行和输出上限，但把所有未预期的
`Exception` 也作为普通 `failed` 结果交给模型。工具公开的 JSON Schema 只约束模型的
生成意图，Runtime 没有按它验证实际调用。Windows 的文本写入还会隐式改写换行，直接
覆盖目标文件可能在写入失败时留下半成品。冻结工具系统 v1 前，需要让这些边界一致且
可验证。

## 决策

- `ToolRegistry` 注册时检查 Draft 2020-12 JSON Schema，并固定定义快照；发给模型和
  Runtime 的校验器使用同一快照。参数不合 schema 是预期调用失败，Runtime 返回
  `failed`，工具不执行。为此新增 `jsonschema` 运行时依赖。
- `write` 按提交内容的 UTF-8 字节写入；`edit` 保留未改区域的原始换行，允许 LF
  多行片段匹配 CRLF，替换片段沿用匹配处的换行风格。两者在目标目录完整写入临时
  文件，再用 `os.replace` 提交；覆盖现有文件时保留权限位。它们仍受已有 workspace
  边界和权限检查约束。此行为补充 [0007](0007-tool-behavior-tradeoffs.md) 的唯一匹配
  决策：换行表示可等价，但匹配仍必须唯一。
- 预期的工具执行错误、取消和跳过仍返回原有结果；未预期的 `Exception` 改为
  `ToolInternalError`，保留原始异常原因并终止当前回合。已经启动的工具在线程真实结束
  后记为 `internal_error`，其余未启动调用记为 `skipped`；不发送该工具的
  `ToolCompleted`，也不继续当前回合的模型调用。收尾成功的会话仍可继续和恢复。
  `ToolCleanupError` 仍是无法确认停止的会话级故障。
- `settle` 从 agent 层移至共享底层模块，工具与 agent 均可引用。Windows 的 PowerShell
  包装命令先冲刷对象输出再退出，同时保留 cmdlet 及原生命令失败状态。
- 保留 [0005](0005-tool-runtime-and-output-limits.md) 的 Runtime 单一执行边界、
  `outcome` 真源，以及各工具与最终出口的 UTF-8 字节输出上限和头尾截断策略。

## 备选方案与取舍

| 备选 | 为什么不采用 |
| --- | --- |
| 只自实现现有 schema 关键字 | 新工具使用标准关键字时易出现静默漏检，长期维护两套语义 |
| 直接覆盖文件 | 中途失败可能留下截断内容，且 Windows 文本模式会改写传入换行 |
| 未预期异常继续作为普通 `failed` | 会掩盖工具实现缺陷，让模型以为可自行修正调用 |
| 内部缺陷直接废弃整份会话 | 线程已完成且结果状态已收尾时，会话历史仍可安全继续 |

## 后果

- `ToolResult.outcome` 与 SQLite `tool_executions.status` 增加 `internal_error`，旧库在
  打开时事务性迁移；该结果用于完成历史记录，下一回合可见，但不会作为当前回合的
  普通工具完成事件交给模型。
- 新增一个运行时依赖，工具注册失败会在启动阶段暴露。文件替换是单文件原子提交，
  不提供跨文件事务，也不解决其他进程在编辑读取与替换之间并发修改目标的问题。

**重新评估的触发条件**：若工具接口改为结构化结果、引入非 JSON 参数，或需要
跨文件事务及并发编辑保护，应重新设计这一边界。

## 取代关系

本文取代 [0005](0005-tool-runtime-and-output-limits.md) 中“其他 `Exception` →
`failed`”的错误分类；其余输出限制和统一执行结论在本文保留。

## 代码与测试

- `src/codehead/tools/registry.py`、`runtime.py` — schema 快照、调用校验与异常分类
- `src/codehead/tools/_atomic.py`、`edit.py`、`write.py` — 原子提交与换行保留
- `src/codehead/agent/loop.py`、`session/store.py` — 缺陷回合收尾及状态迁移
- `tests/test_tool_registry.py`、`test_tools.py`、`test_turn_lifecycle.py` — 边界回归
