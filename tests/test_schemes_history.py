# -*- coding: utf-8 -*-
"""多方案与工程历史测试。"""
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.history import delete, list_all, load, save  # noqa: E402
from core import history as history_module  # noqa: E402
from core.schemes import pick_schemes  # noqa: E402


@contextmanager
def _isolated_history():
    """让历史测试只在临时目录写入，并完整恢复调用方环境。"""
    old = os.environ.get("LSQC_HISTORY_DIR")
    with tempfile.TemporaryDirectory() as td:
        os.environ["LSQC_HISTORY_DIR"] = td
        try:
            yield Path(td)
        finally:
            if old is None:
                os.environ.pop("LSQC_HISTORY_DIR", None)
            else:
                os.environ["LSQC_HISTORY_DIR"] = old


def test_schemes_two():
    s = pick_schemes("用555定时器做一个LED闪烁灯")
    assert len(s) >= 1
    assert all("circuit" in x and "title" in x for x in s)
    assert len(s) <= 2


def test_schemes_empty_query():
    s = pick_schemes("")
    assert isinstance(s, list)


def test_history_roundtrip():
    with _isolated_history():
        rec = save({"parts": [{"type": "resistor", "id": "R1"}], "connections": []},
                   title="测试电路", query="test")
        assert rec["id"]
        got = load(rec["id"])
        assert got is not None
        assert got["title"] == "测试电路"
        assert any(x["id"] == rec["id"] for x in list_all())
        assert delete(rec["id"]) is True
        assert load(rec["id"]) is None


def test_history_save_resolves_storage_directory_once():
    """单次保存只解析一次目录，保证临时文件与目标文件位于同一卷。"""
    with tempfile.TemporaryDirectory() as td:
        storage_dir = Path(td)
        calls = 0

        def one_directory_lookup():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise AssertionError("save() 不得在一次原子写入中重复解析历史目录")
            return storage_dir

        with patch.object(history_module, "_dir", side_effect=one_directory_lookup):
            rec = save({"parts": [], "connections": []}, title="同目录原子写")

        assert calls == 1
        assert (storage_dir / f"{rec['id']}.json").is_file()
        assert not (storage_dir / f"{rec['id']}.json.tmp").exists()


def test_history_bad_id():
    with _isolated_history():
        assert load("../etc/passwd") is None
        assert delete("../etc") is False


def test_history_default_uses_project_data_directory():
    """未显式覆盖时，历史目录落在项目内 data/history，可随包迁移。"""
    old_history = os.environ.pop("LSQC_HISTORY_DIR", None)
    old_local = os.environ.get("LOCALAPPDATA")
    try:
        with tempfile.TemporaryDirectory() as td:
            os.environ["LOCALAPPDATA"] = td
            root = Path(__file__).resolve().parents[1]
            assert history_module._dir() == root / "data" / "history"
    finally:
        if old_history is not None:
            os.environ["LSQC_HISTORY_DIR"] = old_history
        if old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_local


def test_history_directory_is_canonicalized():
    """返回真实路径，避免路径重定向跨卷破坏原子替换。"""
    old_history = os.environ.pop("LSQC_HISTORY_DIR", None)
    try:
        storage_dir = history_module._dir()
        assert storage_dir == storage_dir.resolve()
    finally:
        if old_history is not None:
            os.environ["LSQC_HISTORY_DIR"] = old_history


def test_history_explicit_environment_directory_wins():
    """LSQC_HISTORY_DIR 仍是最高优先级，且不会泄漏测试环境变量。"""
    old_history = os.environ.get("LSQC_HISTORY_DIR")
    try:
        with tempfile.TemporaryDirectory() as override:
            os.environ["LSQC_HISTORY_DIR"] = override
            assert history_module._dir() == Path(override)
    finally:
        if old_history is None:
            os.environ.pop("LSQC_HISTORY_DIR", None)
        else:
            os.environ["LSQC_HISTORY_DIR"] = old_history


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {fn.__name__}: {e}")
    print(f"结果：{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
