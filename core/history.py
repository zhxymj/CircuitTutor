# -*- coding: utf-8 -*-
"""
history.py —— 工程历史（本地 JSON 列表，可回看）
================================================
工业产品需要「保存过的电路能再打开」。本模块用本地 JSON 文件做轻量历史库：

* 存储目录：优先使用 LSQC_HISTORY_DIR；否则写入项目内 ``data/history``，
  随包迁移到任意机器与盘符都可用，不依赖系统用户目录；发布打包
  （tools/build_release.py）会排除该目录。
* 每条记录：id, saved_at, title, query, circuit, meta
* 接口：list / save / load / delete
* 线程安全：写文件用临时文件 + replace

不依赖数据库，零第三方，适合本机演示与机房部署。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

_LOCK = threading.Lock()


def _dir() -> Path:
    env = os.environ.get("LSQC_HISTORY_DIR", "").strip()
    if env:
        d = Path(env).expanduser()
    else:
        # 默认写入项目目录下的 data/history：整包搬到别的机器或盘符后仍可读写，
        # 不依赖用户目录，也避开 Windows 用户目录被重定向或加密时原子替换失败的问题。
        d = Path(__file__).resolve().parents[1] / "data" / "history"
    d.mkdir(parents=True, exist_ok=True)
    # 返回真实路径，确保同目录临时文件与 os.replace 的目标始终由同一卷解释。
    return d.resolve()


def _safe_title(s: str) -> str:
    s = str(s or "circuit")
    return "".join(c if c.isalnum() or c in "一二三四五六七八九十百千万亿电路元件电阻电容电感二极管三极管运放电源" or c in "-_ " else "_" for c in s)[:40]


def save(circuit: dict, title: str = "", query: str = "", meta: Optional[dict] = None) -> dict:
    """保存一条工程历史，返回记录。"""
    cid = uuid.uuid4().hex[:12]
    rec = {
        "id": cid,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "title": title or "未命名电路",
        "query": query,
        "meta": meta or {},
        "circuit": circuit,
    }
    storage_dir = _dir()
    path = storage_dir / f"{cid}.json"
    tmp = storage_dir / f"{cid}.json.tmp"
    with _LOCK:
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    return {"id": rec["id"], "saved_at": rec["saved_at"],
            "title": rec["title"], "query": rec["query"]}


def list_all(limit: int = 50) -> List[dict]:
    items = []
    for p in sorted(_dir().glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.name.endswith(".tmp"):
            continue
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        items.append({
            "id": rec.get("id"),
            "saved_at": rec.get("saved_at"),
            "title": rec.get("title"),
            "query": rec.get("query", ""),
            "parts": len((rec.get("circuit") or {}).get("parts") or []),
            "connections": len((rec.get("circuit") or {}).get("connections") or []),
        })
        if len(items) >= limit:
            break
    return items


def load(cid: str) -> Optional[dict]:
    cid = str(cid or "")
    if not cid or "/" in cid or "\\" in cid or ".." in cid:
        return None
    path = _dir() / f"{cid}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def delete(cid: str) -> bool:
    cid = str(cid or "")
    if not cid or "/" in cid or "\\" in cid or ".." in cid:
        return False
    path = _dir() / f"{cid}.json"
    with _LOCK:
        if path.is_file():
            path.unlink()
            return True
    return False


if __name__ == "__main__":
    r = save({"parts": [], "connections": []}, title="自检", query="test")
    assert load(r["id"]) is not None
    assert any(x["id"] == r["id"] for x in list_all())
    assert delete(r["id"])
    print("history self-check ok")
