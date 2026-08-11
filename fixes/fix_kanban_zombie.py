"""P2: 终止 kanban 任务完成后仍在运行的僵尸 worker 进程树（Issue #61923）。

背景（Windows 上尤为严重）：
- Hermes kanban worker 是 dispatcher 用 ``subprocess.Popen`` 独立 spawn 的 OS
  进程（``hermes_cli/kanban_db.py`` ~10200 行）。worker 进程被
  ``HERMES_KANBAN_TASK`` / ``HERMES_KANBAN_RUN_ID`` 环境变量标记，pid 记录在
  ``tasks.worker_pid``（runs 表也有记录）。
- ``tools.kanban_tools._handle_complete``（~655 行）标记任务完成后只写 DB：
  ``complete_task`` 把 ``worker_pid`` 置 NULL、状态置 done，但**从不终止 worker
  的 OS 进程**。worker 的对话循环在 complete 之后仍会继续跑（kanban_stop 的
  nudge 守卫只在"未调用终止工具就结束"时生效），于是僵尸 worker 继续操作浏览器、
  甚至删除已被 ``_cleanup_workspace`` 清理过的工作区文件。
- POSIX 上 ``start_new_session=True`` 的进程组 + SIGTERM 语义部分掩盖了问题；
  Windows 上 ``start_new_session`` 是 no-op、没有 SIGTERM 语义，完全没有自动清理。

修复方案（保守、鲁棒）：
- monkeypatch ``tools.kanban_tools._handle_complete``，**同时**更新
  ``tools.registry`` 里 ``kanban_complete`` 的 handler 引用 —— registry 在模块
  import 时按对象引用捕获 handler（``registry.register(handler=_handle_complete)``），
  只改模块属性对实际工具调用路径无效。
- wrapper 在**原 handler 之前**读取 ``tasks.worker_pid`` / ``claim_lock``
  （complete_task 会把 worker_pid 置 NULL），原 handler 返回成功
  （``{"ok": true, ...}``）后 best-effort 终止该 worker 的进程树：
  - Windows: ``taskkill /PID <pid> /T /F``（整棵树，覆盖 worker 的浏览器/终端
    子进程），失败回退 ``os.kill(pid, SIGTERM)``（stdlib shim → TerminateProcess）。
  - POSIX: worker 是会话/进程组组长（dispatcher 用 ``start_new_session=True``），
    ``killpg`` SIGTERM 杀整组，3 秒宽限后 SIGKILL 升级。
- **杀前校验（防误杀）**，镜像上游 ``_terminate_reclaimed_worker`` 的 host-local
  守卫：
  1) pid == 本进程且 ``HERMES_KANBAN_TASK == tid`` —— dispatcher-spawned worker
     自完成，杀掉自己正是修复目标；
  2) ``claim_lock`` 的 host 前缀与当前 host 一致（多 gateway 部署下 worker 可能
     在别的机器上，跨 host 的 pid 在本机 kill 会误杀无关进程）；
  3) POSIX 下 ``/proc/<pid>/environ`` 含 ``HERMES_KANBAN_TASK=<tid>`` 兜底校验。
  全部不满足则跳过并 warning。
- 所有 kill 路径 try/except 吞掉，只记 warning —— complete 工具**绝不**因清理
  失败报错；找不到进程（ProcessLookupError）视为"已终止"成功。

已修复检测：``inspect.getsource`` 检查原 handler 是否已含进程终止逻辑
（taskkill/os.kill/killpg/SIGTERM 等标记），有则跳过；本插件已装
（handler 带 ``_wincompat_kanban_zombie_patched`` 标记）则幂等返回 True。

参考：https://github.com/NousResearch/hermes-agent/issues/61923
（无上游 PR，A 类无人处理）

已知覆盖范围：只 patch 工具调用路径（worker 自完成 / orchestrator 工具完成）。
``hermes kanban complete <id>`` CLI 直接调 ``kb.complete_task`` 不走
``_handle_complete``，不在本 fix 覆盖内（文档化限制）。

风险：medium —— 会真实终止进程，但仅在任务已进入终态(done)后、经过上述三层
校验才动手；失败时行为与未 patch 完全一致。
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sys
import time

logger = logging.getLogger(__name__)

PRIORITY = 2
RISK = "medium"
DESCRIPTION = (
    "Terminate the zombie kanban worker process tree after kanban_complete "
    "succeeds (Issue #61923): complete_task only writes the DB and never kills "
    "the worker OS process, so on Windows (no SIGTERM semantics, start_new_session "
    "is a no-op) the worker keeps running, operating the browser and deleting "
    "cleaned workspace files. Patches the kanban_complete registry handler with a "
    "best-effort, host-verified tree kill (taskkill /T /F on Windows, killpg on "
    "POSIX); any kill failure is swallowed so complete never errors."
)

# upstream 已修复检测用的终止逻辑标记（出现在 _handle_complete 源码里就跳过）
_UPSTREAM_FIX_MARKERS = (
    "taskkill",
    "killpg",
    "TerminateProcess",
    "os.kill(",
    "terminate_worker",
)

# Windows CREATE_NO_WINDOW —— taskkill 子进程不弹黑窗
_WIN_CREATE_NO_WINDOW = 0x08000000

_PATCH_MARKER = "_wincompat_kanban_zombie_patched"


# ---------------------------------------------------------------------------
# 进程树终止原语（永不抛异常）
# ---------------------------------------------------------------------------

def _pid_exists(pid: int) -> bool:
    """跨平台 pid 存活探测，永不抛异常。

    Windows 上**禁用** ``os.kill(pid, 0)`` —— Python 的 Windows ``os.kill`` 把
    sig=0 当 CTRL_C_EVENT 广播到目标控制台组（bpo-14484），可能误杀无关进程。
    改用 OpenProcess + CloseHandle 探测。
    """
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM —— 进程存在但无权限探测
        return True


def _taskkill_tree(pid: int) -> bool:
    """Windows: taskkill /PID <pid> /T /F 杀整棵树；失败回退 TerminateProcess。"""
    try:
        import subprocess

        proc = subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_WIN_CREATE_NO_WINDOW,
        )
        if proc.returncode == 0:
            return not _pid_exists(pid)
        # taskkill 非零：进程已死（"not found"）也算终止成功；
        # 其他失败（无权限等）→ 回退单进程 TerminateProcess
        err = (proc.stdout or "") + (proc.stderr or "")
        if "not found" in err.lower():
            return True
    except Exception:
        pass
    try:
        import signal

        os.kill(int(pid), signal.SIGTERM)  # Windows stdlib shim → TerminateProcess
        return not _pid_exists(pid)
    except ProcessLookupError:
        return True  # 已死 = 终止成功
    except OSError:
        return False


def _posix_group_kill(pid: int) -> bool:
    """POSIX: worker 是进程组组长（dispatcher start_new_session=True），
    killpg 杀整组；3 秒宽限后 SIGKILL 升级。单进程兜底。"""
    import signal

    pgid = None
    try:
        if os.getpgid(pid) == pid:  # 组组长 → 组杀
            pgid = pid
    except ProcessLookupError:
        return True  # 已死
    except OSError:
        pass

    def _send(sig: int) -> None:
        try:
            if pgid:
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            pass  # 已死
        except OSError:
            pass

    _send(signal.SIGTERM)
    for _ in range(12):
        if not _pid_exists(pid):
            return True
        time.sleep(0.25)
    _send(getattr(signal, "SIGKILL", signal.SIGTERM))
    return not _pid_exists(pid)


# ---------------------------------------------------------------------------
# 杀前校验：只杀"确认为本 task 的本地 worker"的进程
# ---------------------------------------------------------------------------

def _worker_is_host_local(kb, claim_lock) -> bool:
    """claim_lock 的 host 前缀与当前 host 一致才算本地 worker。

    镜像上游 ``hermes_cli/kanban_db._terminate_reclaimed_worker`` 的 host-local
    守卫（多 gateway 部署下 worker 可能 spawn 在别的机器上，跨 host 的 pid 在
    本机 kill 会误杀 pid 复用的无关进程）。
    """
    if not claim_lock:
        return False
    try:
        if kb is not None and hasattr(kb, "_claimer_id"):
            host_prefix = kb._claimer_id().split(":", 1)[0]
        else:
            import socket

            host_prefix = socket.gethostname() or "unknown"
        lock_host = str(claim_lock).split(":", 1)[0]
        return bool(lock_host) and lock_host == host_prefix
    except Exception:
        return False


def _proc_env_matches_task(pid: int, task_id: str):
    """POSIX: /proc/<pid>/environ 是否含 HERMES_KANBAN_TASK=<task_id>。

    返回 True（匹配）/ False（不匹配）/ None（无法用此法校验：Windows、无 /proc、
    权限不足）。Windows 上 pid 复用误杀风险靠 claim_lock host 校验兜底。
    """
    if sys.platform == "win32" or not task_id:
        return None
    try:
        with open(f"/proc/{int(pid)}/environ", "rb") as f:
            data = f.read()
        return ("HERMES_KANBAN_TASK=" + task_id).encode() in data
    except (OSError, ValueError):
        return None


def _terminate_worker_tree(pid, task_id: str, *, claim_lock=None, kb=None) -> bool:
    """Best-effort 终止 task 的 worker 进程树（含子进程）。永不抛异常。

    - pid 缺失/非法 → False（无害）。
    - pid == 本进程：仅当 ``HERMES_KANBAN_TASK == task_id``（dispatcher-spawned
      worker 自完成，杀掉自己正是修复目标）才动手。
    - 其他 pid：claim_lock host-local 校验或 /proc environ 校验通过才动手。
    - Windows ``taskkill /T /F``（树杀），POSIX ``killpg``（组杀）。
    """
    try:
        if not pid or int(pid) <= 0:
            return False
        pid = int(pid)

        if pid == os.getpid():
            if os.environ.get("HERMES_KANBAN_TASK") != task_id:
                logger.warning(
                    "windows-compat[kanban_zombie]: refuse self-kill pid=%s "
                    "(HERMES_KANBAN_TASK != %s)", pid, task_id
                )
                return False
        else:
            verified = _worker_is_host_local(kb, claim_lock)
            if not verified:
                proc_ok = _proc_env_matches_task(pid, task_id)
                if proc_ok is not True:
                    logger.warning(
                        "windows-compat[kanban_zombie]: skip kill pid=%s for %s — "
                        "not verified as a host-local worker (claim_lock=%r)",
                        pid, task_id, claim_lock,
                    )
                    return False

        if sys.platform == "win32":
            killed = _taskkill_tree(pid)
        else:
            killed = _posix_group_kill(pid)
        logger.info(
            "windows-compat[kanban_zombie]: worker tree pid=%s for %s "
            "terminated=%s", pid, task_id, killed
        )
        return killed
    except Exception as exc:
        logger.warning(
            "windows-compat[kanban_zombie]: terminate worker pid=%s for %s "
            "failed: %s", pid, task_id, exc
        )
        return False


# ---------------------------------------------------------------------------
# worker pid 解析 + wrapper
# ---------------------------------------------------------------------------

def _read_worker_record(kt, task_id: str):
    """complete 前读 tasks.worker_pid + claim_lock（complete_task 会置 NULL）。

    返回 (pid, claim_lock, kb)；DB 不可用/无记录时回退环境变量 ——
    dispatcher-spawned worker 自完成时本进程即 worker（pid = os.getpid()）。
    绝不抛异常。
    """
    kb = None
    try:
        connect = getattr(kt, "_connect", None)
        if connect is not None:
            kb, conn = connect()
            try:
                row = conn.execute(
                    "SELECT worker_pid, claim_lock FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is not None:
                    try:
                        pid = row["worker_pid"]
                    except (KeyError, TypeError):
                        pid = row[0]
                    try:
                        lock = row["claim_lock"]
                    except (KeyError, TypeError):
                        lock = row[1]
                    if pid:
                        return int(pid), lock, kb
                    return None, lock, kb
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("windows-compat[kanban_zombie]: DB worker lookup failed (%s)", exc)
    # 回退：worker 自完成自己的 task → 本进程即 worker
    if os.environ.get("HERMES_KANBAN_TASK") == task_id:
        return os.getpid(), None, kb
    return None, None, kb


def _is_success_result(result) -> bool:
    """原 handler 的返回值是否为成功（{"ok": true, ...}）。错误/非 JSON → False。"""
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and data.get("ok") is True


def _make_wrapped_complete(kt, orig):
    """包一层 _handle_complete：成功后 best-effort 终止该 task 的 worker 进程树。"""

    @functools.wraps(orig)
    def _wrapped(args, **kw):
        # 1) 解析 task_id（复用原模块的解析逻辑，保持一致）
        tid = None
        try:
            if isinstance(args, dict):
                default_tid = getattr(kt, "_default_task_id", None)
                if default_tid is not None:
                    tid = default_tid(args.get("task_id"))
                else:
                    tid = args.get("task_id") or os.environ.get("HERMES_KANBAN_TASK")
        except Exception:
            tid = None

        # 2) 原 handler 之前读 worker 记录（complete_task 会清空 worker_pid）
        pid, claim_lock, kb = (None, None, None)
        if tid:
            pid, claim_lock, kb = _read_worker_record(kt, tid)

        # 3) 原行为完全不变
        result = orig(args, **kw)

        # 4) 成功后清理僵尸 worker（任何失败都不得让 complete 报错）
        if tid and _is_success_result(result):
            try:
                _terminate_worker_tree(pid, tid, claim_lock=claim_lock, kb=kb)
            except Exception as exc:  # 双保险：_terminate_worker_tree 本身也不抛
                logger.warning(
                    "windows-compat[kanban_zombie]: cleanup after complete %s "
                    "failed: %s", tid, exc
                )
        return result

    setattr(_wrapped, _PATCH_MARKER, True)
    return _wrapped


def _upstream_already_fixed(handler) -> bool:
    """检测上游是否已给 handler 加进程终止逻辑（inspect 源码找 kill 标记）。"""
    try:
        import inspect

        src = inspect.getsource(handler)
    except Exception:
        return False
    return any(marker in src for marker in _UPSTREAM_FIX_MARKERS)


def install() -> bool:
    """Patch kanban_complete 的 handler：成功后终止僵尸 worker 进程树。

    返回 True = 已装（或检测到上游已修复/已装过），False = 延迟重试。
    """
    try:
        from tools import kanban_tools as kt

        registry = None
        try:
            from tools.registry import registry
        except Exception:
            registry = None

        entry = registry.get_entry("kanban_complete") if registry is not None else None
        mod_handler = getattr(kt, "_handle_complete", None)
        if mod_handler is None and entry is None:
            logger.debug("windows-compat[kanban_zombie]: kanban_complete not registered yet")
            return False

        # 以实际执行路径（registry entry）为准
        cur = entry.handler if entry is not None else mod_handler

        if getattr(cur, _PATCH_MARKER, False):
            logger.info("windows-compat[kanban_zombie]: already installed — skip")
            return True
        if _upstream_already_fixed(cur):
            logger.info(
                "windows-compat[kanban_zombie]: upstream _handle_complete already "
                "terminates workers — nothing to patch"
            )
            return True

        wrapped = _make_wrapped_complete(kt, cur)
        if entry is not None:
            entry.handler = wrapped
        setattr(kt, "_handle_complete", wrapped)
        logger.info(
            "windows-compat[kanban_zombie]: patched kanban_complete handler — "
            "zombie worker tree terminated after successful complete"
        )
        return True
    except (ImportError, AttributeError) as exc:
        logger.debug("windows-compat[kanban_zombie]: deferred (%s)", exc)
        return False
    except Exception as exc:
        logger.warning("windows-compat[kanban_zombie]: install failed (%s)", exc)
        return False
