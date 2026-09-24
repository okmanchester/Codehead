# 0003. FinishReason 严格语义与零副作用拒绝

- 状态：Accepted
- 日期：2026-09-22
- 来源：`d300282` 完成内部数据模型初步搭建，含message, response, tool calling相关数据结构

## 背景

模型响应自带结束原因（`FinishReason`），它决定了这一轮到底该「把回答交给用户」还是「执行工具」。如果结束原因不可信或不被校验，就会出现两类静默错误：把被截断的文本当完整回答交付，或对一次并未真正完成的工具调用采取行动。

早期实现曾设想「遇到未知的结束原因就当作普通回答继续」——目的是对协议演进保持容忍。这个设想在实现前就被否决。

## 决策

结束原因必须是被**显式枚举并校验**的，无法识别的值一律视为错误：

- 只有 `END_TURN` 和 `TOOL_CALLS` 会被接受，且各自还有附加校验（`_validate_response`，`src/codehead/agent/loop.py`）：
  - `END_TURN` 必须没有工具调用，且必须有非空白的文本内容。
  - `TOOL_CALLS` 必须至少有一个调用，且调用 ID 不得重复。
- `MAX_TOKENS`、`CONTENT_FILTER`、`UNKNOWN` 以及任何其他取值都抛出 `ModelError`。
- Provider 负责把缺失或未知的协议值归一化为 `UNKNOWN`（`src/codehead/llm/openai.py`），由 Loop 决定拒绝——**归一化和裁决分离**，Provider 不做业务判断。
- 被拒绝的响应**零副作用**：不写入 Session、不执行工具、不继续请求。已有历史和当前 user 消息保留。

## 备选方案与取舍

| 备选 | 为什么不采用 |
| --- | --- |
| 未知结束原因视为 `END_TURN` 继续（早期设想） | 会把协议异常或服务端故障静默变成一次「正常回答」，是最难排查的一类错误 |
| `MAX_TOKENS` 时保留已生成的部分文本作为回答 | 截断的文本在 Coding Agent 场景下常常是半句话或半个代码块，交付它比报错更有害 |
| 在 Provider 内直接抛 `ModelError` | 会让适配器承担业务裁决，且同一语义在多个 Provider 中难以保持一致 |
| 仅警告并继续 | 与「零副作用」冲突：继续就意味着要么执行工具、要么写历史 |

## 后果

- 代价：流式场景下，用户可能已经看到若干文本增量，最终却收到错误——因为增量只是展示，落库以完整响应为准（见 [0004](0004-async-streaming-commit-after-validation.md)）。这是刻意选择：宁可事后报错，不可交付被截断的内容。
- 代价：对服务端新增的结束原因不具备向前兼容性，需要跟随协议更新枚举。
- 收益：任何一次「助手说了话」或「工具被执行」都对应一个明确、可信的结束原因。

**重新评估的触发条件**：若某个 Provider 引入了语义明确的新结束原因，应扩展枚举并保持同样严格的校验，而不是退回到「未知即容忍」。

## 代码与测试

- `src/codehead/agent/loop.py`（`_validate_response`）— 结束原因的接受与拒绝规则
- `src/codehead/models/llm.py`（`FinishReason`）— 枚举定义
- `src/codehead/llm/openai.py`（`_finish_reason`）— 协议值归一化
- `tests/test_agent_loop.py` — `test_rejected_response_has_no_side_effects` 覆盖零副作用保证
- `tests/test_openai_provider.py` — 覆盖结束原因归一化
