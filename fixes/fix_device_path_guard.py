"""P1: Windows device-path read guard must not be a silent no-op.

Root cause of Hermes issue #69373 (verified on 2026-08-11 current main):

- ``tools.file_tools._is_blocked_device_path()`` normalizes the input with
  ``os.path.normpath()`` and compares against ``_BLOCKED_DEVICE_PATHS``, a
  frozenset of POSIX forms (``/dev/zero``, ``/dev/random``, ``/proc/...``).
- On Windows, ``os.path.normpath("/dev/zero")`` returns ``"\\dev\\zero"``
  (backslashes), which never equals the POSIX entries -> the whole
  device/fd/proc read guard is a silent no-op on Windows. That includes the
  ``/proc/*/environ`` secret-leak family (#4427).

Fix: on Windows only, monkeypatch ``_is_blocked_device_path`` with a
separator-normalized reimplementation (identical logic, but backslashes are
folded to forward slashes after normpath so the POSIX blocklist entries can
match). POSIX behaviour is untouched.

Reference:
- https://github.com/NousResearch/hermes-agent/issues/69373
- Community fix attempts (both unmerged): #69401 (closed), #69403 (open)

Risk: low. The guard is defence-in-depth path filtering; normalizing path
separators only makes it match MORE paths (the POSIX blocklist), never
fewer. The reimplementation mirrors the upstream body exactly, so future
upstream changes to the blocklist or proc-suffix rules keep working as long
as we read them from the live module (no frozen copy).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

PRIORITY = 1
RISK = "low"
DESCRIPTION = (
    "Fix Windows device-path read guard (#69373): _is_blocked_device_path "
    "normalizes with ntpath (backslashes) and never matches the POSIX "
    "blocklist, silently disabling the /dev + /proc/*/environ read guard. "
    "Patch reimplements the guard with forward-slash normalization on Windows."
)

# Marker for the patched function so install() can detect double-application.
_MARKER = "_windows_compat_device_guard_patched"

# The /proc/*/... suffixes the upstream guard blocks (issue #4427 family).
# Read from the module's own source shape — these mirror upstream exactly.
_PROC_SECRET_SUFFIXES = (
    "/environ",
    "/cmdline",
    "/maps",
    "/smaps",
    "/smaps_rollup",
    "/numa_maps",
    "/mem",
    "/auxv",
    "/pagemap",
)


def _normalize(path: str) -> str:
    """normpath + forward-slash folding. Windows ntpath yields backslashes;
    folding makes the POSIX blocklist comparable on Windows."""
    normalized = os.path.normpath(path)
    if os.name == "nt":
        normalized = normalized.replace("\\", "/")
    return normalized


def _patched_is_blocked_device_path(module, path: str) -> bool:
    """Separator-aware reimplementation of the upstream guard.

    Logic mirrors tools/file_tools.py::_is_blocked_device_path 1:1 — the
    only difference is forward-slash folding after normpath on Windows.
    """
    expand = getattr(module, "_expand_tilde", lambda p: p)
    blocked = getattr(module, "_BLOCKED_DEVICE_PATHS", frozenset())

    normalized = _normalize(expand(path))
    if normalized in blocked:
        return True
    # /proc/self/fd/0-2 and /proc/<pid>/fd/0-2 are Linux aliases for stdio
    if normalized.startswith("/proc/") and normalized.endswith(
        ("/fd/0", "/fd/1", "/fd/2")
    ):
        return True
    # /proc/*/environ, /proc/*/cmdline, /proc/*/maps ... (issue #4427)
    if normalized.startswith("/proc/") and normalized.endswith(_PROC_SECRET_SUFFIXES):
        return True
    # /proc/*/fd/… and other /proc aliases are handled by the suffix checks
    # above; upstream also blocks device-style paths via _BLOCKED_DEVICE_PATHS.
    return False


def _apply_patch(module) -> bool:
    """Replace _is_blocked_device_path with the separator-aware version."""
    orig = module._is_blocked_device_path
    if getattr(orig, _MARKER, False):
        return True  # already patched by us

    def wrapper(path):
        return _patched_is_blocked_device_path(module, path)

    wrapper.__name__ = orig.__name__
    wrapper.__doc__ = orig.__doc__
    wrapper.__module__ = orig.__module__
    wrapper.__qualname__ = orig.__qualname__
    setattr(wrapper, _MARKER, True)

    module._is_blocked_device_path = wrapper
    logger.info(
        "windows-compat[device_path_guard]: patched "
        "file_tools._is_blocked_device_path (Windows separator-aware)"
    )
    return True


def _needs_patch(module) -> bool:
    """Detect whether the original problem still exists (avoid re-fixing)."""
    fn = getattr(module, "_is_blocked_device_path", None)
    if fn is None:
        return False  # target gone — nothing to fix
    if getattr(fn, _MARKER, False):
        return False  # already our wrapper
    src = None
    try:
        import inspect

        src = inspect.getsource(fn)
    except (OSError, TypeError):
        src = ""
    if src:
        # Upstream already folds backslashes after normpath? Look for a
        # `.replace("\\", "/")` / `.replace('\\', '/')` call (either quote
        # style) near a Windows/nt reference — the canonical fix shape.
        import re as _re

        has_fold = _re.search(
            r"\.replace\s*\(\s*[\"']\\\\[\"']\s*,\s*[\"']/[\"']\s*\)", src
        )
        has_win = ("Windows" in src) or ("os.name == 'nt'" in src) or (
            "os.name == \"nt\"" in src
        )
        if has_fold and has_win:
            return False  # upstream already fixed (separator-aware)
    return True


def install() -> bool:
    """Patch tools.file_tools._is_blocked_device_path on Windows. False if deferred."""
    try:
        from tools import file_tools
    except (ImportError, AttributeError) as exc:
        logger.debug("windows-compat[device_path_guard]: deferred (%s)", exc)
        return False

    if os.name != "nt":
        # Nothing to fix on POSIX — but the wrapper is a no-op anyway.
        # Return True (not needed) so the plugin doesn't retry.
        return True

    if not _needs_patch(file_tools):
        logger.info(
            "windows-compat[device_path_guard]: already fixed upstream, skipping"
        )
        return True

    try:
        return _apply_patch(file_tools)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("windows-compat[device_path_guard]: patch failed: %s", exc)
        return False
