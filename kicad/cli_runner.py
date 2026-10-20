# -*- coding: utf-8 -*-
"""kicad-cli 联动：官方 ERC / 官方网表校验（零第三方依赖）。

本模块负责"与外部 kicad-cli 子进程打交道"：CLI 定位、版本查询、
官方 ERC 报告获取与解析、网表导出与连通性反推。所有入口在 CLI
不可用/失败/超时时返回 None（调用方据此降级到内置 ERC）。
原理图正文生成见 kicad.sch_writer（kicad.export 为兼容门面）。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

try:
    from core import config as app_config      # 全局配置（KICAD_CLI_PATH/超时等）
except ImportError:                  # 允许脱离项目目录单文件使用
    app_config = None

log = logging.getLogger(__name__)

_KICAD_CLI_DIRS = (
    r"C:\Program Files\KiCad\9.0\bin",
    r"C:\Program Files\KiCad\8.0\bin",
    r"D:\KICAD\bin",
    r"D:\Program Files\KiCad\9.0\bin",
)


def find_kicad_cli() -> str | None:
    """定位本机 kicad-cli：环境变量 KICAD_CLI_PATH 显式指定优先，其次 PATH，
    再次常见安装路径；没有则 None。"""
    override = getattr(app_config, "KICAD_CLI_PATH", "") if app_config else ""
    if override and Path(override).is_file():
        return override
    hit = shutil.which("kicad-cli")
    if hit:
        return hit
    for d in _KICAD_CLI_DIRS:
        cand = Path(d) / "kicad-cli.exe"
        if cand.is_file():
            return str(cand)
    return None


_KICAD_VERSION: str | None = None


def kicad_cli_version() -> str | None:
    """kicad-cli 版本号（如 "9.0.8"），不可用返回 None；结果缓存。"""
    global _KICAD_VERSION
    if _KICAD_VERSION is None:
        cli = find_kicad_cli()
        if not cli:
            return None
        try:
            r = subprocess.run([cli, "version"], capture_output=True,
                               text=True, timeout=30,
                               encoding="utf-8", errors="replace")
            _KICAD_VERSION = (r.stdout or "").strip() or "?"
        except Exception:
            return None
    return _KICAD_VERSION


def _cli_timeout(timeout: float | None) -> float:
    """子进程超时：显式传入优先，否则取全局配置（默认 90s，与原行为一致）。"""
    if timeout is not None:
        return timeout
    return getattr(app_config, "KICAD_CLI_TIMEOUT_S", 90.0) if app_config else 90.0


def run_official_erc(sch_path, timeout: float | None = None) -> dict | None:
    """kicad-cli 官方 ERC（JSON，全部严重级）。不可用/失败返回 None。

    返回 {total, by_type, violations, report}。"""
    cli = find_kicad_cli()
    if not cli:
        return None
    sch_path = Path(sch_path)
    out = sch_path.with_suffix(".erc.json")
    # 同一工程目录反复复验时不能把上次报告误当本次结果。
    try:
        out.unlink(missing_ok=True)
    except OSError as e:
        log.warning("旧 KiCad ERC 报告清理失败：%s", e)
        return None
    try:
        r = subprocess.run(
            [cli, "sch", "erc", "--format", "json", "--severity-all",
             "-o", str(out), str(sch_path)],
            capture_output=True, text=True, timeout=_cli_timeout(timeout),
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        log.warning("kicad-cli ERC 超时（%s）：%s", _cli_timeout(timeout), sch_path)
        return None
    except OSError as e:
        log.warning("kicad-cli ERC 启动失败：%s", e)
        return None
    # KiCad 在发现 ERC 违规时也可能以非零码退出；只要生成了新 JSON，
    # 就解析并返回具体违规，不能把“发现错误”误写成“工具不可用”。
    if not out.exists():
        log.warning("kicad-cli ERC 未生成报告（退出码 %s）：%s",
                    r.returncode, (r.stderr or "").strip()[:200])
        return None
    try:
        data = json.loads(out.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("kicad-cli ERC 报告解析失败：%s", e)
        return None
    viol = [v for s in data.get("sheets", [])
            for v in s.get("violations", [])]
    if r.returncode != 0 and not viol:
        log.warning("kicad-cli ERC 非零退出但报告无违规（%s）：%s",
                    r.returncode, (r.stderr or "").strip()[:200])
        return None
    by_type = {}
    for v in viol:
        key = f"{v.get('type')}:{v.get('severity')}"
        by_type[key] = by_type.get(key, 0) + 1
    return {"total": len(viol), "by_type": by_type,
            "violations": viol, "report": str(out),
            "returncode": r.returncode}


def export_official_netlist(sch_path, timeout: float | None = None) -> dict | None:
    """kicad-cli 导出网表并反推 {网络名: {(ref, pin)}}（KiCad 引擎视角的
    连通性，金标准比对）。不可用/失败返回 None。"""
    cli = find_kicad_cli()
    if not cli:
        return None
    sch_path = Path(sch_path)
    out = sch_path.with_suffix(".net")
    try:
        out.unlink(missing_ok=True)
    except OSError as e:
        log.warning("旧 KiCad 网表清理失败：%s", e)
        return None
    try:
        r = subprocess.run(
            [cli, "sch", "export", "netlist", "--format", "kicadsexpr",
             "-o", str(out), str(sch_path)],
            capture_output=True, text=True, timeout=_cli_timeout(timeout),
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        log.warning("kicad-cli 网表导出超时（%s）：%s", _cli_timeout(timeout), sch_path)
        return None
    except OSError as e:
        log.warning("kicad-cli 网表导出启动失败：%s", e)
        return None
    if r.returncode != 0 or not out.exists():
        log.warning("kicad-cli 网表导出非零退出（%s）：%s",
                    r.returncode, (r.stderr or "").strip()[:200])
        return None
    text = out.read_text(encoding="utf-8")
    try:
        return _parse_netlist_text(text)
    except (ValueError, TypeError, IndexError) as exc:
        log.warning("KiCad 网表结构无法解析：%s", exc)
        return None


def _parse_netlist_text(text: str) -> dict:
    """按 S 表达式结构解析，兼容 KiCad 8/9 紧凑和 10 多行输出。"""
    from core.symbol_import import parse_sexpr
    root = parse_sexpr(text)
    if not root or root[0] != "export":
        raise ValueError("网表根节点必须为 export")

    def children(node, tag):
        return [item for item in node if isinstance(item, list) and item and item[0] == tag]

    sections = children(root, "nets")
    if len(sections) != 1:
        raise ValueError("网表缺少唯一的 nets 节点")
    nets = {}
    for net in children(sections[0], "net"):
        names = children(net, "name")
        if len(names) != 1 or len(names[0]) != 2:
            raise ValueError("网络缺少名称")
        name = names[0][1].lstrip("/")
        nodes = set()
        for node in children(net, "node"):
            refs, pins = children(node, "ref"), children(node, "pin")
            if len(refs) != 1 or len(pins) != 1 or len(refs[0]) != 2 or len(pins[0]) != 2:
                raise ValueError("网络节点缺少 ref 或 pin")
            nodes.add((refs[0][1], pins[0][1]))
        nets.setdefault(name, set()).update(nodes)
    return nets
