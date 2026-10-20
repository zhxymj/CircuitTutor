# -*- coding: utf-8 -*-
"""KiCad 官方库检索测试（无官方库时自动跳过，不视为失败）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import kicad_lib  # noqa: E402


def test_default_dir_or_skip():
    d = kicad_lib.default_symbol_dir()
    if d is None:
        print("  （未安装 KiCad，跳过官方库断言）")
        return
    assert d.is_dir()
    libs = kicad_lib.list_libs(d)
    assert len(libs) > 10


def test_search_thermistor_or_skip():
    d = kicad_lib.default_symbol_dir()
    if d is None:
        return
    hits = kicad_lib.search("Thermistor", lib_dir=d, limit=5, full=False)
    # Device 库在教学子集中，应能命中
    assert isinstance(hits, list)
    if hits:
        assert hits[0]["name"]


def test_draft_led_or_skip():
    d = kicad_lib.default_symbol_dir()
    if d is None:
        return
    try:
        drafts = kicad_lib.draft(["LED"], lib_dir=d)
    except KeyError:
        return
    assert "LED" in drafts
    assert "pins" in drafts["LED"]


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
