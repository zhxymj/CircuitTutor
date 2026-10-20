# -*- coding: utf-8 -*-
"""
observability.py —— 结构化日志 + 请求追踪 + 熔断（生产级）
============================================================
* setup_logging()：JSON/文本格式，可选写 JSON 文件
* Metrics：计数器（QPS / 延迟 / 错误率）
* CircuitBreaker：连续失败熔断，冷却后半开重试
* log_request()：每请求一条结构化日志，request_id 贯穿
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "text",
                  log_file: str = "") -> None:
    """配置根日志。log_file 非空时同时写 JSON 行日志（生产环境）。"""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    ch = logging.StreamHandler()
    if fmt == "json":
        ch.setFormatter(_JsonFormatter())
    else:
        ch.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s | %(message)s"))
    root.addHandler(ch)
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(_JsonFormatter())
            root.addHandler(fh)
        except OSError:
            pass


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


class Metrics:
    """线程安全的简单计数器。"""
    def __init__(self):
        self._lock = __import__("threading").Lock()
        self.counters: Dict[str, int] = {}
        self.latency_sum: Dict[str, float] = {}
        self.latency_n: Dict[str, int] = {}

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + n

    def observe_latency(self, name: str, seconds: float) -> None:
        with self._lock:
            self.latency_sum[name] = self.latency_sum.get(name, 0.0) + seconds
            self.latency_n[name] = self.latency_n.get(name, 0) + 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            avg = {}
            for k, s in self.latency_sum.items():
                n = self.latency_n.get(k, 1)
                avg[k] = round(s / n, 4)
            return {"counters": dict(self.counters), "avg_latency_s": avg}


METRICS = Metrics()


def log_request(logger, request_id: str, method: str, path: str,
                status: int, duration_s: float, **extra: Any) -> None:
    METRICS.inc(f"http_{method}")
    METRICS.inc(f"status_{status // 100}xx")
    METRICS.observe_latency(path.split("?")[0], duration_s)
    fields = {"request_id": request_id, "method": method, "path": path,
              "status": status, "duration_ms": int(duration_s * 1000)}
    fields.update(extra)
    logger.info(
        "request %s %s -> %s (%.0fms)", method, path, status, duration_s * 1000,
        extra={"extra_fields": fields},
    )


class CircuitBreaker:
    """熔断器：连续失败 N 次后短路，冷却期后半开重试。

    状态机：closed →(连续失败≥阈值)→ open →(冷却到期)→ half
    half 状态放行一次探测请求：成功回 closed，失败回 open。
    """
    def __init__(self, fail_threshold: int = 5, cooldown_s: float = 30.0):
        self.fail_threshold = fail_threshold
        self.cooldown_s = cooldown_s
        self._lock = __import__("threading").Lock()
        self.failures = 0
        self.state = "closed"
        self.opened_at = 0.0
        self.total_rejected = 0

    def allow(self) -> bool:
        with self._lock:
            if self.state == "closed":
                return True
            if self.state == "open":
                if time.time() - self.opened_at >= self.cooldown_s:
                    self.state = "half"
                    return True
                self.total_rejected += 1
                return False
            return True  # half：放行一次探测

    def on_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.state = "closed"

    def on_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.fail_threshold:
                self.state = "open"
                self.opened_at = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "failures": self.failures,
                    "threshold": self.fail_threshold,
                    "rejected": self.total_rejected}


# LLM 调用熔断：连续 5 次失败后短路 30 秒
LLM_BREAKER = CircuitBreaker(fail_threshold=5, cooldown_s=30.0)
