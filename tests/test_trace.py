# -*- coding: utf-8 -*-
"""LLM 链路日志（request_id ContextVar + TraceCollector + span）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.trace import (  # noqa: E402
    set_request_id,
    get_request_id,
    trace_span,
    TraceCollector,
    TRACE_COLLECTOR,
    llm_call_started,
    llm_call_finished,
)


def test_contextvar_isolation():
    rid = "traceunit0000001"
    set_request_id(rid)
    assert get_request_id() == rid
    set_request_id("")
    # 空串不覆盖语义：清空
    assert get_request_id() == ""


def test_span_events_collected():
    rid = "traceunit0000002"
    set_request_id(rid)
    TRACE_COLLECTOR.clear()
    with trace_span("generate") as s:
        s.event("plan", model="deepseek-flash")
        s.event("generate", parts=3)
    events = TRACE_COLLECTOR.get(rid)
    msgs = [e["msg"] for e in events]
    assert "start" in msgs
    assert "plan" in msgs
    assert "done" in msgs
    assert any(e.get("model") == "deepseek-flash" for e in events)


def test_span_error_marks_fail():
    rid = "traceunit0000003"
    set_request_id(rid)
    TRACE_COLLECTOR.clear()
    try:
        with trace_span("llm") as s:
            s.error("empty_content")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    events = TRACE_COLLECTOR.get(rid)
    assert any(e["level"] == "error" for e in events)
    assert any("boom" in e["msg"] or "empty_content" in e["msg"] for e in events)


def test_llm_call_helpers():
    rid = "traceunit0000004"
    set_request_id(rid)
    TRACE_COLLECTOR.clear()
    t0 = llm_call_started("deepseek-flash", stage="plan")
    llm_call_finished("deepseek-flash", t0, ok=True, stage="plan", chars=42)
    events = TRACE_COLLECTOR.get(rid)
    assert any("call start" in e["msg"] for e in events)
    assert any("call ok" in e["msg"] for e in events)


def test_legacy_span_signature():
    # 兼容 trace_span(rid, name) 旧调用
    rid = "aabbccddeeff0011"
    TRACE_COLLECTOR.clear()
    with trace_span(rid, "legacy") as s:
        s.event("hello")
    events = TRACE_COLLECTOR.get(rid)
    assert any(e["msg"] == "hello" for e in events)


def test_collector_ring_bound():
    c = TraceCollector(max_per_rid=3, max_rids=2)
    c.add("r1", "s", "a")
    c.add("r1", "s", "b")
    c.add("r1", "s", "c")
    c.add("r1", "s", "d")  # 挤掉 a
    msgs = [e["msg"] for e in c.get("r1")]
    assert msgs == ["b", "c", "d"]
    c.add("r2", "s", "x")
    c.add("r3", "s", "y")  # 挤掉 r1
    assert c.get("r1") == []
    assert len(c.get("r3")) == 1


def test_latest():
    c = TraceCollector()
    c.add("ra", "s", "1")
    c.add("rb", "s", "2")
    lat = c.latest(5)
    assert "ra" in lat and "rb" in lat


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
    print(f"trace: {len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
