# 0002. 统一领域模型层 `codehead.models`

- 状态：Accepted
- 日期：2026-09-22
- 来源：`d300282` 完成内部数据模型初步搭建，含message, response, tool calling相关数据结构

## 背景

早期实现中，消息类型定义在 `codehead.session.message`，工具 schema 由各工具自己的 `schema()` 方法产出，Provider 层直接暴露 SDK 的响应对象。结果是同一个概念在不同模块有不同形状，跨模块传递需要反复转换，且改动一处会波及多处。

## 决策

建立单一领域模型层 `codehead.models`，作为**所有跨模块契约的唯一入口**：

- 所有跨模块类型统一从 `codehead.models` 导入（导出面见 `src/codehead/models/__init__.py`）。
- `Message` 是按 `role` 判别的联合类型：`SystemMessage` / `UserMessage` / `AssistantMessage` / `ToolMessage`（`src/codehead/models/message.py`，判别键 `role`）。
- 领域对象统一继承 `CodeheadModel`，配置 `ConfigDict(extra="forbid", frozen=True)`（`src/codehead/models/_base.py`）。集合字段使用 tuple 而非 list。
- `ToolResult` 是**独立于消息**的模型，由 `Session.complete_tool`（`src/codehead/session/session.py`）转换为 `ToolMessage` 后才进入历史。工具执行层不构造消息。
- JSON 参数（`ToolDefinition.parameters`、`ToolCall.arguments`）在构造时做防御性深拷贝，使领域对象与调用方的可变对象解耦。
- **不保留兼容层**：迁移时直接移除旧 API（`session.message`、`ToolSchema`、`schema()`/`schemas()`、旧的 `complete` 签名），不做别名或过渡期。

## 备选方案与取舍

| 备选 | 为什么不采用 |
| --- | --- |
| 保留旧 API 作为兼容层，逐步迁移 | 项目处于早期、无外部消费者，兼容层只会让两套形状长期共存，增加理解成本 |
| 让 Provider 直接返回 SDK 类型，在 Loop 内做转换 | 会把厂商协议细节渗透到 AgentLoop，违反 Provider 的适配职责，也妨碍将来接入第二家 Provider |
| 使用可变模型（`frozen=False`） | 回合提交、事件传递、工具参数分派都依赖对象在传递中不被就地修改；不可变把这类 bug 变成构造期错误 |
| 用枚举/类继承表达消息种类 | 判别联合更贴合「消息是按 role 区分的封闭集合」这一事实，且能直接映射到 Provider 协议 |

## 后果

- **不可变只是结构级的**：`frozen=True` 阻止字段重新赋值，但嵌套的 `dict` / `list` 值仍可被就地修改。`JsonObject` 的深拷贝（`src/codehead/models/tool.py`）缓解了工具参数这一路，但不是对全部嵌套值的普遍保证。
- 每次跨模块传递都构造新对象，有轻微开销；当前规模下不是瓶颈。
- 不保留兼容层意味着早期提交之间不保证 API 稳定——这与 `README.md` 声明的「早期阶段不保证向后兼容」一致。

**重新评估的触发条件**：若出现必须在传递中就地累积的场景（例如高频流式片段直接写入模型），应针对该场景单独设计可变载体，而不是放宽全局 `frozen`。

## 代码与测试

- `src/codehead/models/__init__.py` — 统一导出面
- `src/codehead/models/_base.py`（`CodeheadModel`）— `extra="forbid"` + `frozen=True`
- `src/codehead/models/message.py` — 按 `role` 判别的消息联合
- `src/codehead/models/tool.py`、`src/codehead/tools/base.py` — JSON 参数的防御性深拷贝
- `src/codehead/session/session.py`（`Session.complete_tool`）— `ToolResult` → `ToolMessage` 的唯一转换点
- `tests/test_models.py` — 覆盖 role 判别联合、冻结不可变、JSON 输入深拷贝、非 JSON 值拒绝
- `tests/test_tool_registry.py` — 覆盖注册与参数深拷贝
