# -*- coding: utf-8 -*-
"""
trace.py —— LLM/ERC 链路日志（request_id 贯穿）
================================================
生产级排查：一次生成请求从 SSE → plan → generate → erc → autofix
→ precision → simulate，每一步都带同一个 request_id。

设计要点：
* ContextVar 绑定 request_id —— ThreadingHTTPServer 每请求一线程，
  ContextVar 线程隔离，不必给每个函数签名都加 rid 参数。
* TraceCollector —— 同一 request_id 的事件环形缓冲，供 /api/trace 查询。
* chat() / generate 自动读取 ContextVar，调用方只需 set_request_id()。

用法：
    from core.trace import trace_span, set_request_id, get_request_id
    set_request_id(rid)
    with trace_span("generate") as span:
        span.event("llm_call", model="deepseek-flash")
        span.error("empty_content")
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

log = logging.getLogger("circuit.trace")

# ContextVar：每请求独立，ThreadingHTTPServer 下天然隔离
_CURRENT_RID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "circuit_request_id", default="")


def set_request_id(rid: str) -> None:
    _CURRENT_RID.set(rid or "")


def get_request_id() -> str:
    return _CURRENT_RID.get() or ""


class TraceSpan:
    def __init__(self, name: str, request_id: Optional[str] = None):
        self.request_id = request_id or get_request_id() or "-"
        self.name = name
        self.t0 = time.time()
        self.events = 0
        self.ok = True

    def event(self, msg: str, **fields: Any) -> None:
        self.events += 1
        payload = {"request_id": self.request_id, "span": self.name,
                   "msg": msg, **fields}
        log.info("%s | %s | %s", self.request_id, self.name, msg,
                 extra={"extra_fields": payload})
        TRACE_COLLECTOR.add(self.request_id, self.name, msg, level="info", **fields)

    def error(self, msg: str, **fields: Any) -> None:
        self.ok = False
        payload = {"request_id": self.request_id, "span": self.name,
                   "msg": msg, "level": "error", **fields}
        log.error("%s | %s | ERROR %s", self.request_id, self.name, msg,
                  extra={"extra_fields": payload})
        TRACE_COLLECTOR.add(self.request_id, self.name, msg, level="error", **fields)

    def finish(self) -> None:
        dt = time.time() - self.t0
        log.info("%s | %s | done ok=%s events=%d %.0fms",
                 self.request_id, self.name, self.ok, self.events, dt * 1000,
                 extra={"extra_fields": {
                     "request_id": self.request_id, "span": self.name,
                     "ok": self.ok, "events": self.events,
                     "duration_ms": int(dt * 1000)}})
        TRACE_COLLECTOR.add(self.request_id, self.name, "done",
                            level="info", ok=self.ok, events=self.events,
                            duration_ms=int(dt * 1000))


class trace_span:
    """上下文管理器：with trace_span("generate") as s: ...

    兼容旧签名 trace_span(rid, name)：若第一个参数像 request_id（16 hex）
    且第二个参数是名字，则按旧用法解析。
    """
    def __init__(self, name_or_rid: str, name: Optional[str] = None):
        # 兼容 trace_span(rid, "generate") 旧调用
        if name is not None and _looks_like_rid(name_or_rid):
            self.span = TraceSpan(name, request_id=name_or_rid)
        else:
            self.span = TraceSpan(name_or_rid)

    def __enter__(self) -> TraceSpan:
        self.span.event("start")
        return self.span

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            self.span.error(f"exception: {type(exc).__name__}: {exc}")
        self.span.finish()
        return False


def _looks_like_rid(s: str) -> bool:
    return bool(s) and len(s) >= 8 and all(c in "0123456789abcdef" for c in s.lower())


class TraceCollector:
    """按 request_id 收集事件（环形缓冲，防内存膨胀）。"""
    def __init__(self, max_per_rid: int = 80, max_rids: int = 64):
        self._lock = threading.Lock()
        self.max_per_rid = max_per_rid
        self.max_rids = max_rids
        self._buf: Dict[str, Deque[dict]] = {}
        self._order: Deque[str] = deque()

    def add(self, rid: str, span: str, msg: str, level: str = "info", **fields: Any) -> None:
        if not rid or rid == "-":
            return
        entry = {"ts": time.time(), "span": span, "msg": msg, "level": level}
        entry.update(fields)
        with self._lock:
            if rid not in self._buf:
                if len(self._buf) >= self.max_rids:
                    old = self._order.popleft()
                    self._buf.pop(old, None)
                self._buf[rid] = deque(maxlen=self.max_per_rid)
                self._order.append(rid)
            self._buf[rid].append(entry)

    def get(self, rid: str) -> List[dict]:
        with self._lock:
            return list(self._buf.get(rid) or [])

    def latest(self, n: int = 5) -> Dict[str, List[dict]]:
        with self._lock:
            ids = list(self._order)[-n:]
            return {rid: list(self._buf.get(rid) or []) for rid in ids}

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
            self._order.clear()


TRACE_COLLECTOR = TraceCollector()


def bind_llm_logger(request_id: str = ""):
    """返回一个可传给 llm 层的日志适配器（可选）。"""
    rid = request_id or get_request_id()

    class _L:
        def __init__(self, r):
            self.rid = r

        def info(self, msg, *a, **k):
            log.info("%s | llm | %s", self.rid, msg % a if a else msg)

        def warning(self, msg, *a, **k):
            log.warning("%s | llm | %s", self.rid, msg % a if a else msg)

        def debug(self, msg, *a, **k):
            log.debug("%s | llm | %s", self.rid, msg % a if a else msg)

        def error(self, msg, *a, **k):
            log.error("%s | llm | %s", self.rid, msg % a if a else msg)

    return _L(rid)


def llm_call_started(model: str, stage: str = "") -> float:
    """chat() 开始时调用，返回 t0。"""
    rid = get_request_id()
    if rid:
        TRACE_COLLECTOR.add(rid, "llm", f"call start model={model} stage={stage}",
                            level="info", model=model, stage=stage)
    return time.time()


def llm_call_finished(model: str, t0: float, ok: bool = True, stage: str = "",
                      chars: int = 0, **extra: Any) -> None:
    rid = get_request_id()
    if not rid:
        return
    dt_ms = int((time.time() - t0) * 1000)
    TRACE_COLLECTOR.add(rid, "llm",
                        f"call {'ok' if ok else 'fail'} model={model} {dt_ms}ms",
                        level="info" if ok else "error",
                        model=model, stage=stage, duration_ms=dt_ms,
                        ok=ok, chars=chars, **extra)
    log.info("%s | llm | %s %s %dms chars=%d", rid, model, stage or "-",
             dt_ms, chars)


if __name__ == "__main__":
    set_request_id("selfcheck0001")
    with trace_span("generate") as s:
        s.event("plan", model="deepseek-flash")
        t0 = llm_call_started("deepseek-flash", "plan")
        llm_call_finished("deepseek-flash", t0, ok=True, stage="plan", chars=120)
    events = TRACE_COLLECTOR.get("selfcheck0001")
    assert len(events) >= 3, events
    assert get_request_id() == "selfcheck0001"
    print("trace self-check ok", len(events), "events")
