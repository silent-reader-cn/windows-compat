"""windows-compat — collection of Windows compatibility fixes for Hermes.

Each fix lives in ``fixes/`` as an independent module with a uniform
contract (PRIORITY / RISK / DESCRIPTION / install()). Every fix is
individually toggleable via ``config.json`` in this directory.

Recommended enable order (priority):
  P1 workdir_cjk      Allow CJK/Unicode paths as terminal workdir   (low risk)
  P2 bash_safe_path   Native programs get D:/x, not /d/x (search_files fix)
  P3 atomic_write     Bash-free atomic write (WSL-fallback immunity)

Patching follows the deferred pattern (module level -> register() ->
on_session_start) so fixes land as soon as their target module is
importable, and are retried on every new session.

``loaded.flag`` is written by register() with per-fix install status —
lets operators verify the plugin actually executed in the live gateway
process (plugin logs are not reliably captured).
"""

from __future__ import annotations

import json
import logging
import os
import sys

logger = logging.getLogger(__name__)

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

CONFIG_PATH = os.path.join(_PLUGIN_DIR, "config.json")

# Defaults: every fix enabled. config.json can disable any of them.
_DEFAULT_FIXES = {
    "workdir_cjk": {"enabled": True},
    "bash_safe_path": {"enabled": True},
    "atomic_write": {"enabled": True},
}

_INSTALLED: set[str] = set()
_STATUS: dict = {}


def _load_config() -> dict:
    cfg = {"fixes": dict(_DEFAULT_FIXES)}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user_cfg = json.load(f)
        fixes = user_cfg.get("fixes") or {}
        # Merge: missing keys fall back to defaults (enabled).
        merged = dict(_DEFAULT_FIXES)
        for name, opts in fixes.items():
            if name in merged:
                merged[name].update(opts)
            else:
                merged[name] = opts
        cfg["fixes"] = merged
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("windows-compat: config load failed (%s), using defaults", exc)
    return cfg


def _install_fix(name: str) -> None:
    if name in _INSTALLED:
        return
    try:
        # fix modules live in fixes/ as fix_<name>.py
        mod = __import__(f"fixes.fix_{name}", fromlist=["install"])
        ok = mod.install()
        if ok:
            _INSTALLED.add(name)
            _STATUS[name] = "installed"
            logger.info("windows-compat: fix '%s' installed (p%d, risk %s)", name, mod.PRIORITY, mod.RISK)
        else:
            _STATUS[name] = "deferred"
            logger.debug("windows-compat: fix '%s' deferred (will retry on session start)", name)
    except Exception as exc:
        import traceback
        _STATUS[name] = f"FAILED: {exc!r}\n{traceback.format_exc()}"
        logger.warning("windows-compat: fix '%s' install failed: %s", name, exc)


def _write_status() -> None:
    """Persist install status to loaded.flag for out-of-process verification."""
    try:
        import datetime
        with open(os.path.join(_PLUGIN_DIR, "loaded.flag"), "w", encoding="utf-8") as f:
            f.write(f"register at {datetime.datetime.now().isoformat()}\n")
            for k, v in _STATUS.items():
                f.write(f"fix {k}: {v}\n")
    except Exception:
        pass


def _apply_all() -> None:
    cfg = _load_config()
    for name, opts in cfg.get("fixes", {}).items():
        if opts.get("enabled", True):
            _install_fix(name)


def _on_session_start(**kwargs) -> None:
    """Retry pending fixes on each session start (gateway fully loaded by then)."""
    _apply_all()
    _write_status()


def register(ctx) -> None:
    _apply_all()
    ctx.register_hook("on_session_start", _on_session_start)
    _write_status()
    logger.info("windows-compat: plugin registered (fixes: %s)", ", ".join(sorted(_DEFAULT_FIXES)))
