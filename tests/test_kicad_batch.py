# -*- coding: utf-8 -*-
"""KiCad 批量导入脚本测试（无官方库时跳过）。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import kicad_lib  # noqa: E402


def test_batch_scan_or_skip():
    try:
        from core.kicad_batch import batch_scan
    except ImportError:
        print("  （kicad_batch 不可用，跳过）")
        return
    if kicad_lib.default_symbol_dir() is None:
        print("  （未安装 KiCad，跳过）")
        return
    with tempfile.TemporaryDirectory() as td:
        s = batch_scan(limit=5, out_dir=Path(td))
        assert s["new_candidates"] >= 0
        assert (Path(td) / "drafts.json").is_file()
        assert (Path(td) / "review.md").is_file()
        assert (Path(td) / "summary.json").is_file()
        review = (Path(td) / "review.md").read_text(encoding="utf-8")
        assert "批量导入" in review or "草稿" in review


def test_zh_suggest():
    from core.kicad_batch import _zh_suggest
    s = _zh_suggest("Device", "Thermistor")
    assert "器件" in s or "Thermistor" in s
    s2 = _zh_suggest("Sensor_Temperature", "TMP36")
    assert "温度传感" in s2


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
