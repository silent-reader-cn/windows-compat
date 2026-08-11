"""Pytest fixtures: make fix modules importable WITHOUT a Hermes install.

``fix_atomic_write._patched_atomic_write`` imports ``ExecuteResult`` from
``tools.file_operations`` at call time. In CI (no Hermes checkout) that
import must resolve to a stand-in, so we synthesize a minimal
``tools.file_operations`` module with a compatible ``ExecuteResult``.
"""

import sys
import types


class _ExecuteResult:
    """Structural stand-in for Hermes' ExecuteResult (stdout + exit_code)."""

    def __init__(self, stdout: str = "", exit_code: int = 0):
        self.stdout = stdout
        self.exit_code = exit_code


def _install_fake_tools() -> None:
    if "tools.file_operations" in sys.modules:
        return  # real Hermes environment already present

    tools = types.ModuleType("tools")
    tools.__path__ = []
    sys.modules["tools"] = tools

    fo = types.ModuleType("tools.file_operations")
    fo.ExecuteResult = _ExecuteResult
    sys.modules["tools.file_operations"] = fo
    tools.file_operations = fo


_install_fake_tools()
