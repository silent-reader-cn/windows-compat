"""fix_update_nul_preflight 的自包含测试(不依赖 Hermes 安装)。

通过向 sys.modules 注入 fake ``hermes_cli`` 包(含 update_cmd / main 两个模块,
main 是对同一函数的 re-export, 模拟真实 update 流程经 ``_m()`` 调用)来验证
install() 的 monkeypatch 行为:

- 工作树含保留设备名文件(nul.txt)时预检触发: 重命名 -> 继续 autostash;
- 无保留名时预检无害通过, 原 autostash 正常执行;
- 预检自身异常被吞掉, 不影响 update;
- 重命名失败时跳过 autostash 并打印警告;
- 上游已内置预检时 install() 返回 True 且不重复 patch。

注意: 测试可能运行在真实 Windows 上, 创建 / 重命名保留设备名文件必须走
``\\\\?\\`` 扩展路径前缀, 否则系统会按设备名拦截 —— 这也顺带验证了修复里
的 ``\\\\?\\`` 回退逻辑。
"""

import os
import sys
import types
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fixes.fix_update_nul_preflight as fix  # noqa: E402

TARGET = "_stash_local_changes_if_needed"


# ---------- 工具函数: 在 Windows 上创建保留名文件/目录 ----------


def _raw_prefixed(path) -> str:
    """Win32 扩展路径前缀, 绕过设备名解析 (Windows 下创建/删除保留名必需)。"""
    return "\\\\?\\" + str(path.resolve())


def _make_reserved_file(dir_path, name):
    """在 dir_path 下创建名为保留设备名的文件(Windows 需 \\\\?\\ 前缀)。"""
    path = dir_path / name
    if os.name == "nt":
        with open(_raw_prefixed(path), "w", encoding="utf-8") as f:
            f.write("hi")
    else:
        path.write_text("hi")
    return path


def _make_reserved_dir(dir_path, name):
    """在 dir_path 下创建名为保留设备名的目录(Windows 需 \\\\?\\ 前缀)。"""
    path = dir_path / name
    if os.name == "nt":
        os.mkdir(_raw_prefixed(path))
    else:
        path.mkdir()
    return path


def _names(path) -> set:
    """父目录枚举(字面文件名)。不能用 Path.exists 判断保留名 —— 设备名路径
    stat 会命中 NUL 设备, 即使文件已被重命名也会返回"存在"。"""
    return set(os.listdir(path))


# ---------- fake hermes_cli 包注入 ----------


@pytest.fixture()
def fake_hermes(monkeypatch):
    """注入 fake hermes_cli: update_cmd 含 autostash 入口, main 为同一函数 re-export。"""
    calls = []

    def fake_stash(git_cmd, cwd):
        calls.append((list(git_cmd), str(cwd)))
        return "refs/stash/0"

    pkg = types.ModuleType("hermes_cli")
    pkg.__path__ = []  # 标记为包, 使 import hermes_cli.update_cmd 可用
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)

    update_cmd = types.ModuleType("hermes_cli.update_cmd")
    update_cmd._stash_local_changes_if_needed = fake_stash
    monkeypatch.setitem(sys.modules, "hermes_cli.update_cmd", update_cmd)

    main = types.ModuleType("hermes_cli.main")
    main._stash_local_changes_if_needed = fake_stash  # main.py 的 re-export 绑定
    monkeypatch.setitem(sys.modules, "hermes_cli.main", main)

    return SimpleNamespace(
        update_cmd=update_cmd, main=main, calls=calls, fake_stash=fake_stash
    )


# ---------- 保留名检测 ----------


@pytest.mark.parametrize(
    "name",
    [
        "nul", "NUL", "nul.txt", "nul.log", "nul.",  # NUL 及变体
        "CON", "con", "CON.log", "con.ini",          # CON
        "PRN", "prn.txt", "AUX", "aux.bin",          # PRN / AUX
        "com1", "COM1", "com1.txt", "com9.report",   # COM1-9
        "lpt1", "LPT1", "lpt9.txt", "LPT9.x",        # LPT1-9
    ],
)
def test_is_reserved_name_true(name):
    assert fix._is_reserved_name(name), name


@pytest.mark.parametrize(
    "name",
    [
        "normal.txt", "console.log", "com10", "lpt10", "com0", "lpt0",
        "foo.nul", "nullll", ".nul", "com", "auxiliary", "", "nul_extra",
    ],
)
def test_is_reserved_name_false(name):
    assert not fix._is_reserved_name(name), name


# ---------- install() 与预检行为 ----------


def test_install_patches_and_renames_nul_files(fake_hermes, tmp_path):
    """工作树含保留名时: 重命名 + 继续 autostash。"""
    _make_reserved_file(tmp_path, "nul.txt")          # 根目录文件
    (tmp_path / "normal.txt").write_text("ok")        # 普通文件
    (tmp_path / "sub").mkdir()
    _make_reserved_file(tmp_path / "sub", "NUL")      # 子目录里的保留名
    (tmp_path / ".git").mkdir()                       # 应被跳过的目录
    _make_reserved_file(tmp_path / ".git", "CON")     # .git 内部不扫描

    assert fix.install() is True
    wrapped = fake_hermes.update_cmd._stash_local_changes_if_needed

    result = wrapped(["git"], tmp_path)

    assert result == "refs/stash/0"          # autostash 正常执行
    assert len(fake_hermes.calls) == 1       # 原 stash 被调用了一次
    names = _names(tmp_path)
    assert "nul.txt" not in names and "nul.txt.bak" in names
    assert "normal.txt" in names
    sub_names = _names(tmp_path / "sub")
    assert "NUL" not in sub_names and "NUL.bak" in sub_names
    # .git 内部未被触碰(没有 CON 也没有 CON.bak)
    git_names = _names(tmp_path / ".git")
    assert "CON" in git_names and "CON.bak" not in git_names


def test_clean_tree_preflight_harmless(fake_hermes, tmp_path):
    """无保留名: 预检无害通过, 工作树无任何改动。"""
    (tmp_path / "normal.txt").write_text("ok")
    assert fix.install() is True
    wrapped = fake_hermes.update_cmd._stash_local_changes_if_needed

    result = wrapped(["git"], tmp_path)

    assert result == "refs/stash/0"
    assert len(fake_hermes.calls) == 1
    assert _names(tmp_path) == {"normal.txt"}


def test_reserved_directory_renamed(fake_hermes, tmp_path):
    """保留名目录同样被检测并重命名(且不进入该目录遍历)。"""
    _make_reserved_dir(tmp_path, "nul")
    (tmp_path / "keep").mkdir()
    assert fix.install() is True
    wrapped = fake_hermes.update_cmd._stash_local_changes_if_needed

    result = wrapped(["git"], tmp_path)

    assert result == "refs/stash/0"
    assert len(fake_hermes.calls) == 1
    names = _names(tmp_path)
    assert "nul" not in names and "nul.bak" in names and "keep" in names


def test_preflight_exception_swallowed(fake_hermes, tmp_path, monkeypatch):
    """预检自身抛异常: 被吞掉, update 不受影响。"""
    assert fix.install() is True

    def boom(root):
        raise RuntimeError("scan boom")

    monkeypatch.setattr(fix, "_scan_reserved_entries", boom)
    wrapped = fake_hermes.update_cmd._stash_local_changes_if_needed

    result = wrapped(["git"], tmp_path)

    assert result == "refs/stash/0"
    assert len(fake_hermes.calls) == 1  # 原 autostash 正常执行


def test_rename_failure_skips_stash_with_warning(fake_hermes, tmp_path, monkeypatch, capsys):
    """重命名失败(如无法自动处理): 跳过 autostash 并打印手动处理警告。"""
    _make_reserved_file(tmp_path, "nul.txt")
    assert fix.install() is True
    monkeypatch.setattr(fix, "_rename_reserved_entry", lambda p: False)
    wrapped = fake_hermes.update_cmd._stash_local_changes_if_needed

    result = wrapped(["git"], tmp_path)

    assert result is None                 # 跳过 autostash
    assert fake_hermes.calls == []        # 原 stash 未被调用
    out = capsys.readouterr().out
    assert "nul.txt" in out               # 警告列出路径
    assert "手动" in out                  # 提示用户手动处理


def test_non_windows_skips_preflight(fake_hermes, tmp_path, monkeypatch):
    """非 Windows: 预检完全不生效, 保留名文件原样保留。"""
    _make_reserved_file(tmp_path, "nul.txt")
    assert fix.install() is True
    monkeypatch.setattr(fix, "_is_windows", lambda: False)
    wrapped = fake_hermes.update_cmd._stash_local_changes_if_needed

    result = wrapped(["git"], tmp_path)

    assert result == "refs/stash/0"
    assert len(fake_hermes.calls) == 1
    assert "nul.txt" in _names(tmp_path)  # 未被重命名


def test_wrapped_with_none_cwd_still_stashes(fake_hermes):
    """cwd 为 None: 预检直接放行, autostash 正常。"""
    assert fix.install() is True
    wrapped = fake_hermes.update_cmd._stash_local_changes_if_needed

    assert wrapped(["git"], None) == "refs/stash/0"
    assert len(fake_hermes.calls) == 1


# ---------- 已修复检测 / 幂等 / main 绑定 ----------


def _upstream_preflight_stash(git_cmd, cwd):
    """Upstream PR #57212 style: reserved device-name preflight before stash."""
    return "refs/stash/1"


def test_install_skips_when_upstream_already_fixed(fake_hermes):
    """目标函数已内置保留名预检: install() 返回 True 且不重复 patch。"""
    fake_hermes.update_cmd._stash_local_changes_if_needed = _upstream_preflight_stash
    fake_hermes.main._stash_local_changes_if_needed = _upstream_preflight_stash

    assert fix.install() is True

    assert fake_hermes.update_cmd._stash_local_changes_if_needed is _upstream_preflight_stash
    assert not hasattr(_upstream_preflight_stash, fix._MARKER_ATTR)


def test_install_idempotent_no_double_wrap(fake_hermes):
    """重复 install(): 返回 True 且不二次包装。"""
    assert fix.install() is True
    first = fake_hermes.update_cmd._stash_local_changes_if_needed
    assert hasattr(first, fix._MARKER_ATTR)

    assert fix.install() is True
    assert fake_hermes.update_cmd._stash_local_changes_if_needed is first

    assert first(["git"], None) == "refs/stash/0"
    assert len(fake_hermes.calls) == 1


def test_install_syncs_main_binding(fake_hermes):
    """hermes_cli.main 的 re-export 绑定被替换为同一个包装对象。"""
    assert fix.install() is True
    wrapped = fake_hermes.update_cmd._stash_local_changes_if_needed
    assert fake_hermes.main._stash_local_changes_if_needed is wrapped


def test_install_defers_when_target_missing(fake_hermes):
    """目标函数不存在(结构变化): install() 返回 False 延迟重试。"""
    del fake_hermes.update_cmd._stash_local_changes_if_needed
    del fake_hermes.main._stash_local_changes_if_needed
    assert fix.install() is False


def test_is_windows_guard(monkeypatch):
    """_is_windows 同时看 sys.platform 与 os.name。"""
    monkeypatch.setattr(sys, "platform", "linux")
    assert fix._is_windows() == (os.name == "nt")
