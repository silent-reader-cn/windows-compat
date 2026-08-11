"""Tests for fix_device_path_guard (#69373).

Verifies:
1. install() patches tools.file_tools._is_blocked_device_path on Windows.
2. The patched wrapper matches Windows-form paths (backslash-normalized)
   against the POSIX blocklist — i.e. '/dev/zero' -> '\\dev\\zero' is blocked.
3. POSIX-form paths still behave exactly like the original.
4. Re-running install() does not double-patch.
5. An already-fixed upstream implementation is detected and not re-patched.

These tests run WITHOUT a Hermes install: a fake ``tools.file_tools`` module
is injected into sys.modules with the same shapes as the real module.
"""

import os
import sys
import types

import pytest


class _FakeFileTools:
    """Structural stand-in for tools.file_tools (only what the fix touches)."""

    def __init__(self, *, already_fixed: bool = False):
        self._BLOCKED_DEVICE_PATHS = frozenset({
            "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
            "/dev/stdin", "/dev/tty", "/dev/console",
            "/dev/stdout", "/dev/stderr",
            "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
        })
        if already_fixed:
            def _orig(path: str) -> bool:
                # Windows: ntpath normpath yields backslashes; fold them so
                # the POSIX blocklist matches (upstream fix, issue #69373).
                normalized = os.path.normpath(path)
                if os.name == "nt":
                    normalized = normalized.replace("\\", "/")
                return normalized in self._BLOCKED_DEVICE_PATHS
            self._is_blocked_device_path = _orig
        else:
            def _orig(path: str) -> bool:
                normalized = os.path.normpath(path)
                return normalized in self._BLOCKED_DEVICE_PATHS
            self._is_blocked_device_path = _orig
        self._MARKER = "_windows_compat_device_guard_patched"


@pytest.fixture()
def fake_file_tools(monkeypatch, request):
    """Inject a fake tools.file_tools module; return the fake object."""
    already_fixed = getattr(request, "param", False)
    fake = _FakeFileTools(already_fixed=already_fixed)
    mod = types.ModuleType("tools.file_tools")
    mod._BLOCKED_DEVICE_PATHS = fake._BLOCKED_DEVICE_PATHS
    mod._is_blocked_device_path = fake._is_blocked_device_path
    mod._MARKER = fake._MARKER

    tools = types.ModuleType("tools")
    tools.__path__ = []
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.file_tools", mod)
    return mod


@pytest.fixture()
def patch_os_name(monkeypatch):
    """Force os.name to 'nt' inside install() (Windows simulation)."""
    monkeypatch.setattr(os, "name", "nt")
    yield os.name


def _load_fix():
    from fixes import fix_device_path_guard as f
    return f


def test_install_patches_on_windows(fake_file_tools, patch_os_name):
    fix = _load_fix()
    assert fix.install() is True
    fn = fake_file_tools._is_blocked_device_path
    assert getattr(fn, fix._MARKER, False) is True, "wrapper marker missing"


def test_windows_backslash_path_now_blocked(fake_file_tools, patch_os_name):
    fix = _load_fix()
    fix.install()
    # On Windows, normpath('/dev/zero') == '\\dev\\zero' (backslashes).
    # The original implementation would MISS this; the patched one must catch it.
    assert fake_file_tools._is_blocked_device_path("/dev/zero") is True
    assert fake_file_tools._is_blocked_device_path("C:/Users/admin/x.txt") is False


def test_posix_behaviour_preserved(fake_file_tools, patch_os_name):
    fix = _load_fix()
    fix.install()
    # /dev/zero direct POSIX form still blocked; normal files not blocked.
    assert fake_file_tools._is_blocked_device_path("/dev/random") is True
    assert fake_file_tools._is_blocked_device_path("/home/user/notes.md") is False


def test_proc_environ_family_blocked(fake_file_tools, patch_os_name):
    fix = _load_fix()
    fix.install()
    # Windows-form /proc path must still hit the POSIX prefix check.
    assert fake_file_tools._is_blocked_device_path("/proc/123/environ") is True


def test_no_double_patch(fake_file_tools, patch_os_name):
    fix = _load_fix()
    assert fix.install() is True
    first = fake_file_tools._is_blocked_device_path
    assert fix.install() is True  # idempotent
    assert fake_file_tools._is_blocked_device_path is first


@pytest.mark.parametrize("fake_file_tools", [True], indirect=True)
def test_already_fixed_upstream_skipped(fake_file_tools, patch_os_name):
    fix = _load_fix()
    assert fix.install() is True
    # The fake upstream implementation already does replace('\\', '/').
    # Our patch must NOT wrap it (no marker), and behaviour must be intact.
    fn = fake_file_tools._is_blocked_device_path
    assert getattr(fn, fix._MARKER, False) is False
    assert fn("/dev/zero") is True
