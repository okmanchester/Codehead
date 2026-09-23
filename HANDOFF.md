# Project Handoff

## 目标
- 从零构建可运行、易理解、便于增量迭代的 Python Coding Agent，用于学习 Harness 工程机制。
- 最新子任务已完成：统一内部模型，并让 FinishReason 真正控制 AgentLoop 的结束与工具执行。

## 关键背景与决策
- 核心链路：CLI → 内存 Session → AgentLoop → LLMProvider → ToolRegistry → ToolResult → ToolMessage → 下一轮模型请求。
- 跨模块契约统一从 `codehead.models` 导入；Provider 接口是 `complete(LLMRequest) -> LLMResponse`。旧 `session.message`、`ToolSchema`、`schema()/schemas()` 和旧 complete 签名已移除，不保留兼容层。
- Message 是按 role 判别的 SystemMessage / UserMessage / AssistantMessage / ToolMessage 联合；Registry 返回独立 ToolResult，由 Loop 转为 ToolMessage。
- 领域对象采用 frozen Pydantic 模型，messages/tools/tool_calls 使用 tuple；Session 和 Registry 内部仍可变。JSON 参数使用递归类型与边界防御性复制，仅结构级不可变，嵌套 dict/list 仍可修改。
- LLMResponse 包含 assistant message、标准 FinishReason 和可选 TokenUsage。模型名、base URL、thinking 设置留在 Provider。
- END_TURN 必须有非空白文本且无工具调用；TOOL_CALLS 必须有调用，允许附带文本。MAX_TOKENS、CONTENT_FILTER、UNKNOWN 均抛 ModelError。
- UNKNOWN 的最终决定是严格报错，取代早期“未知值不中断”的兼容设想。Provider 仍将缺失或未知协议值映射成 UNKNOWN，Loop 负责拒绝。
- 被拒绝的响应不写入 Session、不执行工具、不继续请求；已有历史和当前 user message 保留。空 AssistantMessage 可构造，但必须通过 Loop 的语义检查。
- 当前没有自动续写、流式、重试、持久化、多模态或 Provider 路由；这些均未纳入本轮。

## 当前状态

### 已完成
- 同步、非流式 Chat Completions 适配器：默认 deepseek-flash / https://api.deepseek.com，从 DEEPSEEK_API_KEY 读取密钥，thinking disabled，SDK max_retries=0。
- read/write/edit/bash 与单 Session 交互 CLI 已接通。文件工具限制在 workspace；bash 使用 Windows PowerShell / Unix Bash，默认超时 60 秒，移除子进程环境中的 DEEPSEEK_API_KEY，但不提供沙箱。
- 统一 Request / Response / Message / Tool Calling 模型，以及 usage 和 finish reason 映射均已接入运行链路。
- Loop 按 FinishReason 分支，校验后才保存消息或执行工具；工具错误仍回传模型，模型调用最多 20 次。
- 相应测试覆盖正常回答、文本伴随工具调用、多工具顺序、工具错误、拒绝响应的副作用隔离和步数限制。

### 进行中
- 无。

### 阻塞 / 未确认
- 无已知阻塞；用户尚未指定下一项开发功能。
- 最新 FinishReason 控制流修正通过 fake provider 验证，未重跑真实 API；之前的真实 smoke test 不覆盖该修正。

## 验证
- 以下为本会话最近一次实现后的结果；本次 handoff 仅检查仓库并写文档，未重复运行测试。
- `conda run -n codehead python -m pytest tests/test_agent_loop.py` → 通过，26 passed。
- `conda run -n codehead python -m pytest` → 通过，73 passed。
- `conda run -n codehead ruff check .` → 通过。
- `git diff --check` → 通过，仅有 Git 换行转换提示。
- 统一模型迁移后的真实 DeepSeek CLI smoke test → 文本返回 models-ok，write/read 读回 unified，临时文件经磁盘核对后已清理；执行时间早于 FinishReason 修正。

## 待处理事项
- 当前任务已闭环，无待补实现。开始新功能前，先确认用户下一步目标。
- 若未来调整结束原因或异常恢复策略，优先检查 `agent/loop.py` 与 `tests/test_agent_loop.py`，继续验证拒绝响应不执行工具、不污染历史的约束。
- 若未来需要验证严格 FinishReason 与真实服务的兼容性，使用仓库外临时 workspace 验证文本和工具调用，避免发送项目上下文；不输出密钥，结束后清理测试目录。
- 这些内容仅用于恢复上下文，不代表用户已经授权执行后续开发或额外联网验证。

## 关键文件
- `src/codehead/models/` — 统一类型入口、角色消息、工具契约、FinishReason 和 TokenUsage。
- `src/codehead/agent/loop.py` — 结束原因校验、消息入库时机、工具结果回传和步数上限。
- `src/codehead/llm/openai.py` — 内部模型与 Chat Completions 协议转换、usage/finish reason 映射、ModelError 包装。
- `src/codehead/tools/registry.py` — ToolDefinition 汇总、ToolCall 分派及 arguments 防御性复制。
- `src/codehead/session/session.py` — 内存历史；内部 list，对外返回 tuple。
- `src/codehead/cli.py` — 依赖装配与 REPL；领域错误显示后以非零状态退出。
- `tests/test_agent_loop.py` — 最新 FinishReason 行为与副作用隔离测试；模型和 Provider 测试分别位于 test_models.py、test_openai_provider.py。

## 仓库状态与注意事项
- 分支：main。
- HEAD：d300282，完成内部数据模型初步搭建，含message, response, tool calling相关数据结构。
- 工作区：生成本文前 clean，无 staged 或 unstaged diff；生成后仅新增 HANDOFF.md，未自动 commit。
- 与前次聊天状态不同：此前未提交的模型迁移和 Loop 修正现已包含在当前提交中，以仓库状态为准。
- HANDOFF.md 未被 Git 忽略，可被加入版本控制；本次未修改 .gitignore。
