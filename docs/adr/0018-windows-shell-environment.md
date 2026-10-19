# 0018. Windows Shell 的默认模块环境与 UTF-8 标准流

- 状态：Accepted
- 日期：2026-10-18
- 来源：工作区未提交变更；提交时补实际 commit hash 与 subject

## 背景

[0015](0015-tool-system-v1-freeze.md) 保证 Windows Shell 冲刷对象输出并保留失败状态，
但没有约定模块环境和输出编码。评测 008 出现 `Get-FileHash` 不可用与中文错误乱码。
本机复现确认：经 Python 继承的 PowerShell 7 模块路径遮蔽 Windows PowerShell 5.1
系统模块；控制台输出使用 GB2312，而工具固定按 UTF-8 解码。

[微软文档](https://learn.microsoft.com/powershell/module/microsoft.powershell.core/about/about_psmodulepath)
说明，经 Python 等中间进程启动 Windows PowerShell 时，不会自动清理 PowerShell 7
模块路径，建议从子进程环境移除 `PSModulePath`。

## 决策

- 继续使用现有 Windows PowerShell 启动和 Job Object 生命周期。
- 仅在 Windows Shell 子进程环境中，按名称大小写不敏感移除 `PSModulePath`，让
  Windows PowerShell 重建默认路径；不修改宿主环境或 PATH。
- 执行用户命令前，将控制台输入、输出与 `$OutputEncoding` 设为无 BOM 的 UTF-8，
  与工具的标准流解码一致；不改变现有退出码、输出上限、取消和超时语义。

## 备选方案与取舍

| 备选 | 为什么不采用 |
| --- | --- |
| 切换或自动选择 PowerShell 7 | 引入安装要求或随宿主变化的 Shell 行为，不是修复现有执行路径所必需 |
| 过滤已知 PowerShell 7 模块目录 | 自定义安装目录难以穷举，仍可能留下不兼容同名模块 |
| 根据本机代码页或输出内容选择解码 | 不能可靠区分混合外部程序编码，且无法修复 Shell 内部的管道编码不一致 |

## 后果

宿主添加的非默认模块目录不再继承；命令需要这类模块时应显式导入。外部程序仍可
自行输出旧编码，工具不保证任意程序的输出自动变成 UTF-8，也不覆盖其 Python 编码
环境变量。此决策补充 0015，不取代其输出冲刷和错误边界。

重新评估条件：需要支持明确配置的 Shell 或自定义模块环境，或实际任务证明旧编码
程序需要独立协议时，再设计相应接口。
