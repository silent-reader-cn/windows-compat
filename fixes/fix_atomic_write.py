"""P3: bash-free atomic write for file tools.

Hermes' ``ShellFileOperations._atomic_write`` writes files by building a bash
script (``mktemp`` + ``cat >`` + ``mv``) and running it through the shell.
This makes every file write depend on the bash backend being healthy:

- 2026-08-10 incident: a broken ``HERMES_GIT_BASH_PATH`` made the gateway fall
  back to WSL bash, and EVERY write_file/patch failed with
  ``unexpected EOF while looking for matching "`'`` (quote convention mismatch).
- Bash spawn per write is also slower than a pure-Python rename.

This fix replaces ``_atomic_write`` with a pure-Python implementation
(``tempfile.mkstemp`` + ``os.replace``), mirroring the original semantics:

- symlink targets are followed (write the file the link points at)
- parent dir is created (``mkdir -p`` folded in)
- temp file lives in the SAME directory as the target (same-FS atomic rename)
- existing target's mode is preserved; new files get umask-default perms
- on any failure the temp file is removed and the original is untouched
- returns the same ``ExecuteResult`` contract (exit_code 0 == swapped in)

Risk: medium — needs verification of encoding (utf-8, newline="") and
Windows file-lock behavior, but the logic is strictly simpler than the
shell version.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile

logger = logging.getLogger(__name__)

PRIORITY = 3
RISK = "medium"
DESCRIPTION = (
    "Replace bash-script atomic write with pure Python (tempfile + os.replace): "
    "immune to WSL-bash-fallback file-tool failures (unexpected EOF class) "
    "and removes one subprocess spawn per write."
)


def _patched_atomic_write(self, path: str, content: str):
    """Pure-Python atomic write. Same contract as ShellFileOperations._atomic_write."""
    from tools.file_operations import ExecuteResult

    # Follow symlink target so we edit the file the link points at (mirror
    # original `readlink -f` behavior). Best-effort: broken link falls back.
    try:
        if os.path.islink(path):
            rt = os.path.realpath(path)
            if rt:
                path = rt
    except OSError:
        pass

    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        return ExecuteResult(exit_code=1, stdout=f"atomic write: mkdir -p failed: {exc}")

    # Preserve mode of an existing target (best-effort, never fatal).
    old_mode = None
    try:
        if os.path.exists(path):
            old_mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        pass

    fd, tmp = tempfile.mkstemp(prefix=".hermes-tmp.", dir=parent)
    try:
        # newline="" keeps \n as-is (no Windows \r\n translation) — matches
        # the original `cat > tmp` byte-for-byte behavior for str content.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        if old_mode is not None:
            os.chmod(tmp, old_mode)
        else:
            # New file: umask-default perms instead of mkstemp's hardcoded 0600.
            um = os.umask(0)
            os.umask(um)
            os.chmod(tmp, 0o666 & ~um)
        os.replace(tmp, path)
    except Exception as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return ExecuteResult(exit_code=1, stdout=f"atomic write failed: {exc}")

    return ExecuteResult(exit_code=0)


def install() -> bool:
    """Replace _atomic_write on ShellFileOperations. False if deferred."""
    try:
        from tools.file_operations import ShellFileOperations

        ShellFileOperations._atomic_write = _patched_atomic_write
        logger.info("windows-compat[atomic_write]: patched ShellFileOperations._atomic_write (pure Python)")
        return True
    except (ImportError, AttributeError) as exc:
        logger.debug("windows-compat[atomic_write]: deferred (%s)", exc)
        return False
