# Windows 兼容问题验证报告

本报告固化上一轮针对 Hermes Agent Windows 兼容问题的源码验证结论，供后续插件开发与追踪使用。本报告仅整理已验证的事实，不包含新的验证结果。

## 1. 概述

- 审计日期：2026-08-11
- 审计对象：Hermes Agent（源码位于 C:\Users\Admin\AppData\Local\hermes\hermes-agent，只读参考）
- 源码版本：当前源码分支 fix/linter-native-path-windows
- 审计方法：基于当前源码进行静态验证与实测（命令实测、代码路径核查、合并状态确认）
- 项目背景：D:\projects\windows-compat 为 Hermes Agent 的 Windows 兼容插件仓库（git 仓库），本报告为上一轮审计验证结论的固化文档

## 2. 分类总表

| 分类 | 条目数 | 涉及问题编号 |
| --- | --- | --- |
| 已修复（上游已修复，验证通过，不需要插件 fix） | 8 | #38964、#71890、#51474、#66718、#62148/#60857/#62514、#62998/#44726/#39834、#35388、#45542 |
| 已插件修复（确认仍存在，由并行子代理实现插件 fix） | 7 | #31738、#69373、#78079、#61444、#61923、#34185、#57081 |
| 不可修复（无代码 fix，记录原因与建议） | 6 | #55004/#70544、#54927、#68541、#45435、#39251、#64983/#51215 |
| 合计 | 21 个条目（27 个问题编号） | — |

## 3. 问题详情

### 3.1 已在上游修复（验证通过，不需要插件 fix）

#### #38964 盘符危险命令审批

- 验证结论：已修复
- 证据：approval.py 已有 drive-root 折叠 + home prefix fold；实测 `rm C:/test.txt`、`del`、`taskkill`、`cmd.exe /c del` 全部被 BLOCKED
- 备注：无需插件 fix

#### #71890 DANGEROUS_PATTERNS 反斜杠绕过

- 验证结论：已修复
- 证据：approval.py 的 `_normalize_command_for_detection` 已有 backslash-escape strip + home fold + IFS collapse
- 备注：无需插件 fix

#### #51474 凭据 deny 大小写

- 验证结论：已修复
- 证据：agent/file_safety.py 的 `get_read_block_error` 使用 `Path.resolve()`（Windows resolve 归一化大小写）；实测大小写变体 equal
- 备注：无需插件 fix

#### #66718 os.kill(pid, 0) WinError 87

- 验证结论：已修复
- 证据：gateway/status.py、gateway/run.py、iron_proxy.py 已有多处 windows-footgun 注释与防护
- 备注：无需插件 fix

#### #62148 / #60857 / #62514 cron 路径反斜杠

- 验证结论：已修复
- 证据：cron/scheduler.py 的 `_run_job_script` 已使用 `Path(script_path).expanduser()` 处理
- 备注：无需插件 fix

#### #62998 / #44726 / #39834 MSYS 路径错写 C:\c\Users

- 验证结论：已修复
- 证据：file_tools.py 已有 `_msys_to_windows_path` 归一化
- 备注：无需插件 fix

#### #35388 MEDIA 标签盘符路径

- 验证结论：已修复
- 证据：上游已合并（S9 确认 merged）
- 备注：无需插件 fix

#### #45542 update-incomplete 自锁

- 验证结论：已修复
- 证据：update_cmd.py 已有 marker + lock 处理
- 备注：无需插件 fix

### 3.2 确认仍存在，由并行子代理实现插件 fix

以下问题经源码验证确认仍存在，无法依赖上游修复，已由并行子代理实现对应插件 fix。FIX_ID 与问题编号对应如下：

#### #31738 fix_memory_lock

- 验证结论：已插件修复
- 证据：确认问题仍存在；FIX_ID: fix_memory_lock
- 备注：修复由并行子代理实现，效果需在插件落地后回归验证

#### #69373 fix_device_path_guard

- 验证结论：已插件修复
- 证据：确认问题仍存在；FIX_ID: fix_device_path_guard
- 备注：修复由并行子代理实现，效果需在插件落地后回归验证

#### #78079 fix_sensitive_path_guard

- 验证结论：已插件修复
- 证据：确认问题仍存在；FIX_ID: fix_sensitive_path_guard
- 备注：修复由并行子代理实现，效果需在插件落地后回归验证

#### #61444 fix_mcp_selector_loop

- 验证结论：已插件修复
- 证据：确认问题仍存在；FIX_ID: fix_mcp_selector_loop
- 备注：修复由并行子代理实现，效果需在插件落地后回归验证

#### #61923 fix_kanban_zombie

- 验证结论：已插件修复
- 证据：确认问题仍存在；FIX_ID: fix_kanban_zombie
- 备注：修复由并行子代理实现，效果需在插件落地后回归验证

#### #34185 fix_uninstall_rmtree

- 验证结论：已插件修复
- 证据：确认问题仍存在；FIX_ID: fix_uninstall_rmtree
- 备注：修复由并行子代理实现，效果需在插件落地后回归验证

#### #57081 fix_update_nul_preflight

- 验证结论：已插件修复
- 证据：确认问题仍存在；FIX_ID: fix_update_nul_preflight
- 备注：修复由并行子代理实现，效果需在插件落地后回归验证

### 3.3 不可插件修复（无代码 fix，记录原因与建议）

#### #55004 / #70544 Smart App Control / WDAC 拦截未签名 exe

- 验证结论：不可修复
- 原因：属于系统策略层（Smart App Control / WDAC）拦截，插件无法绕过
- 建议：安装后用 PowerShell `Add-MpPreference` 排除目录，或等待上游对 exe 签名

#### #54927 winpty-rs 中文 Windows panic

- 验证结论：不可修复
- 原因：第三方 Rust 依赖 bug，超出插件可 patch 范围
- 建议：等待上游 pywinpty 修复

#### #68541 桌面 exe Authenticode 签名

- 验证结论：不可修复
- 原因：需要签名基础设施；维护者已拒绝不完整方案
- 建议：无插件侧方案，跟进上游签名计划

#### #45435 findGitBash 硬编码 5 个路径

- 验证结论：不可修复
- 原因：位于 Electron/TS 代码（apps/desktop），Python 插件无法 patch
- 建议：设置 `HERMES_GIT_BASH_PATH` 环境变量（上游 PR #38044 方案）

#### #39251 terminal/write_file 0x80070022

- 验证结论：不可修复
- 原因：环境特定问题（Win10 Pro 22H2 + 特定 shell），无稳定复现
- 建议：建议用户升级系统或更换 shell

#### #64983 / #51215 桌面 GUI 多进程残留 + WAL 锁

- 验证结论：不可修复
- 原因：属于 Electron 层进程管理问题，Python 插件难以介入
- 建议：跟进上游 Electron 层修复

## 4. 审计范围与局限

- 检索范围：GitHub Search API 采样 5000 条；其中深挖 300+ 条
- 源码基线：当前源码分支 fix/linter-native-path-windows，审计日期 2026-08-11
- 验证方式：以源码静态核查与命令实测为准；未重新执行完整回归
- 局限：
  - Search API 为采样而非全量枚举，可能存在未覆盖的 Windows 兼容问题
  - 验证基于当前分支快照，上游后续变更可能使"已修复"结论过期
  - 插件 fix 条目（3.2 节）由并行子代理实现，本轮仅确认问题存在与 fix 分配，插件落地后的实际效果需另行回归验证
  - 环境特定问题（如 #39251）无稳定复现环境，结论以现场证据为准
