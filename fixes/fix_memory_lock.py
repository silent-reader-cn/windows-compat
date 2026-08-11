"""P1: retry + graceful-degrade for the memory tool's Windows file lock.

Reference: Hermes issue #31738
https://github.com/NousResearch/hermes-agent/issues/31738  (no upstream PR)

On Windows the built-in memory tool fails EVERY add/replace with
``OSError: [Errno 36] Resource deadlock avoided``. Root cause (analyzed in
the issue by AzureLency): ``tools/memory_tool.py`` ``MemoryStore._file_lock``
calls ``msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)`` with no retry. The
CRT raises EDEADLK immediately when the SAME process already holds the lock
region from another thread, and the multi-threaded gateway hits memory from
several threads at once — so the lock call dies on every contention instead
of waiting. Persistent memory is therefore completely unusable on Windows.

The lock region (roughly lines 278-313 of memory_tool.py) is a
``@staticmethod @contextmanager`` generator:

    fd.seek(0)
    msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)   # <- raises EDEADLK
    yield
    ...
    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)  # unlock (already guarded)

This fix monkeypatches ``tools.memory_tool.MemoryStore._file_lock`` with a
variant that:

- wraps the msvcrt lock acquisition in try/except OSError and retries up to
  ``_LOCK_RETRIES`` times with randomized 0.1-0.5s backoff (jitter avoids
  livelock when several threads contend);
- treats EDEADLK / EWOULDBLOCK / EAGAIN as transient and re-raises any other
  OSError unchanged;
- if retries are exhausted, degrades gracefully: yields WITHOUT the lock and
  logs a warning, so the memory tool stays usable (better briefly unlocked
  than dead);
- leaves the POSIX fcntl.flock path untouched (Windows-only fix); unlock is
  only attempted when the lock was actually acquired.

Detection: patch only when the module is importable AND ``_file_lock`` still
has the vulnerable shape — its source contains the bare
``msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)`` call and no
``fix_memory_lock`` marker. If the source already carries the marker (or the
bare call is gone) the issue is treated as fixed upstream and we do NOT
re-patch. If the source cannot be read (dynamic function) we also skip.
"""

from __future__ import annotations

import contextlib
import errno
import inspect
import logging
import random
import time

logger = logging.getLogger(__name__)

PRIORITY = 1
RISK = "medium"
DESCRIPTION = (
    "Windows: memory tool add/replace crashes with OSError [Errno 36] "
    "(Resource deadlock avoided) because MemoryStore._file_lock calls "
    "msvcrt.locking(LK_LOCK) bare; the multi-threaded gateway trips the CRT "
    "same-process lock guard. Patch retries EDEADLK with jittered backoff and "
    "degrades to lock-free (with warning) instead of crashing. POSIX fcntl "
    "path untouched. Issue #31738."
)

# 加锁重试参数
_LOCK_RETRIES = 5      # 最大重试次数
_LOCK_BASE_DELAY = 0.1  # 基础退避秒数
_LOCK_JITTER = 0.4     # 随机抖动上限：每次退避 0.1 ~ 0.5s，避免多线程活锁

# 检测标记：补丁版源码必须包含此字符串；原始脆弱形态源码中不包含。
_PATCH_MARKER = "fix_memory_lock"
# 脆弱形态特征：裸 msvcrt 加锁调用（无任何重试/异常包裹）。
_BARE_LOCK_CALL = "msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)"

# msvcrt.locking 在多线程同进程竞争时抛出的瞬时错误码（EDEADLK=36）
_TRANSIENT_ERRNOS = frozenset({errno.EDEADLK, errno.EWOULDBLOCK, errno.EAGAIN})


def _msvcrt_lock_with_retry(msvcrt_mod, fd) -> bool:
    """带重试的 msvcrt 加锁；返回 True 表示成功拿到锁。

    只把 EDEADLK/EWOULDBLOCK/EAGAIN 当作瞬时错误重试；其他 OSError 原样
    抛出（保持原行为）。重试耗尽返回 False，由调用方决定降级继续。
    """
    for attempt in range(1, _LOCK_RETRIES + 1):
        try:
            fd.seek(0)
            msvcrt_mod.locking(fd.fileno(), msvcrt_mod.LK_LOCK, 1)
            return True
        except OSError as exc:
            if exc.errno not in _TRANSIENT_ERRNOS:
                raise
            if attempt < _LOCK_RETRIES:
                delay = _LOCK_BASE_DELAY + random.random() * _LOCK_JITTER
                logger.warning(
                    "windows-compat[memory_lock]: msvcrt 加锁被瞬时占用"
                    " (%s, errno=%s)，第 %d/%d 次重试，%.2fs 后重试",
                    exc, exc.errno, attempt, _LOCK_RETRIES, delay,
                )
                time.sleep(delay)
    logger.warning(
        "windows-compat[memory_lock]: 重试 %d 次后仍无法获得 msvcrt 文件锁，"
        "降级为无锁继续（memory 工具保持可用，但并发写保护暂时失效）",
        _LOCK_RETRIES,
    )
    return False


@staticmethod
@contextlib.contextmanager
def _file_lock(path):
    """补丁版 _file_lock（fix_memory_lock 标记，勿删）。

    语义与原始实现一致：单独的 .lock 文件 + 独占锁保护 read-modify-write；
    区别仅在 msvcrt 加锁段带重试与优雅降级。fcntl（POSIX）分支原样保留。
    """
    from tools import memory_tool as _mt

    _fcntl = _mt.fcntl
    _msvcrt = _mt.msvcrt

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if _fcntl is None and _msvcrt is None:
        yield
        return

    fd = open(lock_path, "a+", encoding="utf-8")
    locked = False
    try:
        if _fcntl:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
            locked = True
        else:
            locked = _msvcrt_lock_with_retry(_msvcrt, fd)
        yield
    finally:
        if _fcntl:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        elif _msvcrt:
            # 只有真正拿到锁才解锁；降级路径（未加锁）不调用 unlock，
            # 避免误释放其他线程持有的锁。
            if locked:
                try:
                    fd.seek(0)
                    _msvcrt.locking(fd.fileno(), _msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass
        fd.close()


def _is_vulnerable(memory_tool) -> bool:
    """判断 _file_lock 是否仍是脆弱形态（裸 msvcrt 加锁、无重试）。

    返回 False 的情形：目标缺失、源码不可读（动态函数）、已含补丁标记、
    或裸调用已不存在——这些情况都不应重复 patch。
    """
    cls = getattr(memory_tool, "MemoryStore", None)
    if cls is None:
        return False
    func = getattr(cls, "_file_lock", None)
    if func is None:
        return False
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError):
        logger.debug("windows-compat[memory_lock]: 无法读取 _file_lock 源码，跳过检测")
        return False
    if _BARE_LOCK_CALL not in src:
        return False
    if _PATCH_MARKER in src:
        return False
    return True


def install() -> bool:
    """检测并替换 tools.memory_tool.MemoryStore._file_lock。

    True = 已完成（已装、无需装或跳过）；False = 目标模块尚不可用，延迟重试。
    """
    try:
        from tools import memory_tool
    except (ImportError, AttributeError) as exc:
        logger.debug("windows-compat[memory_lock]: tools.memory_tool 尚不可用，延迟重试 (%s)", exc)
        return False

    cls = getattr(memory_tool, "MemoryStore", None)
    if cls is None:
        logger.warning("windows-compat[memory_lock]: memory_tool 缺少 MemoryStore 类，跳过")
        return True
    func = getattr(cls, "_file_lock", None)
    if func is None:
        logger.warning("windows-compat[memory_lock]: MemoryStore 缺少 _file_lock，跳过")
        return True

    # 形态检查：_file_lock 必须是 contextmanager/generator（有 yield 的
    # with 协议），整体替换才不会破坏调用方；否则跳过并记录。
    # contextmanager 返回的 helper 是 @wraps 包装的普通函数，需经 __wrapped__
    # 解包才能看到原始生成器函数。
    wrapped = getattr(func, "__wrapped__", func)
    if not (hasattr(func, "__enter__") or inspect.isgeneratorfunction(wrapped)):
        logger.warning(
            "windows-compat[memory_lock]: _file_lock 不是 contextmanager/generator 形态，"
            "跳过 patch（避免破坏调用约定）"
        )
        return True

    try:
        if not _is_vulnerable(memory_tool):
            logger.info("windows-compat[memory_lock]: _file_lock 已修复或非脆弱形态，无需 patch")
            return True
    except Exception as exc:
        # 检测过程本身异常：保守起见不 patch，避免误伤。
        logger.warning("windows-compat[memory_lock]: 脆弱形态检测异常，跳过 patch (%s)", exc)
        return True

    try:
        cls._file_lock = _file_lock
    except AttributeError as exc:
        logger.debug("windows-compat[memory_lock]: 替换 _file_lock 失败，延迟重试 (%s)", exc)
        return False

    logger.info(
        "windows-compat[memory_lock]: 已替换 tools.memory_tool.MemoryStore._file_lock"
        "（msvcrt 加锁带 %d 次重试 + 降级，POSIX fcntl 路径不变）",
        _LOCK_RETRIES,
    )
    return True
