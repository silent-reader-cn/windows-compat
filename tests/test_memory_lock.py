"""Tests for fix_memory_lock — retry + graceful-degrade for the Windows
msvcrt file lock in tools.memory_tool (Hermes issue #31738).

These tests are self-contained: they inject a fake ``tools.memory_tool``
module (vulnerable shape + programmable fake msvcrt.locking) into
``sys.modules``, so they do NOT depend on a real Hermes installation.

Covered behaviors:

1. vulnerable shape  -> install() returns True and replaces _file_lock;
   the patched lock retries transient EDEADLK failures and succeeds;
2. retries exhausted -> the context manager yields WITHOUT the lock (graceful
   degrade) instead of raising; unlock is NOT called on the degrade path;
3. already-fixed shape (source carries the fix_memory_lock marker / no bare
   call) -> install() returns True but does NOT re-patch;
4. non-deadlock OSError -> re-raised unchanged (no swallowing);
5. tools.memory_tool missing -> install() returns False (deferred);
6. _file_lock not a contextmanager/generator -> skipped, not replaced.
"""

import contextlib
import errno
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from fixes import fix_memory_lock as fix  # noqa: E402


# ---------------------------------------------------------------------------
# 顶层 fake 形态函数：inspect.getsource 需要真实源码（本文件可读），
# 这些函数只被检测/引用，绝不会被执行。
# ---------------------------------------------------------------------------

@staticmethod
@contextlib.contextmanager
def _vulnerable_file_lock(path):
    """模拟 Hermes 原始脆弱形态：裸 msvcrt 加锁调用，无重试无异常包裹。"""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None and msvcrt is None:
        yield
        return
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        if msvcrt:
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        fd.close()


@staticmethod
@contextlib.contextmanager
def _already_fixed_file_lock(path):
    """fix_memory_lock 已应用形态：源码含补丁标记，且无裸 msvcrt 调用。"""
    yield


def _plain_no_yield(path):
    """普通函数形态（非 contextmanager/generator），源码却含裸调用。"""
    fd = open(path.with_suffix(path.suffix + ".lock"), "a+", encoding="utf-8")
    msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
    return fd


# ---------------------------------------------------------------------------
# fake 基础设施
# ---------------------------------------------------------------------------

def _make_locking(fail_first_n=0, always_fail=False, err=errno.EDEADLK):
    """构造可编程 fake msvcrt.locking，并返回调用统计。"""
    state = {"lock_calls": 0, "unlock_calls": 0}

    def locking(fd, mode, nbytes):
        if mode == 0:  # LK_UNLCK
            state["unlock_calls"] += 1
            return
        state["lock_calls"] += 1
        if always_fail or state["lock_calls"] <= fail_first_n:
            raise OSError(err, "Resource deadlock avoided")

    return locking, state


def _inject_fake_module(lock_func, locking):
    """向 sys.modules 注入 fake tools / tools.memory_tool，返回 fake 模块。"""
    pkg = types.ModuleType("tools")
    pkg.__path__ = []
    mod = types.ModuleType("tools.memory_tool")
    mod.fcntl = None  # Windows 形态：无 fcntl，走 msvcrt 分支
    mod.msvcrt = types.SimpleNamespace(LK_LOCK=1, LK_UNLCK=0, locking=locking)

    class MemoryStore:
        _file_lock = lock_func

    mod.MemoryStore = MemoryStore
    pkg.memory_tool = mod
    sys.modules["tools"] = pkg
    sys.modules["tools.memory_tool"] = mod
    return mod


@pytest.fixture(autouse=True)
def _clean_sys_modules():
    """每个测试前后清理注入，保证测试间隔离、不污染真实环境。"""
    saved = {k: sys.modules.pop(k, None) for k in ("tools", "tools.memory_tool")}
    yield
    for k, v in saved.items():
        if v is not None:
            sys.modules[k] = v


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """把重试退避的 sleep 置空，避免测试等待真实 0.1-0.5s 退避。"""
    monkeypatch.setattr(fix.time, "sleep", lambda _s: None)


def _raw(fn):
    """解开 staticmethod 包装，拿到真正的函数对象（用于身份比较）。"""
    return fn.__func__ if isinstance(fn, staticmethod) else fn


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

def test_install_patches_and_retries_lock(tmp_path):
    """脆弱形态：install() 返回 True 并替换 _file_lock；EDEADLK 重试生效。"""
    locking, state = _make_locking(fail_first_n=2)  # 先抛 2 次 EDEADLK 再成功
    mod = _inject_fake_module(_vulnerable_file_lock, locking)

    assert fix.install() is True
    # 已替换为补丁版（比较解包后的函数对象）
    assert _raw(mod.MemoryStore._file_lock) is _raw(fix._file_lock)

    # 通过补丁版加锁执行临界区：2 次失败 + 1 次成功 = 3 次加锁调用
    with mod.MemoryStore._file_lock(tmp_path / "MEMORY.md"):
        pass
    assert state["lock_calls"] == 3
    # 成功拿到锁 → 解锁调用 1 次
    assert state["unlock_calls"] == 1

    # 幂等：二次 install() 检测到已修复，不重复替换
    assert fix.install() is True
    assert _raw(mod.MemoryStore._file_lock) is _raw(fix._file_lock)


def test_retry_exhausted_degrades_without_lock(tmp_path):
    """重试耗尽：不抛异常，无锁继续（优雅降级），且不调用 unlock。"""
    locking, state = _make_locking(always_fail=True)
    mod = _inject_fake_module(_vulnerable_file_lock, locking)

    assert fix.install() is True

    # 临界区必须正常执行完毕，不能抛 OSError
    with mod.MemoryStore._file_lock(tmp_path / "MEMORY.md"):
        pass
    # 恰好重试 _LOCK_RETRIES 次后放弃
    assert state["lock_calls"] == fix._LOCK_RETRIES
    # 降级路径（未拿到锁）不调用 unlock
    assert state["unlock_calls"] == 0


def test_already_fixed_shape_skips_repatch(tmp_path):
    """已修复形态（源码含 fix_memory_lock 标记）：install() 返回 True 且不重复 patch。"""
    locking, _state = _make_locking()
    mod = _inject_fake_module(_already_fixed_file_lock, locking)

    assert fix.install() is True
    # 对象未被替换（还是原来的已修复函数）
    assert _raw(mod.MemoryStore._file_lock) is _raw(_already_fixed_file_lock)


def test_non_deadlock_oserror_propagates(tmp_path):
    """非死锁类 OSError（如 EACCES）必须原样抛出，不能被重试逻辑吞掉。"""
    locking, state = _make_locking(always_fail=True, err=errno.EACCES)
    mod = _inject_fake_module(_vulnerable_file_lock, locking)

    assert fix.install() is True
    with pytest.raises(OSError) as excinfo:
        with mod.MemoryStore._file_lock(tmp_path / "MEMORY.md"):
            pass
    assert excinfo.value.errno == errno.EACCES
    # 非瞬时错误：不进入重试循环，只调用 1 次
    assert state["lock_calls"] == 1


def test_missing_module_returns_false():
    """tools.memory_tool 不可 import：install() 返回 False（延迟重试）。"""
    # autouse fixture 已清理 sys.modules，未注入任何 fake
    assert fix.install() is False


def test_non_generator_lock_skipped(tmp_path):
    """_file_lock 不是 contextmanager/generator：跳过 patch，不替换。"""
    locking, _state = _make_locking()
    mod = _inject_fake_module(_plain_no_yield, locking)

    assert fix.install() is True
    assert _raw(mod.MemoryStore._file_lock) is _plain_no_yield
