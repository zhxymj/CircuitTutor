# -*- coding: utf-8 -*-
"""
test_http_integration.py —— HTTP 集成测试（零 Key，生产级）
=============================================================
用真实 HTTP 打关键端点，验证：
* /api/health 返回版本与功能
* /api/docs 返回端点列表
* /api/metrics 返回计数器
* /api/erc 处理合法/非法电路
* /api/simulate 计算 LED 电流
* /api/bom 生成 CSV
* /api/spice 生成网表
* /api/schemes 返回草案
* /api/history 保存/列表/载入/删除
* 安全头存在
* 静态文件带 ETag

不依赖外部 LLM，可在 CI 跑。
"""

from __future__ import annotations

import json
import http.client
import os
import sys
import threading
import tempfile
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.version import VERSION

PASS = 0
FAIL = 0
BASE_URL = ""


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def _req(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    port = int(BASE_URL.rsplit(":", 1)[1])
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {"Content-Type": "application/json"} if data is not None else {}
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        try:
            payload = json.loads(raw.decode())
        except Exception:
            payload = raw
        return response.status, payload, dict(response.getheaders())
    except Exception as e:
        return -1, {"error": str(e)}, {}
    finally:
        conn.close()


def _read_sse(path, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", int(BASE_URL.rsplit(":", 1)[1]), timeout=timeout)
    events = set()
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        while True:
            raw_line = response.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", "replace").strip()
            if line.startswith("event: "):
                event = line[7:]
                events.add(event)
                if event == "done":
                    break
        return response.status, events
    finally:
        conn.close()


@contextmanager
def start_server():
    """Start the real Handler on a private loopback port without reuse."""
    global BASE_URL
    from http.server import ThreadingHTTPServer

    env_names = (
        "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
        "LSQC_HOST", "LSQC_PORT", "PORT", "LSQC_HISTORY_DIR",
        "PYTHON_DOTENV_DISABLED",
    )
    saved_env = {name: os.environ.get(name) for name in env_names}
    previous_base_url = BASE_URL
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    history_dir = tempfile.TemporaryDirectory(
        prefix=".tmp-http-integration-history-",
        dir=project_root,
    )
    httpd = None
    thread = None
    app = None
    previous_api_key = None
    try:
        for name in env_names:
            os.environ.pop(name, None)
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        os.environ["LSQC_HISTORY_DIR"] = history_dir.name

        import server as app
        previous_api_key = app.config.API_KEY
        app.config.API_KEY = ""
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        httpd.daemon_threads = True
        port = httpd.server_address[1]
        if port == 8765:
            raise RuntimeError("random ephemeral port unexpectedly matched 8765")
        BASE_URL = f"http://127.0.0.1:{port}"
        thread = threading.Thread(target=httpd.serve_forever, name="test-http-integration", daemon=True)
        thread.start()
        yield httpd, app
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)
        if app is not None and previous_api_key is not None:
            app.config.API_KEY = previous_api_key
        BASE_URL = previous_base_url
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        history_dir.cleanup()


def main():
    print("== HTTP 集成测试 ==")
    server_context = start_server()
    httpd, app = server_context.__enter__()
    try:
        check("测试使用独立随机端口", httpd.server_address[1] != 8765, httpd.server_address)
        check("测试服务仅绑定 loopback", httpd.server_address[0] == "127.0.0.1", httpd.server_address)
        check("测试配置不含外部 LLM key", not app.config.api_key_configured())

        # health
        st, d, h = _req("GET", "/api/health")
        check("GET /api/health 200", st == 200, st)
        check(
            "health version 精确匹配 VERSION",
            isinstance(d, dict) and d.get("version") == VERSION,
            d.get("version") if isinstance(d, dict) else d,
        )
        check("health 含 X-Request-Id", "X-Request-Id" in h or "x-request-id" in
              {k.lower() for k in h})
        check("安全头 X-Content-Type-Options",
              "X-Content-Type-Options" in h or "x-content-type-options" in
              {k.lower() for k in h})

        # docs
        st, d, h = _req("GET", "/api/docs")
        check("GET /api/docs 200", st == 200, st)
        check("docs 含 endpoints", isinstance(d, dict) and len(d.get("endpoints", [])) >= 10)

        # metrics
        st, d, h = _req("GET", "/api/metrics")
        check("GET /api/metrics 200", st == 200, st)
        check("metrics 含 counters", isinstance(d, dict) and "counters" in d)

        # providers
        st, d, h = _req("GET", "/api/providers")
        check("GET /api/providers 200", st == 200, st)
        check("providers 含 deepseek", isinstance(d, dict) and
              any(p.get("id") == "deepseek" for p in d.get("providers", [])))

        # static ETag
        st, raw, h = _req("GET", "/static/app.js")
        check("静态 app.js 返回 200", st == 200, st)
        check("静态 app.js 带 ETag", "ETag" in h or "etag" in
              {k.lower() for k in h})
        check("静态 app.js 带 Cache-Control",
              "Cache-Control" in h or "cache-control" in
              {k.lower() for k in h})

        # erc
        circuit = {
            "parts": [
                {"type": "battery", "id": "BAT1", "attrs": {"value": "5V"}},
                {"type": "resistor", "id": "R1", "attrs": {"value": "330"}},
                {"type": "led", "id": "LED1", "attrs": {"value": "红"}},
                {"type": "gnd", "id": "GND1"},
            ],
            "connections": [
                ["BAT1:+", "R1:1", "red", []],
                ["R1:2", "LED1:+", "red", []],
                ["LED1:-", "GND1:1", "black", []],
                ["BAT1:-", "GND1:1", "black", []],
            ],
        }
        st, d, h = _req("POST", "/api/erc", {"circuit": circuit})
        check("POST /api/erc 200", st == 200, st)
        check("erc passed", isinstance(d, dict) and d.get("passed") is True)

        st, d, h = _req("POST", "/api/erc", {"circuit": {}})
        check("POST /api/erc 空电路 400", st == 400, st)

        # simulate
        st, d, h = _req("POST", "/api/simulate", {"circuit": circuit})
        check("POST /api/simulate 200", st == 200, st)
        check("simulate 含 LED 电流",
              isinstance(d, dict) and d.get("led_currents") and
              5 < d["led_currents"][0].get("i_ma", 0) < 15)

        # bom
        st, d, h = _req("POST", "/api/bom", {"circuit": circuit, "format": "json"})
        check("POST /api/bom json 200", st == 200, st)
        check("bom 含 items", isinstance(d, dict) and d.get("items"))

        st, raw, h = _req("POST", "/api/bom", {"circuit": circuit, "format": "csv"})
        check("POST /api/bom csv 200", st == 200, st)
        check("csv 含序号", isinstance(raw, bytes) and ("序号" in raw.decode("utf-8", "ignore")))

        # spice
        st, raw, h = _req("POST", "/api/spice", {"circuit": circuit, "format": "cir"})
        check("POST /api/spice 200", st == 200, st)
        check("spice 含 .end", isinstance(raw, bytes) and b".end" in raw)

        # spice analyze
        st, d, h = _req("POST", "/api/spice_analyze",
                        {"circuit": circuit, "kind": "op"})
        check("POST /api/spice_analyze 200", st == 200, st)
        check("spice_analyze 含 deck",
              isinstance(d, dict) and ".end" in (d.get("deck") or ""))
        check("spice_analyze 含 analytic",
              isinstance(d, dict) and "analytic" in d)

        st, d, h = _req("POST", "/api/spice_analyze",
                        {"circuit": circuit, "kind": "bogus"})
        check("spice_analyze 非法 kind 400", st == 400, st)

        # trace query
        st, d, h = _req("GET", "/api/trace?n=3")
        check("GET /api/trace 200", st == 200, st)
        check("trace 返回 latest", isinstance(d, dict) and "latest" in d)

        # schemes
        st, d, h = _req("POST", "/api/schemes", {"query": "用555定时器做一个LED闪烁灯"})
        check("POST /api/schemes 200", st == 200, st)
        check("schemes 非空", isinstance(d, dict) and len(d.get("schemes", [])) >= 1)

        # history roundtrip
        st, d, h = _req("POST", "/api/history",
                        {"circuit": circuit, "title": "集成测试电路", "query": "test"})
        check("POST /api/history 200", st == 200, st)
        hid = d.get("id") if isinstance(d, dict) else None
        check("history 返回 id", bool(hid), d)

        st, d, h = _req("GET", "/api/history")
        check("GET /api/history 200", st == 200, st)
        check("history 含 items", isinstance(d, dict) and "items" in d)

        if hid:
            st, d, h = _req("GET", f"/api/history?id={hid}")
            check("GET /api/history?id= 200", st == 200, st)
            check("history 单条含 circuit", isinstance(d, dict) and d.get("circuit"))
            st, d, h = _req("DELETE", f"/api/history?id={hid}")
            check("DELETE /api/history 200", st == 200, st)

        # invalid path
        st, d, h = _req("GET", "/api/nope")
        check("未知端点 404", st == 404, st)

        # ---- /api/generate 全链路（零 Key 走模板兜底）----
        # SSE 端点：用流式读取，超时设长一点（兜底路径也要几秒）
        gen_ok = False
        gen_events = set()
        with patch.object(app, "make_llm_client", return_value=(None, None)) as llm_factory, \
                patch.object(app.llm, "DeepSeekClient", side_effect=AssertionError("LLM access is forbidden in this test")) as llm_constructor:
            try:
                path = "/api/generate?q=" + quote("用5V电池经330欧姆电阻点亮一个红色LED")
                status, gen_events = _read_sse(path, timeout=30)
                check("GET /api/generate 200", status == 200, status)
                gen_ok = "done" in gen_events
            except Exception as e:
                check("GET /api/generate 连接", False, str(e))
            check("SSE 仅使用离线 client=None 分支", llm_factory.call_count == 1, llm_factory.call_count)
            check("SSE 未构造任何 LLM client", llm_constructor.call_count == 0, llm_constructor.call_count)
        check("generate 收到 done 事件", gen_ok, gen_events)
        check("generate 含 circuit 事件", "circuit" in gen_events, gen_events)
        check("generate 含 fallback 或 erc",
              "fallback" in gen_events or "erc" in gen_events, gen_events)

        # ---- /api/pcb 预布局 ----
        st, d, h = _req("POST", "/api/pcb", {"circuit": circuit, "summary": True})
        check("POST /api/pcb summary 200", st == 200, st)
        check("pcb summary 含 board_w_mm",
              isinstance(d, dict) and "board_w_mm" in d, d)
        check("pcb summary 封装覆盖率>0",
              isinstance(d, dict) and d.get("with_footprint", 0) > 0, d)

    finally:
        server_context.__exit__(None, None, None)

    print(f"\nHTTP 集成：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
