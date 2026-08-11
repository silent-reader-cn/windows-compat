# windows-compat

![CI](https://github.com/silent-reader-cn/windows-compat/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

A **Hermes Agent plugin** that collects Windows compatibility fixes in one
place — each fix independently toggleable, documented, and ordered by
recommended enablement priority.

## Why this exists

Hermes on Windows executes shell commands through Git Bash (MSYS). MSYS path
translation is incompatible with native Win32 programs in several known ways:

| Symptom | Root cause |
|---|---|
| `search_files` on absolute drive paths returns empty / `os error 3` | `_bash_safe_path` rewrites `D:\x` → `/d/x` (MSYS form), but native `rg.exe` can't eat `/d/x` when Hermes disables MSYS auto-conversion (`MSYS_NO_PATHCONV=1`) |
| `phantom lint` false errors on Windows | same `/d/x` form reaches node-based linters |
| ALL file tools break when bash backend falls back to WSL (`unexpected EOF` class) | `_atomic_write` shells out to bash (`mktemp` + `cat` + `mv`) |
| terminal `workdir` rejects Chinese paths like `末世生存小队` | `_WORKDIR_SAFE_RE` validator is ASCII-only |

Upstream fixes for these have been proposed repeatedly
([#78224](https://github.com/NousResearch/hermes-agent/pull/78224) and 6+
sibling PRs) and remain unmerged. This plugin delivers the fixes locally via
the official plugin mechanism — no source patches, survives Hermes upgrades.

## Fixes (recommended order)

| # | ID | Risk | What it does |
|---|---|---|---|
| P1 | `workdir_cjk` | low | Terminal `workdir` accepts Unicode/CJK paths; still blocks shell metacharacters |
| P2 | `bash_safe_path` | medium | Paths are emitted as `D:/x` forward-slash form — accepted by BOTH bash builtins and native Win32 programs. Fixes `search_files` + linters on Windows |
| P3 | `atomic_write` | medium | File writes use pure Python (`tempfile` + `os.replace`) instead of a bash script — immune to bash/WSL backend failures, one fewer subprocess per write |
| P4 | `device_path_guard` | low | Windows device-path read guard was a silent no-op (`normpath` yields backslashes, never matching the POSIX `/dev` + `/proc/*/environ` blocklist). Issue #69373 |
| P5 | `memory_lock` | medium | Windows `memory` tool crashed with `Errno 36` (EDEADLK) — bare `msvcrt.locking(LK_LOCK)` under the multi-threaded gateway. Adds jittered retry + graceful lock-free degrade. Issue #31738 |
| P6 | `sensitive_path_guard` | low | Windows sensitive-path write guard (`/etc/` etc.) never matched backslash-normalized paths — file tools could write system files. Issue #78079 / #76247 |
| P7 | `mcp_selector_loop` | medium | Windows MCP stdio servers hung — background event loop used `ProactorEventLoop`; now forced to `SelectorEventLoop`. Issue #61444 / PR #61445 |
| P8 | `kanban_zombie` | medium | Windows kanban workers kept running after `complete` (zombie processes mutating browser/workspace). Completes now reap the worker process tree. Issue #61923 |
| P9 | `uninstall_rmtree` | low | Windows `hermes uninstall` left data behind — `rmtree` fails on read-only/locked files; now chmod-then-retry without aborting. Issue #34185 / PR #34364 |
| P10 | `update_nul_preflight` | low | Windows `hermes update` hung in `git stash` — reserved device names (`nul`, `con`, …) break git. Preflight renames them before autostash. Issue #57081 / PR #57212 |

## Install

```bash
# 1. Clone / copy this repo into Hermes' user plugins directory
git clone https://github.com/silent-reader-cn/windows-compat.git \
  "$HOME/AppData/Local/hermes/plugins/windows-compat"

# 2. Enable the plugin (takes effect on next process start)
hermes plugins enable windows-compat

# 3. Restart the Hermes WebUI / gateway process
```

All fixes are enabled by default. Toggle any of them in `config.json`:

```json
{
  "fixes": {
    "workdir_cjk":          { "enabled": true },
    "bash_safe_path":       { "enabled": true },
    "atomic_write":         { "enabled": true },
    "device_path_guard":    { "enabled": true },
    "memory_lock":          { "enabled": true },
    "sensitive_path_guard": { "enabled": true },
    "mcp_selector_loop":    { "enabled": true },
    "kanban_zombie":        { "enabled": true },
    "uninstall_rmtree":     { "enabled": true },
    "update_nul_preflight": { "enabled": true }
  }
}
```

## Verify

The plugin writes `loaded.flag` (in the plugin directory) every time it
registers, with per-fix install status:

```
register at 2026-08-11T11:47:35.040376
fix workdir_cjk: installed
fix bash_safe_path: installed
fix atomic_write: installed
```

After restarting, `search_files` on absolute drive paths should return real
results on Windows.

## How it works

- Each fix is an independent module in `fixes/` with a uniform contract:
  `PRIORITY`, `RISK`, `DESCRIPTION`, `install()`.
- Patching uses the deferred pattern (module load → `register()` →
  `on_session_start`) so fixes land as soon as their target module is
  importable.
- One fix failing never blocks the others (independent try/except).
- No `hermes_agent` namespace is used — imports are bare `tools.*` (Hermes is
  an editable install).

## Development

```bash
pip install pytest
python -m pytest tests/ -v
```

The test suite runs on **both Ubuntu and Windows** in CI. Tests target the
pure logic of each fix (path translation, regex validation, atomic-write
semantics) and do not require a Hermes checkout — `tests/conftest.py` injects
a stand-in `ExecuteResult` when Hermes isn't installed.

## License

MIT
