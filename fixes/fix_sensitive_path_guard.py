"""P1: 修复 Windows 上敏感路径写守卫（_check_sensitive_path）整体失效。

参考链接：
- PR #78079（本问题，被关闭为重复）:
  https://github.com/NousResearch/hermes-agent/pull/78079
- superset PR #76247（两者均未合并）:
  https://github.com/NousResearch/hermes-agent/pull/76247
- 根因同类 issue #51474（大小写不敏感文件系统绕过 —— 注意：本 fix 只管
  反斜杠归一化，不混入大小写折叠逻辑）

根因：
tools/file_tools.py 的 _check_sensitive_path() 对 resolved / normalized 用
os.path.normpath(...) 归一化后与 POSIX 前缀（'/etc/'、'/boot/'、
'/usr/lib/systemd/'、'/private/etc/' 等）做 startswith 比对。
Windows 上 os.path.normpath('/etc/hosts') 返回 '\\etc\\hosts'
（反斜杠分隔），startswith('/etc/') 恒为 False → 前缀守卫整体失效，
agent 可绕过审批写任意系统文件。

修复方式（鲁棒、保守，additive-only）：
- monkeypatch _check_sensitive_path 为包装函数；先调用原函数，原函数判定
  敏感则直接返回 → 保留其全部原有行为，POSIX 行为不变。
- 原函数放行时再做补充比对：把 resolved / normalized 统一「反斜杠→正斜杠」
  归一化（前缀与 _SENSITIVE_EXACT_PATHS 同样归一化），并额外生成「去掉
  盘符」的变体（C:/etc/hosts -> /etc/hosts）再比对前缀/精确路径。
- 附加最小 Windows 系统目录前缀 C:/Windows/（可选的保守增强，仅一条）。
- 检测逻辑：tools.file_tools 可 import、_check_sensitive_path 源码中不含
  正斜杠归一化处理（本插件 marker 或 .replace("\\", "/") 形态）时才 patch；
  已修复则跳过并返回 True，不重复修复。

保守性说明：补充检查只会「多拦截」不会「少拦截」；被拦截的写入仍可通过
terminal 工具（带审批）执行，不阻断任何合法操作路径。
"""

from __future__ import annotations

import inspect
import logging
import os
import re

logger = logging.getLogger(__name__)

PRIORITY = 1
RISK = "low"
DESCRIPTION = (
    "Restore Windows file-write sensitive-path guard: Hermes' _check_sensitive_path "
    "compares os.path.normpath output (backslash form on Windows) against POSIX "
    "prefixes (/etc/, /boot/, ...) so every prefix check misses and agents could "
    "write arbitrary system files. Wrap it: original runs first (POSIX unchanged), "
    "then re-check backslash->slash normalized resolved/normalized paths plus "
    "drive-stripped variants, and an added C:/Windows/ system-dir prefix. "
    "See PR #78079 / #76247 / issue #51474."
)
FIX_ID = "sensitive_path_guard"

# 本插件安装标记：出现在被 patch 函数（wrapper）源码中，用于「已修复则跳过」检测
_MARKER = "windows-compat[sensitive_path_guard]"

# 已修复形态检测：源码含反斜杠→正斜杠归一化（.replace("\\", "/")）即视为已修复
_POSIXIFY_RE = re.compile(r'\.replace\(\s*["\']\\\\["\'],\s*["\']/["\']\s*\)')

# 附加的 Windows 系统目录前缀（最小侵入：仅系统根目录；其余子树可按需扩展）。
# POSIX 上形如 C:/Windows/... 的相对路径几乎不存在，即使误拦也只是退回
# terminal 审批通道，方向安全。
_WIN_SENSITIVE_PREFIXES = ("C:/Windows/",)

_ERR_MSG = (
    "Refusing to write to sensitive system path: {filepath}\n"
    "Use the terminal tool with sudo if you need to modify system files."
)

_DRIVE_RE = re.compile(r"^([A-Za-z]):/(.*)$")


def _posixify(p: str) -> str:
    """反斜杠 → 正斜杠：Windows os.path.normpath 产出 \\ 分隔形态，统一后再比对前缀。"""
    return p.replace("\\", "/")


def _strip_drive(p: str) -> str:
    """去掉盘符前缀：C:/etc/hosts -> /etc/hosts（输入须已 posixify）。"""
    m = _DRIVE_RE.match(p)
    if m:
        return "/" + m.group(2)
    return p


def _build_patched(file_tools, orig):
    """构造包装函数：原函数优先 + 正斜杠归一化补充比对。

    _SENSITIVE_PATH_PREFIXES / _SENSITIVE_EXACT_PATHS / _resolve_path_for_task /
    _expand_tilde 均在调用时从模块实时读取，避免安装时快照漂移。
    """
    _resolve = getattr(file_tools, "_resolve_path_for_task", None)
    _expand = getattr(file_tools, "_expand_tilde", os.path.expanduser)

    def wrapper(filepath: str, task_id: str = "default") -> str | None:
        """windows-compat[sensitive_path_guard]：orig-first + 反斜杠归一化补充检查。"""
        # 1) 原函数优先：保留全部原有行为（POSIX 路径、hermes config 保护等）
        err = orig(filepath, task_id)
        if err is not None:
            return err
        # 2) 补充检查：Windows 上 normpath 产出反斜杠形态导致 startswith 失效，
        #    统一归一化为正斜杠后再比对前缀/精确路径（前缀同样归一化）。
        try:
            resolved = str(_resolve(filepath, task_id)) if _resolve else filepath
        except (OSError, ValueError):
            resolved = filepath
        try:
            normalized = os.path.normpath(_expand(filepath))
        except Exception:
            normalized = filepath
        cands = {
            _posixify(resolved),
            _posixify(normalized),
            _strip_drive(_posixify(resolved)),
            _strip_drive(_posixify(normalized)),
        }
        try:
            prefixes = tuple(_posixify(p) for p in file_tools._SENSITIVE_PATH_PREFIXES) + _WIN_SENSITIVE_PREFIXES
            exact = {_posixify(p) for p in file_tools._SENSITIVE_EXACT_PATHS}
        except AttributeError:
            # 模块结构异常：保守降级，仅依赖原函数行为
            return None
        for prefix in prefixes:
            if prefix and any(c.startswith(prefix) for c in cands):
                return _ERR_MSG.format(filepath=filepath)
        if cands & exact:
            return _ERR_MSG.format(filepath=filepath)
        return None

    return wrapper


def install() -> bool:
    """patch tools.file_tools._check_sensitive_path；已修复则跳过。False = 延迟重试。"""
    try:
        from tools import file_tools

        orig = file_tools._check_sensitive_path
        if not callable(orig):
            return False  # 结构变化（AttributeError 形态），延迟重试
        # 检测：源码已含正斜杠归一化处理 → 上游或本插件已修复，跳过
        try:
            src = inspect.getsource(orig)
        except (TypeError, OSError):
            src = ""
        if _MARKER in src or _POSIXIFY_RE.search(src):
            logger.info("windows-compat[sensitive_path_guard]: already fixed — skip")
            return True
        file_tools._check_sensitive_path = _build_patched(file_tools, orig)
        logger.info(
            "windows-compat[sensitive_path_guard]: patched _check_sensitive_path "
            "(backslash-normalized prefix guard restored on Windows)"
        )
        return True
    except (ImportError, AttributeError) as exc:
        logger.debug("windows-compat[sensitive_path_guard]: deferred (%s)", exc)
        return False
