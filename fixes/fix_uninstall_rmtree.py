"""fix_uninstall_rmtree — Windows 卸载残留数据修复（Hermes issue #34185）。

问题背景
--------
``hermes uninstall``（全量模式）用 ``shutil.rmtree`` 删除安装目录、
``$HERMES_HOME`` 数据目录、profile 目录等（见 ``hermes_cli/uninstall.py`` 的
``_perform_uninstall`` / ``_uninstall_profile`` / ``remove_portable_tooling_windows``，
以及 ``hermes_cli/gui_uninstall.py`` 的 ``_remove_path``）。Windows 上有三类失败：

1. 只读文件：``rmtree`` 删文件时抛 ``PermissionError``（POSIX 删只读文件只要求
   目录可写，故无此问题）。而 ``rmtree`` 一旦在某条目上抛异常就立即中止整棵
   目录删除 —— 只删了一半，剩余全部残留。
2. 运行中进程锁定的文件（``hermes.exe`` / ``pythonw.exe`` / ``.pyd`` / venv 里
   正在运行的 ``python.exe``）：Windows 对运行中的可执行文件强制加锁，
   ``unlink`` 必然失败。
3. 其他进程（杀毒、索引器）短时占用的句柄未释放。

上游现状（2026-08 检查）：``hermes_cli/uninstall.py`` 与 ``gui_uninstall.py`` 的
``rmtree`` 调用均为裸调用（无 chmod / onerror / 重试），问题真实存在。社区补丁
PR #34364（robust rmtree with retries，open 未合并）、#50455（closed 未合并）
均未落地，故本插件自行修复。

修复方式（鲁棒、保守）
--------------------
用 ``_ShutilProxy`` 替换 ``hermes_cli.uninstall``（及 ``hermes_cli.gui_uninstall``）
模块命名空间里的 ``shutil`` 模块对象：这两个模块内部的所有 ``shutil.rmtree(...)``
调用改走 ``_rmtree_win`` —— 给 stdlib ``rmtree`` 注入 onerror 回调，遇只读路径
先 ``os.chmod(path, 0o777)`` 清掉只读属性再重试（最多 ``MAX_RETRIES`` 次）；
重试耗尽（真被进程锁死）则跳过该路径并记录告警，绝不中断整棵目录的删除，
从而把"一个坏文件 → 全树残留"降级为"一个坏文件 → 仅该文件残留"。

- 只替换目标模块命名空间内的引用，全局 ``shutil`` 不动，其他模块不受影响。
- 只影响 Windows（``sys.platform == "win32"``）；POSIX 分支原样返回 True。
- 幂等：重复 install() 不会二次包裹。
- 若上游源码已自带只读重试（检测 chmod / S_IWRITE / _rmtree_win 标记）则跳过。

参考
----
- https://github.com/NousResearch/hermes-agent/issues/34185
- https://github.com/NousResearch/hermes-agent/pull/34364 （open，未合并）
- https://github.com/NousResearch/hermes-agent/pull/50455 （closed，未合并）
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import shutil
import sys
import time

logger = logging.getLogger(__name__)

PRIORITY = 4
RISK = "low"
DESCRIPTION = (
    "Windows 上 hermes uninstall 全量卸载残留大量数据（issue #34185）："
    "shutil.rmtree 遇只读/被锁文件抛 PermissionError 即中止整棵目录删除。"
    "patch hermes_cli.uninstall/gui_uninstall 模块内 shutil.rmtree 为鲁棒版："
    "onerror 先 chmod 0o777 清只读再重试 3 次，仍失败（进程锁定）则跳过并记录，"
    "不中断卸载。仅 Windows，POSIX 不受影响。"
)

# 运行平台快照（测试可 monkeypatch 本模块的 _PLATFORM 以模拟 POSIX）
_PLATFORM = sys.platform

# 真实 stdlib rmtree：模块导入时捕获，避免被后续任何 patch 污染
_REAL_RMTREE = shutil.rmtree

MAX_RETRIES = 3      # 每个失败路径的重试次数
RETRY_DELAY = 0.15   # 重试间隔（秒），给杀毒/索引器释放瞬时句柄的时间
_CHMOD_MODE = 0o777  # Windows 上 chmod 只影响只读属性；0o777 一并清掉


def _record(log, msg):
    """记录跳过告警：优先走目标模块的 log_warn（卸载输出里可见），否则走 logging。"""
    if log is not None:
        try:
            log(msg)
            return
        except Exception:
            pass
    logger.warning(msg)


def _handle_rmtree_error(func, path, exc_info, *, log=None):
    """shutil.rmtree 的 onerror 回调：先 chmod 清只读，再重试；耗尽则跳过。

    签名与 rmtree 约定的 ``onerror(func, path, exc_info)`` 一致。回调正常返回
    （不抛异常）即告知 rmtree 继续处理其余条目 —— 单个坏文件不会中止整棵删除。
    """
    exc = exc_info[1] if isinstance(exc_info, tuple) else exc_info
    if not isinstance(exc, OSError):
        return
    if isinstance(exc, FileNotFoundError):
        return  # 已被并发进程删除，视为成功
    for _ in range(MAX_RETRIES):
        try:
            # Windows：先去掉只读属性（只读是 rmtree 失败的头号原因）
            os.chmod(path, _CHMOD_MODE)
            func(path)  # 重试原操作（unlink / rmdir / scandir）
            return
        except OSError:
            # 仍失败：可能是进程锁（hermes.exe / .pyd / pythonw.exe）或句柄未释放
            time.sleep(RETRY_DELAY)
    # 重试耗尽：跳过并记录，不中断整个卸载
    _record(log, f"跳过无法删除的路径（可能被进程锁定）: {path} ({exc})")


def _rmtree_win(path, ignore_errors=False, onerror=None, *, _real_rmtree=None, _log=None):
    """Windows 鲁棒 rmtree：只读文件先 chmod 再重试，锁死则跳过并记录。

    - 非 Windows：原样委托真实 rmtree（含调用方自定义 onerror）。
    - 调用方未提供 onerror 时注入容错回调。
    - ``_real_rmtree`` / ``_log`` 为测试注入点。
    """
    real = _REAL_RMTREE if _real_rmtree is None else _real_rmtree
    if _PLATFORM != "win32":
        return real(path, ignore_errors=ignore_errors, onerror=onerror)
    if onerror is None:
        onerror = functools.partial(_handle_rmtree_error, log=_log)
    return real(path, ignore_errors=ignore_errors, onerror=onerror)


class _ShutilProxy:
    """shutil 模块代理：只接管 rmtree，其余属性透传真实 shutil。

    用于替换 ``hermes_cli.uninstall`` / ``gui_uninstall`` 模块命名空间里的
    ``shutil`` 名字 —— 只影响这两个模块内部的调用，全局 shutil 不受影响。
    """

    def __init__(self, real, log=None):
        self._real = real
        self._log = log

    def rmtree(self, path, ignore_errors=False, onerror=None):
        """与 shutil.rmtree 同签名；Windows 上走 chmod+重试+跳过逻辑。"""
        return _rmtree_win(
            path,
            ignore_errors=ignore_errors,
            onerror=onerror,
            _real_rmtree=self._real.rmtree,
            _log=self._log,
        )

    def __getattr__(self, name):
        # 除 rmtree 外的属性（copy2、which 等）透传到真实模块
        return getattr(self._real, name)


def _source_already_robust(mod) -> bool:
    """上游 uninstall 源码已自带 Windows 重试（chmod / S_IWRITE / _rmtree_win）则跳过。"""
    try:
        src = inspect.getsource(mod)
    except Exception:
        return False  # 拿不到源码（如测试 fake）→ 视为未修复，继续 patch
    return any(marker in src for marker in ("S_IWRITE", "os.chmod", "_rmtree_win"))


def _patch_module(mod, *, log=None) -> bool:
    """把模块命名空间里的 shutil 换成代理（或包裹模块级 rmtree 名字）。"""
    real = getattr(mod, "shutil", None)
    if isinstance(real, _ShutilProxy):
        return True  # 幂等：已 patch 过
    if real is not None and hasattr(real, "rmtree"):
        mod.shutil = _ShutilProxy(real, log=log)
        return True
    # 上游若改成 `from shutil import rmtree`：直接包裹模块级 rmtree 名字
    rmtree_fn = getattr(mod, "rmtree", None)
    if callable(rmtree_fn) and not getattr(rmtree_fn, "_wincompat_robust", False):
        wrapped = functools.partial(_rmtree_win, _real_rmtree=rmtree_fn, _log=log)
        wrapped._wincompat_robust = True  # type: ignore[attr-defined]
        mod.rmtree = wrapped
        return True
    return False


def install() -> bool:
    """Windows 上为 hermes_cli.uninstall / gui_uninstall 注入鲁棒 rmtree。

    返回 True = 已就绪（已 patch 或无需处理）；False = 目标暂不可导入，延迟重试。
    """
    if _PLATFORM != "win32":
        return True  # POSIX 原样：rmtree 无只读文件问题
    try:
        from hermes_cli import uninstall as uninstall_mod
    except (ImportError, AttributeError) as exc:
        logger.debug("windows-compat[uninstall_rmtree]: deferred (%s)", exc)
        return False

    if _source_already_robust(uninstall_mod):
        logger.info("windows-compat[uninstall_rmtree]: 上游已自带 Windows rmtree 重试，跳过")
        return True

    try:
        ok = _patch_module(uninstall_mod, log=getattr(uninstall_mod, "log_warn", None))
    except AttributeError:
        ok = False
    if not ok:
        logger.debug("windows-compat[uninstall_rmtree]: 找不到可 patch 的 rmtree 引用，延迟重试")
        return False

    # run_gui_uninstall 的删除逻辑在 hermes_cli.gui_uninstall 模块里，顺带覆盖
    try:
        from hermes_cli import gui_uninstall as gui_mod
        _patch_module(gui_mod, log=getattr(gui_mod, "log_warn", None))
    except (ImportError, AttributeError):
        pass  # 该模块缺失也不影响主卸载路径

    logger.info(
        "windows-compat[uninstall_rmtree]: 已 patch hermes_cli.uninstall 的 "
        "shutil.rmtree（Windows 鲁棒版：chmod + 重试 + 跳过）"
    )
    return True
