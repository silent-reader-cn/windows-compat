"""P1: 修复 Windows 上 stdio MCP 服务器全部连不上的问题（Hermes issue #61444）。

根因（issue #61444；社区 PR #61445 未合并，open）：
    tools/mcp_tool.py::_ensure_mcp_loop() 用 asyncio.new_event_loop() 创建
    后台事件循环线程。Windows 上 Python 3.8+ 的 new_event_loop() 默认返回
    ProactorEventLoop；MCP SDK 的 stdio_client 通过子进程管道读取服务器的
    initialize 响应，Proactor 的管道读取在这个后台线程上永不完成，握手一直
    挂到 connect_timeout —— 所以 Windows 上没有任何 stdio MCP 服务器能连上
    （hermes mcp test 打印头部后挂起，mcp__* 工具注册数为零）。

修复方式（与仓库既有 win32 Selector 模式一致，参考 hermes_cli/web_server.py
    17925-17946 的 WindowsSelectorEventLoopPolicy 处理，以及 cli.py）：
    monkeypatch 替换 mcp_tool._ensure_mcp_loop：Windows 上显式用
    asyncio.SelectorEventLoop() 创建后台循环 —— 该构造在 win32 上实际返回
    _WindowsSelectorEventLoop（支持 stdio 子进程管道，PR #61445 在原生
    Win11 + CPython 3.11 实测验证）；POSIX 保持 asyncio.new_event_loop()
    不变。不动 Hermes 源码，只影响 Windows。

检测逻辑（只在问题真实存在时 patch）：
    1. 非 win32 → 问题不存在，直接返回 True 不 patch；
    2. _ensure_mcp_loop 源码含 'Selector'（上游已修复）→ 跳过；
    3. _mcp_loop 实例已是 SelectorEventLoop → 跳过；
    4. 否则替换 _ensure_mcp_loop 为 Windows 感知版本。

风险：medium。替换的是 MCP 核心生命周期函数，但仅在 win32 且问题真实
    存在时生效；POSIX 路径完全不改；幂等（已修复形态不会重复 patch）。

参考：
    https://github.com/NousResearch/hermes-agent/issues/61444
    https://github.com/NousResearch/hermes-agent/pull/61445
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import threading

logger = logging.getLogger(__name__)

PRIORITY = 1
RISK = "medium"
DESCRIPTION = (
    "Fix all stdio MCP servers failing to connect on Windows (issue #61444): "
    "mcp_tool._ensure_mcp_loop() gets a ProactorEventLoop from "
    "asyncio.new_event_loop() on win32, which hangs stdio_client's initialize "
    "handshake. Patch _ensure_mcp_loop to build an explicit SelectorEventLoop "
    "on win32; POSIX unchanged."
)


def _build_patched_ensure_mcp_loop(mcp_tool):
    """构造 Windows 感知的 _ensure_mcp_loop 替代实现（闭包绑定模块对象）。

    所有全局读写都走模块属性（mcp_tool._mcp_loop 等），等价于上游函数体
    里的 global 赋值，但不会污染本模块的 globals；线程安全语义与上游
    一致（模块锁 + 进入锁后二次检查，避免竞态重复创建）。
    """
    # 上游模块级锁；若未来重构移除，退回独立锁保证互斥语义。
    lock = getattr(mcp_tool, "_lock", None) or threading.Lock()
    # 上游异常处理器（抑制 "Event loop is closed" 关闭噪音）；缺失则跳过。
    exc_handler = getattr(mcp_tool, "_mcp_loop_exception_handler", None)

    def _ensure_mcp_loop_selector():
        """启动后台事件循环线程：win32 用 Selector，其他平台保持默认。"""
        # 无锁快检：已运行则直接返回（与上游语义一致）
        if getattr(mcp_tool, "_mcp_loop", None) is not None and mcp_tool._mcp_loop.is_running():
            return
        with lock:
            # 双检：进入锁后再次确认，避免竞态重复创建
            if getattr(mcp_tool, "_mcp_loop", None) is not None and mcp_tool._mcp_loop.is_running():
                return
            if sys.platform == "win32":
                # issue #61444：win32 上 new_event_loop() 返回 ProactorEventLoop，
                # stdio_client 的管道读取永不完成。显式 SelectorEventLoop：
                # Windows 上该构造即 _WindowsSelectorEventLoop，支持子进程管道。
                try:
                    loop = asyncio.SelectorEventLoop()
                except Exception:
                    # 极端兜底：构造失败退回默认，避免直接崩溃
                    loop = asyncio.new_event_loop()
            else:
                loop = asyncio.new_event_loop()
            if exc_handler is not None:
                loop.set_exception_handler(exc_handler)
            mcp_tool._mcp_loop = loop
            mcp_tool._mcp_thread = threading.Thread(
                target=loop.run_forever,
                name="mcp-event-loop",  # 与上游线程名保持一致
                daemon=True,
            )
            mcp_tool._mcp_thread.start()

    return _ensure_mcp_loop_selector


def install() -> bool:
    """在 Windows 上替换 mcp_tool._ensure_mcp_loop 为 Selector 感知版本。

    返回 True = 已就绪（已 patch、或问题不存在、或上游已修复）；
    返回 False = 目标模块尚不可 import，延迟到下次会话重试。
    """
    try:
        from tools import mcp_tool
    except (ImportError, AttributeError) as exc:
        logger.debug(
            "windows-compat[mcp_selector_loop]: deferred — tools.mcp_tool not importable (%s)", exc
        )
        return False

    try:
        # 1) 非 Windows：问题不存在，直接返回 True，不做任何改动。
        if sys.platform != "win32":
            logger.debug("windows-compat[mcp_selector_loop]: not win32, nothing to do")
            return True

        # 2) 已修复检测：上游源码已改用 Selector（源码含 'Selector'），或
        #    _mcp_loop 实例已经是 SelectorEventLoop —— 都不重复 patch。
        src = ""
        try:
            src = inspect.getsource(mcp_tool._ensure_mcp_loop) or ""
        except (OSError, TypeError):
            src = ""  # 源码不可得（动态模块等），退化为下面的实例检测
        if "Selector" in src:
            logger.info(
                "windows-compat[mcp_selector_loop]: _ensure_mcp_loop already selector-aware, skip"
            )
            return True
        cur_loop = getattr(mcp_tool, "_mcp_loop", None)
        if cur_loop is not None and isinstance(cur_loop, asyncio.SelectorEventLoop):
            logger.info(
                "windows-compat[mcp_selector_loop]: MCP loop already a SelectorEventLoop, skip"
            )
            return True

        # 3) 结构变化防护：_ensure_mcp_loop 必须是可替换的可调用对象。
        if not callable(getattr(mcp_tool, "_ensure_mcp_loop", None)):
            logger.warning(
                "windows-compat[mcp_selector_loop]: _ensure_mcp_loop missing or not "
                "callable, skip (upstream structure changed)"
            )
            return True  # 无法修复也不阻塞：返回 True 避免无限重试

        # 4) 替换为 Windows 感知版本（发生在 new_event_loop() 调用点之前：
        #    后续任何 MCP 连接首次触发 _ensure_mcp_loop 时都会走新实现）。
        mcp_tool._ensure_mcp_loop = _build_patched_ensure_mcp_loop(mcp_tool)
        logger.info(
            "windows-compat[mcp_selector_loop]: patched mcp_tool._ensure_mcp_loop "
            "(win32 -> SelectorEventLoop, fixes #61444)"
        )
        return True
    except (ImportError, AttributeError) as exc:
        logger.debug("windows-compat[mcp_selector_loop]: deferred (%s)", exc)
        return False
