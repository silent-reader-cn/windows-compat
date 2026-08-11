"""Tests for fix_mcp_selector_loop — win32 MCP 后台循环 Proactor -> Selector (P1).

自包含测试：构造 fake ``tools.mcp_tool`` 模块（原始形态 _ensure_mcp_loop +
全局 _lock/_mcp_loop/_mcp_thread/_mcp_loop_exception_handler）注入
sys.modules，不依赖 Hermes 安装，也不修改 conftest.py。覆盖：

  - Windows 模拟下 install() 返回 True 且 _ensure_mcp_loop 被替换（幂等）
  - 替换后创建的 loop 类型是 SelectorEventLoop 相关
  - POSIX 下 install() 不改动任何东西
  - 已修复形态（源码含 'Selector' / loop 已是 Selector）不重复 patch
  - tools.mcp_tool 不可 import 时返回 False（延迟重试契约）

fake 的 _ensure_mcp_loop 用工厂闭包生成（读写 fake 模块属性，与 fix 的
patched 实现同一风格，状态一致）；工厂函数定义在本测试文件里，因此
inspect.getsource 能取到真实源码，可分别构造"未修复"（无 Selector）与
"已修复"（含 Selector）两种形态。
"""

import asyncio
import os
import sys
import threading
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fixes import fix_mcp_selector_loop as fix  # noqa: E402


# ---------------------------------------------------------------------------
# fake tools.mcp_tool 的两种形态（源码形态与 Hermes 上游一致，见 issue #61444）
# ---------------------------------------------------------------------------


def _fake_exception_handler(loop, context):
    """模拟上游 _mcp_loop_exception_handler：抑制 'Event loop is closed' 噪音。"""
    exc = context.get("exception")
    if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
        return
    loop.default_exception_handler(context)


def _make_original_ensure(mcp_fake):
    """原始形态：asyncio.new_event_loop()（win32 上即 ProactorEventLoop）。

    闭包读写 mcp_fake 模块属性，与上游 `global _mcp_loop, _mcp_thread`
    的语义等价。源码不含 'Selector' —— getsource 检测判为"未修复"。
    """

    def _ensure():
        with mcp_fake._lock:
            if mcp_fake._mcp_loop is not None and mcp_fake._mcp_loop.is_running():
                return
            mcp_fake._mcp_loop = asyncio.new_event_loop()
            mcp_fake._mcp_loop.set_exception_handler(mcp_fake._mcp_loop_exception_handler)
            mcp_fake._mcp_thread = threading.Thread(
                target=mcp_fake._mcp_loop.run_forever,
                name="mcp-event-loop",
                daemon=True,
            )
            mcp_fake._mcp_thread.start()

    return _ensure


def _make_fixed_ensure(mcp_fake):
    """已修复形态：win32 显式 asyncio.SelectorEventLoop()（PR #61445 形状）。

    源码含 'Selector' —— getsource 检测判为"上游已修复"，应跳过。
    """

    def _ensure():
        with mcp_fake._lock:
            if sys.platform == "win32":
                mcp_fake._mcp_loop = asyncio.SelectorEventLoop()
            else:
                mcp_fake._mcp_loop = asyncio.new_event_loop()
            mcp_fake._mcp_thread = threading.Thread(
                target=mcp_fake._mcp_loop.run_forever,
                name="mcp-event-loop",
                daemon=True,
            )
            mcp_fake._mcp_thread.start()

    return _ensure


def _shutdown_loop(mcp_fake):
    """停止并关闭 fake 模块里可能启动的后台 loop 线程，避免测试泄漏。"""
    loop = getattr(mcp_fake, "_mcp_loop", None)
    if loop is not None:
        try:
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        thread = getattr(mcp_fake, "_mcp_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        try:
            loop.close()
        except RuntimeError:
            pass
    mcp_fake._mcp_loop = None
    mcp_fake._mcp_thread = None


@pytest.fixture
def fake_mcp_tool(monkeypatch):
    """构造并注入 fake tools.mcp_tool；测试结束自动还原 sys.modules。"""
    fake = types.ModuleType("tools.mcp_tool")
    fake._lock = threading.Lock()
    fake._mcp_loop = None
    fake._mcp_thread = None
    fake._mcp_loop_exception_handler = _fake_exception_handler
    fake._ensure_mcp_loop = _make_original_ensure(fake)

    tools_pkg = sys.modules.get("tools")
    if tools_pkg is None:
        tools_pkg = types.ModuleType("tools")
        tools_pkg.__path__ = []
        monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setattr(tools_pkg, "mcp_tool", fake, raising=False)
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", fake)

    yield fake

    _shutdown_loop(fake)


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


def test_install_patches_on_windows(fake_mcp_tool, monkeypatch):
    """Windows 模拟下：install() 返回 True，_ensure_mcp_loop 被替换；幂等。"""
    monkeypatch.setattr(sys, "platform", "win32")
    original = fake_mcp_tool._ensure_mcp_loop

    assert fix.install() is True
    assert fake_mcp_tool._ensure_mcp_loop is not original  # 已替换

    # 幂等：第二次 install 不再重复替换
    patched = fake_mcp_tool._ensure_mcp_loop
    assert fix.install() is True
    assert fake_mcp_tool._ensure_mcp_loop is patched


def test_created_loop_is_selector_on_windows(fake_mcp_tool, monkeypatch):
    """Windows 模拟下：替换后的 _ensure_mcp_loop 创建 Selector 相关 loop。"""
    monkeypatch.setattr(sys, "platform", "win32")
    assert fix.install() is True

    fake_mcp_tool._ensure_mcp_loop()  # 首次触发：走 patched 实现

    loop = fake_mcp_tool._mcp_loop
    assert loop is not None
    assert isinstance(loop, asyncio.SelectorEventLoop)  # win32 = _WindowsSelectorEventLoop
    assert loop.is_running()
    assert fake_mcp_tool._mcp_thread is not None
    assert fake_mcp_tool._mcp_thread.is_alive()
    assert fake_mcp_tool._mcp_thread.name == "mcp-event-loop"  # 线程名与上游一致


def test_posix_behavior_unchanged(fake_mcp_tool, monkeypatch):
    """POSIX 模拟下：install() 返回 True 且完全不改动 _ensure_mcp_loop。"""
    monkeypatch.setattr(sys, "platform", "linux")
    original = fake_mcp_tool._ensure_mcp_loop

    assert fix.install() is True
    assert fake_mcp_tool._ensure_mcp_loop is original  # 未替换


def test_already_fixed_source_skipped(fake_mcp_tool, monkeypatch):
    """上游已修复形态（源码含 'Selector'）：install() 返回 True 且不重复 patch。"""
    monkeypatch.setattr(sys, "platform", "win32")
    fixed = _make_fixed_ensure(fake_mcp_tool)
    fake_mcp_tool._ensure_mcp_loop = fixed

    assert fix.install() is True
    assert fake_mcp_tool._ensure_mcp_loop is fixed  # 保持原样


def test_already_selector_loop_skipped(fake_mcp_tool, monkeypatch):
    """_mcp_loop 已是 SelectorEventLoop 实例：install() 跳过不重复 patch。"""
    monkeypatch.setattr(sys, "platform", "win32")
    existing = asyncio.SelectorEventLoop()
    fake_mcp_tool._mcp_loop = existing
    original = fake_mcp_tool._ensure_mcp_loop

    assert fix.install() is True
    assert fake_mcp_tool._ensure_mcp_loop is original  # 未替换


def test_install_returns_false_when_not_importable(fake_mcp_tool, monkeypatch):
    """tools.mcp_tool 不可 import 时：install() 返回 False（延迟重试契约）。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delitem(sys.modules, "tools.mcp_tool")
    monkeypatch.delattr(sys.modules["tools"], "mcp_tool")

    assert fix.install() is False
