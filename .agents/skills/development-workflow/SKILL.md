---
name: development-workflow
description: "用于软件仓库中的开发改动：保护当前工作区，先理解现状再实施，按实际风险选择规划深度，以最小必要上下文工作，并用真实验证证据完成交付。"
---

# Development Workflow

## 核心原则

- 当前用户要求、工作区、源码、测试和实际运行结果优先于历史计划与旧文档。
- 先理解再修改；优先读取与当前任务直接相关的最小上下文，不默认扫描整个仓库或历史记录。
- 规划深度与改动风险匹配。不要因为模块名字“重要”就自动使用重流程，也不要把高风险修改当作普通小改。
- 不覆盖、回滚或整理用户已有修改，除非用户明确要求。
- 不擅自扩大范围、增加依赖、重构邻近模块或引入新的抽象。
- 完成必须建立在实际执行的验证和实际 diff 上，而不是“看起来正确”。
- 文档是为了保存仍有价值的知识，不是为了给每次改动制造流程产物。

## 主流程

1. **保护工作区**
   - 理解用户要求。
   - 检查 staged、unstaged、untracked 状态以及与当前任务相关的已有修改。
   - 明确哪些改动已经存在，避免误覆盖或误归因。

2. **理解当前系统**
   - 阅读目标源码、直接相关测试和当前有效的仓库说明。
   - 优先查找仓库自身约定，例如 `AGENTS.md`、`README`、项目配置、测试配置和架构文档。
   - 需要历史理由时再按需读取历史记录，不把 completed/legacy 内容默认塞入上下文。

3. **确定范围与规划深度**
   - 明确 goal、non-goals、affected areas、兼容性要求和已知限制。
   - 按风险、影响面、既有契约和回滚成本判断 S / M / L。
   - 需要具体分级和 ExecPlan 规则时读取 [planning-policy.md](references/planning-policy.md)。

4. **实施最小充分改动**
   - 优先沿用现有抽象和项目惯例。
   - 保持改动聚焦；发现范围外问题时记录，不顺手扩大任务。
   - 如果实施过程中出现新的架构影响、兼容风险或恢复成本，重新分级并调整计划。

5. **验证**
   - 从最快、最相关的验证开始，再根据风险扩大验证范围。
   - 验证命令优先来自当前仓库自己的配置和说明，不在 Skill 中写死项目命令。
   - 最后检查 diff、格式问题和非预期修改。
   - 具体规则见 [verification-policy.md](references/verification-policy.md)。

6. **沉淀必要知识**
   - 只有当前架构事实确实变化时才更新 architecture 类文档。
   - 只有形成持久、非显然且未来仍需遵守的决策时才创建 Decision Record。
   - 只有需要跨步骤、跨会话或高风险跟踪时才维护 living ExecPlan。
   - 具体边界见 [knowledge-policy.md](references/knowledge-policy.md)。

7. **交付**
   - 基于实际 diff 和验证结果说明：改了什么、为什么、验证了什么、失败或未验证什么。
   - 不把用户已有改动误报为本轮成果，不把计划中的验证写成已经执行。

## 渐进式加载

- 分级、重分级或决定是否创建 ExecPlan：读取 [planning-policy.md](references/planning-policy.md)。
- 判断 architecture、Decision Record、active plan 与历史记录边界：读取 [knowledge-policy.md](references/knowledge-policy.md)。
- 选择测试、检查 diff、处理失败与报告证据：读取 [verification-policy.md](references/verification-policy.md)。

不要为了“完整了解项目”预先加载全部 reference、整个文档目录、completed plans 或 legacy records。只读取当前任务真正需要的内容。
