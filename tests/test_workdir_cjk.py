"""Tests for fix_workdir_cjk — Unicode workdir validation (P1).

The replacement regex must accept CJK/Unicode paths while still rejecting
shell metacharacters that could break out of the workdir argument.
"""

import sys
import os
import re

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fixes.fix_workdir_cjk import _NEW_SAFE_RE_PATTERN

_RE = re.compile(_NEW_SAFE_RE_PATTERN)


def test_cjk_path_accepted():
    assert _RE.match(r"D:\projects\末世生存小队")
    assert _RE.match(r"D:\worktrees\末社-动作系统研究")
    assert _RE.match(r"C:\Users\张三\数据")


def test_ascii_path_accepted():
    assert _RE.match(r"D:\projects\tile-stitching-editor")
    assert _RE.match("/d/projects/x")
    assert _RE.match("C:/Users/Admin")


def test_shell_metacharacters_rejected():
    # NOTE: trailing newline is intentionally NOT tested — Python re `$`
    # matches before a trailing \n, and Hermes' original regex has the same
    # semantics (workdir args come from JSON, never end with \n).
    for bad in [r"D:\x;rm -rf", r"D:\x|ls", r"D:\x$HOME", r"D:\x`id`", r"D:\x$(id)", r"D:\x&", "D:\\x\n;rm"]:
        assert not _RE.match(bad), f"should reject: {bad!r}"


def test_spaces_accepted():
    assert _RE.match(r"D:\my project\src")
