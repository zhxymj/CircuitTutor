# -*- coding: utf-8 -*-
"""
kicad_lib.py —— KiCad 官方符号库检索 / 预览 / 导入草稿（零第三方依赖）
======================================================================
回答「能不能接入 KiCad 官方库」：

**能，分三层接入，按侵入性从低到高：**

1. **检索层（本模块，默认可用）**  
   本机安装了 KiCad 时，扫描 `share/kicad/symbols/*.kicad_sym`
   （实测 KiCad 9 约 220+ 库文件），支持按中英文关键词搜索符号名，
   查看引脚电气类型，并生成与 `symbol_import.py` 兼容的目录草稿 JSON。
   ——不改动运行时 CATALOG，不影响 19 套件；给作者/评委「可扩展」证据。

2. **草稿导入层（已有协议）**  
   `python -m core.kicad_lib draft Thermistor --apply-hint`  
   输出需要人工补中文别名后的四件套改动清单（catalog/symbols/kicad/app.js）。
   教学目录必须保持「可解释的 37 种」，**禁止**一键灌入全部 220 库。

3. **导出映射层（已有能力）**  
   `kicad/export.py` 已能产出带 `lib_symbols` 的自包含原理图；
   未来可把内部 type 映射到官方 `Device:R` / `Device:LED` 等 lib_id，
   实现「与官方库符号视觉一致」。本模块先提供 lib_id 查表接口。

用法：
    python -m core.kicad_lib search 热敏
    python -m core.kicad_lib search thermistor --limit 10
    python -m core.kicad_lib show Thermistor
    python -m core.kicad_lib draft Thermistor,LDR --json draft.json
    python -m core.kicad_lib libs          # 列出官方库文件
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# 常见 KiCad 安装路径（Windows / Linux / macOS）
_DEFAULT_LIB_CANDIDATES = [
    r"D:\KICAD\share\kicad\symbols",
    r"C:\Program Files\KiCad\9.0\share\kicad\symbols",
    r"C:\Program Files\KiCad\8.0\share\kicad\symbols",
    r"/usr/share/kicad/symbols",
    r"/usr/local/share/kicad/symbols",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/kicad/symbols",
]

# 环境变量优先
ENV_LIB = "KICAD_SYMBOL_DIR"


def default_symbol_dir() -> Optional[Path]:
    env = os.environ.get(ENV_LIB)
    if env and Path(env).is_dir():
        return Path(env)
    for p in _DEFAULT_LIB_CANDIDATES:
        if Path(p).is_dir():
            return Path(p)
    return None


def list_libs(lib_dir: Optional[Path] = None) -> List[str]:
    d = lib_dir or default_symbol_dir()
    if not d:
        return []
    return sorted(p.stem for p in Path(d).glob("*.kicad_sym"))


def _import_symbol_import():
    from core import symbol_import as si
    return si


def load_symbol_index(lib_dir: Optional[Path] = None,
                      libs: Optional[Iterable[str]] = None) -> Dict[str, dict]:
    """构建 {符号名: {lib, pins, pin_count}} 索引。

    默认扫全部库可能较慢（200+ 文件、数秒）；可用 libs 限定子集
    （教学优先 Device / Transistor_BJT / Regulator_Linear / Timer / Amplifier_Operational）。
    """
    d = Path(lib_dir) if lib_dir else default_symbol_dir()
    if not d or not d.is_dir():
        raise FileNotFoundError(
            f"未找到 KiCad 官方符号库目录。请设置环境变量 {ENV_LIB}=... "
            f"或安装 KiCad 8/9。")
    si = _import_symbol_import()
    names = list(libs) if libs else list_libs(d)
    index: Dict[str, dict] = {}
    for lib in names:
        path = d / f"{lib}.kicad_sym"
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            ast = si.parse_sexpr(text)
            for node in si.iter_symbol_nodes(ast):
                sname = si.symbol_name(node)
                # 跳过 unit 子符号（X_1_1 等）——symbol_name 已处理则无妨
                pins = si.symbol_pins(node)
                index[sname] = {
                    "lib": lib,
                    "lib_id": f"{lib}:{sname}",
                    "pin_count": len(pins),
                    "pins": [
                        {"number": p["number"], "name": p["name"],
                         "etype": p["etype"]}
                        for p in pins
                    ],
                }
        except Exception:
            continue  # 单个库解析失败不拖垮全索引
    return index


# 教学优先库：检索时先扫这些（快）
TEACHING_LIBS = [
    "Device", "Transistor_BJT", "Transistor_FET", "Regulator_Linear",
    "Regulator_Switching", "Timer", "Amplifier_Operational",
    "Sensor_Temperature", "Sensor_Optical", "Switch", "Connector",
    "power", "Diode", "LED", "Reference_Voltage",
]


def search(query: str, lib_dir: Optional[Path] = None,
           limit: int = 20, full: bool = False) -> List[dict]:
    """按关键词搜索官方符号名（大小写不敏感，子串匹配）。

    full=False 时只扫教学优先库（快）；full=True 扫全部官方库。
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    libs = None if full else TEACHING_LIBS
    try:
        idx = load_symbol_index(lib_dir, libs=libs)
    except FileNotFoundError:
        # 教学子集失败再试全量目录枚举空
        return []
    hits = []
    for name, meta in idx.items():
        score = 0
        nl = name.lower()
        if nl == q:
            score = 100
        elif nl.startswith(q):
            score = 80
        elif q in nl:
            score = 60
        elif any(q in (p.get("name") or "").lower() for p in meta.get("pins") or []):
            score = 40
        if score:
            hits.append({
                "name": name,
                "lib": meta["lib"],
                "lib_id": meta["lib_id"],
                "pin_count": meta["pin_count"],
                "score": score,
            })
    hits.sort(key=lambda x: (-x["score"], x["name"]))
    return hits[:limit]


def show(name: str, lib_dir: Optional[Path] = None) -> Optional[dict]:
    """显示单个符号的引脚与 lib_id。"""
    idx = load_symbol_index(lib_dir, libs=TEACHING_LIBS)
    if name not in idx:
        idx = load_symbol_index(lib_dir, libs=None)
    return idx.get(name)


def draft(names: List[str], lib_dir: Optional[Path] = None) -> dict:
    """生成目录草稿（复用 symbol_import.draft_from_lib）。"""
    d = Path(lib_dir) if lib_dir else default_symbol_dir()
    if not d:
        raise FileNotFoundError("未找到 KiCad 符号库目录")
    si = _import_symbol_import()
    # 确定每个符号在哪个库
    idx = load_symbol_index(d, libs=TEACHING_LIBS)
    missing = [n for n in names if n not in idx]
    if missing:
        idx2 = load_symbol_index(d, libs=None)
        idx.update({k: idx2[k] for k in missing if k in idx2})
    by_lib: Dict[str, List[str]] = {}
    for n in names:
        if n not in idx:
            raise KeyError(f"官方库中未找到符号：{n}")
        by_lib.setdefault(idx[n]["lib"], []).append(n)
    out = {}
    for lib, ns in by_lib.items():
        out.update(si.draft_from_lib(d / f"{lib}.kicad_sym", ns))
    return out


def apply_hint(drafts: dict) -> List[str]:
    """把草稿翻译成「作者要改哪四处」的人话清单。"""
    lines = []
    for name, e in (drafts or {}).items():
        pins = e.get("pins") or {}
        lines.append(f"■ {name}  (lib_id={e.get('kicad_lib_id')})")
        lines.append(f"  1) core/catalog.py  CATALOG['{e.get('type')}'] = {{")
        lines.append(f"       'name': '<中文名>', 'ref': '{e.get('ref')}',")
        lines.append(f"       'pins': {pins},")
        lines.append(f"       'aliases': ['<中文别名>'], 'negative': [],")
        lines.append(f"     }}")
        lines.append(f"  2) web/symbols.js   LIB['{e.get('type')}'] 自绘 SVG（ports 对齐 pins）")
        lines.append(f"  3) kicad/symbols.py 嵌入符号 + sch_writer.TYPE_MAP")
        lines.append(f"  4) web/app.js       INSERT_GROUPS 与 PART_INFO 分组")
        lines.append(f"  然后：python core/catalog.py --manifest tests/frontend_manifest.json")
        lines.append(f"        python -B tests/run_tests.py")
    return lines


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def main(argv) -> int:
    args = list(argv)
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    try:
        if cmd == "libs":
            d = default_symbol_dir()
            if not d:
                print("未找到 KiCad 官方符号库。请设置 KICAD_SYMBOL_DIR。")
                return 1
            libs = list_libs(d)
            print(f"符号库目录：{d}")
            print(f"共 {len(libs)} 个 .kicad_sym：")
            for x in libs[:40]:
                print(" ", x)
            if len(libs) > 40:
                print(f"  … 其余 {len(libs) - 40} 个省略")
            return 0

        if cmd == "search":
            if len(args) < 2:
                print("用法：python -m core.kicad_lib search <关键词> [--limit N] [--full]")
                return 2
            q = args[1]
            limit, full = 20, False
            i = 2
            while i < len(args):
                if args[i] == "--limit" and i + 1 < len(args):
                    limit = int(args[i + 1]); i += 2
                elif args[i] == "--full":
                    full = True; i += 1
                else:
                    i += 1
            hits = search(q, limit=limit, full=full)
            if not hits:
                print("无匹配。可加 --full 扫全部官方库。")
                return 1
            for h in hits:
                print(f"{h['lib_id']:48s}  pins={h['pin_count']}")
            return 0

        if cmd == "show":
            if len(args) < 2:
                print("用法：python -m core.kicad_lib show <SymbolName>")
                return 2
            meta = show(args[1])
            if not meta:
                print("未找到", args[1])
                return 1
            print(json.dumps(meta, ensure_ascii=False, indent=2))
            return 0

        if cmd == "draft":
            if len(args) < 2:
                print("用法：python -m core.kicad_lib draft A,B [--json out.json] [--hint]")
                return 2
            names = [x.strip() for x in args[1].split(",") if x.strip()]
            out_json = None
            hint = False
            i = 2
            while i < len(args):
                if args[i] == "--json" and i + 1 < len(args):
                    out_json = args[i + 1]; i += 2
                elif args[i] in ("--hint", "--apply-hint"):
                    hint = True; i += 1
                else:
                    i += 1
            drafts = draft(names)
            if out_json:
                Path(out_json).write_text(
                    json.dumps(drafts, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
                print(f"草稿已写入 {out_json}（{len(drafts)} 个）")
            else:
                print(json.dumps(drafts, ensure_ascii=False, indent=2))
            if hint:
                print("\n—— 应用提示（人工补中文后落库）——")
                for line in apply_hint(drafts):
                    print(line)
            return 0

        print(f"未知命令：{cmd}")
        return 2
    except (FileNotFoundError, KeyError, ValueError, OSError) as e:
        print(f"错误：{e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
