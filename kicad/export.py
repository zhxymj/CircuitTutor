# -*- coding: utf-8 -*-
"""CircuitJSON -> KiCad 导出器 · 兼容门面（零第三方依赖）。

历史上全部导出逻辑集中在单文件（kicad_export.py）；现按职责拆分为
三个模块，本文件作为兼容门面保留全部公共 API，供既有调用方
（ai2kicad / server / tests / 命令行）零改动继续使用：

    kicad.symbols      符号定义（S 表达式图元 + 各元件符号 + 注册表）
    kicad.sch_writer   原理图正文与工程文件（.kicad_sch/.kicad_pro 等）+ CLI
    kicad.cli_runner   kicad-cli 子进程联动（官方 ERC / 网表）

纯 re-export，无任何逻辑：导出产物与拆分前逐字节一致（等价性由
22 文件 md5 比对与全量套件回归证明）。

用法：
    python -m kicad.export                # 导出全部模板到 ./kicad_export/
    python -m kicad.export led_limit      # 导出单个模板
    python -m kicad.export --all --out DIR
    python -m kicad.export --stdin < circuit.json
"""

from __future__ import annotations

import sys

# ---- kicad_symbols：符号定义与共享常量 ----
from kicad.symbols import (  # noqa: F401
    FONT_SZ,
    NS_URL,
    STUB,
    build_symbol_defs,
)

# ---- kicad_sch_writer：正文生成、工程文件、slug、CLI ----
from kicad.sch_writer import (  # noqa: F401
    ANCHOR_NET_NAMES,
    GENERATOR,
    GENERATOR_VERSION,
    HOW_TO_OPEN_NAME,
    LIB_NAME,
    OPEN_BAT_NAME,
    PREVIEW_SVG_NAME,
    SCH_VERSION,
    SYM_LIB_TABLE_TEXT,
    TYPE_MAP,
    ZIP_README_NAME,
    _slug,               # 兼容旧内部调用
    _uid,
    build_symbol_lib_text,
    export_kicad_sch,
    how_to_open_text,
    kicad_pro_text,
    main,
    open_in_kicad_bat_text,
    safe_project_stem,
    slug,
    write_project_kit,
    zip_readme_text,
    export_to_dir,
)

# ---- kicad.cli_runner：外部 CLI 联动 ----
from kicad.cli_runner import (  # noqa: F401
    export_official_netlist,
    find_kicad_cli,
    kicad_cli_version,
    run_official_erc,
)

__all__ = [
    # 常量
    "SCH_VERSION", "GENERATOR", "GENERATOR_VERSION", "STUB", "FONT_SZ",
    "NS_URL", "LIB_NAME", "SYM_LIB_TABLE_TEXT",
    # 符号与映射
    "build_symbol_defs", "build_symbol_lib_text", "TYPE_MAP",
    "ANCHOR_NET_NAMES",
    # 导出
    "export_kicad_sch", "kicad_pro_text", "write_project_kit",
    "export_to_dir", "slug", "_slug", "main",
    "safe_project_stem", "HOW_TO_OPEN_NAME", "how_to_open_text",
    "OPEN_BAT_NAME", "PREVIEW_SVG_NAME", "ZIP_README_NAME",
    "open_in_kicad_bat_text", "zip_readme_text",
    # kicad-cli 联动
    "find_kicad_cli", "kicad_cli_version", "run_official_erc",
    "export_official_netlist",
]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
