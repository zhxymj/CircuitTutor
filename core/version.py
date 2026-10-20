# -*- coding: utf-8 -*-
"""从指定项目根目录的 ``VERSION`` 文件读取产品版本。"""

from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = PROJECT_ROOT / "VERSION"
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def read_version(root: Path | None = None) -> str:
    """读取并校验 ``root/VERSION``；``root`` 缺省时使用项目根目录。"""

    version_file = (Path(root) if root is not None else PROJECT_ROOT) / "VERSION"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"无法读取产品版本文件：{version_file}（{exc}）") from exc

    if not version:
        raise RuntimeError(f"产品版本文件为空：{version_file}")
    if SEMVER_RE.fullmatch(version) is None:
        raise RuntimeError(
            f"产品版本不是合法 SemVer：{version!r}（文件：{version_file}）"
        )
    return version


VERSION = read_version()
