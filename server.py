# -*- coding: utf-8 -*-
"""
server.py —— 运行时优化演示服务（Python 标准库，零依赖）
=========================================================
把 core/llm.py 的运行时优化（预算/进度回调/本地短路/连接复用）
串成一个可直接体验的 Web 服务：

* ThreadingHTTPServer —— 每个请求独立线程。一个学生的 30s LLM 等待不会
  阻塞其他人的请求（映射到 Flask 只需 app.run(threaded=True) 一行）。
* GET /api/generate?q=… —— text/event-stream（SSE）实时推送：
  stage（规划中→生成中→结构修复→ERC 修复→完成）→ circuit（CircuitJSON）
  → done（含本次预算消耗）。前端从“黑盒转圈”变成有节奏的进度条。
* SSE 断开止损：客户端提前断开时 send_event 写失败即置位 cancel_event，
  流水线下一次 LLM 调用前抛 OperationCancelled —— 最多浪费“正在进行的
  这一次”调用，后续（结构修复/ERC 修复）全部取消，不再烧 token。
* SSE 心跳：LLM 思考期间流上几十秒无字节会被代理/网关掐断，
  心跳线程周期写 ": ping" 注释行保活（EventSource 自动忽略注释行）。
* POST /api/erc —— 编辑后重算 ERC（body: {"circuit": CircuitJSON}）→
  ERCReport JSON。让画布上的每次编辑都能立刻看到设计规则检查结果，
  补上「生成时算一次、之后永不重算」的能力断点。
* DEEPSEEK_API_KEY 未配置 / 网络 / 预算耗尽 —— 仅回落明确匹配且覆盖需求的
  circuit_templates.json 内置模板；关键条件未定时澄清，无覆盖模板时拒绝。
* GET /static/* —— 前端脚本与工程台样式的静态白名单分发，basename 归一化
  防目录穿越；响应带 nosniff，杜绝 MIME 嗅探注入。

运行：python server.py   →   http://127.0.0.1:8765/
配置：见 config.py（DEEPSEEK_API_KEY / LSQC_PORT / LSQC_HOST 等环境变量）
"""

import copy
import io
import json
import logging
import os
from pathlib import Path
import secrets
import select
import socket
import tempfile
import threading
import time
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs, unquote, quote

from core import catalog
from core import blocks as circuit_blocks
from core import config
from core import clarify
from core import llm
from core import templates
from core import erc
from core import autofix
from core import circuit_edit
from core.external_import import import_external_result
from core.observability import METRICS, log_request, new_request_id, LLM_BREAKER
from core.trace import set_request_id, trace_span, get_request_id, TRACE_COLLECTOR
from core.version import VERSION
from kicad import export

from core import llm_providers

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")   # 前端静态资源目录（index.html + 6 个 JS）


# ---------------------------------------------------------------------
# 静态文件缓存：按 (mtime_ns, size) 校验的内存缓存
# 页面加载会反复请求 index.html 与 5 个 JS，原先每次都走磁盘读；
# 命中缓存时零磁盘 IO，文件一旦改动 mtime 变化即自动失效重读，
# 服务的内容与磁盘始终保持一致（行为等价于每次直读文件）。
# ---------------------------------------------------------------------
_STATIC_CACHE: Dict[str, Tuple[int, int, bytes]] = {}
_CACHE_LOCK = threading.Lock()


def _read_static(path: str) -> Optional[bytes]:
    """读文件并缓存；返回 None 表示文件不存在/不可读（调用方按 404 处理）。"""
    key = os.path.abspath(path)
    try:
        st = os.stat(path)
    except OSError:
        return None
    with _CACHE_LOCK:
        hit = _STATIC_CACHE.get(key)
        if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return hit[2]
    try:
        with open(path, "rb") as f:
            body = f.read()
    except OSError:
        return None
    with _CACHE_LOCK:
        _STATIC_CACHE[key] = (st.st_mtime_ns, st.st_size, body)
    return body


def _vision_ocr_root() -> Path:
    """Resolve local OCR assets from configuration or the shared runtime root."""
    configured = os.environ.get("VISION_OCR_HOME") or os.environ.get("ZH_VISION_ROOT")
    if configured:
        return Path(configured).expanduser()
    from core.image_detector import RUNTIME_ROOT
    return RUNTIME_ROOT / "vision-ocr"


# 内置模板与兜底路由表：数据层已拆至 core/templates（与 ai2kicad 共用），
# 此处再导出保持 server.BUILTIN_TEMPLATES / server.FALLBACK_TEMPLATES 对外接口不变
BUILTIN_TEMPLATES = templates.BUILTIN_TEMPLATES
FALLBACK_TEMPLATES = templates.FALLBACK_TEMPLATES

CATALOG = catalog.CATALOG
ERC_KB = catalog.build_erc_kb()

# 静态文件白名单：只暴露前端必需脚本与工程台样式。
ALLOWED_STATIC = {"symbols.js", "wire_geometry.js", "layout.js", "render.js",
                  "interact.js", "image_recognition.js", "upstream_vision.js", "tscircuit_editor.js", "app.js",
                  "workbench.js", "workbench.css",
                  # 第三方时序图渲染器（wavedrom 3.7.0，MIT）预构建自包含 bundle
                  "wavedrom.unpkg.min.js"}
MIME = {".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".wasm": "application/wasm", ".gz": "application/gzip",
        ".json": "application/json; charset=utf-8"}

# Handler 读超时：高于单次 chat() 最坏静默窗口（3 次尝试×25s+退避≈80s），
# 既不会误杀慢请求，又能回收客户端异常消失后挂着的 keep-alive 线程
HANDLER_TIMEOUT_S = 120

SOURCE_TEXT = {"local": "本地命中（0 次 LLM 调用）", "llm": "LLM 抽取", "none": "未识别"}


class RateLimiter:
    """每 IP 滑动窗口限流（线程安全）。

    只护住消耗 LLM 预算的端点（/api/generate、/api/clarify）：
    LSQC_HOST=0.0.0.0 局域网暴露时，防止他人无限刷接口烧光
    DEEPSEEK_API_KEY 余额。窗口内请求数 < limit 放行，否则拒绝；
    limit=0 关闭限流（全放行）。

    clock 可注入（测试用假时钟做确定性滑动窗口断言）。
    """

    # 内存闸门：IP 表超过该规模时顺带清理空队列，防恶意伪造源 IP 撑爆内存
    _PURGE_THRESHOLD = 4096

    def __init__(self, limit: int, window_s: float = 60.0, clock=time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        if self.limit <= 0:
            return True
        now = self._clock()
        with self._lock:
            if len(self._hits) > self._PURGE_THRESHOLD:
                # 稀有路径：全量清一次滑出窗口的空队列（O(IP 数)，摊销极低）
                self._hits = {k: v for k, v in self._hits.items() if v}
            dq = self._hits.get(ip)
            if dq is None:
                dq = self._hits[ip] = deque()
            while dq and now - dq[0] >= self.window_s:
                dq.popleft()
            if len(dq) >= self.limit:
                return False
            dq.append(now)
            return True


# 模块级单例：测试可整体替换（如 RateLimiter(0) 关闭 / 小限额验证拒绝路径）
rate_limiter = RateLimiter(config.RATE_LIMIT)
# /api/kicad 独立限流：kicad-cli 子进程导出比 LLM 调用更重，
# 独立配额防止单 IP 打满子进程池；0 = 关闭。测试可整体替换。
kicad_rate_limiter = RateLimiter(config.KICAD_RATE_LIMIT)


class _LLMConfigStore:
    """前端上报的 LLM 配置（api_key/base_url/model）进程内存表。

    安全约定：Key 只活在本进程内存里——不落盘、不进 URL（EventSource
    只能 GET，故用 cfg id 间接引用）、不打日志；TTL 过期即销毁。
    多用户（LSQC_HOST=0.0.0.0 机房）场景下各浏览器持各自 cfg id，
    互不覆盖。
    """

    TTL_S = 2 * 3600          # 配置存活期：一节课/一场演示足够
    MAX_ENTRIES = 256         # 超上限清最旧，防恶意注册撑内存

    def __init__(self):
        self._items: Dict[str, Tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _purge_locked(self, now: float) -> None:
        expired = [k for k, (ts, _) in self._items.items()
                   if now - ts > self.TTL_S]
        for k in expired:
            del self._items[k]
        if len(self._items) >= self.MAX_ENTRIES:
            for k, _ in sorted(self._items.items(),
                               key=lambda kv: kv[1][0])[:len(self._items) // 4]:
                del self._items[k]

    def put(self, cfg: dict) -> str:
        """存配置，返回随机 cfg id（不可枚举，token_urlsafe 16 字节）。"""
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            cfg_id = secrets.token_urlsafe(16)
            self._items[cfg_id] = (now, cfg)
            return cfg_id

    def get(self, cfg_id: str) -> Optional[dict]:
        """取配置；不存在/过期返回 None（调用方回退服务端默认配置）。"""
        if not cfg_id:
            return None
        with self._lock:
            item = self._items.get(cfg_id)
            if item is None:
                return None
            ts, cfg = item
            if time.monotonic() - ts > self.TTL_S:
                del self._items[cfg_id]
                return None
            return cfg


# 模块级单例：测试可整体替换/直接 inspect
llm_config_store = _LLMConfigStore()


def make_llm_client(cfg_id: str):
    """按 cfg id 构造 LLM client。

    返回 (client, label)：cfg 为空/非法/过期 → 回退服务端 env 默认配置
    （未配 key 则 client=None，走模板兜底路径）。label 供 SSE 进度显示
    当前所用模型，让"切换模型真的生效了"在界面上可见。"""
    if cfg_id:
        cfg = llm_config_store.get(cfg_id)
        if cfg is not None:
            client = llm.DeepSeekClient(api_key=cfg["api_key"],
                                        base_url=cfg["base_url"],
                                        model=cfg["model"])
            return client, cfg.get("label") or cfg["model"]
        log.warning("LLM 配置 %s 不存在或已过期，回退服务端默认", cfg_id[:8])
    if config.api_key_configured():
        return llm.DeepSeekClient(), f"{llm.DeepSeekClient.DEFAULT_MODEL}（服务端默认）"
    return None, None

log = logging.getLogger(__name__)


def _erc_summary(rep) -> dict:
    """ERC 终检结果的紧凑摘要（SSE 'erc' 事件负载）。"""
    counts = rep.counts()
    return {"passed": rep.passed, **counts}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive：静态资源复用连接
    timeout = HANDLER_TIMEOUT_S     # 读超时：死连接不再长期占用线程

    def log_message(self, fmt, *args):   # 精简日志：只记关键行
        # args[0] 并不总是字符串：send_error 路径下会传 HTTPStatus 枚举
        # （如非法方法 HEAD/OPTIONS 触发），把它当字符串做 in 判断会抛
        # TypeError: argument of type 'HTTPStatus' is not iterable，
        # 在日志里刷出无意义的异常堆栈。这里统一转成字符串再判断。
        first = str(args[0]) if args else ""
        if "/api/generate" in first:
            print(f"[{threading.current_thread().name}] {fmt % args}")

    # ---------- 基础 ----------
    def _send(self, code: int, body: bytes, ctype: str):
        try:
            rid = getattr(self, "_rid", None) or new_request_id()
            self._rid = rid
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-Id", rid)
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 客户端提前断开（刷新页面/关标签页）：静默结束即可，
            # 不向上抛，避免 ThreadingHTTPServer 打出无意义调用栈
            self.close_connection = True


    def _security_headers(self):
        """生产级安全响应头（零依赖，标准库）。"""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        # CSP：允许同源脚本/样式/连接；图片允许 data:（SVG 内联）
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; worker-src 'self' blob:"
        )

    def _send_json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/kicad":
            return self._handle_kicad_export()
        if u.path == "/api/clarify":
            return self._handle_clarify()
        if u.path == "/api/llm_config":
            return self._handle_llm_config()
        if u.path == "/api/erc":
            return self._handle_erc()
        if u.path == "/api/circuit/migrate":
            return self._handle_circuit_migrate()
        if u.path == "/api/validate":
            return self._handle_validate()
        if u.path == "/api/import/external":
            return self._handle_external_import()
        if u.path == "/api/design/tscircuit":
            return self._handle_tscircuit_design()
        if u.path == "/api/image/analyze":
            return self._handle_image_analyze()
        if u.path == "/api/edit":
            return self._handle_edit()
        if u.path == "/api/simulate":
            return self._handle_simulate()
        if u.path == "/api/bom":
            return self._handle_bom()
        if u.path == "/api/spice":
            return self._handle_spice()
        if u.path == "/api/spice_analyze":
            return self._handle_spice_analyze()
        if u.path == "/api/spice_transient":
            return self._handle_spice_transient()
        if u.path == "/api/pcb":
            return self._handle_pcb()
        if u.path == "/api/pcb/candidate":
            return self._handle_pcb_candidate()
        if u.path == "/api/pcb/manufacture":
            return self._handle_pcb_manufacture()
        if u.path == "/api/routing_job":
            return self._handle_routing_job()
        if u.path == "/api/schemes":
            return self._handle_schemes()
        if u.path == "/api/history":
            return self._handle_history()
        if u.path == "/api/trace":
            return self._handle_trace_query()
        if u.path == "/api/evidence/summary":
            return self._handle_evidence_summary()
        self._send_json(404, {"error": "not found"})

    def _read_json_body(self, max_bytes=1 << 20):
        """读取并解析 JSON 请求体；非法抛 ValueError（调用方转 400）。

        统一的请求体入口：长度上限 1MB（防超大 body 打爆内存/磁盘），
        UTF-8 解码失败与 JSON 解析失败一并抛 ValueError，调用方只需捕获
        ValueError 即可覆盖全部非法情形（json.JSONDecodeError 是
        ValueError 的子类，无需单独列举）。
        /api/clarify 与 /api/kicad 均直接复用本方法，避免各写一份
        Content-Length 读取+校验（此前三处重复，长度上限或解码方式一旦
        改动就有漏改风险）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Content-Length 必须是整数") from exc
        if length <= 0 or length > max_bytes:
            raise ValueError("请求体为空或超过请求大小上限")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _handle_image_analyze(self):
        try:
            payload = self._read_json_body(max_bytes=10 << 20)
            if not isinstance(payload, dict):
                raise ValueError('请求体必须是 JSON 对象')
            if payload.get('backend') != 'circuitvision':
                raise ValueError('backend 必须显式指定为 circuitvision')
            image = payload.get('image')
            source_name = payload.get('source_name', '')
            if not isinstance(image, str) or not isinstance(source_name, str):
                raise ValueError('image 与 source_name 必须是字符串')
        except ValueError as exc:
            return self._send_json(400, {'error': str(exc), 'status': 'incomplete',
                                         'accepted': False, 'electrical_validated': False})
        try:
            from core import upstream_vision
            result = upstream_vision.analyze_image(image, source_name=source_name,
                                                  is_cancelled=self._client_disconnected)
        except ImportError:
            return self._send_json(503, {'error': '本地 CircuitVision 运行桥接缺失',
                                         'status': 'incomplete', 'accepted': False,
                                         'electrical_validated': False})
        except Exception as exc:
            code = getattr(exc, 'code', '')
            if code == 'cancelled' or self._client_disconnected():
                return
            status = {'invalid_request': 400, 'runtime_unavailable': 503,
                      'busy': 409, 'timeout': 504, 'inference_failed': 502}.get(code, 502)
            return self._send_json(status, {'error': str(exc), 'code': code or 'inference_failed',
                                            'status': 'incomplete', 'accepted': False,
                                            'electrical_validated': False})
        if not isinstance(result, dict):
            return self._send_json(502, {'error': '本地识别结果格式无效',
                                         'status': 'incomplete', 'accepted': False,
                                         'electrical_validated': False})
        response = dict(result)
        response.update(status='needs_manual_correction', circuit=None,
                        accepted=False, electrical_validated=False)
        if not self._client_disconnected():
            return self._send_json(200, response)

    def _client_disconnected(self):
        """Check this HTTP connection without consuming a subsequent request."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b''
        except (OSError, ValueError):
            return True

    def _handle_tscircuit_design(self):
        try:
            payload = self._read_json_body()
            if not isinstance(payload, dict):
                raise ValueError('请求体必须是 JSON 对象')
            from core.tscircuit_engine import generate_tscircuit, TscircuitError
            result = generate_tscircuit(payload.get('code'), name=payload.get('name', ''),
                                        is_cancelled=self._client_disconnected)
        except ValueError as exc:
            return self._send_json(400, {'error': str(exc), 'status': 'rejected', 'accepted': False})
        except ImportError:
            return self._send_json(503, {'error': '本机代码建电路运行桥接缺失', 'status': 'incomplete', 'accepted': False})
        except Exception as exc:
            code = getattr(exc, 'code', '')
            if code == 'cancelled' or self._client_disconnected():
                return
            status = {'runtime_unavailable': 503, 'timeout': 504, 'busy': 409}.get(code, 502)
            return self._send_json(status, {'error': str(exc), 'code': code or 'generation_failed',
                                          'status': 'incomplete', 'accepted': False})
        if not self._client_disconnected():
            return self._send_json(200 if result['status'] == 'needs_review' else 422, result)

    def _handle_external_import(self):
        try:
            payload = self._read_json_body()
            if not isinstance(payload, dict):
                raise ValueError('请求体必须是 JSON 对象')
            if not {'source', 'content'} <= payload.keys():
                raise ValueError('缺少 source 或 content')
            result = import_external_result(payload['source'], payload['content'],
                                            name=payload.get('name', ''))
        except ValueError as exc:
            return self._send_json(400, {'error': str(exc)})
        return self._send_json(200 if result['status'] == 'needs_review' else 422, result)

    def _handle_llm_config(self):
        """注册前端选择的 LLM 配置 → 返回 cfg id（后续生成/判歧引用）。

        请求体:
          provider : 预设 id（见 llm_providers.PROVIDERS）或 "custom"
          model    : 模型名（custom 必填；预设可省略→该提供商首个模型）
          api_key  : 用户自己的 Key（只存内存，TTL 过期即销毁）
          base_url : 仅 provider=custom 时必填（https://，或本机回环 http://）
          label    : 可选显示名（界面进度条展示，≤40 字符）
        响应: {"cfg": "<id>", "label": "..."}；Key 不回显、不落盘、不进日志。
        """
        if not rate_limiter.allow(self.client_address[0]):
            log.warning("限流拒绝 /api/llm_config：ip=%s", self.client_address[0])
            return self._send_json(429, {
                "error": f"请求过于频繁（每 IP 每分钟限 {rate_limiter.limit} 次），"
                         f"请稍后再试"})
        try:
            payload = self._read_json_body()
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            provider_id = str(payload.get("provider") or "").strip()
            provider = llm_providers.get_provider(provider_id)
            if provider is None:
                raise ValueError(f"未知 provider: {provider_id!r}")
            model = str(payload.get("model") or "").strip()
            if not model:
                if provider["models"]:
                    model = provider["models"][0]["id"]
                else:
                    raise ValueError("自定义端点必须提供 model")
            model = llm_providers.validate_model_name(model)
            if provider["id"] == "custom":
                base_url = llm_providers.validate_custom_base_url(
                    str(payload.get("base_url") or ""))
            else:
                base_url = provider["base_url"]
            api_key = llm_providers.validate_api_key(
                str(payload.get("api_key") or ""))
            label = str(payload.get("label") or "").strip()[:40] or model
        except (ValueError, json.JSONDecodeError) as e:
            return self._send_json(400, {"error": f"配置非法: {e}"})
        cfg_id = llm_config_store.put({
            "provider": provider["id"], "base_url": base_url,
            "model": model, "api_key": api_key, "label": label,
        })
        log.info("LLM 配置已注册：provider=%s model=%s（key 不落盘不进日志）",
                 provider["id"], model)
        self._send_json(200, {"cfg": cfg_id, "label": label, "model": model})

    def _handle_clarify(self):
        """需求判歧：POST {"query": "..."} → ClarifyResult JSON。

        判歧是“增强”而非“门禁”：任何内部失败都返回 clear，
        绝不阻塞后续 /api/generate 主流程。"""
        if not rate_limiter.allow(self.client_address[0]):
            log.warning("限流拒绝 /api/clarify：ip=%s", self.client_address[0])
            return self._send_json(429, {
                "error": f"请求过于频繁（每 IP 每分钟限 {rate_limiter.limit} 次），"
                         f"请稍后再试"})
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send_json(400, {"error": f"请求体非法: {e}"})
        query = str(payload.get("query") or "").strip()
        if not query:
            return self._send_json(400, {"error": "缺少 query（需求文本）"})
        if len(query) > config.MAX_QUERY_LEN:
            return self._send_json(
                400, {"error": f"需求文本超过上限 {config.MAX_QUERY_LEN} 字符"})
        try:
            client, _label = make_llm_client(str(payload.get("cfg") or ""))
            # 判歧单独计费：独立小预算（1 次），不占用生成流水线的预算
            budget = llm.CallBudget(max_calls=1)
            result = clarify.analyze(query, client, budget)
        except Exception as e:  # noqa: BLE001 —— 判歧失败不阻塞生成
            log.warning("判歧接口内部失败，按 clear 放行：%s: %s",
                        type(e).__name__, e)
            result = {"status": "clear", "questions": [],
                      "assumptions": [], "source": "error_fallback"}
        self._send_json(200, result)

    def _handle_circuit_migrate(self):
        """将已有 CircuitJSON v1 无损迁移至 v2 工程元数据合同。"""
        from core.circuit_schema import migrate_v1_to_v2

        try:
            payload = self._read_json_body()
            if not isinstance(payload, dict) or "circuit" not in payload:
                raise ValueError("请求体必须包含 circuit 对象")
            migrated = migrate_v1_to_v2(payload["circuit"])
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        return self._send_json(200, {"circuit": migrated})

    def _handle_validate(self):
        """统一候选门禁；默认需要官方 KiCad ERC 和网表复验。"""
        from core.validation_gate import SOURCES, validate_candidate

        if not kicad_rate_limiter.allow(self.client_address[0]):
            return self._send_json(429, {"error": "校验请求过于频繁，请稍后再试"})
        try:
            payload = self._read_json_body()
            if not isinstance(payload, dict) or not isinstance(payload.get("circuit"), dict):
                raise ValueError("请求体必须包含 circuit 对象")
            source = payload.get("source", "manual")
            if not isinstance(source, str) or source not in SOURCES:
                raise ValueError("source 必须是 template、ai、import、image 或 manual")
            name = payload.get("name", "candidate")
            if not isinstance(name, str) or not name or len(name) > 64 or not all(
                char.isascii() and (char.isalnum() or char in "_-") for char in name
            ):
                raise ValueError("name 只能包含 1–64 个 ASCII 字母、数字、下划线或连字符")
            official = payload.get("require_official", True)
            runtime = payload.get("require_spice_runtime", official)
            if not isinstance(official, bool) or not isinstance(runtime, bool):
                raise ValueError("require_official 和 require_spice_runtime 必须是布尔值")
            result = validate_candidate(payload["circuit"], source=source, name=name,
                                        require_official=official,
                                        require_spice_runtime=runtime)
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        except Exception:
            log.exception("候选门禁运行失败")
            return self._send_json(500, {"error": "候选门禁运行失败，请查看服务端日志"})
        return self._send_json(200, result)

    def _handle_pcb_candidate(self):
        """用已核实的本地封装生成未布线的双层 PCB 候选及焊盘审计。"""
        from core.pcb_real import PCBContractError, build_pcb_candidate

        if not kicad_rate_limiter.allow(self.client_address[0]):
            return self._send_json(429, {"error": "PCB 候选请求过于频繁，请稍后再试"})
        try:
            payload = self._read_json_body()
            if not isinstance(payload, dict) or not isinstance(payload.get("circuit"), dict):
                raise ValueError("请求体必须包含 circuit 对象")
            name = payload.get("name", "circuit")
            if not isinstance(name, str) or not name or len(name) > 64 or not all(
                char.isascii() and (char.isalnum() or char in "_-") for char in name
            ):
                raise ValueError("name 只能包含 1–64 个 ASCII 字母、数字、下划线或连字符")
            from kicad.cli_runner import kicad_cli_version
            version = kicad_cli_version() or ""
            target = payload.get("target_version", 20260206 if version.startswith("10.") else 20240108)
            if isinstance(target, bool) or target not in (20240108, 20260206):
                raise ValueError("target_version 只支持 KiCad 8 的 20240108 或 KiCad 10 的 20260206")
            result = build_pcb_candidate(payload["circuit"], name=name, target_version=target)
        except PCBContractError as exc:
            return self._send_json(422, {"error": str(exc), "code": exc.code,
                                         "status": "blocked"})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        except Exception:
            log.exception("真实封装 PCB 候选生成失败")
            return self._send_json(500, {"error": "PCB 候选生成失败，请查看服务端日志"})
        return self._send_json(200, result)

    def _handle_pcb_manufacture(self):
        """实际布线板 → 源原理图门禁 → 焊盘审计 → 官方 DRC → 待复核 CAM 包。"""
        if not kicad_rate_limiter.allow(self.client_address[0]):
            return self._send_json(429, {"error": "制造校验请求过于频繁，请稍后再试"})
        try:
            payload = self._read_json_body()
            if not isinstance(payload, dict) or not isinstance(payload.get("circuit"), dict):
                raise ValueError("请求体必须包含 circuit 对象")
            board_text = payload.get("board_text")
            if not isinstance(board_text, str) or not board_text.lstrip().startswith("(kicad_pcb"):
                raise ValueError("board_text 必须是实际已布线的 KiCad PCB 文本")
            from core.validation_gate import validate_candidate
            from core.pcb_manufacturing import run_manufacturing_pipeline
            rid = new_request_id()
            self._rid = rid
            directory = Path(ROOT) / "data" / "pcb_runs" / rid
            directory.mkdir(parents=True, exist_ok=True)
            validation = validate_candidate(payload["circuit"], source="manual", name="source",
                                            work_dir=directory / "source")
            (directory / "source_validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
            if not validation["accepted"]:
                return self._send_json(422, {"error": "源原理图未通过完整门禁，未执行制造导出",
                                             "status": "source_rejected", "validation": validation})
            manifest = run_manufacturing_pipeline(payload["circuit"], board_text, directory)
            if manifest["status"] != "fabrication_candidate_for_review":
                return self._send_json(422, {"error": "PCB 尚未通过制造候选门禁", "manifest": manifest})
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                with zipfile.ZipFile(manifest["bundle"]["path"]) as candidate:
                    for member in candidate.infolist():
                        archive.writestr(member.filename, candidate.read(member.filename))
                archive.writestr("source_validation.json", json.dumps(validation, ensure_ascii=False, indent=2))
                archive.writestr("manufacturing_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            blob = buffer.getvalue()
        except (ValueError, TypeError) as exc:
            return self._send_json(400, {"error": str(exc)})
        except Exception:
            log.exception("制造候选导出失败")
            return self._send_json(500, {"error": "制造候选导出失败，请查看服务端证据"})
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="fabrication_candidate_for_review.zip"')
        self.send_header("Content-Length", str(len(blob)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(blob)

    def _handle_erc(self):
        """编辑后重算 ERC：POST {"circuit": {CircuitJSON}} → ERCReport JSON。

        背景：ERC 原先只在 /api/generate 流水线末尾算一次，用户在画布上
        拖动/改值/删除/新增元件后就再不重算 —— 「所见即所得的设计规则
        检查」这个核心卖点存在断点。本端点让前端在每次编辑提交后防抖
        回调，把最新电路送回来重算，状态栏与画布标注实时刷新。

        纯计算、零 token 成本、零磁盘 IO，故不挂 rate_limiter（限流是为
        保护 API Key 与子进程开销而设，本端点两者都不涉及）。

        响应即 ERCReport.to_dict()：含 issues[].{severity,rule,message,pins}
        —— 渲染层直接用它把错误标注画到对应引脚上，无需前端再解析。
        """
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send_json(400, {"error": f"请求体非法: {e}"})
        if not isinstance(payload, dict):
            return self._send_json(400, {"error": "请求体必须是 JSON 对象"})
        circuit = payload.get("circuit")
        # 与 /api/kicad 同口径的防御式校验：空电路不是错误（用户把元件全
        # 删光时前端仍会追问一次），直接返回一份“无问题”的空报告，
        # 避免前端把 400 当成异常在控制台刷屏
        if not isinstance(circuit, dict):
            return self._send_json(400, {"error": "缺少电路数据（circuit）"})
        parts = circuit.get("parts")
        if not isinstance(parts, list):
            return self._send_json(400, {"error": "circuit.parts 必须是数组"})
        if parts:
            for p in parts:
                if not isinstance(p, dict) or not p.get("id") or not p.get("type"):
                    return self._send_json(400, {
                        "error": f"元件数据非法（缺 id/type）: {str(p)[:80]}"})
        if not isinstance(circuit.get("connections") or [], list):
            return self._send_json(400, {"error": "connections 必须是数组"})
        try:
            rep = erc.run_erc(circuit, ERC_KB)
        except Exception as e:   # noqa: BLE001 —— 不让 Handler 线程裸崩
            log.exception("编辑后 ERC 重算失败（parts=%d）", len(parts))
            return self._send_json(500, {"error": f"ERC 计算失败: {e}"})
        payload = rep.to_dict()
        try:
            payload["blocks"] = circuit_blocks.analyze_blocks(circuit) if circuit_blocks else []
        except Exception:
            payload["blocks"] = []
        self._send_json(200, payload)

    def _handle_edit(self):
        """AI 增量改图：失败只回错误，绝不发送任何替代 circuit。"""
        started = time.monotonic()
        rid = getattr(self, "_rid", None) or new_request_id()
        self._rid = rid
        set_request_id(rid)
        if not rate_limiter.allow(self.client_address[0]):
            log.warning("限流拒绝 /api/edit：ip=%s", self.client_address[0])
            log_request(log, rid, "POST", "/api/edit", 429, time.monotonic() - started)
            return self._send_json(429, {
                "error": f"请求过于频繁（每 IP 每分钟限 {rate_limiter.limit} 次），请稍后再试"})
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            log_request(log, rid, "POST", "/api/edit", 400, time.monotonic() - started)
            return self._send_json(400, {"error": f"请求体非法: {exc}"})
        if not isinstance(payload, dict):
            log_request(log, rid, "POST", "/api/edit", 400, time.monotonic() - started)
            return self._send_json(400, {"error": "请求体必须是 JSON 对象"})

        circuit = payload.get("circuit")
        instruction = payload.get("instruction")
        if not isinstance(circuit, dict):
            log_request(log, rid, "POST", "/api/edit", 400, time.monotonic() - started)
            return self._send_json(400, {"error": "缺少电路数据（circuit）"})
        if not isinstance(instruction, str) or not instruction.strip():
            log_request(log, rid, "POST", "/api/edit", 400, time.monotonic() - started)
            return self._send_json(400, {"error": "缺少修改指令（instruction）"})
        if len(instruction) > config.MAX_QUERY_LEN:
            log_request(log, rid, "POST", "/api/edit", 400, time.monotonic() - started)
            return self._send_json(400, {
                "error": f"修改指令超过上限 {config.MAX_QUERY_LEN} 字符，请精简后重试"})
        blockers = llm.detect_requirement_blockers(instruction)
        if blockers:
            return self._send_json(422, {"error": "修改指令存在明确电气冲突", "blockers": blockers})

        client, model_label = make_llm_client(str(payload.get("cfg") or "").strip())
        if client is None:
            log_request(log, rid, "POST", "/api/edit", 503, time.monotonic() - started)
            return self._send_json(503, {"error": "未配置可用模型，无法修改当前电路"})
        try:
            with trace_span("edit") as span:
                span.event("request", instruction_len=len(instruction),
                           parts=len(circuit.get("parts") or []), model=model_label or "")
                result = circuit_edit.edit_circuit(circuit, instruction, client, ERC_KB)
                result["circuit"]["provenance"] = {"origin": "prompt", "source_id": rid}
                if isinstance(getattr(client, "model", None), str):
                    result["circuit"]["provenance"]["model_id"] = client.model
                span.event("candidate", parts=len(result["circuit"].get("parts") or []),
                           changes=result["changes"])
                from core.validation_gate import validate_candidate
                result["validation"] = validate_candidate(
                    result["circuit"], source="ai", name="edit_" + rid,
                    require_official=True)
        except circuit_edit.CircuitEditStructureError as exc:
            log_request(log, rid, "POST", "/api/edit", 422, time.monotonic() - started)
            return self._send_json(422, {"error": str(exc)})
        except circuit_edit.CircuitEditQualityError as exc:
            log_request(log, rid, "POST", "/api/edit", 422, time.monotonic() - started)
            return self._send_json(422, {"error": str(exc)})
        except circuit_edit.CircuitEditModelUnavailable as exc:
            log_request(log, rid, "POST", "/api/edit", 503, time.monotonic() - started)
            return self._send_json(503, {"error": str(exc)})
        except circuit_edit.CircuitEditError as exc:
            log_request(log, rid, "POST", "/api/edit", 502, time.monotonic() - started)
            return self._send_json(502, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 -- Handler 不能因模型/解析异常裸崩
            log.exception("增量改图内部失败（request_id=%s）", rid)
            log_request(log, rid, "POST", "/api/edit", 500, time.monotonic() - started)
            return self._send_json(500, {"error": "增量改图服务内部失败"})

        result.update({"ok": True, "request_id": rid})
        log_request(log, rid, "POST", "/api/edit", 200, time.monotonic() - started,
                    parts=len(result["circuit"].get("parts") or []))
        self._send_json(200, result)

    def _handle_simulate(self):
        """准仿真：POST {"circuit": CircuitJSON} → SimulationReport JSON。"""
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send_json(400, {"error": f"请求体非法: {e}"})
        if not isinstance(payload, dict):
            return self._send_json(400, {"error": "请求体必须是 JSON 对象"})
        circuit = payload.get("circuit")
        if not isinstance(circuit, dict):
            return self._send_json(400, {"error": "缺少电路数据（circuit）"})
        try:
            from core.simulate import run_simulation
            rep = run_simulation(circuit)
            try:
                block_list = circuit_blocks.analyze_blocks(circuit) if circuit_blocks else []
            except Exception:
                block_list = []
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"准仿真失败: {e}"})
        out = rep.to_dict()
        try:
            out["blocks"] = block_list
            vcc = None
            for led in out.get("led_currents") or []:
                if led.get("vcc") is not None:
                    vcc = led.get("vcc")
                    break
            out["vcc"] = vcc
        except Exception:
            pass
        self._send_json(200, out)

    def _handle_bom(self):
        """BOM 导出：POST {"circuit": CircuitJSON, "format": "json"|"csv"}。"""
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send_json(400, {"error": f"请求体非法: {e}"})
        if not isinstance(payload, dict):
            return self._send_json(400, {"error": "请求体必须是 JSON 对象"})
        circuit = payload.get("circuit")
        if not isinstance(circuit, dict):
            return self._send_json(400, {"error": "缺少电路数据（circuit）"})
        fmt = str(payload.get("format") or "json").lower()
        try:
            from core import bom as bom_mod
            if fmt == "csv":
                text = bom_mod.to_csv(circuit)
                body = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="bom.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            self._send_json(200, bom_mod.to_json(circuit))
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"BOM 生成失败: {e}"})

    def _handle_spice(self):
        """SPICE 导出：POST {"circuit":..., "name":..., "format":"cir"|"subckt"}。"""
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send_json(400, {"error": f"请求体非法: {e}"})
        if not isinstance(payload, dict):
            return self._send_json(400, {"error": "请求体必须是 JSON 对象"})
        circuit = payload.get("circuit")
        if not isinstance(circuit, dict):
            return self._send_json(400, {"error": "缺少电路数据（circuit）"})
        name = str(payload.get("name") or "Circuit")[:40]
        fmt = str(payload.get("format") or "cir").lower()
        try:
            from core import spice as spice_mod
            if fmt == "subckt":
                text = spice_mod.to_subckt(circuit, name)
            else:
                text = spice_mod.to_cir(circuit, name)
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{name}.cir"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"SPICE 生成失败: {e}"})

    def _handle_spice_analyze(self):
        """SPICE 仿真分析：POST {"circuit":..., "kind":"op"|"ac"|"tran"|"dc"}。

        优先本机 ngspice；不可用时回退解析解（LED 电流 / RC 幅频 / 分压）。
        返回 JSON：deck + engine + analytic [+ ac_response + node_voltages]。
        """
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send_json(400, {"error": f"请求体非法: {e}"})
        if not isinstance(payload, dict):
            return self._send_json(400, {"error": "请求体必须是 JSON 对象"})
        circuit = payload.get("circuit")
        if not isinstance(circuit, dict):
            return self._send_json(400, {"error": "缺少电路数据（circuit）"})
        kind = str(payload.get("kind") or "op").lower()
        if kind not in ("op", "ac", "tran", "dc"):
            return self._send_json(400, {"error": f"kind 必须是 op/ac/tran/dc，收到 {kind!r}"})
        try:
            from core import spice_enhanced as se
            result = se.simulate(circuit, kind=kind)
            # 网表可能很长，响应里保留（前端可下载），但截断 ngspice 原始输出
            if result.get("ngspice_output") and len(result["ngspice_output"]) > 8000:
                result["ngspice_output"] = result["ngspice_output"][:8000] + "\n...[truncated]"
            self._send_json(200, result)
        except Exception as e:  # noqa: BLE001
            log.exception("SPICE 分析失败 kind=%s", kind)
            return self._send_json(500, {"error": f"SPICE 分析失败: {e}"})

    def _handle_spice_transient(self):
        """瞬态时间序列：POST {"circuit":..., "step":..., "stop":...}。

        仅供教学时序图使用：返回真实 .tran 输出解析出的节点电压序列。
        ngspice 不可用、无电压节点或解析失败时返回 ran=false 与 reason，
        不合成、不插值、不用解析解替代波形。
        """
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send_json(400, {"error": f"请求体非法: {e}"})
        if not isinstance(payload, dict):
            return self._send_json(400, {"error": "请求体必须是 JSON 对象"})
        circuit = payload.get("circuit")
        if not isinstance(circuit, dict):
            return self._send_json(400, {"error": "缺少电路数据（circuit）"})
        try:
            step = float(payload.get("step") or 1e-4)
            stop = float(payload.get("stop") or 0.05)
        except (TypeError, ValueError):
            return self._send_json(400, {"error": "step/stop 必须是数值"})
        if not (0 < step < stop <= 1.0):
            return self._send_json(400, {"error": "step/stop 超出允许范围（0<step<stop≤1.0）"})
        try:
            from core import spice_enhanced as se
            self._send_json(200, se.transient_series(circuit, step=step, stop=stop))
        except Exception as e:  # noqa: BLE001
            log.exception("瞬态序列失败")
            return self._send_json(500, {"error": f"瞬态序列失败: {e}"})

    def _handle_pcb(self):
        """PCB 预布局：POST {"circuit":..., "name":"..."} → .kicad_pcb 下载。

        教学/原型向：板框 + 网格放置的封装占位 + 位号丝印。
        优先返回 summary JSON（?summary=1），否则返回 .kicad_pcb 文件。
        """
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send_json(400, {"error": f"请求体非法: {e}"})
        if not isinstance(payload, dict):
            return self._send_json(400, {"error": "请求体必须是 JSON 对象"})
        circuit = payload.get("circuit")
        if not isinstance(circuit, dict):
            return self._send_json(400, {"error": "缺少电路数据（circuit）"})
        name = str(payload.get("name") or "circuit").strip()[:60] or "circuit"
        want_summary = bool(payload.get("summary"))
        try:
            from core import pcb_layout
            if want_summary:
                return self._send_json(200, pcb_layout.summary(circuit))
            text = pcb_layout.to_kicad_pcb(circuit, name)
            body = text.encode("utf-8")
            fname = quote(f"{name}.kicad_pcb")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-kicad-pcb")
            self.send_header("Content-Disposition", f'attachment; filename*=UTF-8\'\'{fname}')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:  # noqa: BLE001
            log.exception("PCB 预布局失败")
            return self._send_json(500, {"error": f"PCB 预布局失败: {e}"})

    def _handle_routing_job(self):
        """校验隔离式路由任务合同；核心进程不自动启动 WSL/PCBWorld。

        POST {"job": routing_job_v1}。合法任务返回规范化合同与明确的 fallback
        结果，供独立服务消费；这避免把外部 GPL/PyTorch 依赖混入 A 主线，也
        防止“接受了任务”被误读成“已经完成 PPO 布线”。
        """
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send_json(400, {"error": f"请求体非法: {e}"})
        if not isinstance(payload, dict) or not isinstance(payload.get("job"), dict):
            return self._send_json(400, {"error": "缺少路由任务对象（job）"})
        from core.routing_job import RoutingJobError, fallback_result, validate_job
        try:
            job = validate_job(payload["job"])
        except RoutingJobError as e:
            return self._send_json(400, {
                "error": "路由任务合同校验失败",
                "errors": e.errors,
            })
        result = fallback_result(
            job,
            "核心进程仅校验并转交任务；PCBWorld/PPO 必须由隔离式外部服务执行",
        )
        return self._send_json(200, {
            "accepted": True,
            "job": job,
            "result": result,
        })

    def _handle_trace_query(self):
        """链路日志查询：GET /api/trace?rid=xxx 或 ?latest=5。

        只读内存环形缓冲，不含密钥/完整 prompt，适合课堂现场排查。
        """
        u = urlparse(self.path)
        q = parse_qs(u.query or "")
        rid = (q.get("rid") or [""])[0]
        if rid:
            return self._send_json(200, {"request_id": rid,
                                         "events": TRACE_COLLECTOR.get(rid)})
        n = int((q.get("n") or ["5"])[0])
        return self._send_json(200, {"latest": TRACE_COLLECTOR.latest(max(1, min(20, n)))})

    def _handle_schemes(self):
        """多方案草案：POST {"query":"..."} → 2 个拓扑候选。"""
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send_json(400, {"error": f"请求体非法: {e}"})
        q = (payload or {}).get("query") or ""
        try:
            from core.schemes import pick_schemes
            schemes = pick_schemes(q)
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"方案生成失败: {e}"})
        self._send_json(200, {"schemes": schemes})

    def _handle_history(self):
        """工程历史：GET 列表；POST 保存；DELETE 删除。"""
        u = urlparse(self.path)
        if self.command == "GET":
            try:
                from core import history as hist
                q = urlparse(self.path).query
                cid = ""
                if "id=" in q:
                    cid = q.split("id=", 1)[-1].split("&", 1)[0]
                if cid:
                    rec = hist.load(cid)
                    if rec is None:
                        return self._send_json(404, {"error": "历史记录不存在"})
                    return self._send_json(200, rec)
                return self._send_json(200, {"items": hist.list_all()})
            except Exception as e:  # noqa: BLE001
                return self._send_json(500, {"error": f"历史列表失败: {e}"})
        if self.command == "POST":
            try:
                payload = self._read_json_body()
            except (ValueError, json.JSONDecodeError) as e:
                return self._send_json(400, {"error": f"请求体非法: {e}"})
            circuit = (payload or {}).get("circuit")
            if not isinstance(circuit, dict):
                return self._send_json(400, {"error": "缺少电路数据（circuit）"})
            try:
                from core import history as hist
                rec = hist.save(circuit,
                                title=str((payload or {}).get("title") or "未命名电路"),
                                query=str((payload or {}).get("query") or ""))
                return self._send_json(200, rec)
            except Exception as e:  # noqa: BLE001
                return self._send_json(500, {"error": f"保存失败: {e}"})
        if self.command == "DELETE":
            cid = u.query.split("id=", 1)[-1].split("&", 1)[0] if "id=" in u.query else ""
            try:
                from core import history as hist
                ok = hist.delete(cid)
                return self._send_json(200 if ok else 404, {"ok": ok})
            except Exception as e:  # noqa: BLE001
                return self._send_json(500, {"error": f"删除失败: {e}"})
        return self._send_json(405, {"error": "方法不允许"})

    def _handle_kicad_export(self):
        """CircuitJSON -> KiCad 工程压缩包下载。

        请求体: {"name": "<名称>", "circuit": {CircuitJSON}}
        响应:   application/zip —— .kicad_sch + .kicad_pro + 共享符号库 +
                sym-lib-table，解压后双击 .kicad_pro 即可在 KiCad 中打开
                并完全编辑（KiCad 9.0.8 真机验证）。"""
        if not kicad_rate_limiter.allow(self.client_address[0]):
            # 导出端点独立限流——子进程导出的 CPU/磁盘开销大于
            # 一次 LLM 调用，与 LLM 端点分开计配额
            log.warning("限流拒绝 /api/kicad：ip=%s", self.client_address[0])
            return self._send_json(429, {
                "error": f"导出请求过于频繁（每 IP 每分钟限 {kicad_rate_limiter.limit} 次），"
                         f"请稍后再试"})
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send_json(400, {"error": f"请求体非法: {e}"})
        circuit = payload.get("circuit")
        name = str(payload.get("name") or "circuit").strip()[:60] or "circuit"
        if (not isinstance(circuit, dict)
                or not isinstance(circuit.get("parts"), list)
                or not circuit.get("parts")):
            return self._send_json(400, {"error": "缺少电路数据（circuit.parts）"})
        # 防御性校验：parts 元素必须是带 id/type 的对象，否则导出层会在深处
        # 抛难以定位的 KeyError/TypeError，这里提前转成可读的 400
        for p in circuit["parts"]:
            if not isinstance(p, dict) or not p.get("id") or not p.get("type"):
                return self._send_json(400, {
                    "error": f"元件数据非法（缺 id/type）: {str(p)[:80]}"})
        if not isinstance(circuit.get("connections") or [], list):
            return self._send_json(400, {"error": "connections 必须是数组"})
        try:
            rep = erc.run_erc(circuit, ERC_KB)
            buf = io.BytesIO()
            with tempfile.TemporaryDirectory() as td:
                export.export_to_dir(circuit, name, td, kb=ERC_KB, erc_report=rep)
                # GitHub/KiCad 生态惯例：zip 内是完整工程目录，而不是散落文件。
                # 用户解压后进入 stem/ 打开 .kicad_pro（或双击 OPEN_IN_KICAD.bat）。
                stem = export.safe_project_stem(name)
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for root_dir, _dirs, files in os.walk(td):
                        for fn in sorted(files):
                            fp = os.path.join(root_dir, fn)
                            rel = os.path.relpath(fp, td).replace("\\", "/")
                            zf.write(fp, f"{stem}/{rel}")
        except Exception as e:
            # 导出链路任何异常都不应让 Handler 线程崩溃打出裸调用栈：
            # 记录日志并返回结构化 500，前端可正常提示
            log.exception("KiCad 导出失败（name=%s, parts=%d）",
                          name, len(circuit["parts"]))
            return self._send_json(500, {"error": f"KiCad 导出失败: {e}"})
        body = buf.getvalue()
        fname = quote(f"zhihui-kicad-{name}.zip")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{fname}")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


    def _handle_evidence_summary(self):
        """只读 evidence 摘要：供前端「证据」面板展示，不读取 .env/密钥。"""
        import csv as _csv
        ev_dir = Path(__file__).resolve().parent / "evidence"
        out = {"note": "以包内 evidence 文件为准；仅摘要，不含密钥。", "summary": {}}
        try:
            current_validation = ev_dir / "upgrade" / "validation_current.json"
            validation_path = current_validation if current_validation.is_file() else ev_dir / "validation_results.json"
            vr = json.loads(validation_path.read_text(encoding="utf-8"))
            sm = vr.get("summary") or {}
            out["summary"]["validation"] = {
                "total": sm.get("total"), "pass": sm.get("pass"),
                "fail": sm.get("fail"), "skip": sm.get("skip"),
                "elapsed_s": sm.get("elapsed_s"), "source": validation_path.relative_to(ev_dir).as_posix(),
            }
        except Exception:
            out["summary"]["validation"] = None
        try:
            cr = json.loads((ev_dir / "compliance_results.json").read_text(encoding="utf-8"))
            agg = ((cr.get("metrics") or {}).get("aggregate")) or {}
            out["summary"]["compliance"] = {
                "tp": agg.get("tp"), "fp": agg.get("fp"), "fn": agg.get("fn"),
                "tn": agg.get("tn"), "precision": agg.get("precision"),
                "recall": agg.get("recall"), "f1": agg.get("f1"),
                "case_count": agg.get("case_count"), "source": "compliance_results.json",
            }
        except Exception:
            out["summary"]["compliance"] = None
        try:
            gr = json.loads((ev_dir / "generation_results.json").read_text(encoding="utf-8"))
            gsum = gr.get("summary") or {}
            off = gsum.get("official_validation") or {}
            out["summary"]["generation"] = {
                "template_count": gsum.get("template_count"),
                "kicad_erc_attempted": off.get("erc_attempted"),
                "kicad_erc_passed": off.get("erc_passed"),
                "netlist_attempted": off.get("netlist_attempted"),
                "netlist_passed": off.get("netlist_passed"),
                "source": "generation_results.json",
            }
        except Exception:
            out["summary"]["generation"] = None
        try:
            csv_path = ev_dir / "ai_requirements_summary.csv"
            text = csv_path.read_text(encoding="utf-8-sig")
            rows = list(_csv.DictReader(text.splitlines()))
            def _truthy(x):
                return str(x).strip().lower() in ("1", "true", "yes")
            llm = sum(1 for r in rows if _truthy(r.get("llm_completed")))
            fb = sum(1 for r in rows if not _truthy(r.get("llm_completed")))
            e2e = sum(1 for r in rows if _truthy(r.get("end_to_end_pass")))
            out["summary"]["ai_requirements"] = {
                "total": len(rows), "llm_completed": llm, "template_fallback": fb,
                "end_to_end_pass": e2e, "source": "ai_requirements_summary.csv",
            }
        except Exception:
            out["summary"]["ai_requirements"] = None
        try:
            gate_path = ev_dir / "upgrade" / "strict_templates" / "validation_gate_results.json"
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            out["summary"]["strict_template_gate"] = {
                **gate["summary"], "kicad_version": gate.get("kicad_version"),
                "scope": gate.get("scope"), "source": gate_path.relative_to(ev_dir).as_posix(),
            }
        except (OSError, ValueError, KeyError):
            out["summary"]["strict_template_gate"] = None
        self._send_json(200, out)

    def _handle_docs(self):
        """API 文档：生产级自描述接口。"""
        docs = {
            "product": "CircuitTutor",
            "version": VERSION,
            "description": "中文需求 → 电路原理图 → KiCad/SPICE/BOM 导出",
            "endpoints": [
                {"method": "GET", "path": "/api/health", "desc": "健康检查"},
                {"method": "GET", "path": "/api/metrics", "desc": "运行指标"},
                {"method": "GET", "path": "/api/docs", "desc": "本接口"},
                {"method": "GET", "path": "/api/schema/circuitjson", "desc": "CircuitJSON v1/v2 Schema", "params": ["version"]},
                {"method": "POST", "path": "/api/circuit/migrate", "desc": "CircuitJSON v1 到 v2 无损迁移"},
                {"method": "POST", "path": "/api/validate", "desc": "统一候选门禁（默认含 KiCad 官方复验）"},
                {"method": "POST", "path": "/api/import/external", "desc": "CircuitVision、HCD 或 tscircuit 外部结果转待复核候选"},
                {"method": "POST", "path": "/api/design/tscircuit", "desc": "本机离线 TSX/JSX 生成原理图和待审核候选", "params": ["code", "name"]},
                {"method": "GET", "path": "/api/design/tscircuit/example", "desc": "返回本机 RC 示例代码"},
                {"method": "POST", "path": "/api/image/analyze", "desc": "本地 CircuitVision 图片检测与分割，结果需人工校正"},
                {"method": "GET", "path": "/api/providers", "desc": "模型预设目录"},
                {"method": "GET", "path": "/api/generate", "desc": "SSE 生成流", "params": ["q", "c", "cfg"]},
                {"method": "POST", "path": "/api/erc", "desc": "编辑后 ERC 重算"},
                {"method": "POST", "path": "/api/edit", "desc": "AI 增量改图", "params": ["circuit", "instruction", "cfg"]},
                {"method": "POST", "path": "/api/simulate", "desc": "准仿真"},
                {"method": "POST", "path": "/api/bom", "desc": "BOM 导出"},
                {"method": "POST", "path": "/api/spice", "desc": "SPICE 导出"},
                {"method": "POST", "path": "/api/spice_analyze", "desc": "SPICE 仿真（op/ac/tran/dc）", "params": ["circuit", "kind"]},
                {"method": "POST", "path": "/api/spice_transient", "desc": "真实 .tran 节点电压时间序列（教学时序图数据源，跑不出来则 ran=false）", "params": ["circuit", "step", "stop"]},
                {"method": "POST", "path": "/api/pcb", "desc": "PCB 预布局 .kicad_pcb", "params": ["circuit", "name"]},
                {"method": "POST", "path": "/api/pcb/candidate", "desc": "真实封装和焊盘映射的未布线 PCB 候选", "params": ["circuit", "name"]},
                {"method": "POST", "path": "/api/pcb/manufacture", "desc": "真实布线板经源原理图、焊盘、官方 DRC 校验后导出待复核制造候选包", "params": ["circuit", "board_text"]},
                {"method": "POST", "path": "/api/routing_job", "desc": "隔离式 PCB 路由任务合同校验", "params": ["job"]},
                {"method": "POST", "path": "/api/schemes", "desc": "多方案草案"},
                {"method": "GET", "path": "/api/trace", "desc": "链路日志（rid/latest）", "params": ["rid", "n"]},
                {"method": "GET", "path": "/api/evidence/summary", "desc": "evidence 摘要（只读）"},
                {"method": "GET", "path": "/api/history", "desc": "工程历史列表"},
                {"method": "POST", "path": "/api/history", "desc": "保存工程"},
                {"method": "POST", "path": "/api/kicad", "desc": "KiCad 导出"},
                {"method": "POST", "path": "/api/llm_config", "desc": "注册模型配置"},
                {"method": "POST", "path": "/api/clarify", "desc": "需求澄清"},
            ],
            # 规则数量直接取当前检查器清单，避免文档能力标签随实现演进而漂移。
            "features": [f"erc_{len(erc.ALL_CHECKERS)}_rules", "simulate", "bom", "spice", "spice_analyze",
                         "trace", "schemes", "history", "kicad_export", "pcb_layout",
                          "routing_job_v1_fallback", "circuitjson_schema_v1", "circuitjson_schema_v2", "incremental_edit", "evidence_summary", "ux_examples", "erc_fix_tips", "edit_undo", "demo_mode", "kicad_open_guide"],
        }
        self._send_json(200, docs)


    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path == "/api/history":
            return self._handle_history()
        self._send_json(404, {"error": "not found"})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/api/design/tscircuit/example':
            example = Path(__file__).resolve().parent / 'tools/upstream_examples/tscircuit_rc.tsx'
            try:
                return self._send_json(200, {'code': example.read_text(encoding='utf-8')})
            except OSError:
                return self._send_json(503, {'error': '本机RC示例文件缺失'})
        if u.path in ("/", "/index.html"):
            self._serve_file(os.path.join(WEB_DIR, "index.html"))
        elif u.path == "/api/evidence/summary":
            return self._handle_evidence_summary()
        elif u.path == "/api/providers":
            # 多模型预设目录（无敏感信息）：前端多级按钮的数据源
            self._send_json(200, llm_providers.public_catalog(
                config.api_key_configured()))
        elif u.path == "/api/docs":
            return self._handle_docs()
        elif u.path == "/api/schema/circuitjson":
            from core.circuit_schema import get_circuit_json_schema
            raw_version = parse_qs(u.query).get("version", ["1"])[0]
            try:
                version = int(raw_version)
                schema = get_circuit_json_schema(version)
            except (TypeError, ValueError):
                return self._send_json(400, {"error": "version 仅支持 1 或 2"})
            return self._send_json(200, schema)
        elif u.path == "/api/metrics":
            snap = METRICS.snapshot()
            snap["llm_breaker"] = LLM_BREAKER.snapshot()
            return self._send_json(200, snap)
        elif u.path == "/api/trace":
            return self._handle_trace_query()
        elif u.path == "/api/history":
            return self._handle_history()
        elif u.path == "/api/health":
            # 工业级健康检查：版本 / Key / 模型 / KiCad / 套件数
            from core.llm import DeepSeekClient
            try:
                from kicad import cli_runner as _kr
                kcli = _kr.find_kicad_cli()
            except Exception:
                kcli = None
            self._send_json(200, {
                "status": "ok",
                "version": VERSION,
                "product": "CircuitTutor",
                "llm_key_configured": config.api_key_configured(),
                "default_model": DeepSeekClient.DEFAULT_MODEL,
                "kicad_cli": bool(kcli),
                "features": ["erc", "autofix", "precision", "simulate",
                              "spice_analyze", "trace", "bom", "kicad_export",
                              "circuitjson_schema_v1", "circuitjson_schema_v2", "incremental_edit", "evidence_summary", "ux_examples", "erc_fix_tips", "edit_undo", "demo_mode", "kicad_open_guide", "image_import", "validation_gate", "pcb_real", "pcb_manufacturing"],
            })
        elif u.path.startswith("/static/vision/"):
            # 固定软件资产映射；请求不能指定本机目录或下载任意依赖文件。
            vendor = _vision_ocr_root() / "node_modules"
            assets = {
                "tesseract.min.js": vendor / "tesseract.js/dist/tesseract.min.js",
                "worker.min.js": vendor / "tesseract.js/dist/worker.min.js",
                "lang/eng.traineddata.gz": vendor / "@tesseract.js-data/eng/4.0.0_best_int/eng.traineddata.gz",
            }
            core_dir = vendor / "tesseract.js-core"
            for asset in core_dir.glob("tesseract-core*"):
                if asset.is_file() and asset.name.endswith((".js", ".wasm")):
                    assets["core/" + asset.name] = asset
            target = assets.get(u.path[len("/static/vision/"):])
            if target is None:
                return self._send_json(404, {"error": "not found"})
            return self._serve_file(str(target))
        elif u.path.startswith("/static/"):
            name = os.path.basename(unquote(u.path))   # basename 防目录穿越
            if name in ALLOWED_STATIC:
                # 前端资源统一放 web/；部分脚本同时被 Node 测试引用。
                path = os.path.join(WEB_DIR, name)
                if os.path.exists(path):
                    return self._serve_file(path)
                self._send_json(404, {"error": "not found"})
            else:
                self._send_json(404, {"error": "not found"})
        elif u.path == "/api/generate":
            if not rate_limiter.allow(self.client_address[0]):
                # 限流：每次命中都计数（含被拒的），滑动窗口自然过期
                log.warning("限流拒绝 /api/generate：ip=%s", self.client_address[0])
                return self._reject_sse(
                    f"请求过于频繁（每 IP 每分钟限 {rate_limiter.limit} 次），"
                    f"请稍后再试")
            params = parse_qs(u.query)
            q = params.get("q", [""])[0]
            if len(q) > config.MAX_QUERY_LEN:
                # 输入上限：超长需求既拖慢 LLM 又浪费 token，直接拒绝
                return self._reject_sse(
                    f"需求文本超过上限 {config.MAX_QUERY_LEN} 字符，请精简后重试")
            # 澄清问答合并：前端 /api/clarify 确认后把问答对经 c 参数带回，
            # 合并为增强需求再进流水线（非法 c 静默忽略，不影响正常生成）
            raw_c = params.get("c", [""])[0]
            if raw_c:
                try:
                    qa_pairs = json.loads(raw_c)
                    merged = clarify.merge_clarifications(
                        q, qa_pairs, max_len=config.MAX_QUERY_LEN)
                    if merged != q:
                        log.info("需求澄清已并入：%d 条问答", len(qa_pairs))
                        q = merged
                except (json.JSONDecodeError, TypeError):
                    log.warning("澄清参数 c 非法，按原需求继续：%.60r", raw_c)
            # 多模型：前端经 /api/llm_config 注册后带回的 cfg id（Key 不进 URL）
            unsupported = catalog.unsupported_part_numbers(q)
            if unsupported:
                return self._reject_sse("需求含目录外器件，需先补充器件合同：" +
                                        "、".join(unsupported))
            blockers = llm.detect_requirement_blockers(q)
            if blockers:
                return self._reject_sse("需求约束存在明确冲突：" +
                                        "；".join(item["message"] for item in blockers), blockers)
            required = clarify.rule_analyze(q)
            if required["status"] == "clarify":
                return self._reject_sse(
                    "需求尚需澄清：" + "；".join(item["question"] for item in required["questions"]),
                    questions=required["questions"])
            cfg_id = params.get("cfg", [""])[0].strip()
            self.handle_generate(q, cfg_id)
        else:
            self._send_json(404, {"error": "not found"})

    def _sse_headers(self):
        """SSE 响应头（正常生成流与超长输入拒绝流两处复用）。
        Connection: close —— 流结束即关，EventSource 不重连。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _reject_sse(self, reason: str, blockers=None, questions=None):
        """生成前置拒绝（输入超长 / 限流）：SSE 头照发（前端 EventSource
        依赖事件流协议），随后推一条错误说明并收尾，让前端能显示原因
        而不是静默卡死。
        rejected=True：与“管线失败回落模板”的 fallback 区分——
        前者请求根本没进流水线（不该显示内置模板电路），后者才回落模板。"""
        self._sse_headers()
        try:
            payload = json.dumps({"reason": reason, "rejected": True, "blockers": blockers or [],
                                  "questions": questions or []},
                                 ensure_ascii=False)
            self.wfile.write(f"event: fallback\ndata: {payload}\n\n".encode("utf-8"))
            self.wfile.write(b'event: done\ndata: {"ok": false, "rejected": true, "accepted": false}\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            # 前置拒绝流（超长/限流）写到一半客户端就断了：连接已不可用，
            # 无法也不需补发任何事件。此处【有意】不抛不兜底，但留一条 debug
            # 便于排查“用户没看到拒绝原因”类反馈（此前完全静默，无从判断
            # 是没触发拒绝还是拒绝事件没送达）。
            log.debug("前置拒绝 SSE 写入失败（客户端已断开）：%s", e)
        self.close_connection = True

    def _serve_file(self, path):
        body = _read_static(path)   # 带缓存读取；None = 不存在/不可读
        if body is None:
            return self._send_json(404, {"error": "not found"})
        ext = os.path.splitext(path)[1].lower()
        # 生产级静态缓存：带 ETag 的协商缓存（零依赖，标准库 hashlib）
        import hashlib
        etag = hashlib.md5(body).hexdigest()[:16]
        rid = getattr(self, "_rid", None) or new_request_id()
        self._rid = rid
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", rid)
        self.send_header("ETag", f'W/"{etag}"')
        self.send_header("Cache-Control", "public, max-age=60")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    # ---------- SSE 生成流水线 ----------
    def handle_generate(self, q: str, cfg_id: str = ""):
        rid = getattr(self, "_rid", None) or new_request_id()
        self._rid = rid
        set_request_id(rid)   # ContextVar：chat()/trace_span 自动带上同一 rid
        self._sse_headers()

        # SSE 断开止损：客户端断开后置位，流水线下一次 LLM 调用前感知并中止。
        # 最多浪费“当前正在进行的这一次”调用，后续（结构修复/ERC 修复）全部取消。
        cancel_event = threading.Event()
        # 发送互斥：业务事件与心跳线程共用一把锁，SSE 帧
        # （"event: X\ndata: Y\n\n"）与注释行（": ping\n\n"）不允许交错写坏帧
        send_lock = threading.Lock()

        def send_event(event: str, data) -> None:
            payload = json.dumps(data, ensure_ascii=False)
            try:
                with send_lock:
                    self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                # 写入失败 = 客户端已断开：置位取消信号后原样抛出，
                # 正在进行的 LLM 调用结束后，下一次 chat() 会抛 OperationCancelled
                cancel_event.set()
                raise

        # SSE 心跳：LLM 规划/生成期间流上可能几十秒无任何字节，代理
        # 与网关常把“长时间无数据”的连接掐断（浏览器侧表现为 EventSource
        # 异常断开、进度条卡死）。心跳线程周期性写 SSE 注释行 ": ping"
        # （协议规定客户端忽略注释行，EventSource 自动无视），保活连接。
        hb_stop = threading.Event()

        def heartbeat() -> None:
            # wait(interval) 返回 True = 收到停止信号，退出循环；
            # False = 超时到期，写一拍心跳。首拍在 SSE_HEARTBEAT_S 之后。
            while not hb_stop.wait(config.SSE_HEARTBEAT_S):
                try:
                    with send_lock:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    # 心跳写失败同样说明客户端已断开：置位取消信号，
                    # 让还在跑的 LLM 流水线尽快止损，线程随即退出
                    cancel_event.set()
                    return

        threading.Thread(target=heartbeat, name="sse-heartbeat",
                         daemon=True).start()
        try:
            if not q.strip():
                raise ValueError("需求为空，请输入一句中文电路需求")
            self._run_pipeline(q, send_event, cancel_event, cfg_id)
        except (BrokenPipeError, ConnectionResetError):
            return   # 客户端提前关闭
        except llm.OperationCancelled:
            # 客户端断开触发的取消：连接已没了，无需也不能再补发任何事件，
            # 与普通异常分开处理，避免走进 fallback 分支打无意义日志
            return
        except llm.IntentConstraintError as exc:
            try:
                send_event("fallback", {"rejected": True, "reason": str(exc),
                                        "blockers": exc.blockers})
                send_event("done", {"ok": False, "rejected": True, "accepted": False})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        except Exception as e:
            # 模板兜底：运行错误后仍须匹配并完整覆盖需求，否则明确拒绝。
            log.warning("生成流水线失败，转入模板兜底（q=%.40s）：%s: %s",
                        q, type(e).__name__, e)
            try:
                try:
                    circuit, tpl_name = self._pick_fallback(q)
                except ValueError as mismatch:
                    send_event("fallback", {"rejected": True, "reason": str(mismatch),
                                            "generation_error": f"{type(e).__name__}: {e}",
                                            "blockers": [], "questions": []})
                    send_event("done", {"ok": False, "rejected": True, "accepted": False})
                    return
                circuit["provenance"] = {"origin": "template", "source_id": tpl_name}
                # 兜底透明化：把选中的内置模板名透出给前端——用户明确知道
                # 看到的是哪个内置电路、与自己需求的偏差有多大（可改需求重试）
                send_event("fallback", {"reason": f"{type(e).__name__}: {e}",
                                        "template": tpl_name})
                send_event("circuit", circuit)
                # 兜底电路同样做 ERC 终检：内置模板本身就是"零 ERC 问题"的
                # 示例电路，把结果透出后前端徽章会显示"全部通过 ✔"，与 LLM
                # 成功路径体验一致。同时前端依赖本事件触发一次 /api/erc 重算
                # 以获得 issues[]（画布标注）——此前兜底路径不发 erc，
                # 未配置 API Key 的整个演示路径上 ERC 完全不可见。
                try:
                    send_event("erc", _erc_summary(erc.run_erc(circuit, ERC_KB)))
                except Exception:   # noqa: BLE001 —— 检查失败不妨碍出图
                    log.exception("兜底电路的 ERC 终检失败（模板=%s）", tpl_name)
                from core.validation_gate import validate_candidate
                validation = validate_candidate(circuit, source="template", name="fallback")
                send_event("validation", validation)
                send_event("done", {"ok": False, "fallback": True,
                                    "accepted": validation["accepted"],
                                    "validation_verdict": validation["verdict"]})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass   # 兜底阶段客户端也断了：无法送达，静默结束
            except Exception:
                # 兜底自身崩掉不能无声无息（原 except Exception: pass 吞掉
                # 一切，模板数据损坏时表现为前端卡死且无任何日志可查）
                log.exception("模板兜底流程失败（q=%.40s）", q)
        finally:
            # 无论成功/失败/取消，事件帧已发完或连接已死——停止心跳线程
            # （daemon 线程本身不阻塞进程退出，set 只是让它立刻醒来收尾）
            hb_stop.set()

    def _run_pipeline(self, q: str, send_event, cancel_event: threading.Event,
                      cfg_id: str = ""):
        rid = get_request_id() or (getattr(self, "_rid", None) or new_request_id())
        set_request_id(rid)
        with trace_span("pipeline") as span:
            span.event("start", q_len=len(q), has_cfg=bool(cfg_id))
            budget = llm.CallBudget(max_calls=config.LLM_BUDGET)
            client, model_label = make_llm_client(cfg_id)
            if client is not None:
                # 让"切换模型真的生效"在进度条上可见（演示时评委可确认）
                send_event("stage", {"stage": "model",
                                     "detail": f"模型：{model_label}"})
                span.event("model", label=model_label)

            def on_progress(stage, detail):
                send_event("stage", {"stage": stage, "detail": detail})
                span.event("stage", stage=stage, detail=detail[:120])

            # ① 元件抽取：本地命中即免第一次 LLM 往返（cancel_event 一并透传）
            types, source = llm.extract_components(q, client, budget, on_progress,
                                                   cancel_event=cancel_event)
            span.event("extract", source=source, types=list(types)[:20])
            send_event("stage", {"stage": "components",
                                 "detail": f"元件：{'、'.join(t for t in types) or '（无）'}"
                                           f"（{SOURCE_TEXT[source]}）"})
            if not types:
                raise ValueError("未识别到任何元件，触发模板兜底")
            if client is None:
                raise ValueError("未配置 DEEPSEEK_API_KEY，触发模板兜底")

            # 熔断检查：连续 LLM 失败过多时短路，直接走模板兜底
            if not LLM_BREAKER.allow():
                raise RuntimeError("LLM 熔断器打开（连续失败过多），触发模板兜底")

            # ② 规划 → 生成 → 校验 → 修复（预算封顶 + 实时进度 + 断开止损）
            matched_lib = catalog.build_matched_lib(types)
            try:
                circuit = llm.generate_circuit_json(q, matched_lib, client,
                                                    max_retry=config.LLM_STRUCT_RETRIES,
                                                    budget=budget, on_progress=on_progress,
                                                    erc_kb=ERC_KB, cancel_event=cancel_event)
                LLM_BREAKER.on_success()
            except Exception:
                LLM_BREAKER.on_failure()
                raise
            span.event("generated", parts=len(circuit.get("parts") or []),
                       conns=len(circuit.get("connections") or []))

            # ③ ERC 终检 + 警告自动修复
            rep = erc.run_erc(circuit, ERC_KB)
            if config.AUTOFIX:
                fixed, fixes = autofix.apply(circuit, ERC_KB, rep=rep)
                if fixes and not llm.validate_circuit_json(fixed):
                    circuit = fixed
                    rep = erc.run_erc(circuit, ERC_KB)
                    send_event("stage", {"stage": "autofix", "detail": "；".join(fixes)})
                    span.event("autofix", fixes=fixes[:5])

            # 参数计算 + 拓扑黄金模式（只改无争议标量，不改连线）
            try:
                from core import design_rules
                circuit, pnotes, pwarns = design_rules.apply_precision(circuit, q)
                detail_parts = []
                if pnotes:
                    detail_parts.extend(pnotes)
                if pwarns:
                    detail_parts.extend(pwarns)
                if pnotes:
                    send_event("stage", {"stage": "precision",
                                         "detail": "；".join(pnotes)[:500]})
                    rep = erc.run_erc(circuit, ERC_KB)
                if pwarns:
                    send_event("stage", {"stage": "precision",
                                         "detail": "拓扑提示：" + "；".join(pwarns)[:500]})
                # 额定值告警（LED/电阻功耗/稳压压差）
                try:
                    from core.erc_ratings import run_rating_checks
                    for iss in run_rating_checks(circuit):
                        if iss.severity in ("warn", "danger"):
                            send_event("stage", {"stage": "ratings",
                                                 "detail": iss.message[:500]})
                except Exception as e:  # noqa: BLE001
                    log.debug("erc_ratings 跳过: %s", e)
                # 准仿真：LED 电流 / 分压 / 稳压功耗（教学向直流工作点）
                try:
                    from core.simulate import run_simulation
                    sim = run_simulation(circuit)
                    if sim.led_currents or sim.dividers or sim.regulator:
                        bits = []
                        for led in sim.led_currents[:3]:
                            bits.append(f"{led['led']}≈{led['i_ma']:.1f}mA")
                        for dv in sim.dividers[:2]:
                            bits.append(f"中点≈{dv['vout']:.2f}V")
                        for rg in sim.regulator[:1]:
                            bits.append(f"{rg['reg']}功耗≈{rg['p_w']:.2f}W")
                        if bits:
                            send_event("stage", {"stage": "simulate",
                                                 "detail": "准仿真：" + "；".join(bits)[:500]})
                    for iss in sim.issues:
                        if iss.severity in ("warn", "danger"):
                            send_event("stage", {"stage": "simulate",
                                                 "detail": iss.message[:500]})
                except Exception as e:  # noqa: BLE001
                    log.debug("simulate 跳过: %s", e)
            except Exception as e:  # noqa: BLE001
                log.debug("design_rules 跳过: %s", e)

            circuit["provenance"] = {"origin": "prompt", "source_id": rid}
            if isinstance(getattr(client, "model", None), str):
                circuit["provenance"]["model_id"] = client.model
            send_event("circuit", circuit)
            # 生成后自动附带拓扑草案（工业级：一次需求出 2 个候选）
            try:
                from core.schemes import pick_schemes
                schemes = pick_schemes(q)
                if schemes:
                    # 只发精简字段，避免 SSE 过大
                    slim = [{"key": s["key"], "title": s["title"],
                             "reason": s.get("reason",""), "score": s.get("score",0)}
                            for s in schemes]
                    send_event("schemes", {"schemes": slim})
            except Exception as e:  # noqa: BLE001
                log.debug("schemes 生成失败: %s", e)
            send_event("erc", _erc_summary(rep))
            from core.validation_gate import validate_candidate
            validation = validate_candidate(circuit, source="ai", name="generated")
            send_event("validation", validation)
            span.event("done", budget_used=budget.used, erc_passed=rep.passed)
            send_event("done", {"ok": True, "budget_used": budget.used,
                                "budget_max": budget.max_calls,
                                "accepted": validation["accepted"],
                                "validation_verdict": validation["verdict"],
                                "request_id": rid})

    def _pick_fallback(self, q: str):
        """模板兜底。返回 (circuit, 模板名)——模板名（内置电路的中文标题）
        随 SSE fallback 事件透出，前端明示"你看到的是哪个内置电路"。"""
        key = catalog.match_template(q, FALLBACK_TEMPLATES)
        if key is None:
            raise ValueError("当前没有覆盖此需求的离线模板；请配置文本生成模型或使用明确匹配的模板需求。")
        required = set(catalog.match_components(q)) - {"battery", "vcc", "gnd"}
        available = {part["type"] for part in BUILTIN_TEMPLATES[key]["parts"]}
        missing = sorted(required - available)
        if missing:
            raise ValueError("离线模板无法覆盖需求中的器件：" + "、".join(missing) +
                             "；请配置文本生成模型或调整需求。")
        # 深拷贝后注入用户显式参数（470Ω/5V/红色 等）：共享模板本体不得被
        # 单次请求污染；注入逻辑与 ai2kicad 同源（component_catalog）
        circuit = copy.deepcopy(BUILTIN_TEMPLATES[key])
        injected = catalog.inject_query_values(circuit, q)
        if injected:
            log.info("模板兜底采纳描述参数: %s",
                     "、".join(f"{r} {o}→{n}" for r, o, n in injected))
        # boardsmith 式参数计算：LED 串联电阻按 (Vcc-Vf)/10mA 重算并吸 E12
        # （用户已写明且落在安全区间的值会保留）
        try:
            from core import design_rules
            _, pnotes, pwarns = design_rules.apply_precision(circuit, q)
            for n in pnotes:
                log.info("参数精确化: %s", n)
            for w in pwarns:
                log.warning("拓扑告警: %s", w)
        except Exception as e:  # noqa: BLE001 —— 精确化失败不阻断兜底
            log.debug("design_rules 跳过: %s", e)
        return circuit, BUILTIN_TEMPLATES[key].get("title", key)


def main():
    # 日志配置收敛到 config.setup_logging() 单一入口（幂等）。
    # 此前此处另有一份 logging.basicConfig（格式与本函数不同），与 config 的
    # 配置重复：basicConfig 只在根 logger 尚无 handler 时生效，两处并存会导致
    # 实际生效的格式取决于谁先跑（库模块导入期若已触发配置，这里的格式静默
    # 失效）——日志格式不一致却难以察觉。统一走 config 一处，格式确定。
    config.setup_logging()
    srv = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    print(f"智绘电路 · 运行时优化演示服务已启动：http://{config.HOST}:{config.PORT}/")
    print(f"  API Key：{'已配置，走完整 LLM 流水线' if config.api_key_configured() else '未配置，走内置模板兜底（可正常演示 SSE + 渲染）'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        srv.server_close()   # 释放监听端口，进程退出前收干净


if __name__ == "__main__":
    main()
