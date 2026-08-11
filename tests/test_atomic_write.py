"""Tests for fix_atomic_write — bash-free atomic write (P3).

The pure-Python replacement must mirror the original shell semantics:
same-directory temp file, atomic rename, parent-dir creation, mode
preservation on overwrite, cleanup on failure.
"""

import sys
import os
import stat
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fixes.fix_atomic_write import _patched_atomic_write


def _write(path, content):
    return _patched_atomic_write(None, path, content)


def test_write_new_file():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "t.txt")
        r = _write(target, "hello\nworld\n")
        assert r.exit_code == 0
        with open(target, encoding="utf-8") as f:
            assert f.read() == "hello\nworld\n"


def test_utf8_content_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "cjk.txt")
        content = "柚子喵~ 中文内容 line2\n"
        assert _write(target, content).exit_code == 0
        with open(target, encoding="utf-8") as f:
            assert f.read() == content


def test_overwrite_existing():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "t.txt")
        _write(target, "old")
        r = _write(target, "new content")
        assert r.exit_code == 0
        with open(target, encoding="utf-8") as f:
            assert f.read() == "new content"


def test_nested_parent_dir_created():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "a", "b", "c.txt")
        assert _write(target, "nested").exit_code == 0
        assert os.path.exists(target)


def test_no_temp_leak_on_success():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "t.txt")
        _write(target, "x")
        leftovers = [f for f in os.listdir(d) if ".hermes-tmp" in f]
        assert leftovers == []


def test_mode_preserved_on_overwrite():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "t.txt")
        _write(target, "first")
        if os.name != "nt":  # chmod semantics differ on Windows
            os.chmod(target, 0o600)
            _write(target, "second")
            assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
