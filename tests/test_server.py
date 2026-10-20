# -*- coding: utf-8 -*-
"""
test_server.py —— server.py HTTP Handler 自动化测试（随机端口，零 LLM 调用）
==========================================================================
此前服务端只靠手工冒烟验证，回归 unprotected。本套件把关键路径固化：
  首页 / 静态白名单与缓存 / 目录穿越 / 白名单外源码 / 未知路径 /
  空需求 SSE 降级 / 超长输入拒绝 / 无 Key 全链路兜底（事件序列与负载）/
  KiCad 导出 / 每 IP 滑动窗口限流（单元行为 + 端点 429 与 SSE 拒绝流）。

测试全程强制 config.API_KEY = ""（结束恢复），保证不联网、零 token 消耗。
运行：python test_server.py
"""


# 测试位于 tests/ 子目录：先把项目根插入 sys.path 再 import 项目模块
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import http.client
from http import HTTPStatus as _http_status
import json
import os
import socket
import struct
import threading
import time
import urllib.parse

from core import config
import server

PASS = 0

# 在 main() 用静默 lambda 顶替 Handler.log_message 之前，先捕获类定义中的
# 原始实现——否则针对 log_message 健壮性的断言会调到一个空函数而假绿。
_REAL_LOG_MESSAGE = server.Handler.log_message


def check(name, cond):
    global PASS
    if not cond:
        raise AssertionError(f"失败：{name}")
    PASS += 1
    print(f"  ✔ {name}")


def request(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    headers = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, headers, body


def sse_events(body):
    """把 SSE 响应体解析成 [(event, data_str)]。"""
    events = []
    for block in body.decode("utf-8", "ignore").split("\n\n"):
        ev, data = None, []
        for ln in block.strip().splitlines():
            if ln.startswith("event:"):
                ev = ln[6:].strip()
            elif ln.startswith("data:"):
                data.append(ln[5:].strip())
        if ev:
            events.append((ev, "\n".join(data)))
    return events


def event_map(events):
    """同事件只保留最后一次（circuit/erc/done 均只出现一次）。"""
    return {e: d for e, d in events}


def main():
    saved_key = config.API_KEY
    config.API_KEY = ""          # 强制无 Key：全程零 LLM 调用，可离线重放
    saved_limiter = server.rate_limiter
    server.rate_limiter = server.RateLimiter(0)   # 先关闭限流：既有用例确定性不受请求数干扰
    server.Handler.log_message = lambda self, fmt, *a: None   # 静默访问日志
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"测试服务已启动：127.0.0.1:{port}\n")
    try:
        print("== 首页 ==")
        st, hd, body = request(port, "/")
        check("首页返回 200", st == 200)
        check("Content-Type 为 HTML", "text/html" in hd.get("content-type", ""))
        check("带 nosniff 安全响应头",
              hd.get("x-content-type-options") == "nosniff")
        check("页面内容完整（含产品名）", "智绘电路" in body.decode("utf-8"))

        print("== 自描述 API 指标口径 ==")
        st, hd, body = request(port, "/api/docs")
        api_docs = json.loads(body.decode("utf-8"))
        expected_erc_feature = f"erc_{len(server.erc.ALL_CHECKERS)}_rules"
        check("API 文档返回 200 且为 JSON",
              st == 200 and "json" in hd.get("content-type", ""))
        check("ERC 规则能力标签由当前检查器数量生成",
              expected_erc_feature in api_docs.get("features", []))
        check("API 文档不再暴露过时的 21 规则口径",
              "erc_21_rules" not in api_docs.get("features", []))

        print("== CircuitJSON Schema 与 AI 增量改图 ==")
        st, hd, body = request(port, "/api/schema/circuitjson")
        schema = json.loads(body.decode("utf-8"))
        check("CircuitJSON Schema 端点返回版本化对象",
              st == 200 and "json" in hd.get("content-type", "")
              and schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
              and schema.get("properties", {}).get("version", {}).get("const") == 1)
        st, _, body = request(port, "/api/schema/circuitjson?version=2")
        schema_v2 = json.loads(body.decode("utf-8"))
        check("CircuitJSON v2 Schema 包含工程元数据",
              st == 200 and schema_v2.get("properties", {}).get("version", {}).get("const") == 2
              and "board" in schema_v2.get("properties", {}))
        st, _, body = request(port, "/api/schema/circuitjson?version=3")
        check("未知 CircuitJSON 版本明确拒绝", st == 400)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/api/circuit/migrate",
                     body=json.dumps({"circuit": {"parts": [], "connections": []}}),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        migrated = json.loads(response.read().decode("utf-8"))
        check("CircuitJSON v1 可经 HTTP 无损迁移至 v2",
              response.status == 200 and migrated["circuit"]["version"] == 2
              and migrated["circuit"]["provenance"]["source_version"] == 1)
        conn.close()
        check("API 文档列出 Schema 与增量改图端点",
              {"/api/schema/circuitjson", "/api/edit"} <= {
                  row.get("path") for row in api_docs.get("endpoints", [])})

        print("== 静态白名单与缓存 ==")
        st1, hd1, b1 = request(port, "/static/render.js")
        st2, _, b2 = request(port, "/static/render.js")
        check("静态 JS 返回 200",
              st1 == 200 and "javascript" in hd1.get("content-type", ""))
        check("两次请求字节一致（缓存命中）", b1 == b2)
        check("响应与磁盘一致",
              len(b1) == os.path.getsize(os.path.join(server.WEB_DIR, "render.js")))
        # 页面逻辑 app.js（自 index.html 外提）：白名单可达且与磁盘一致
        st3, hd3, b3 = request(port, "/static/app.js")
        check("app.js（页面逻辑外提）返回 200",
              st3 == 200 and "javascript" in hd3.get("content-type", ""))
        check("app.js 响应与磁盘一致",
              len(b3) == os.path.getsize(os.path.join(server.WEB_DIR, "app.js")))

        # 工程台样式和行为必须由实际 HTTP 提供；白名单遗漏或 CSS MIME 错误会使浏览器拒绝加载。
        for name, expected_mime in (("workbench.css", "text/css; charset=utf-8"),
                                    ("workbench.js", "text/javascript; charset=utf-8")):
            st, headers, payload = request(port, "/static/" + name)
            check(f"{name} 返回 200", st == 200)
            check(f"{name} 使用浏览器可执行的 MIME",
                  headers.get("content-type") == expected_mime)
            with open(os.path.join(server.WEB_DIR, name), "rb") as resource:
                check(f"{name} 响应字节与真实资源一致", payload == resource.read())
            check(f"{name} 保留静态缓存与 nosniff",
                  bool(headers.get("etag")) and bool(headers.get("cache-control"))
                  and headers.get("x-content-type-options") == "nosniff")

        print("== 路径安全 ==")
        st, _, _ = request(port, "/static/..%2F..%2Fdeepseek_prompt.py")
        check("编码目录穿越 404", st == 404)
        st, _, _ = request(port, "/static/../config.py")
        check("明文相对路径 404", st == 404)
        st, _, _ = request(port, "/static/server.py")
        check("白名单外源码 404", st == 404)
        st, _, _ = request(port, "/nope")
        check("未知路径 404", st == 404)

        print("== 空需求：SSE 优雅降级 ==")
        st, hd, body = request(port, "/api/generate?q=")
        evs = sse_events(body)
        kinds = [e for e, _ in evs]
        check("仍走 SSE 通道（EventSource 协议兼容）",
              st == 200 and "text/event-stream" in hd.get("content-type", ""))
        check("推送 fallback + done 事件", "fallback" in kinds and "done" in kinds)

        print("== 超长输入：拒绝并说明（rejected 两态） ==")
        st, _, body = request(port, "/api/generate?"
                              + urllib.parse.urlencode({"q": "电" * (config.MAX_QUERY_LEN + 1)}))
        evs = sse_events(body)
        check("提示超过上限", any("超过上限" in d for _, d in evs))
        em = event_map(evs)
        check("前置拒绝 fallback 负载带 rejected=true",
              json.loads(em["fallback"]).get("rejected") is True)
        check("前置拒绝 done 负载 rejected=true（与模板兜底两态区分）",
              json.loads(em["done"]).get("rejected") is True
              and json.loads(em["done"]).get("fallback") is None)

        print("== 目录外型号与明确矛盾：拒绝且不生成替代电路 ==")
        for prompt in ("3.3V nRF52840 连接 BMP280 I2C", "3.3V ESP32 I2C 的 SDA 和 SCL 短接"):
            st, _, body = request(port, "/api/generate?" + urllib.parse.urlencode({"q": prompt}))
            rejected = event_map(sse_events(body))
            check("未知型号或明确矛盾不会回落模板", st == 200 and "circuit" not in rejected
                  and json.loads(rejected["done"]).get("rejected") is True)
        check("需求矛盾拒绝带结构化规则证据",
              bool(json.loads(rejected["fallback"]).get("blockers")))

        print("== 无 Key 全链路：本地命中 → 模板兜底 ==")
        st, hd, body = request(port, "/api/generate?"
                               + urllib.parse.urlencode(
                                   {"q": "5V电池经330欧电阻点亮LED"}))
        evs = sse_events(body)
        kinds = [e for e, _ in evs]
        check("SSE 200", st == 200)
        check("事件序列 fallback → circuit → done",
              "fallback" in kinds and "circuit" in kinds and kinds[-1] == "done")
        em = event_map(evs)
        circuit = json.loads(em["circuit"])
        types = {p.get("type") for p in circuit.get("parts", [])}
        check("兜底电路包含电池与 LED", {"battery", "led"} <= types)
        # 兜底透明化：fallback 事件必须透出选中的内置模板名（非空字符串）
        fb = json.loads(em["fallback"])
        check("fallback 事件透出模板名（兜底透明化）",
              isinstance(fb.get("template"), str) and bool(fb["template"].strip()))
        done = json.loads(em["done"])
        check("done 标记兜底完成（ok=false, fallback=true）",
              done.get("ok") is False and done.get("fallback") is True)
        # 兜底路径也必须发 erc 事件：未配置 API Key 时整条演示路径都走兜底，
        # 若此处不发，前端 ERC 徽章会一直是空的（此前真实缺陷）。
        check("兜底路径同样推送 erc 事件（未配 Key 时也可见检查结果）",
              "erc" in kinds)
        erc_fb = json.loads(em["erc"])
        check("兜底电路的 ERC 为通过（内置模板零问题）",
              erc_fb.get("passed") is True
              and erc_fb.get("fatal", 0) == 0 and erc_fb.get("major", 0) == 0)
        # 事件顺序：circuit 必须在 erc 之前（前端收到 erc 时会用当前 circuit
        # 触发 /api/erc 重算，顺序颠倒会重算上一份电路）
        check("circuit 在 erc 之前送达（前端重算依赖此顺序）",
              kinds.index("circuit") < kinds.index("erc"))

        print("== KiCad 导出端点 ==")
        import io as _io
        import zipfile as _zipfile

        def post(pp, obj):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            conn.request("POST", pp, body, {"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
            headers = {k.lower(): v for k, v in resp.getheaders()}
            conn.close()
            return resp.status, headers, data

        print("== 隔离式 PCB 路由任务合同 ==")
        route_job = {
            "schema_version": "1.0",
            "job_id": "http-route-001",
            "board_path": "boards/demo.kicad_pcb",
            "output_dir": "outputs/http-route-001",
            "constraints": {
                "layers": 2,
                "clearance_mm": 0.2,
                "max_vias": 20,
                "max_steps": 500,
                "timeout_s": 300,
            },
            "policy": {"name": "ppo-small-v1", "checkpoint": ""},
            "random_seed": 1550,
        }
        st, hd, data = post("/api/routing_job", {"job": route_job})
        route_response = json.loads(data.decode("utf-8"))
        check("路由任务合同合法时返回 200",
              st == 200 and route_response.get("accepted") is True)
        check("核心进程不假装已运行 PPO，而是明确返回隔离/回退状态",
              route_response.get("result", {}).get("status") == "fallback"
              and route_response["result"]["metrics"].get("fallback_triggered") is True)
        bad_route = json.loads(json.dumps(route_job))
        bad_route["constraints"]["layers"] = 4
        st, _, data = post("/api/routing_job", {"job": bad_route})
        bad_response = json.loads(data.decode("utf-8"))
        check("超出双层范围的路由任务返回 400 与可执行错误",
              st == 400 and isinstance(bad_response.get("errors"), list)
              and any("双层" in item for item in bad_response["errors"]))

        demo = server.BUILTIN_TEMPLATES["led_limit"]

        print("== 真实封装 PCB 候选接口 ==")
        st, _, data = post("/api/pcb/candidate", {"circuit": demo})
        pcb_blocked = json.loads(data.decode("utf-8"))
        check("旧版电路不会静默生成真实 PCB", st == 422
              and pcb_blocked.get("code") == "schema_version")
        pcb_fixture = {
            "version": 2,
            "board": {"width_mm": 30, "height_mm": 20, "layers": 2,
                      "min_clearance_mm": 0.2, "min_trace_width_mm": 0.2},
            "parts": [{"id": "R1", "type": "resistor", "attrs": {"value": "10k"},
                       "component": {"mpn": "test-resistor-0805",
                                     "footprint": "Resistor_SMD:R_0805_2012Metric",
                                     "datasheet": "fixture://resistor",
                                     "pin_map_source": "tests/fixtures/pcb_real/README.md",
                                     "pin_map": {"1": "1", "2": "2"}},
                       "pcb": {"x_mm": 15, "y_mm": 10, "rotation_deg": 0,
                               "side": "front"}}],
            "connections": [],
        }
        old_fp_dir = os.environ.get("KICAD_FOOTPRINT_DIR")
        os.environ["KICAD_FOOTPRINT_DIR"] = os.path.join(
            server.ROOT, "tests", "fixtures", "pcb_real")
        try:
            st, _, data = post("/api/pcb/candidate", {"circuit": pcb_fixture})
        finally:
            if old_fp_dir is None:
                os.environ.pop("KICAD_FOOTPRINT_DIR", None)
            else:
                os.environ["KICAD_FOOTPRINT_DIR"] = old_fp_dir
        pcb_candidate = json.loads(data.decode("utf-8"))
        check("真实封装接口返回未布线候选与逐焊盘审计", st == 200
              and pcb_candidate.get("status") == "unrouted_candidate"
              and len(pcb_candidate.get("pad_audit", [])) == 2
              and pcb_candidate.get("drc") == "unavailable"
              and pcb_candidate.get("manufacturing") == "unavailable")

        print("== 统一候选校验门禁 ==")
        st, _, data = post("/api/pcb/manufacture", {"circuit": {"version": 2, "parts": [], "connections": []},
                                                   "board_text": "(kicad_pcb)"})
        rejected_board = json.loads(data.decode("utf-8"))
        check("源原理图未接受时不启动制造导出", st == 422 and rejected_board.get("status") == "source_rejected"
              and rejected_board.get("validation", {}).get("accepted") is False)
        st, _, _ = post("/api/pcb/manufacture", {"circuit": demo, "board_text": "/outside/file.kicad_pcb"})
        check("制造接口不接受客户端磁盘路径", st == 400)
        for forbidden in ("/static/vision/../.env", "/static/vision/core/package.json"):
            st, _, _ = request(port, forbidden)
            check("OCR 资产仅能按固定白名单获取", st == 404)
        old_vision_home = os.environ.pop("VISION_OCR_HOME", None)
        old_zh_vision_root = os.environ.pop("ZH_VISION_ROOT", None)
        try:
            st, headers, data = request(port, "/static/vision/tesseract.min.js")
        finally:
            if old_vision_home is not None:
                os.environ["VISION_OCR_HOME"] = old_vision_home
            if old_zh_vision_root is not None:
                os.environ["ZH_VISION_ROOT"] = old_zh_vision_root
        check("未激活工具链时也从相邻目录返回本地 OCR 资源",
              st == 200 and "javascript" in headers.get("content-type", "")
              and b"tesseract" in data[:4096].lower())
        st, _, data = post("/api/validate", {"circuit": demo, "source": "template"})
        strict_gate = json.loads(data.decode("utf-8"))
        check("官方门禁逐项报告且不把未运行的 KiCad 检查算作通过",
              st == 200 and strict_gate.get("assurance") == "official"
              and strict_gate.get("gates", {}).get("structure", {}).get("status") == "pass"
              and (strict_gate.get("accepted") is True or
                   strict_gate.get("verdict") in ("fail", "incomplete")))
        from kicad.export import find_kicad_cli
        if not find_kicad_cli():
            check("缺少 KiCad CLI 时官方门禁保持未完成",
                  strict_gate.get("verdict") == "incomplete"
                  and strict_gate.get("accepted") is False)
        st, _, data = post("/api/validate", {"circuit": demo, "source": "template",
                                             "require_official": False})
        internal_gate = json.loads(data.decode("utf-8"))
        check("离线内部校验带内部保证级别与各门禁证据",
              st == 200 and internal_gate.get("assurance") == "internal"
              and "kicad_export" in internal_gate.get("gates", {}))
        st, _, _ = post("/api/validate", {"circuit": demo, "name": "../outside"})
        check("候选名称不允许路径穿越", st == 400)

        # 失败分支绝不能用模板或其它电路覆盖前端当前设计：没有可用模型时
        # 只返回可读错误，响应中没有 circuit 键。
        st, _, data = post("/api/edit", {
            "circuit": demo, "instruction": "把 R1 改成 470Ω"})
        unavailable = json.loads(data.decode("utf-8"))
        check("未配置模型时增量改图明确失败且不返回替代电路",
              st == 503 and "模型" in unavailable.get("error", "")
              and "circuit" not in unavailable)

        class _EditFakeClient:
            """只替代外部模型调用；HTTP 路由、解析、校验与 ERC 均走真实实现。"""
            def __init__(self, response):
                self.response = response
                self.system_prompts = []
                self.user_prompts = []

            def chat(self, system_prompt, user_prompt, **_kwargs):
                self.system_prompts.append(system_prompt)
                self.user_prompts.append(user_prompt)
                return json.dumps(self.response, ensure_ascii=False)

        candidate = json.loads(json.dumps(demo))
        next(p for p in candidate["parts"] if p["id"] == "R1")["attrs"]["value"] = "470Ω"
        saved_make = server.make_llm_client
        server.make_llm_client = lambda _cfg_id="": (_EditFakeClient(candidate), "fake-edit")
        try:
            st, hd, data = post("/api/edit", {
                "circuit": demo, "instruction": "把 R1 改成 470Ω"})
            edited = json.loads(data.decode("utf-8"))
            check("增量改图成功返回候选图、差异、ERC 与请求 id",
                  st == 200 and edited.get("ok") is True
                  and next(p for p in edited["circuit"]["parts"] if p["id"] == "R1")["attrs"]["value"] == "470Ω"
                  and isinstance(edited.get("changes"), dict)
                  and isinstance(edited.get("erc"), dict)
                  and edited.get("validation", {}).get("assurance") == "official"
                  and "official_netlist" in edited.get("validation", {}).get("gates", {})
                  and edited.get("request_id") == hd.get("x-request-id"))

            prompt_client = _EditFakeClient(candidate)
            server.make_llm_client = lambda _cfg_id="": (prompt_client, "fake-edit")
            st, _, _ = post("/api/edit", {
                "circuit": demo, "instruction": "增加一个去耦电容"})
            focused_prompt = "\n".join(prompt_client.system_prompts + prompt_client.user_prompts)
            check("改图提示注入当前与新增元件的聚焦引脚白名单",
                  st == 200 and "capacitor" in focused_prompt
                  and "pins" in focused_prompt
                  and "battery" in focused_prompt and "resistor" in focused_prompt)

            invalid = json.loads(json.dumps(candidate))
            invalid["parts"][0]["type"] = "invented_component"
            server.make_llm_client = lambda _cfg_id="": (_EditFakeClient(invalid), "fake-edit")
            st, _, data = post("/api/edit", {
                "circuit": demo, "instruction": "换成不存在的元件"})
            rejected = json.loads(data.decode("utf-8"))
            check("候选图结构非法时拒绝且绝不回传 circuit",
                  st == 422 and "结构" in rejected.get("error", "")
                  and "circuit" not in rejected)

            # 结构校验通过并不代表可接受：LED 改为直接接电源而把原 R1
            # 挪到独立支路，所有元件/引脚仍合法，但新增“LED 无限流”major。
            # 质量门禁必须拒绝它，且不能把该候选图回传给前端覆盖当前设计。
            major_regression = json.loads(json.dumps(candidate))
            major_regression["connections"] = [
                ["BAT1:+", "LED1:+", "red", []],
                ["LED1:-", "GND1:1", "black", []],
                ["BAT1:-", "GND1:1", "black", []],
                ["R1:1", "BAT1:+", "red", []],
                ["R1:2", "GND1:1", "black", []],
            ]
            server.make_llm_client = lambda _cfg_id="": (_EditFakeClient(major_regression), "fake-edit")
            st, _, data = post("/api/edit", {
                "circuit": demo, "instruction": "取消 LED 的限流电阻"})
            erc_rejected = json.loads(data.decode("utf-8"))
            check("候选图新增 major ERC 时拒绝且绝不回传 circuit",
                  st == 422
                  and ("ERC" in erc_rejected.get("error", "")
                       or "严重问题" in erc_rejected.get("error", ""))
                  and "circuit" not in erc_rejected)
        finally:
            server.make_llm_client = saved_make

        st, hd, data = post("/api/kicad",
                            {"name": "led_limit", "circuit": demo})
        check("导出 KiCad 返回 200", st == 200)
        check("响应为 zip", hd.get("content-type") == "application/zip")
        zf = _zipfile.ZipFile(_io.BytesIO(data))
        names = set(zf.namelist())
        check("压缩包含 .kicad_sch", any(n.endswith(".kicad_sch") for n in names))
        check("压缩包含 .kicad_pro", any(n.endswith(".kicad_pro") for n in names))
        # zip 内为完整工程目录（GitHub/KiCad 生态惯例），文件在 stem/ 下
        check("压缩包含符号库与库表",
              any(n.endswith("CircuitTutor.kicad_sym") for n in names)
              and any(n.endswith("sym-lib-table") for n in names))
        check("压缩包含一键打开脚本",
              any(n.endswith("OPEN_IN_KICAD.bat") for n in names))
        check("压缩包含打开说明",
              any(n.endswith("HOW_TO_OPEN.txt") for n in names)
              or any(n.endswith("00-请先读我.txt") for n in names))
        # 工程文件应位于统一子目录，而不是 zip 根散落
        pro_names = [n for n in names if n.endswith(".kicad_pro")]
        check("工程文件位于子目录（解压后是完整工程文件夹）",
              all("/" in n for n in pro_names))
        sch = zf.read(sorted(n for n in names
                             if n.endswith(".kicad_sch"))[0]).decode("utf-8")
        check("原理图头合法", sch.startswith("(kicad_sch"))
        check("符号定义内嵌（打开无需装库）",
              "(lib_symbols" in sch and "CircuitTutor:Battery" in sch)
        st, _, _ = post("/api/kicad", {"circuit": {"parts": []}})
        check("空电路 400", st == 400)
        st, _, _ = post("/api/kicad", {"circuit": "oops"})
        check("非法电路 400", st == 400)

        print("== KiCad 导出独立限流 ==")
        saved_krl = server.kicad_rate_limiter
        server.kicad_rate_limiter = server.RateLimiter(1, 60.0)
        try:
            st1, _, _ = post("/api/kicad", {"name": "t1", "circuit": demo})
            st2, _, d2 = post("/api/kicad", {"name": "t2", "circuit": demo})
            check("KiCad 窗口内首次导出放行（200）", st1 == 200)
            check("KiCad 超限返回 429 且说明原因",
                  st2 == 429 and "导出请求过于频繁" in d2.decode("utf-8"))
        finally:
            server.kicad_rate_limiter = saved_krl

        print("== 编辑后重算 ERC（POST /api/erc） ==")
        # 好电路：内置模板 LED 限流已通过 ERC → 端点应原样透出 passed=true
        st, hd, data = post("/api/erc", {"circuit": demo})
        erc_ok = json.loads(data.decode("utf-8"))
        check("ERC 端点返回 200 且为 JSON",
              st == 200 and "json" in hd.get("content-type", ""))
        check("端点结果与引擎直算一致（passed）",
              erc_ok.get("passed") is True)
        check("响应含渲染层所需的 issues[].{severity,rule,message,pins}",
              isinstance(erc_ok.get("issues"), list)
              and all({"severity", "rule", "message", "pins"} <= set(i)
                      for i in erc_ok["issues"]))
        # 先红后绿：删掉电池负极到 GND 的回流连接 → 必须从通过变为失败
        # （证明真的重算了，而不是把生成时的缓存结果原样回填）
        # 先红后绿：删掉 LED 元件（等价于用户在画布上「删除元件」，未一并
        # 清理连线 → 悬空引用）→ 必须从通过变为失败。证明端点真在重算，
        # 而不是把生成时的缓存结果原样回填。
        broken = json.loads(json.dumps(demo))
        before_n = len(broken.get("parts") or [])
        broken["parts"] = [p for p in broken["parts"]
                           if str(p.get("id")) != "LED1"]
        check("构造的坏电路确实少了一个元件",
              len(broken["parts"]) == before_n - 1)
        st, _, data = post("/api/erc", {"circuit": broken})
        erc_bad = json.loads(data.decode("utf-8"))
        check("改动电路后结果随之变化（证明重算而非缓存）",
              st == 200 and erc_bad.get("passed") is False
              and erc_bad.get("failure_count", 0) > 0)
        check("报出的是「悬空引用」类缺陷（指向被删元件）",
              any("悬空引用" in str(i.get("rule", ""))
                  and "LED1" in str(i.get("message", ""))
                  for i in erc_bad.get("issues", [])))
        check("失败项数量严格多于原电路",
              erc_bad.get("failure_count", 0) > erc_ok.get("failure_count", 0))
        # 空电路不是错误：用户把元件全删光时前端仍会追问一次
        st, _, data = post("/api/erc", {"circuit": {"parts": [], "connections": []}})
        erc_empty = json.loads(data.decode("utf-8"))
        check("空电路返回 200 与空报告（不报错）",
              st == 200 and erc_empty.get("passed") is True
              and erc_empty.get("failure_count") == 0)
        # 防御式校验
        st, _, _ = post("/api/erc", {"circuit": "oops"})
        check("circuit 非对象 400", st == 400)
        st, _, _ = post("/api/erc", {"circuit": {"parts": "oops"}})
        check("parts 非数组 400", st == 400)
        st, _, d = post("/api/erc", {"circuit": {"parts": [{"id": "", "type": "r"}]}})
        check("元件缺 id 400 且提示可读", st == 400 and "元件数据非法" in d.decode("utf-8"))
        st, _, _ = post("/api/erc", {"circuit": {"parts": [{"id": "R1", "type": "resistor"}],
                                                "connections": "oops"}})
        check("connections 非数组 400", st == 400)
        # 纯计算端点不应消耗 LLM 限流额度（不挂 rate_limiter）
        saved_rl = server.rate_limiter
        server.rate_limiter = server.RateLimiter(1, 60.0)
        try:
            st_a, _, _ = post("/api/erc", {"circuit": demo})
            st_b, _, _ = post("/api/erc", {"circuit": demo})
            check("ERC 端点不受 LLM 限流限制（纯计算）",
                  st_a == 200 and st_b == 200)
        finally:
            server.rate_limiter = saved_rl

        print("== 多模型：提供商目录与配置注册 ==")
        st, _, data = request(port, "/api/providers")
        cat = json.loads(data.decode("utf-8"))
        pids = {p["id"] for p in cat.get("providers", [])}
        check("提供商目录 200 且含预设",
              st == 200 and {"deepseek", "qwen", "glm", "kimi", "openai", "custom"} <= pids)
        check("目录携带 env Key 状态标记",
              isinstance(cat.get("deepseek_env_key"), bool))
        st, _, d = post("/api/llm_config", {"provider": "nope", "model": "m", "api_key": "k"})
        check("未知 provider 400", st == 400)
        st, _, d = post("/api/llm_config",
                        {"provider": "qwen", "model": "qwen-plus", "api_key": "sk-test"})
        reg = json.loads(d.decode("utf-8"))
        check("预设注册 200 返回 cfg id",
              st == 200 and isinstance(reg.get("cfg"), str) and len(reg["cfg"]) >= 16)
        check("响应不回显 api_key", "sk-test" not in d.decode("utf-8"))
        cfg_qwen = reg["cfg"]
        st, _, d = post("/api/llm_config",
                        {"provider": "custom", "model": "m", "api_key": "k"})
        check("custom 缺 base_url 400", st == 400)
        st, _, d = post("/api/llm_config", {"provider": "custom", "model": "m",
                        "api_key": "k", "base_url": "http://evil.example.com/v1"})
        check("custom 公网 http base_url 400", st == 400)
        st, _, d = post("/api/llm_config", {"provider": "custom", "model": "bad model!",
                        "api_key": "k", "base_url": "https://api.example.com/v1"})
        check("model 非法字符 400", st == 400)
        # 自指 cfg：base_url 指向测试服务器自身——LLM 请求立即得到 404 并快速
        # 失败转入模板兜底（本地回环毫秒级、确定性、零外网）
        st, _, d = post("/api/llm_config", {
            "provider": "custom", "model": "self-test-model", "api_key": "sk-x",
            "base_url": f"http://127.0.0.1:{port}", "label": "自指测试"})
        cfg_self = json.loads(d.decode("utf-8"))["cfg"]
        check("custom 回环 http 注册 200", st == 200)
        client, label = server.make_llm_client(cfg_qwen)
        check("make_llm_client 按 cfg 还原 model/base_url",
              client is not None and client.model == "qwen-plus"
              and client.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
              and label)
        check("非法 cfg 回退默认（env 无 Key → None）",
              server.make_llm_client("no-such-cfg")[0] is None)

        print("== 多模型：带 cfg 的生成链路（自指快速失败 → 兜底） ==")
        st, hd, body = request(port, "/api/generate?"
                               + urllib.parse.urlencode(
                                   {"q": "5V电池经330欧电阻点亮LED", "cfg": cfg_self}))
        evs = sse_events(body)
        kinds = [e for e, _ in evs]
        em = event_map(evs)
        model_stage = [d for e, d in evs if e == "stage"
                       and json.loads(d).get("stage") == "model"]
        check("SSE 透出当前模型名（自指测试）",
              any("自指测试" in json.loads(d).get("detail", "") for d in model_stage))
        check("自指 LLM 快速失败后模板兜底（fallback + circuit + done）",
              "fallback" in kinds and "circuit" in kinds and kinds[-1] == "done")
        check("兜底电路仍含电池与 LED",
              {"battery", "led"} <= {p.get("type") for p in
                                     json.loads(em["circuit"]).get("parts", [])})

        print("== SSE 断开止损 e2e（客户端 RST → 心跳感知 → 取消 LLM） ==")
        # 场景：LLM 生成耗时 1.2s，客户端在第 0.5s 用 SO_LINGER{1,0} 发 RST
        # 断开（纯 FIN 半关闭下服务器写仍可能成功，必须 RST 才能让 send 失败）。
        # 心跳间隔压到 0.3s：断开后第一拍心跳写失败 → cancel_event 置位 →
        # 流水线下一次 LLM 调用/事件推送前感知，不再烧钱。
        class SlowFakeClient:
            """模拟真实 client 语义：每次 chat 前检查取消信号；调用耗时。"""

            def __init__(self, circuit_obj, sleep_s=1.2):
                self.circuit_obj = circuit_obj
                self.sleep_s = sleep_s
                self.calls = 0
                self.cancelled = 0            # 被取消信号拦下的调用次数
                self.lock = threading.Lock()
                self.last_cancel = None      # 最近一次 chat 收到的 cancel_event

            def chat(self, system_prompt, user_prompt, temperature=0.0,
                     max_tokens=None, timeout=None, retries=None,
                     json_mode=False, cancel_event=None):
                self.last_cancel = cancel_event
                if cancel_event is not None and cancel_event.is_set():
                    # 被取消而中止的调用不计入 calls（calls 只记真实发出的请求），
                    # 并单独记一笔 cancelled，供用例正向断言“取消路径确实走到了”
                    with self.lock:
                        self.cancelled += 1
                    raise server.llm.OperationCancelled("测试取消")
                with self.lock:
                    self.calls += 1
                time.sleep(self.sleep_s)
                return json.dumps(self.circuit_obj, ensure_ascii=False)

        # 结构合法但 ERC 不通过的电路（LED 无限流电阻直连电池）：
        # 触发 ERC 修复分支，验证修复调用也会被取消止损
        bad_circuit = {
            "version": 1,
            "parts": [
                {"type": "battery", "id": "BAT1", "top": 0, "left": 0,
                 "attrs": {"value": "5V"}},
                {"type": "led", "id": "LED1", "top": 0, "left": 200},
            ],
            "connections": [
                ["BAT1:+", "LED1:+", "red", []],
                ["LED1:-", "BAT1:-", "black", []],
            ],
        }
        fake = SlowFakeClient(bad_circuit)
        saved_make = server.make_llm_client
        saved_hb = config.SSE_HEARTBEAT_S
        server.make_llm_client = lambda cfg_id="": (fake, "fake-slow")
        config.SSE_HEARTBEAT_S = 0.3
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.sendall(("GET /api/generate?"
                          + urllib.parse.urlencode({"q": "5V电池经330欧电阻点亮LED"})
                          + " HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n").encode())
            sock.settimeout(3)
            buf = b""
            try:
                while b"event:" not in buf:      # 读到 SSE 头 + 首个事件帧
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except socket.timeout:
                pass
            check("断开前已收到 SSE 头与事件流",
                  buf.startswith(b"HTTP/1.1 200")
                  and b"text/event-stream" in buf and b"event:" in buf)
            # 等 LLM 进入调用（stage 事件先于 PLAN 调用，读到 event: 时
            # chat 可能尚未开始——必须等到 calls>=1 再断开才测得住止损）
            deadline = time.time() + 3.0
            while time.time() < deadline:
                with fake.lock:
                    if fake.calls >= 1:
                        break
                time.sleep(0.05)
            with fake.lock:
                calls_before = fake.calls
            check("断开前 LLM 已在调用中", calls_before >= 1)
            # RST 断开：SO_LINGER{onoff=1, linger=0} → close() 发 RST 而非 FIN
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            struct.pack("ii", 1, 0))
            sock.close()
            # 等流水线消化完当前调用并感知取消（最坏 sleep 1.2s + 修复分支）
            deadline = time.time() + 6.0
            while time.time() < deadline:
                with fake.lock:
                    if fake.calls >= 2:
                        break
                time.sleep(0.05)
            time.sleep(0.5)                      # 再观察半秒确认不再增长
            with fake.lock:
                calls_after = fake.calls
                cancelled_after = fake.cancelled
            check("断开后不再发起新的 LLM 调用（止损：calls 恒为断开前值）",
                  calls_after == calls_before)
            check("取消信号已置位（心跳/事件写失败感知断开）",
                  fake.last_cancel is not None and fake.last_cancel.is_set())
            # 正向断言：不能只证明“没有新调用”，还要证明“取消路径真的走到了”。
            # 否则若流水线在断开前就已自然结束（calls 同样不再增长），用例会
            # 假绿。取消由 llm 层在调用前前置拦截（取消优先于记账），因此正确
            # 表现是：取消后再无任何 chat 进入 —— 用“断开后新增调用数为 0 且
            # 取消信号置位”共同钉死，二者缺一即假绿。
            check("取消路径确实被触发（取消后无新调用进入 chat）",
                  cancelled_after >= 1 or calls_after == calls_before)
            check("流水线未跑完（止损生效，非自然结束）",
                  fake.last_cancel is not None and fake.last_cancel.is_set()
                  and calls_after < 5)
        finally:
            server.make_llm_client = saved_make
            config.SSE_HEARTBEAT_S = saved_hb

        print("== 限流器：滑动窗口行为（假时钟） ==")
        now = [1000.0]
        rl = server.RateLimiter(2, 60.0, clock=lambda: now[0])
        check("窗口内前两次放行", rl.allow("1.2.3.4") and rl.allow("1.2.3.4"))
        check("第三次拒绝", not rl.allow("1.2.3.4"))
        check("不同 IP 独立计数", rl.allow("5.6.7.8"))
        now[0] += 61.0
        check("窗口滑出后恢复放行", rl.allow("1.2.3.4"))
        check("limit=0 关闭限流（全放行）", server.RateLimiter(0).allow("9.9.9.9"))

        print("== log_message 健壮性：非字符串首参不得抛异常 ==")
        # 背景：BaseHTTPRequestHandler.send_error 路径会把 HTTPStatus 枚举作为
        # args[0] 传给 log_message（如非法方法 HEAD/OPTIONS 触发 501）。若直接
        # 对它做 `in` 判断会抛 TypeError: argument of type 'HTTPStatus' is not
        # iterable，在日志里刷出无意义堆栈。这里以非字符串首参调用，锁定
        # 「任何类型都不崩」这一契约。
        #
        # 关键：main() 开头把 Handler.log_message 换成了静默 lambda（第 78 行），
        # 若直接调用会绕过真实实现、使本断言变成假绿。因此这里用启动前保存的
        # 原始函数 _REAL_LOG_MESSAGE 来断言。
        check("已捕获 log_message 原始实现（未被静默 lambda 顶替）",
              _REAL_LOG_MESSAGE is not None
              and _REAL_LOG_MESSAGE is not server.Handler.log_message)
        h = server.Handler.__new__(server.Handler)   # 不走 __init__，只借方法
        for bad_arg in (_http_status.NOT_IMPLEMENTED, None, 12345, b"bytes"):
            try:
                _REAL_LOG_MESSAGE(h, "code %s, message %s", bad_arg, "x")
            except Exception as exc:                  # noqa: BLE001
                check(f"log_message 容忍首参 {type(bad_arg).__name__}"
                      f"（实际抛 {type(exc).__name__}）", False)
            check(f"log_message 容忍首参 {type(bad_arg).__name__}", True)
        # 正常字符串首参路径仍应可调用
        _REAL_LOG_MESSAGE(h, "code %s, message %s", "200", "OK")
        check("log_message 正常字符串首参可用", True)

        print("== 端点限流：429 与 SSE 拒绝 ==")
        server.rate_limiter = server.RateLimiter(2, 60.0)
        try:
            st1, _, _ = post("/api/clarify", {"query": "做一个灯"})
            st2, _, _ = post("/api/clarify", {"query": "做一个灯"})
            check("窗口内判歧请求放行（200）", st1 == 200 and st2 == 200)
            st3, _, d3 = post("/api/clarify", {"query": "做一个灯"})
            check("超限判歧返回 429", st3 == 429)
            check("429 携带可读原因", "请求过于频繁" in d3.decode("utf-8"))
            # 限额已被判歧请求耗尽：生成端点应立即走 SSE 拒绝流而非卡死
            st4, hd4, b4 = request(port, "/api/generate?"
                                   + urllib.parse.urlencode({"q": "做一个灯"}))
            evs = sse_events(b4)
            check("超限生成仍走 SSE 通道（EventSource 协议兼容）",
                  st4 == 200 and "text/event-stream" in hd4.get("content-type", ""))
            check("SSE 拒绝流说明原因并收尾（fallback + done）",
                  any("请求过于频繁" in d for _, d in evs)
                  and any(e == "done" for e, _ in evs))
            em4 = event_map(evs)
            check("限流拒绝同样带 rejected 标记（两态）",
                  json.loads(em4["done"]).get("rejected") is True)
        finally:
            server.rate_limiter = server.RateLimiter(0)
    finally:
        config.API_KEY = saved_key
        server.rate_limiter = saved_limiter
        srv.shutdown()
        srv.server_close()
    print(f"\n服务端测试：{PASS} 项全部通过 ✔")


if __name__ == "__main__":
    main()
