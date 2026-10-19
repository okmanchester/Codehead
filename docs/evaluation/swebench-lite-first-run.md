# SWE-bench Lite 首轮评测

数据：`princeton-nlp/SWE-bench_Lite@6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`。模型：`deepseek-flash`，非思考模式。
固定三题、单并发、每题一次；此结果不代表 Lite 整体能力。

离线检查：True；环境就绪：True；评分对照：True。

| 任务 | 官方结果 | Agent 状态 | 请求 / 摘要 | 输入 / 输出 token | 缺 usage | 估算费用（元） | 推理 / 评分秒 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| sympy__sympy-20590 | resolved | budget_exhausted | 40 / 0 | 502603 / 6360 | 0 | 1.056086 | 80.0 / 21.4 |
| astropy__astropy-12907 | resolved | budget_exhausted | 40 / 0 | 490141 / 4876 | 0 | 1.019290 | 155.4 / 64.6 |
| django__django-11099 | resolved | completed | 11 / 0 | 34473 / 1382 | 0 | 0.080002 | 27.8 / 26.9 |

真实 resolved：3/3。累计估算费用：2.155378 元。
费用按全部输入未命中缓存和高峰价估算，usage 缺失使用保守预留，非服务端账单保证。

- `sympy__sympy-20590`：官方 resolved；40 次正常请求耗尽后仍成功导出并评分。第 14 步完成关键修复，第 36 步相关交互式测试 8 passed，但继续探索至第 40 步，没有最终答复。补丁扩展到 3 个文件，额外交互式打印修改的必要性未获证明。产物：`.codehead/evaluations/swebench/first-run/sympy__sympy-20590/`。
- `astropy__astropy-12907`：官方 resolved；40 次正常请求耗尽。原推理镜像遗漏官方已编译扩展，第 6、15 步导入失败，约第 8—20 步用于安装或重建环境；第 30 步完成修复，第 40 步相关测试 13 passed。该样本的步数、时长和费用受环境缺陷干扰，不能作为修正后环境的效率基线；没有补跑模型。产物：`.codehead/evaluations/swebench/first-run/astropy__astropy-12907/`。
- `django__django-11099`：官方 resolved，Agent 正常结束；11 次请求。提交两个用户名验证器的边界匹配修复及相关回归测试，最终答复如实记录验证命令与范围。产物：`.codehead/evaluations/swebench/first-run/django__django-11099/`。

本轮三题均有官方 resolved；这只验证固定三题闭环，不代表 Lite 整体能力。两题 Agent 未正常收尾与官方 resolved 同时成立，不能将评分成功记成 Agent 正常结束。本轮没有官方 unresolved 样本；复盘重点是环境准备、验证可信度和收尾行为。

驱动缺陷：首个 gold 的测试已 resolved，但官方 Harness 清理阶段依赖的 Docker CLI 缺失，且 5.0.2 日志目录实际为 logs/run_evaluation。已补齐 CLI、使用官方路径常量，并以新 run_id 重做全部对照。该缺陷发现与修复发生在付费推理前。

环境缺陷：最初只恢复 Git 源码，误删了原官方镜像中的 Astropy 编译扩展。确认时三题推理均已结束，没有可停止的模型调用，也没有追加尝试。驱动已改为仅保留项目运行二进制、翻译文件、根包版本文件与必要安装元数据；排除忽略目录中的其他源码与测试，并在免费验证中直接导入每个目标项目。修正后的环境验证结果以 runtime-validation.json 和 validation.json 为准；首轮原始结果与其 isolation.json 保留修正前镜像身份，未被新验证覆盖。

重复的验证问题：SymPy step 32 中 pytest 未安装、Astropy step 33—34 中测试报错，但通过管道尾部命令得到退出码 0，ToolCompleted 因而为 succeeded。Django step 8 的测试模块名错误也被后续自检的成功退出码覆盖，之后使用正确模块名复验通过。成功执行 Shell 不等于测试通过。Astropy 后续使用 stash 比较修复前后的广泛失败，但不能据此确认所有非目标失败的根因，原因未确认。

统计范围：91 次模型请求，全部有 usage，摘要次数为 0。输入 1,027,217 token，输出 12,618 token；按全部输入缓存未命中、高峰价 2/8 元每百万 token 估算 2.155378 元。未触发全局费用门禁；缺失 usage、摘要计数和总请求上限由离线测试覆盖，本次真实样本未触发这些分支。

输入边界复核：统一模型提示词没有数据集 ID 或参考补丁；初版私有运行配置另含数据集元信息，现已收紧为必需模型与预算字段。逐题原始工具轨迹未发现读取 /input/task.json、获取参考补丁或拉取后续 Git 历史。

重放验收：三份首次预测逐题使用新 run_id 独立评分，结果仍为 3/3 resolved；原始补丁、轨迹、usage、终态与首轮结果的 SHA-256 均未改变。重放记录见 replays/，首轮封存记录见 first-run/manifest.json。没有补跑 Agent。

工程验证：新增评测与 Provider 离线检查 54 通过；完整 pytest 438 通过、1 跳过。Ruff 检查及 src/tests/scripts 格式检查通过。全目录格式检查仅有 3 个未改动的既有文档代码块不通过：005-streaming-jsonl.md、008-targeted-edit.md、010-context-retention.md。完整证据见 verification.json、pytest.xml 和 ruff-*.log。

版本记录：控制环境与 Codehead 的依赖冻结文件分别为 control-requirements.lock、agent-requirements.lock；三个官方起始项目环境另存于 project-environments/，均关联镜像身份。首轮 Agent 临时安装或重建环境的命令、依赖版本和结果保留于各题工具轨迹。

计费依据：[DeepSeek 官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)，2026-10-19 核实；本报告使用高峰、输入未命中缓存的估算，不代表服务端账单。

下一批待办（按证据和优先级排序）：

- 环境与驱动：下一批基线先固定修正后的环境与产物检查，保留本轮三题为调试样本；不拿本轮 Astropy 的费用或步数与新环境直接比较。
- 重复的 Harness 问题：先建立管道掩盖测试失败的针对性回归，再单独验证 Shell 退出码处理方案的收益与兼容性；本轮不改变现有工具语义。
- 定位、实现与验证：优先检验及时收尾和限制额外修改范围的一个主要假设；两题耗尽 40 步，不据此直接提高步数或引入重试、多 Agent、后台 Shell。
- 成本：在环境和验证可信度稳定后，再比较请求数和输入 token；本轮不根据 3/3 结果换题、追加尝试或直接扩大到全量。

原始证据：`preflight.json`、`offline.log`、`validation.json`、`logs/run_evaluation/`、`first-run/`；均位于 `.codehead/evaluations/swebench/`。
评分可用 `validate --predictions <predictions.jsonl>` 独立重放；每次生成新 run_id。
