"""P2: native Win32 programs must receive D:/x, not MSYS /d/x.

Root cause of the search_files Windows bug (#78224 family, 6+ open PRs upstream):

- Hermes sets ``MSYS_NO_PATHCONV=1`` + ``MSYS2_ARG_CONV_EXCL=*`` (deliberately,
  to stop ``cmd /c`` mangling), which disables MSYS automatic path conversion.
- Hermes' own ``_bash_safe_path`` then rewrites ``D:\\x`` -> ``/d/x`` (MSYS
  mount form) so bash builtins can eat it.
- But when the path is passed to a NATIVE Win32 program (WinGet ripgrep,
  node-based linters), ``/d/x`` is not converted back — native rg parses it as
  ``C:\\d\\x`` -> ``os error 3`` (or silent empty when stderr is swallowed).

Fix: return the drive path in ``D:/x`` forward-slash form instead of ``/d/x``.
Both bash builtins (``cd D:/x``, ``mv D:/a D:/b``) AND native Win32 programs
(rg, node) accept this form, so one form works everywhere — the translation
layer problem disappears instead of being patched.

Patch target: ``tools.environments.local._bash_safe_path``. file_operations
imports it *inside the function* (``from tools.environments.local import
_bash_safe_path``), so replacing the module attribute takes effect on the next
call — no timing hazards. Also handles MSYS leftover ``/d/x`` input (e.g. from
tools that already converted the path).

Risk: medium. Widely used (all shell file ops), but ``D:/x`` is a strict
superset of accepted path forms on Git Bash (verified: cd/mv/rg all accept it).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

PRIORITY = 2
RISK = "medium"
DESCRIPTION = (
    "Fix native Win32 programs (rg, node linters) receiving MSYS /d/x paths: "
    "return D:/x forward-slash form which both bash builtins and native "
    "programs accept. Fixes search_files os-error-3 / silent-empty, phantom lint."
)

_DRIVE_RE = re.compile(r"^([a-zA-Z]):[\\/]*(.*)$")
_MSYS_DRIVE_RE = re.compile(r"^/([a-zA-Z])/(.*)$")


def _patched_bash_safe_path(path: str) -> str:
    """D:\\x / D:/x / /d/x -> D:/x (forward slashes). One form works everywhere."""
    if not path:
        return path
    m = _DRIVE_RE.match(path)
    if m:
        drive = m.group(1).upper()
        tail = m.group(2).replace("\\", "/").lstrip("/")
        return f"{drive}:/{tail}" if tail else f"{drive}:/"
    m2 = _MSYS_DRIVE_RE.match(path)
    if m2:
        drive = m2.group(1).upper()
        tail = m2.group(2)
        return f"{drive}:/{tail}" if tail else f"{drive}:/"
    # normalize any remaining backslashes (mixed MSYS leftovers, non-drive paths)
    if "\\" in path:
        path = path.replace("\\", "/")
    return path


def install() -> bool:
    """Replace _bash_safe_path in tools.environments.local. False if deferred."""
    try:
        from tools.environments import local

        local._bash_safe_path = _patched_bash_safe_path
        logger.info(
            "windows-compat[bash_safe_path]: patched local._bash_safe_path "
            "(drive paths now D:/x form, native-program safe)"
        )
        return True
    except (ImportError, AttributeError) as exc:
        logger.debug("windows-compat[bash_safe_path]: deferred (%s)", exc)
        return False
