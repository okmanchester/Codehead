# Astropy 单题 100 步复验

日期：2026-10-20。任务：`astropy__astropy-12907`，独立新会话、干净基线、单次运行。
配置见 [`tasks-astropy-100.json`](../../scripts/swebench/tasks-astropy-100.json)。

## 条件与结果

相较 [40 步复验](shell-fix-three-task-rerun.md)，只保留 Astropy 一题，将正常请求上限
40 → 100、含摘要总请求上限 50 → 125。模型、提示词、核心工具源码、控制镜像、
官方项目基础镜像和其余参数不变，包括 900 秒 Agent 超时、60 秒 Shell 超时、
累计 40 元停止与 50 元保守预留上限。没有修改完成条件或恢复命令。

| 指标 | 40 步复验 | 本次 100 步复验 |
| --- | --- | --- |
| 官方结果 | resolved | resolved |
| Agent 终态 | budget_exhausted | budget_exhausted |
| 正常请求 / 摘要请求 | 40 / 0 | 100 / 2 |
| 估算费用（元） | 1.108300 | 4.396150 |
| 最终答复 | 无 | 无 |

本次共输入 2,128,427 token、输出 17,412 token，全部 102 次请求有 usage。
停止原因为 **100 步耗尽**，未触发费用、总请求数或时间上限。
费用按高峰价与输入缓存未命中保守估算，不代表服务端账单。

离线门禁 145 passed、11 skipped；实际容器的项目导入、核心源码一致性、Shell
回归和隔离检查通过。两次 gold 均 resolved、空补丁负对照 unresolved。
推理后的补丁、轨迹、usage、终态、会话及官方评分均通过产物验收。

## 关键轨迹

- step 20、23 开始修改实现；step 44 首次压缩上下文。
- step 45 原有目标模块 11 passed；step 46 扩大测试范围后出现失败。
- step 47 `git stash` 后测试失败，`errexit` 跳过 `git stash pop`，实现修复留在 stash 中。
- step 51 添加回归测试，之后多次验证失败；step 74 发现早先修改不在工作区，却将
  原因误归为“环境不保存修改”。这一说法与实际的暂存和提前退出轨迹不符。
- step 74—80 重新应用并补齐实现；step 83 目标模块 12 passed。
- step 84—94 继续运行更广测试、收集失败集合和比较基线；85、87、88 的失败退出
  再次阻止后续命令执行，89 开始显式用 `|| true` 收集失败日志。
- step 95—97 补充 changelog；step 98 第二次上下文压缩后，目标模块 12 passed；
  step 99 doctest 8 passed；step 100 执行 `git status` 和 `git diff --stat`，随后耗尽步数。

提交补丁涉及 3 个文件：实现、回归测试和 changelog；实现中除 `_cstack` 外还修改了
`_coord_matrix`。额外修改的必要性未在本轮单独证明。

本次提高上限后仍未自然结束，恢复失误与反复验证仍消耗大量请求。但最后几步已进入
收尾检查，不能断言再增加少量步数也不会结束。两次运行是独立采样，不能把轨迹差异
全部归因于预算变化，也不能据单题推断整体能力。

## 证据与保留

原始证据位于 `.codehead/evaluations/swebench-astropy-100/`，文件哈希封存在
`first-run/manifest.json`，轨迹分析保存在 `analysis.json`，实际配置与差异在
`config.json`、`frozen-manifest.json`。按既有策略释放完整工作区和准备归档，保留
补丁、事件、SQLite 会话、usage、终态与官方评分。历史 40 步运行保持原目录。

20 题仍暂缓，本轮没有运行其他题目。
