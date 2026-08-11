"""Tests for fix_uninstall_rmtree — Windows 鲁棒 rmtree 修复（issue #34185）。

自包含测试：用 fake ``hermes_cli.uninstall`` 模块注入 sys.modules，验证：

1. install() 在 Windows 上把模块内 shutil 换成代理；只读文件（真实文件系统）
   经 chmod+重试后整棵删除成功，无残留。
2. onerror 回调先 chmod(0o777) 再重试原操作。
3. 重试耗尽（进程锁定）不抛异常，跳过并记录告警。
4. POSIX 分支：install() 不 patch；_rmtree_win 原样透传调用方参数。
5. 上游源码已自带重试时跳过；目标模块不可导入时返回 False（延迟重试）。
6. 幂等；代理透传其他 shutil 属性；gui_uninstall 模块一并覆盖。

不依赖 Hermes 安装，不修改 conftest.py。
"""

import os
import shutil as real_shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fixes.fix_uninstall_rmtree as fix  # noqa: E402


# ---------------------------------------------------------------------------
# 测试基建：fake hermes_cli.uninstall 模块
# ---------------------------------------------------------------------------

def _make_fake_uninstall(*, rmtree_impl=None, log_msgs=None):
    """构造结构等同 hermes_cli.uninstall 的 fake 模块（shutil + log_warn）。"""
    mod = types.ModuleType("hermes_cli.uninstall")
    if rmtree_impl is not None:
        # 自定义 rmtree 实现的 shim（模拟"只读/锁定"失败场景）
        mod.shutil = types.SimpleNamespace(rmtree=rmtree_impl)
    else:
        mod.shutil = real_shutil  # 默认用真实 shutil（rmtree 即 stdlib 实现）
    _msgs = [] if log_msgs is None else log_msgs
    mod.log_warn = lambda msg: _msgs.append(msg)
    mod.log_msgs = _msgs
    return mod


def _install_fake(mod):
    """把 fake 模块注入 sys.modules，模拟 hermes_cli.uninstall 已可导入。"""
    pkg = sys.modules.get("hermes_cli")
    if pkg is None or not hasattr(pkg, "__path__"):
        pkg = types.ModuleType("hermes_cli")
        pkg.__path__ = []
        sys.modules["hermes_cli"] = pkg
    sys.modules["hermes_cli.uninstall"] = mod
    pkg.uninstall = mod
    return mod


def _force_win32(monkeypatch):
    """本模块内部统一按 win32 分支测试（真实机器通常就是 win32，显式更稳）。"""
    monkeypatch.setattr(fix, "_PLATFORM", "win32")


def _force_posix(monkeypatch):
    monkeypatch.setattr(fix, "_PLATFORM", "linux")


# ---------------------------------------------------------------------------
# 端到端：真实文件系统 + 代理 rmtree
# ---------------------------------------------------------------------------

def test_proxy_rmtree_cleans_readonly_tree(monkeypatch):
    """Windows 只读文件不再让 rmtree 中止：chmod+重试后整棵目录删除成功。"""
    mod = _make_fake_uninstall()
    _install_fake(mod)
    _force_win32(monkeypatch)
    assert fix.install() is True
    assert isinstance(mod.shutil, fix._ShutilProxy)

    d = tempfile.mkdtemp(prefix="wcompat_ro_")
    try:
        ro_file = os.path.join(d, "readonly.txt")
        with open(ro_file, "w", encoding="utf-8") as f:
            f.write("x")
        os.chmod(ro_file, 0o444)  # 只读 —— Windows 上裸 rmtree 必抛 PermissionError
        sub = os.path.join(d, "sub")
        os.makedirs(sub)
        nested = os.path.join(sub, "nested.txt")
        with open(nested, "w", encoding="utf-8") as f:
            f.write("y")
        os.chmod(nested, 0o444)

        mod.shutil.rmtree(d)  # 走代理 → _rmtree_win

        assert not os.path.exists(d), "只读文件不应再导致整棵目录残留"
        assert mod.log_msgs == [], "成功清理时不应产生跳过告警"
    finally:
        if os.path.exists(d):
            for root, _, files in os.walk(d):
                for f in files:
                    try:
                        os.chmod(os.path.join(root, f), 0o777)
                    except OSError:
                        pass
            real_shutil.rmtree(d, ignore_errors=True)


def test_proxy_rmtree_locked_file_skipped_not_crash(monkeypatch):
    """真被锁定的文件：不抛异常，跳过该文件，其余文件照删（核心修复语义）。"""
    mod = _make_fake_uninstall()
    _install_fake(mod)
    _force_win32(monkeypatch)
    assert fix.install() is True

    d = tempfile.mkdtemp(prefix="wcompat_lk_")
    locked = os.path.join(d, "locked.txt")
    free = os.path.join(d, "free.txt")
    try:
        with open(locked, "w", encoding="utf-8") as f:
            f.write("x")
            with open(free, "w", encoding="utf-8") as g:
                g.write("y")
            # 注意：free.txt 的句柄在此已关闭（可删），locked.txt 仍被 f 锁住
            # Windows 上无 FILE_SHARE_DELETE 的打开句柄会锁住 unlink；
            # 修复前这里直接抛 PermissionError，整棵目录删除中止。
            mod.shutil.rmtree(d)
        # 核心断言：绝不抛异常，且未被锁的文件一定删掉了
        assert not os.path.exists(free)
        if os.path.exists(locked):  # Windows 真锁住时：跳过 + 记录
            assert len(mod.log_msgs) >= 1
            assert "跳过无法删除的路径" in mod.log_msgs[0]
    finally:
        if os.path.exists(d):
            try:
                os.chmod(locked, 0o777)
            except OSError:
                pass
            real_shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# onerror 回调单元级验证（fake rmtree，确定性）
# ---------------------------------------------------------------------------

def test_readonly_failure_chmods_then_retries(monkeypatch):
    """onerror 语义：先 chmod(0o777) 清只读，再重试原操作；成功且无告警。"""
    _force_win32(monkeypatch)
    chmod_calls, unlink_calls, msgs = [], [], []
    monkeypatch.setattr(fix.os, "chmod", lambda p, m: chmod_calls.append((p, m)))
    monkeypatch.setattr(fix.os, "unlink", lambda p: unlink_calls.append(p))

    def fake_rmtree(path, ignore_errors=False, onerror=None):
        # 模拟 stdlib 语义：unlink 失败 → 调 onerror；回调返回则继续（不重抛）
        try:
            raise PermissionError("read-only file")
        except PermissionError:
            if onerror is not None:
                onerror(os.unlink, "C:/x/ro.txt", sys.exc_info())
                return
            raise

    fix._rmtree_win("C:/x", _real_rmtree=fake_rmtree, _log=msgs.append)

    assert chmod_calls == [("C:/x/ro.txt", fix._CHMOD_MODE)], "重试前必须 chmod 清只读"
    assert unlink_calls == ["C:/x/ro.txt"], "chmod 后必须重试原删除操作"
    assert msgs == []


def test_retry_exhaustion_skips_and_records(monkeypatch):
    """重试耗尽（进程锁定，chmod 也失败）：不抛异常，跳过并记录一条告警。"""
    _force_win32(monkeypatch)
    msgs = []
    monkeypatch.setattr(fix, "MAX_RETRIES", 2)
    monkeypatch.setattr(fix, "RETRY_DELAY", 0)

    def fake_chmod(p, m):
        raise PermissionError("locked by pid 1234")

    monkeypatch.setattr(fix.os, "chmod", fake_chmod)

    def fake_rmtree(path, ignore_errors=False, onerror=None):
        try:
            raise PermissionError("locked by pid 1234")
        except PermissionError:
            if onerror is not None:
                onerror(os.unlink, "C:/x/locked.dll", sys.exc_info())
                return
            raise

    # 不应抛任何异常
    fix._rmtree_win("C:/x", _real_rmtree=fake_rmtree, _log=msgs.append)

    assert len(msgs) == 1, "重试耗尽应恰好记录一条跳过告警"
    assert "locked.dll" in msgs[0]
    assert "锁定" in msgs[0]


def test_caller_onerror_preserved_on_win32(monkeypatch):
    """调用方自定义 onerror 时不得被覆盖（win32 分支也遵守）。"""
    _force_win32(monkeypatch)
    seen = {}
    my_onerror = lambda *a: None  # noqa: E731

    def fake_rmtree(path, ignore_errors=False, onerror=None):
        seen["onerror"] = onerror
        seen["ignore_errors"] = ignore_errors
        return "called"

    result = fix._rmtree_win(
        "C:/x", ignore_errors=True, onerror=my_onerror, _real_rmtree=fake_rmtree
    )
    assert result == "called"
    assert seen["onerror"] is my_onerror
    assert seen["ignore_errors"] is True


# ---------------------------------------------------------------------------
# POSIX 分支
# ---------------------------------------------------------------------------

def test_posix_install_noop(monkeypatch):
    """POSIX：install() 返回 True 且绝不 patch 模块。"""
    mod = _make_fake_uninstall()
    _install_fake(mod)
    _force_posix(monkeypatch)
    assert fix.install() is True
    assert not isinstance(mod.shutil, fix._ShutilProxy)


def test_posix_rmtree_delegates_unchanged(monkeypatch):
    """POSIX：_rmtree_win 原样委托真实 rmtree，调用方参数透传。"""
    _force_posix(monkeypatch)
    seen = {}
    my_onerror = lambda *a: None  # noqa: E731

    def real(path, ignore_errors=False, onerror=None):
        seen["onerror"] = onerror
        seen["ignore_errors"] = ignore_errors

    fix._rmtree_win("P:/x", ignore_errors=True, onerror=my_onerror, _real_rmtree=real)
    assert seen["ignore_errors"] is True
    assert seen["onerror"] is my_onerror, "POSIX 必须原样透传调用方 onerror"


# ---------------------------------------------------------------------------
# install() 检测 / 幂等 / 延迟
# ---------------------------------------------------------------------------

def test_install_idempotent(monkeypatch):
    """重复 install() 不二次包裹（幂等）。"""
    mod = _make_fake_uninstall()
    _install_fake(mod)
    _force_win32(monkeypatch)
    assert fix.install() is True
    proxy = mod.shutil
    assert isinstance(proxy, fix._ShutilProxy)
    assert fix.install() is True
    assert mod.shutil is proxy, "已 patch 过不得再次包裹"


def test_skip_when_upstream_already_robust(monkeypatch, tmp_path):
    """上游源码已自带 Windows 重试（源码含 S_IWRITE/chmod 标记）→ 跳过不 patch。"""
    src = tmp_path / "uninstall.py"
    src.write_text(
        "import shutil\n"
        "# upstream already does S_IWRITE chmod + retry\n"
        "shutil.rmtree(x, onerror=handler)\n",
        encoding="utf-8",
    )
    mod = _make_fake_uninstall()
    mod.__file__ = str(src)
    _install_fake(mod)
    _force_win32(monkeypatch)
    assert fix.install() is True
    assert not isinstance(mod.shutil, fix._ShutilProxy), "已修复的源码不应再被 patch"


def test_deferred_when_uninstall_not_importable(monkeypatch):
    """hermes_cli.uninstall 不可导入 → 返回 False（插件延迟重试）。"""
    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []  # 空路径 → 子模块找不到 → ImportError
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.delitem(sys.modules, "hermes_cli.uninstall", raising=False)
    _force_win32(monkeypatch)
    assert fix.install() is False


# ---------------------------------------------------------------------------
# 代理行为
# ---------------------------------------------------------------------------

def test_proxy_delegates_other_shutil_attrs():
    """代理只接管 rmtree，其余 shutil 属性（copy2/which 等）透传。"""
    proxy = fix._ShutilProxy(real_shutil)
    assert proxy.copy2 is real_shutil.copy2
    assert proxy.which is real_shutil.which


def test_install_also_patches_gui_uninstall(monkeypatch):
    """run_gui_uninstall 的删除路径（hermes_cli.gui_uninstall）一并覆盖。"""
    mod = _make_fake_uninstall()
    _install_fake(mod)
    gui = types.ModuleType("hermes_cli.gui_uninstall")
    gui.shutil = real_shutil
    gui.log_warn = lambda m: None
    sys.modules["hermes_cli.gui_uninstall"] = gui
    _force_win32(monkeypatch)
    assert fix.install() is True
    assert isinstance(gui.shutil, fix._ShutilProxy), "gui_uninstall 模块的 shutil 也应被替换"
