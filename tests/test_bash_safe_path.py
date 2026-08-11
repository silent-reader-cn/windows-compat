"""Tests for fix_bash_safe_path — the native-path translation fix (P2).

The patched ``_bash_safe_path`` must return ``D:/x`` forward-slash form for
every input style (native backslash, forward slash, MSYS mount form) so that
BOTH bash builtins and native Win32 programs (rg, node) can consume the path.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fixes.fix_bash_safe_path import _patched_bash_safe_path


def test_native_backslash_drive_path():
    assert _patched_bash_safe_path(r"D:\projects\末世生存小队") == "D:/projects/末世生存小队"


def test_native_forward_slash_path():
    assert _patched_bash_safe_path("D:/projects/tile-stitching-editor") == "D:/projects/tile-stitching-editor"


def test_lowercase_drive_normalized():
    assert _patched_bash_safe_path(r"d:\proj\sub") == "D:/proj/sub"


def test_drive_root_only():
    assert _patched_bash_safe_path("D:\\") == "D:/"
    assert _patched_bash_safe_path("C:") == "C:/"


def test_msys_mount_form_converted():
    # MSYS leftovers (/d/x) must become native D:/x — native rg can't eat /d/x
    assert _patched_bash_safe_path("/d/projects/x") == "D:/projects/x"
    assert _patched_bash_safe_path("/c/Users/Admin/foo") == "C:/Users/Admin/foo"


def test_non_drive_posix_path_unchanged():
    assert _patched_bash_safe_path("/usr/local/bin") == "/usr/local/bin"
    assert _patched_bash_safe_path("relative/path") == "relative/path"


def test_empty_and_none_safe():
    assert _patched_bash_safe_path("") == ""


def test_mixed_backslash_non_drive_normalized():
    assert _patched_bash_safe_path("foo\\bar") == "foo/bar"


def test_unicode_roundtrip():
    assert _patched_bash_safe_path(r"D:\worktrees\末社-动作系统研究") == "D:/worktrees/末社-动作系统研究"
