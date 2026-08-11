"""P2: hermes update 在 Windows 上因保留设备名文件(nul 等)卡死 autostash 的预检修复。

问题: hermes update 检测到本地改动后调用 ``_stash_local_changes_if_needed``
执行 ``git stash push --include-untracked``。若仓库工作树里存在 Windows 保留
设备名条目(如 ``nul`` —— 常见于 Linux/macOS/WSL 写进仓库的 0 字节文件,
``git status`` 显示 ``?? nul``), git for Windows 递归读取工作树时会把该路径
当成 NUL 设备打开, 读到无限字节流 -> ``stashing before update...`` 永久卡住
(issue #57081, 用户卡了 7 小时; 社区已用 0 字节 ``nul`` 文件确认无限挂起)。

修复: monkeypatch autostash 入口 ``_stash_local_changes_if_needed``, 在真正
执行 ``git stash`` 之前先做 Windows 保留设备名预检:
  1. ``os.walk`` 扫描仓库工作树(跳过 .git 内部), 检测保留设备名条目 ——
     CON / PRN / AUX / NUL / COM1-9 / LPT1-9 及其带扩展名变体(如 ``nul.txt``),
     大小写不敏感;
  2. 发现后先尝试重命名为 ``<原名>.bak``(带 ``\\\\?\\`` 扩展路径前缀回退,
     可绕过 Win32 设备名解析), 全部成功则继续 autostash, 文件数据不丢失;
  3. 若有条目无法自动重命名, 跳过 autostash 并打印明确警告 —— 绝不删除 /
     reset / clean(与社区 PR #57212 的非破坏原则一致), 让用户手动处理后重跑。

鲁棒性: 预检自身任何异常(扫描失败 / 无权限)都被吞掉, 最坏跳过预检, 绝不
阻断 update; 只在 Windows(win32/nt)上生效; 上游若已内置预检(PR #57212
合并后)会被检测到并跳过, 不重复修复。

参考:
- issue #57081: https://github.com/NousResearch/hermes-agent/issues/57081
- 社区 PR #57212 (preflight guard, open): https://github.com/NousResearch/hermes-agent/pull/57212
- 社区 PR #57134 (stash 120s 超时, open)
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import re
import sys

logger = logging.getLogger(__name__)

PRIORITY = 2
RISK = "low"
DESCRIPTION = (
    "Windows hermes update autostash preflight: scan working tree for reserved "
    "device-name files (nul/CON/PRN/AUX/COM1-9/LPT1-9 + extension variants), "
    "rename to .bak (\\\\?\\ fallback) or skip stash with warning — git stash "
    "would otherwise hang forever (issue #57081)."
)

# 标记属性: 用于识别"已经是我们包装过的"函数, 防止重复包装
_MARKER_ATTR = "_windows_compat_nul_preflight_wrapped"

# Windows 保留设备名(不含扩展名前缀, 大小写不敏感)。COM10/LPT10 不是保留名。
_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# 上游已内置保留名预检的特征(PR #57212 风格)。必须用词边界匹配:
# 原函数注释里的 "preserved" 含 "reserved" 子串, 裸子串匹配会误判成已修复。
_UPSTREAM_PREFLIGHT_RE = re.compile(
    r"\b(reserved|device[- ]name|preflight)\b", re.IGNORECASE
)


def _is_windows() -> bool:
    """只影响 Windows(win32 / nt)。"""
    return sys.platform == "win32" or os.name == "nt"


def _is_reserved_name(name: str) -> bool:
    """文件名是否为 Windows 保留设备名(大小写不敏感, 含扩展名变体如 nul.txt)。

    Windows 的保留名检查针对第一个点号之前的前缀, 因此 ``foo.nul`` 不是保留名,
    而 ``nul.txt`` / ``NUL`` / ``com1.log`` 都是。
    """
    stem = name.split(".", 1)[0].rstrip(" ")
    return stem.upper() in _RESERVED_STEMS


def _scan_reserved_entries(repo_root: str) -> list:
    """os.walk 扫描工作树, 返回保留设备名条目的路径列表。

    跳过 .git 内部(不扫描 git 内部数据), 也不进入保留名目录本身(Win32 打开
    它会解析成设备)。os.walk 默认忽略无权限目录的扫描错误 —— 最坏少扫一部分。
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        for name in dirnames:
            if _is_reserved_name(name):
                found.append(os.path.join(dirpath, name))
        for name in filenames:
            if _is_reserved_name(name):
                found.append(os.path.join(dirpath, name))
        # 剪枝: 不进入 .git, 也不进入保留名目录
        dirnames[:] = [d for d in dirnames if d != ".git" and not _is_reserved_name(d)]
    return found


def _try_rename_verified(src: str, dst: str) -> bool:
    """尝试重命名并验证真正生效。

    Win32 对设备名路径的 rename 可能报错, 也可能"成功"但什么都没做
    (把 NUL 设备当源), 因此用父目录枚举验证源名已消失。
    """
    try:
        os.rename(src, dst)
    except OSError:
        return False
    try:
        parent = os.path.dirname(src)
        return os.path.basename(src) not in os.listdir(parent)
    except OSError:
        return True  # 无法枚举父目录(权限等)时信任 rename 结果


def _with_extended_prefix(path: str) -> str:
    """把(已是绝对路径的)路径转成 ``\\\\?\\`` 扩展路径前缀形式。

    注意不能用 ``ntpath.abspath`` 拼前缀: Python 3.13+ 的 ``nt._path.abspath``
    走 Win32 ``GetFullPathNameW``, 会把无扩展名的保留设备名(如 ``NUL``)解析成
    ``\\\\.\\NUL``, 拼上前缀后变成无效路径 ``\\\\?\\\\.\\NUL``。路径来自
    ``os.walk``(绝对根), 本身就是绝对路径, 直接加前缀即可。
    """
    p = str(path).replace("/", "\\")
    if p.startswith("\\\\?\\"):
        return p
    return "\\\\?\\" + p


def _rename_reserved_entry(path: str) -> bool:
    """把保留名条目重命名为 ``<原名>.bak`` 并验证生效, 失败返回 False。

    先试普通 rename; 失败则用 ``\\\\?\\`` 扩展路径前缀重试 —— 该前缀可绕过
    Win32 的设备名解析(社区 PR #57212 评论区的 os.remove 同款方案)。
    """
    target = path + ".bak"
    if _try_rename_verified(path, target):
        return True
    return _try_rename_verified(
        _with_extended_prefix(path), _with_extended_prefix(target)
    )


def _run_nul_preflight(cwd) -> bool:
    """autostash 前的 Windows 保留设备名预检。

    返回 True = 应跳过 autostash(存在无法自动处理的保留名, 已打印警告);
    返回 False = 可以继续(无保留名 / 已自动重命名 / 预检自身出错, 最坏跳过预检)。
    任何异常都不会向外抛出 —— 绝不阻断 update。
    """
    if not _is_windows():
        return False  # 只影响 Windows
    if cwd is None:
        return False
    try:
        # 根目录本身是普通目录(保留名只会出现在叶节点), abspath 安全;
        # 绝对根保证扫描出的条目都是绝对路径, \\\\?\\ 前缀才有效。
        root = os.path.abspath(os.fspath(cwd))
        found = _scan_reserved_entries(root)
        if not found:
            return False  # 预检无害通过

        renamed: list = []
        failed: list = []
        for p in found:
            if _rename_reserved_entry(p):
                renamed.append(p)
            else:
                failed.append(p)
        for p in renamed:
            print(
                f"  ⚠ Windows reserved name {os.path.basename(p)!r} renamed to "
                f"{os.path.basename(p) + '.bak'!r} (git stash would hang, issue #57081)"
            )
            logger.warning("windows-compat[nul_preflight]: renamed %s -> %s.bak", p, p)
        if failed:
            print("✗ Reserved Windows device-name paths block git stash (would hang `hermes update`):")
            for p in failed:
                print(f"    {p}")
            print("  Please move or delete these files manually, then re-run `hermes update`.")
            print("  请手动处理上述文件后重跑 `hermes update` (Windows 下删除可用 os.remove(r'\\\\?\\<绝对路径>'), 见 PR #57212)")
            return True  # 跳过 autostash, 交给用户手动处理
        return False
    except Exception as exc:
        logger.debug("windows-compat[nul_preflight]: preflight failed, continuing (%r)", exc)
        return False


def _wrap_stash(original):
    """包装 autostash 入口: 执行前先跑保留名预检(预检异常全部吞掉)。"""

    @functools.wraps(original)
    def wrapped(git_cmd, cwd, *args, **kwargs):
        try:
            skip = _run_nul_preflight(cwd)
        except Exception:
            skip = False  # 预检自身异常绝不阻断 update
        if skip:
            return None  # 跳过 autostash(已打印警告), 等价于"无本地改动"
        return original(git_cmd, cwd, *args, **kwargs)

    setattr(wrapped, _MARKER_ATTR, True)
    return wrapped


def _already_has_preflight(func) -> bool:
    """目标函数是否已内置保留名预检(上游已修复则跳过, 不重复 patch)。"""
    if getattr(func, _MARKER_ATTR, False):
        return True  # 已经是我们包装过的
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError):
        return False  # 拿不到源码(内置/C 函数)时按未修复处理, 防御性包装
    return bool(_UPSTREAM_PREFLIGHT_RE.search(src))


def install() -> bool:
    """包装 autostash 入口 _stash_local_changes_if_needed, 返回 False 则延迟重试。

    只修 update_cmd 是不够的: hermes_cli.main 从 update_cmd re-export 了同一
    函数对象, 而 update 流程实际经 ``_m()``(惰性 hermes_cli.main 引用)调用,
    属性查找发生在 hermes_cli.main 上 —— 所以两个模块的绑定都要替换。
    """
    try:
        import hermes_cli.update_cmd as update_cmd
    except (ImportError, AttributeError) as exc:
        logger.debug("windows-compat[nul_preflight]: deferred — update_cmd 不可导入 (%s)", exc)
        return False

    target_name = "_stash_local_changes_if_needed"
    original = getattr(update_cmd, target_name, None)
    if original is None:
        # 结构变化: 目标函数不存在, 延迟重试(下一会话或更新后再试)
        logger.debug("windows-compat[nul_preflight]: deferred — 未找到 %s", target_name)
        return False

    if _already_has_preflight(original):
        logger.info("windows-compat[nul_preflight]: 上游已内置保留名预检, 跳过 (issue #57081 已修)")
        return True

    wrapped = _wrap_stash(original)
    setattr(update_cmd, target_name, wrapped)

    # 同步替换 hermes_cli.main 上的 re-export 绑定(真实 update 调用路径)
    try:
        import hermes_cli.main as main_mod
        if getattr(main_mod, target_name, None) is original:
            setattr(main_mod, target_name, wrapped)
    except ImportError:
        pass  # main 不可导入时, update_cmd 路径的包装仍然生效

    logger.info(
        "windows-compat[nul_preflight]: 已包装 %s (autostash 前保留名预检生效)", target_name
    )
    return True
