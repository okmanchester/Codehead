# Shell 失败传播修复：原三题复验

日期：2026-10-20。原三题各重跑一次，未改变完成条件或修改范围约束。
数据与 revision 沿用首轮；`deepseek-flash` 非思考模式，40 步、含摘要最多 50 次请求，
其他上下文、输出和超时参数不变。配置见
[`tasks-shell-fix.json`](../../scripts/swebench/tasks-shell-fix.json)。

## 修复与验收

Unix Bash 在登录初始化后、用户命令前启用 `errexit + pipefail`。
旧实现对新增回归有 5 项误报；修复后相同 10 项全部通过。
回归经过真实 BashTool / ToolRuntime，覆盖管道、后续成功命令、显式恢复、条件、
AND 列表、SIGPIPE 和含错误文字但退出成功的命令。Windows 行为保持不变。
取舍见 [ADR-0024](../adr/0024-unix-shell-failure-status.md)。

三题组付费前已通过离线检查、逐题项目导入、实际安装源码一致性、Shell 回归和
容器隔离检查；7 次官方 gold / 空补丁负对照均符合预期。重建使用首轮 Agent 依赖
版本约束，付费门禁关联配置、驱动、核心源码、控制镜像和逐题推理镜像。

## 三题结果

| 任务 | 本轮官方结果 | 首轮收尾 / 请求数 | 本轮收尾 / 请求数 | 本轮估算费用（元） |
| --- | --- | --- | --- | --- |
| sympy__sympy-20590 | resolved | 步数耗尽 / 40 | 正常完成 / 33 | 0.693798 |
| astropy__astropy-12907 | resolved | 步数耗尽 / 40 | 步数耗尽 / 40 | 1.108300 |
| django__django-11099 | resolved | 正常完成 / 11 | 正常完成 / 10 | 0.069074 |

官方 resolved 为 3/3，Agent 正常收尾为 2/3。共 83 次请求、0 次摘要，
输入 884,574 token，输出 12,753 token，全部有 usage；累计估算 **1.871172 元**。
按全部输入缓存未命中、高峰价输入 2 元 / 输出 8 元每百万 token 保守计费，
2026-10-20 已核实[官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。
估算不等于服务端账单；本轮未触及累计 40 元停止、50 元保守预留上限。

首轮 Astropy 曾受编译扩展遗漏影响，本轮使用修正后的环境。
前后比较仅用于诊断，不能归因为 Shell 修复带来的效率收益，也不代表 Lite 整体能力。
首轮报告和原始证据保持原目录，未覆盖或追加其尝试。

## 轨迹证据

- SymPy step 15：`python -m pytest ... | tail` 因 pytest 未安装返回 exit 1 / failed；
  Agent 随后改用项目 `bin/test` 验证并正常收尾。补丁包含实现与回归测试两个文件。
- Astropy step 34：目标测试通过，但同次命令中的另一个测试失败，经 `tail` 后仍返回
  exit 1 / failed。step 39 目标测试 14 passed；step 40 继续运行更广测试并失败，
  随即耗尽步数，无最终答复。更广失败的根因未在本轮逐项确认。
- Astropy step 36：临时 checkout 后测试失败，errexit 使分号后的恢复未执行；
  Agent 后续恢复修复，step 39 目标测试重新通过。这体现失败即停的兼容性代价，
  需要保证执行的收尾操作应显式处理失败或使用 trap；工具不回滚副作用。
- Django 本轮使用正确模块名：验证器 22 项及 auth_tests 494 项测试通过，10 步完成。
  历史错误模块名场景本次未重现，对应失败传播由离线回归覆盖。

失败管道已在真实轨迹中如实暴露；Astropy 仍显示目标修复通过后继续探索的收尾问题。
完成条件与修改范围约束留待后续各自单独实验。

## 工程检查与证据

- Windows 完整 pytest：548 passed、12 skipped；Linux：546 passed、14 skipped。
- 评测离线门禁：145 passed、11 skipped。Ruff 检查及 src/tests/scripts 格式检查通过。
- 三题补丁、轨迹、usage、终态、会话及官方评分均通过产物验收；最终文件哈希保存在
  `first-run/manifest.json`。按保留策略释放完整工作区及准备归档后，清理预览无候选。

本轮原始证据：`.codehead/evaluations/swebench-shell-fix/`；工程验证日志：
`.codehead/evaluations/shell-verification/`，均已被现有 `.gitignore` 覆盖。

## 20 题状态

[`tasks-20.json`](../../scripts/swebench/tasks-20.json) 已冻结 20 题、顺序、100 步与
含摘要 125 次请求配置，覆盖 12 个仓库并排除原三题。
按用户最新指示，本次停止镜像准备，**20 题付费推理次数为 0**；未运行基线。
保留固定清单、部分已下载镜像及下载失败记录，供后续安排。
