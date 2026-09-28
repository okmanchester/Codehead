# 架构决策记录（ADR）

本目录记录 Codehead 的架构决策：**为什么这样决定、否决了哪些备选、代价是什么、什么条件下应该重新评估**。

## 与其他文档的分工

| 文档 | 回答的问题 | 不要写什么 |
| --- | --- | --- |
| `docs/adr/` | 为什么这样决定；备选与取舍；代价与已知缺口 | 当前行为的完整清单 |
| `README.md` | 当前行为是什么（事实性规格） | 决策的论证过程 |
| `HANDOFF.md` | 当前开发状态 | 决策的复述（只引用 ADR 编号） |

三者会互相引用，但不重复内容。ADR 是决策的**唯一存档位置**；行为发生变化时更新 `README.md`，决策发生变化时新增或取代 ADR。

## 编号与文件命名

- 文件名：`NNNN-<kebab-case-slug>.md`，`NNNN` 为四位零填充序号。
- 序号**只增不减、永不复用**，即使某篇被取代或废弃。
- 新决策取当前最大序号 + 1，按时间追加在最后。

## 状态机

每篇 ADR 头部必须有 `状态` 字段，取值：

| 状态 | 含义 |
| --- | --- |
| `Accepted` | 当前有效，是实现的依据 |
| `Superseded by [NNNN](NNNN-....md)` | 已被新决策取代；文件保留，正文不改 |
| `Deprecated` | 不再适用，但没有替代决策 |
| `Rejected` | 曾经考虑并明确否决，留档以免重复讨论 |

### 取代旧决策的操作规程

**旧 ADR 永远不删除，正文也不改写**——决策的历史和被推翻的理由本身就是有价值的信息。

1. 新增一篇 ADR 记录新决策。
2. 新 ADR 的 `## 背景` 必须链接被取代的旧 ADR，并说明为什么原决策不再成立。
3. 旧 ADR 只改 `状态` 一行：`Superseded by [NNNN](NNNN-slug.md)`。其余内容一字不动。
4. 更新下方索引表中两篇的 `状态` 列。

## 何时需要新增 ADR

满足以下任一条即**必须**记录；都不满足时**不要**创建（避免为流程造文档）：

- 改变模块边界或分层，例如新增/合并顶层模块。
- 改变跨模块契约：领域模型的字段语义、消息/事件类型、工具接口。
- 改变持久化格式或数据生命周期（数据库 schema、存档策略、恢复条件）。
- 新增或移除外**部**依赖（含非 Python 依赖，如 `rg`）。
- 改变失败、错误或中断语义（什么算失败、失败后是否重试、副作用是否回滚）。
- 改变安全边界（文件访问范围、进程隔离、凭据处理）。

以下情况**不需要** ADR：局部实现细节、bug 修复、不改变契约的重构、测试补充、文案与日志调整。

## 演进方式

ADR 是**增量演进**的，不是一次性快照：

- 决策**不变**时，不新增文件，也不"刷新"内容。
- 决策**扩展**（原有结论仍成立，只是加了新约束）时，优先新增一篇 ADR 并在 `## 背景` 中链接前一篇，而不是改写旧文。
- 决策**被推翻**时，走上面的取代流程。

这样，任何时刻的架构都由"全部 `Accepted` 的 ADR 之和"决定，历史决策链完整可追溯。

## 索引

| 编号 | 标题 | 状态 | 日期 | 来源 |
| --- | --- | --- | --- | --- |
| [0001](0001-self-built-harness.md) | 自实现 Harness，不引入 Agent Framework | Accepted | 2026-09-21 | `e2c3dee` |
| [0002](0002-unified-domain-models.md) | 统一领域模型层 `codehead.models` | Accepted | 2026-09-22 | `d300282` |
| [0003](0003-strict-finish-reason.md) | FinishReason 严格语义与零副作用拒绝 | Accepted | 2026-09-22 | `d300282` |
| [0004](0004-async-streaming-commit-after-validation.md) | 异步流式事件与"校验后提交" | Accepted | 2026-09-23 | `873ae9d` |
| [0005](0005-tool-runtime-and-output-limits.md) | 工具统一经 `ToolRuntime` 执行与分层输出上限 | Superseded by [0015](0015-tool-system-v1-freeze.md) | 2026-09-23 | `0109042` |
| [0006](0006-workspace-boundary-no-sandbox.md) | workspace 文件边界、无沙箱与环境清理 | Superseded by [0014](0014-tool-permissions.md) | 2026-09-22 | `afa3983`、`1e05fd2` |
| [0007](0007-tool-behavior-tradeoffs.md) | 工具行为取舍：read 分页、edit 唯一精确匹配、检索依赖 ripgrep | Accepted | 2026-09-23 | `70af0ec`、`ce3f600` |
| [0008](0008-append-only-archive.md) | append-only SQLite 存档与活跃上下文分离 | Accepted | 2026-09-23 | `7faa835` |
| [0009](0009-estimated-context-compaction.md) | 估算驱动的阈值自动压缩 | Accepted | 2026-09-23 | `7faa835` |
| [0010](0010-resume-contract.md) | 恢复契约：只有正常退出的会话可恢复 | Accepted | 2026-09-23 | `6273528` |
| [0011](0011-turn-tool-lifecycle.md) | Turn / Tool 生命周期与原子提交边界 | Accepted | 2026-09-23 | `1e05fd2` |
| [0012](0012-cancellation-and-process-trees.md) | 可控中断语义与跨平台进程树回收 | Accepted | 2026-09-23 | `1e05fd2` |
| [0013](0013-sync-provider-path.md) | 同步、非流式的 Provider 调用路径 | Superseded by [0004](0004-async-streaming-commit-after-validation.md) | 2026-09-22 | `afa3983`、`d300282` |
| [0014](0014-tool-permissions.md) | 工具调用权限与会话授权 | Accepted | 2026-09-24 | 工作区未提交变更；提交时补来源 |
| [0015](0015-tool-system-v1-freeze.md) | 工具系统 v1 冻结：参数契约、文件提交与错误边界 | Accepted | 2026-09-24 | 工作区未提交变更；提交时补来源 |

模板见 [template.md](template.md)。

## 回填说明

首批 13 篇依据 `e2c3dee`..`1e05fd2` 的完整提交历史回填，不是按时间顺序新写。回填规则：

- 只记录能从**提交内容、提交信息或当时的 `HANDOFF.md`** 中确证的内容；动机无法确证时不写。
- 代码引用中的行号是**成文时的事实**，不作为长期契约；行号漂移时以符号名为准。
- 回填是一次性例外。之后只按「何时需要新增 ADR」的条件增量新增，**不要为已有代码补写描述性的 ADR**。

回填时 [0013](0013-sync-provider-path.md) 是唯一一篇 `Superseded`——它记录了真实落地过、随后被推翻的同步调用路径。[0003](0003-strict-finish-reason.md) 还记录了一条**从未落地**的设想（「未知结束原因不中断」），因为它从未成为代码，所以不单独成文，改记在该篇的备选方案中。这两处的区别是刻意的：只有真实存在过的决策才占用编号。
