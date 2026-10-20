# -*- coding: utf-8 -*-
"""
schemes.py —— 多方案拓扑草案（一次需求出 2 个对比）
=====================================================
工业级产品常见能力：不锁死单一答案，给用户可比较的候选。

策略（零 LLM、零依赖）：
  1) 用 catalog.match_template 对内置 20 套模板打分；
  2) 取 top-2 不同模板，深拷贝为两份草案；
  3) 若本地只命中 1 套，用「最接近」的第二套补齐；
  4) 若完全无命中，返回空列表（调用方回落 LLM 单方案）。

返回：List[{key, title, circuit, reason}]
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

try:
    from core import catalog
    from core import templates as templates_mod
except ImportError:
    catalog = None
    templates_mod = None


def pick_schemes(query: str, top_k: int = 2) -> List[Dict[str, Any]]:
    """按需求选出 top_k 个拓扑草案。"""
    if catalog is None or templates_mod is None:
        return []
    builtin = getattr(templates_mod, "BUILTIN_TEMPLATES", None) or {}
    if not builtin:
        return []
    # 用 match_template 只会返回 1 个；这里做简单的关键词/别名打分扩展
    scores = []
    q = str(query or "")
    for key, tpl in builtin.items():
        title = str(tpl.get("title") or key)
        # 标题关键词命中
        s = 0.0
        for token in (key, title):
            tok = str(token).replace("_", " ").lower()
            if tok and tok[:4] in q.lower():
                s += 1.0
        # 类型关键词（含中文别名反查）
        type_hits = 0
        for p in tpl.get("parts") or []:
            pt = str(p.get("type") or "")
            if not pt:
                continue
            if pt in q.lower():
                type_hits += 1
                continue
            # 用目录别名反查
            try:
                entry = catalog.CATALOG.get(pt) or {}
                for a in ([entry.get("name", "")] + entry.get("aliases", [])):
                    if a and str(a) in q:
                        type_hits += 1
                        break
            except Exception:
                pass
        s += min(type_hits, 4) * 1.2
        # match_template 单点加分（需 FALLBACK_TEMPLATES 路由表，不是 BUILTIN_TEMPLATES）
        try:
            route = getattr(templates_mod, "FALLBACK_TEMPLATES", None) or builtin
            mk = catalog.match_template(q, route)
            if mk == key:
                s += 8.0
        except Exception:
            pass
        scores.append((s, key, title))
    scores.sort(reverse=True)
    # 去掉分数过低的（除非一个都命中）
    best = scores[0][0] if scores else 0
    picked = [x for x in scores if x[0] > 0]
    if len(picked) < top_k:
        # 补足：从剩余里取标题最像的
        for x in scores:
            if x not in picked:
                picked.append(x)
            if len(picked) >= top_k:
                break
    picked = picked[:top_k]
    out = []
    for s, key, title in picked:
        circuit = copy.deepcopy(builtin[key])
        reason = "模板命中" if s >= 1 else "邻近拓扑参考"
        if key == "led_no_limit":
            reason = "反例：缺限流电阻（教学演示用）"
        out.append({
            "key": key,
            "title": title,
            "circuit": circuit,
            "reason": reason,
            "score": round(s, 2),
        })
    return out


if __name__ == "__main__":
    r = pick_schemes("用555定时器做一个LED闪烁灯")
    print("schemes", [(x["key"], x["score"], x["reason"]) for x in r])
    assert len(r) >= 1
    print("schemes self-check ok")
