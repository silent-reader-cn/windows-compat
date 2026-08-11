"""Tests for fix_kanban_zombie — kanban worker zombie process cleanup (P2).

Issue #61923: after ``kanban_complete`` the worker OS process keeps running on
Windows (complete_task only writes the DB). The fix patches the
``kanban_complete`` registry handler so a successful completion best-effort
terminates the task's worker process tree.

Self-contained: injects fake ``tools.kanban_tools`` / ``tools.registry`` modules
(no Hermes install needed). Reuses the ``tools`` package created by conftest.py
and restores it afterwards so other test files are unaffected.
"""

import json
import os
import socket
import sqlite3
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fixes import fix_kanban_zombie as fz

_CONFTEST_TOOLS = sys.modules.get("tools")  # conftest.py 建的 tools 包


def _fresh_registry():
    """fake tools.registry：最小可注册表，handler 按对象引用存储（同真实实现）。"""

    class _FakeEntry:
        def __init__(self, handler):
            self.handler = handler

    class _FakeRegistry:
        def __init__(self):
            self.entries = {}

        def get_entry(self, name):
            return self.entries.get(name)

    return _FakeRegistry(), _FakeEntry


@pytest.fixture()
def fakes():
    """注入 fake tools.kanban_tools / tools.registry，测试后恢复 conftest 原状。

    fake _handle_complete：
      - 无 task_id（args 与 env 都没有）→ error
      - args["fail"] → error（模拟 goal-mode 拒绝等失败路径）
      - 否则 → {"ok": true, ...}
    """
    tools = _CONFTEST_TOOLS or types.ModuleType("tools")
    tools.__path__ = []
    sys.modules["tools"] = tools

    reg, entry_cls = _fresh_registry()
    reg_mod = types.ModuleType("tools.registry")
    reg_mod.registry = reg
    tools.registry = reg_mod
    sys.modules["tools.registry"] = reg_mod

    kt = types.ModuleType("tools.kanban_tools")
    calls = {"orig": 0}

    def fake_ok(**fields):
        return json.dumps({"ok": True, **fields})

    def fake_default_task_id(arg):
        return arg or os.environ.get("HERMES_KANBAN_TASK")

    def fake_handle_complete(args, **kw):
        calls["orig"] += 1
        tid = args.get("task_id") or os.environ.get("HERMES_KANBAN_TASK")
        if not tid:
            return json.dumps({"error": "task_id required"})
        if args.get("fail"):
            return json.dumps({"error": "goal completion rejected"})
        return fake_ok(task_id=tid, run_id=7)

    kt._ok = fake_ok
    kt._default_task_id = fake_default_task_id
    kt._handle_complete = fake_handle_complete
    tools.kanban_tools = kt
    sys.modules["tools.kanban_tools"] = kt

    entry = entry_cls(fake_handle_complete)
    reg.entries["kanban_complete"] = entry

    yield {
        "tools": tools,
        "kt": kt,
        "reg": reg,
        "entry": entry,
        "calls": calls,
    }

    # teardown：只移除本测试注入的模块，恢复 conftest 的 tools 包
    for name in ("tools.kanban_tools", "tools.registry"):
        sys.modules.pop(name, None)
        attr = name.split(".")[-1]
        if hasattr(tools, attr):
            delattr(tools, attr)
    if _CONFTEST_TOOLS is not None:
        sys.modules["tools"] = _CONFTEST_TOOLS
    else:
        sys.modules.pop("tools", None)


def _local_claimer_id():
    return f"{socket.gethostname() or 'unknown'}:{os.getpid()}"


def _add_fake_db(kt, rows):
    """给 fake kanban_tools 挂一个真实 sqlite3 内存库的 _connect（同真实契约）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, worker_pid INTEGER, claim_lock TEXT)"
    )
    for row in rows:
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?)", row)
    kb = types.SimpleNamespace(_claimer_id=_local_claimer_id)

    def fake_connect(board=None):
        return kb, conn

    kt._connect = fake_connect
    return kb, conn


# ---------------------------------------------------------------------------
# install() 行为
# ---------------------------------------------------------------------------

def test_install_patches_registry_handler(fakes):
    assert fz.install() is True
    # registry entry 是实际执行路径 —— 必须被 patch
    assert getattr(fakes["entry"].handler, fz._PATCH_MARKER, False)
    # 模块属性同步 patch（其他直接引用的代码路径）
    assert getattr(fakes["kt"]._handle_complete, fz._PATCH_MARKER, False)


def test_install_idempotent_no_double_wrap(fakes):
    assert fz.install() is True
    assert fz.install() is True
    handler = fakes["entry"].handler
    assert getattr(handler, fz._PATCH_MARKER, False)
    # 调用一次 → 原 handler 只执行一次（未被二次包裹）
    result = handler({"task_id": "t_x", "summary": "s"})
    assert json.loads(result)["ok"] is True
    assert fakes["calls"]["orig"] == 1


def test_install_deferred_when_kanban_tools_unavailable(fakes):
    tools = fakes["tools"]
    sys.modules.pop("tools.kanban_tools", None)
    delattr(tools, "kanban_tools")
    sys.modules.pop("tools.registry", None)
    delattr(tools, "registry")
    assert fz.install() is False  # 延迟重试


def test_install_patches_module_attr_when_registry_entry_missing(fakes):
    # 注册表里没有 kanban_complete（异常场景）→ 退而 patch 模块属性（best effort）
    fakes["reg"].entries.clear()
    assert fz.install() is True
    assert getattr(fakes["kt"]._handle_complete, fz._PATCH_MARKER, False)


def test_install_skips_when_upstream_already_fixed(fakes):
    def fixed_handler(args, **kw):
        """Upstream fix: taskkill /T /F the worker process tree on completion."""
        return json.dumps({"ok": True})

    fakes["kt"]._handle_complete = fixed_handler
    fakes["entry"].handler = fixed_handler
    assert fz.install() is True
    # 未再包裹 —— 原 handler 原样保留
    assert fakes["entry"].handler is fixed_handler
    assert not getattr(fixed_handler, fz._PATCH_MARKER, False)


# ---------------------------------------------------------------------------
# patch 后 complete 行为
# ---------------------------------------------------------------------------

def test_complete_success_terminates_db_worker(fakes, monkeypatch):
    _add_fake_db(fakes["kt"], [("t_db", 424242, _local_claimer_id())])

    spy = []
    monkeypatch.setattr(
        fz, "_terminate_worker_tree",
        lambda pid, task_id, **kw: spy.append((pid, task_id)),
    )

    assert fz.install() is True
    result = fakes["entry"].handler({"task_id": "t_db", "summary": "done"})
    assert json.loads(result)["ok"] is True
    assert spy == [(424242, "t_db")]  # complete 前读到的 worker_pid


def test_self_complete_resolves_own_pid(fakes, monkeypatch):
    # worker 自完成（HERMES_KANBAN_TASK 环境变量 + 无 DB）→ pid 回退为本进程
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_self")
    spy = []
    monkeypatch.setattr(
        fz, "_terminate_worker_tree",
        lambda pid, task_id, **kw: spy.append((pid, task_id)),
    )

    assert fz.install() is True
    result = fakes["entry"].handler({"summary": "done"})  # task_id 来自 env
    assert json.loads(result)["ok"] is True
    assert spy == [(os.getpid(), "t_self")]


def test_error_result_does_not_kill(fakes, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_err")
    spy = []
    monkeypatch.setattr(
        fz, "_terminate_worker_tree",
        lambda pid, task_id, **kw: spy.append(pid),
    )

    assert fz.install() is True
    result = fakes["entry"].handler({"summary": "x", "fail": True})
    assert json.loads(result)["error"]
    assert spy == []  # complete 失败（任务仍 in-flight）绝不动手


def test_kill_failure_swallowed(fakes, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_boom")

    def boom(pid, task_id, **kw):
        raise RuntimeError("taskkill exploded")

    monkeypatch.setattr(fz, "_terminate_worker_tree", boom)

    assert fz.install() is True
    result = fakes["entry"].handler({"summary": "x"})
    # kill 失败被吞掉 —— complete 工具正常返回成功
    assert json.loads(result)["ok"] is True


def test_remote_claim_lock_skips_kill(fakes, monkeypatch):
    # worker 在别的 host（多 gateway 部署）→ 不杀（防 pid 复用误杀）
    _add_fake_db(fakes["kt"], [("t_remote", 424242, "other-host-xyz:777")])

    spy = []
    monkeypatch.setattr(
        fz, "_terminate_worker_tree",
        lambda pid, task_id, **kw: spy.append((pid, task_id)),
    )

    assert fz.install() is True
    result = fakes["entry"].handler({"task_id": "t_remote", "summary": "s"})
    assert json.loads(result)["ok"] is True
    # _terminate_worker_tree 内部校验失败 → 不会真正 kill（spy 仍会记录，
    # 但真实实现会因 host 校验拒绝 —— 见下个测试直接测真实实现）
    assert spy == [(424242, "t_remote")]


# ---------------------------------------------------------------------------
# _terminate_worker_tree 真实实现（不依赖 Hermes）
# ---------------------------------------------------------------------------

def test_terminate_nonexistent_pid_is_harmless():
    # host-local claim_lock 校验通过，但 pid 不存在 → 视为已终止，无异常
    ok = fz._terminate_worker_tree(
        2147483647, "t_ghost",
        claim_lock=_local_claimer_id(),
        kb=types.SimpleNamespace(_claimer_id=_local_claimer_id),
    )
    assert ok is True  # 已死 = 终止成功
    # 无 claim_lock 且非本进程 → 校验不过，跳过（保守）
    ok2 = fz._terminate_worker_tree(2147483647, "t_ghost")
    assert ok2 is False


def test_terminate_remote_worker_skipped():
    # 远程 claim_lock → 拒绝杀，返回 False，不抛异常
    ok = fz._terminate_worker_tree(
        2147483647, "t_remote",
        claim_lock="other-host:777",
        kb=types.SimpleNamespace(_claimer_id=_local_claimer_id),
    )
    assert ok is False


def test_terminate_invalid_pid_safe():
    assert fz._terminate_worker_tree(None, "t_x") is False
    assert fz._terminate_worker_tree(0, "t_x") is False
    assert fz._terminate_worker_tree(-5, "t_x") is False
