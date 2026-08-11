"""P1: allow CJK/Unicode characters in terminal tool's workdir parameter.

The terminal tool's ``workdir`` parameter is validated by ``_WORKDIR_SAFE_RE``,
which only allows ASCII characters — rejecting perfectly valid UTF-8 paths like
``末世生存小队``. This fix replaces the regex with a Unicode-aware version
(``\\w`` in Python 3 covers CJK/Cyrillic/Greek/accented Latin) while still
blocking shell metacharacters (``;`` ``|`` ``$`` backticks etc).

Risk: low. The regex is read once per validation; replacing it only widens
the accepted character class.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

PRIORITY = 1
RISK = "low"
DESCRIPTION = (
    "Allow CJK/Unicode characters in terminal tool's workdir parameter "
    "(e.g. 末世生存小队). Original _WORKDIR_SAFE_RE only accepts ASCII."
)

# \\w in Python 3 (re.UNICODE default) matches Unicode letters/digits/underscore
_NEW_SAFE_RE_PATTERN = r"^[\w/\\:_\-\.~ +@=,]+$"


def install() -> bool:
    """Replace _WORKDIR_SAFE_RE in tools.terminal_tool. Returns False if deferred."""
    try:
        from tools import terminal_tool  # editable install: no hermes_agent namespace

        new_re = re.compile(_NEW_SAFE_RE_PATTERN)
        terminal_tool._WORKDIR_SAFE_RE = new_re
        logger.info("windows-compat[workdir_cjk]: patched _WORKDIR_SAFE_RE -> %s", new_re.pattern)
        return True
    except (ImportError, AttributeError) as exc:
        logger.debug("windows-compat[workdir_cjk]: deferred — terminal_tool not importable yet (%s)", exc)
        return False
