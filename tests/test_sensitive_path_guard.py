"""Tests for fix_sensitive_path_guard — Windows 敏感路径写守卫恢复（P1）。

自包含：向 sys.modules 注入 fake tools.file_tools（模拟 Hermes 源码
tools/file_tools.py 中 _check_sensitive_path 的原始 bug 形态），不依赖
Hermes 安装、不修改 conftest.py。

模拟两种平台形态（跨平台测试确定性）：
- posix_sim=False（默认）：os.path.normpath 产出反斜杠（Windows 行为，
  即 bug 触发形态）
- posix_sim=True：产出正斜杠（POSIX 行为，验证「修复后行为不变」）

参考：PR #78079（https://github.com/NousResearch/hermes-agent/pull/78079，
被关闭为重复，superset #76247 未合并）；根因同类 issue #51474。
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fixes.fix_sensitive_path_guard import install  # noqa: E402

# 与 Hermes 源码一致的敏感前缀 / 精确路径（fake 模块用）
_PREFIXES = (
    "/etc/", "/boot/", "/usr/lib/systemd/",
    "/private/etc/", "/private/var/db/", "/private/var/root/",
)
_EXACT = {"/var/run/docker.sock", "/run/docker.sock"}
_ERR = "Refusing to write to sensitive system path: {filepath}\n..."


def _make_fake_file_tools(posix_sim: bool = False, fixed=None):
    """构造模拟 Hermes tools/file_tools.py 的假模块（含 bug 原始形态函数）。"""

    def _norm(path: str) -> str:
        # 模拟目标平台的 os.path.normpath 输出形态（与宿主机平台无关，确定性）
        n = os.path.normpath(path)
        if posix_sim:
            return n.replace("\\", "/")
        return n.replace("/", "\\")

    def _expand_tilde(path: str) -> str:
        return os.path.expanduser(path) if "~" in path else path

    def _resolve_path_for_task(filepath: str, task_id: str = "default"):
        return _norm(filepath)

    def _buggy(filepath: str, task_id: str = "default"):
        # 与 Hermes 源码同构的原始形态：normpath 后直接与 POSIX 前缀比对
        try:
            resolved = str(_resolve_path_for_task(filepath, task_id))
        except (OSError, ValueError):
            resolved = filepath
        normalized = _norm(_expand_tilde(filepath))
        for prefix in _PREFIXES:
            if resolved.startswith(prefix) or normalized.startswith(prefix):
                return _ERR.format(filepath=filepath)
        if resolved in _EXACT or normalized in _EXACT:
            return _ERR.format(filepath=filepath)
        return None

    mod = types.ModuleType("tools.file_tools")
    mod._SENSITIVE_PATH_PREFIXES = _PREFIXES
    mod._SENSITIVE_EXACT_PATHS = _EXACT
    mod._expand_tilde = _expand_tilde
    mod._resolve_path_for_task = _resolve_path_for_task
    mod._check_sensitive_path = fixed if fixed is not None else _buggy
    return mod


def _inject(monkeypatch, mod):
    """把 sys.modules['tools'] 替换为假包并注入 file_tools（完全隔离真实 Hermes）。"""
    pkg = types.ModuleType("tools")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.setitem(sys.modules, "tools.file_tools", mod)
    monkeypatch.setattr(pkg, "file_tools", mod, raising=False)
    return pkg


def _fixed_by_marker(filepath: str, task_id: str = "default"):
    """已修复形态：源码含本插件 marker（windows-compat[sensitive_path_guard]）。"""
    return None


def _fixed_by_replace(filepath: str, task_id: str = "default"):
    """已修复形态：源码含正斜杠归一化处理 .replace("\\", "/")。"""
    p = filepath.replace("\\", "/")
    return None


# ---------------------------------------------------------------------------
# Windows 形态（bug 触发）：原始形态放行 → 修复后必须拦截
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/etc/hosts",
    r"\etc\hosts",
    "C:/etc/hosts",
    r"C:\etc\hosts",
    "/boot/grub/grub.cfg",
    r"\boot\grub\grub.cfg",
    "/usr/lib/systemd/system/evil.service",
    "C:/Windows/System32/drivers/etc/hosts",   # 附加的 Windows 系统目录前缀
    "/var/run/docker.sock",                     # exact：Windows 形态下原函数放行
    r"\var\run\docker.sock",
    "C:/var/run/docker.sock",
])
def test_windows_sim_sensitive_blocked(monkeypatch, path):
    mod = _make_fake_file_tools(posix_sim=False)
    _inject(monkeypatch, mod)
    # 修复前：原始形态确实放行（复现 bug）
    assert mod._check_sensitive_path(path) is None
    assert install() is True
    # 修复后：被拦截，且返回错误消息
    err = mod._check_sensitive_path(path)
    assert err is not None
    assert "Refusing to write" in err


@pytest.mark.parametrize("path", [
    "C:/Users/Admin/Desktop/notes.txt",
    r"C:\Users\Admin\Desktop\note.txt",
    "D:/projects/windows-compat/foo.txt",
    "C:/Program Files/App/conf.json",
    "relative/path.txt",
    r"worktrees\末社\a.txt",
    "~/notes.txt",
    "/c/Users/Admin/foo.txt",   # MSYS 形态 → posixify 后 /c/... 不匹配前缀
])
def test_windows_sim_normal_allowed(monkeypatch, path):
    mod = _make_fake_file_tools(posix_sim=False)
    _inject(monkeypatch, mod)
    assert install() is True
    assert mod._check_sensitive_path(path) is None


# ---------------------------------------------------------------------------
# POSIX 形态：修复前后判定完全一致（行为不变）
# ---------------------------------------------------------------------------

def test_posix_sim_behavior_unchanged(monkeypatch):
    mod = _make_fake_file_tools(posix_sim=True)
    _inject(monkeypatch, mod)
    paths = [
        "/etc/hosts", "/boot/vmlinuz", "/usr/lib/systemd/system/x.service",
        "/private/etc/passwd", "/var/run/docker.sock",
        "/home/user/foo.txt", "/tmp/x.txt", "~/a.txt", "rel/path.txt",
        "/var/run/user/1000/x.sock",
    ]
    before = {p: mod._check_sensitive_path(p) for p in paths}
    assert install() is True
    after = {p: mod._check_sensitive_path(p) for p in paths}
    assert [a is not None for a in after.values()] == [b is not None for b in before.values()]


# ---------------------------------------------------------------------------
# 检测逻辑：已修复形态不重复 patch；目标不可 import 时延迟重试
# ---------------------------------------------------------------------------

def test_skipped_when_marker_present(monkeypatch):
    mod = _make_fake_file_tools(fixed=_fixed_by_marker)
    _inject(monkeypatch, mod)
    orig_fn = mod._check_sensitive_path
    assert install() is True
    assert mod._check_sensitive_path is orig_fn  # 未重复 patch


def test_skipped_when_replace_normalization_present(monkeypatch):
    mod = _make_fake_file_tools(fixed=_fixed_by_replace)
    _inject(monkeypatch, mod)
    orig_fn = mod._check_sensitive_path
    assert install() is True
    assert mod._check_sensitive_path is orig_fn  # 未重复 patch


def test_double_install_no_double_wrap(monkeypatch):
    mod = _make_fake_file_tools(posix_sim=False)
    _inject(monkeypatch, mod)
    assert install() is True
    w1 = mod._check_sensitive_path
    assert install() is True
    assert mod._check_sensitive_path is w1  # 第二次安装不重复包装
    assert mod._check_sensitive_path("/etc/hosts") is not None


def test_deferred_when_file_tools_missing(monkeypatch):
    """tools.file_tools 不可 import → install() 返回 False（延迟重试）。"""
    pkg = types.ModuleType("tools")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.delitem(sys.modules, "tools.file_tools", raising=False)
    assert install() is False
