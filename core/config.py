# -*- coding: utf-8 -*-
"""
config.py —— 智绘电路全局配置（环境变量统一入口）
==================================================
把散落在 server.py / benchmark.py / core/llm.py 里的环境变量与
默认参数集中到一处：带类型校验和取值范围检查，非法配置在启动瞬间
给出明确报错（而不是跑到一半才以难以定位的方式崩掉）。

环境变量一览
------------
LSQC_PORT          服务监听端口，默认 8765（1~65535）。云端发布沙箱无
                   LSQC_PORT 时会回退到标准 PORT 环境变量并绑定 0.0.0.0
LSQC_HOST          服务监听地址，默认 127.0.0.1（只本机可访问；教学机房
                   如需局域网访问可显式设为 0.0.0.0，自行承担暴露风险）
DEEPSEEK_API_KEY   DeepSeek API Key（未配置时服务自动走内置模板兜底）
DEEPSEEK_TLS12     设为 1/true 时 TLS 回退 1.2（部分机房网关干扰 TLS 1.3 握手）；0/false/off/no 显式关闭
DEEPSEEK_BASE_URL  DeepSeek 接口地址，默认 https://api.deepseek.com
DEEPSEEK_MODEL     默认模型 ID，留空取 DeepSeekClient.DEFAULT_MODEL
                   （deepseek-flash = 官方 V4.1 Flash 滚动别名）。可切
                   deepseek-v4-pro 等。模型 ID 本身是别名，官方
                   后台自动指向最新正式版，故不写带日期的快照名
LLM_TIMEOUT_S      单次 LLM 请求超时秒数，默认 90（5~120）。V4 时代规划阶段
                   为真思维链（默认 effort=high），实测约 36s，25s 必超时
LLM_RETRIES        网络错误/429/5xx 重试次数，默认 2（0~5）
LLM_STRUCT_RETRIES 结构非法/误返回数组时的额外重试次数，默认 2（0~5），
                   即最多 3 次生成尝试（“返回数组”是 LLM 高发故障，
                   重试 3 次实践中命中率最高；总调用仍受 LLM_BUDGET 封顶）
LLM_ERC_RETRIES    ERC 电气错误回灌修复轮数，默认 1（0~3）。0 = 只报不修；
                   每轮基于最新电路重新回灌错误清单，问题数不减即停
                   （轮数 + LLM_BUDGET 双上限，不会无限烧预算）
LLM_BUDGET         每条需求的全局 LLM 调用预算，默认 5（1~20）
LLM_MAX_TOKENS     单次 LLM 响应 max_tokens 上限，默认 4096（256~32768）
MAX_QUERY_LEN      /api/generate 需求文本最大长度，默认 500 字符（10~10000）
LSQC_AUTOFIX       是否自动附挂去耦/旁路电容（ERC 警告闭环），默认 1 开启，
                   设为 0/false/off/no 关闭后警告仅提示不自动修复
LSQC_LOG_LEVEL     日志级别 DEBUG/INFO/WARNING/ERROR，默认 INFO
LSQC_RATE_LIMIT    每 IP 每分钟 LLM 端点（/api/generate、/api/clarify）请求
                   上限，默认 30（0=关闭限流）。教学演示单人使用远够；设
                   LSQC_HOST=0.0.0.0 局域网暴露时的第一道防刷闸门，
                   防止他人刷爆你的 DEEPSEEK_API_KEY 余额
LSQC_SSE_HEARTBEAT_S SSE 心跳间隔秒数，默认 15（3~120）。注释行 ": ping"
                   保持连接活跃，防代理/网关把 30~60s 无数据的长连接掐断
LSQC_KICAD_RATE_LIMIT 每 IP 每分钟 /api/kicad（导出 .kicad_pro 工程）请求
                   上限，默认 20（0=关闭）。子进程导出比 LLM 调用更重，
                   局域网暴露时防止单 IP 打满子进程池
KICAD_CLI_PATH     kicad-cli.exe 完整路径（可选；未设置时按 PATH + 常见
                   安装目录自动探测，两者都找不到才降级跳过官方校验）
KICAD_CLI_TIMEOUT_S kicad-cli 官方 ERC/网表子进程超时秒数，默认 90（5~600）
"""

import logging
import os

# ---------------------------------------------------------------------
# 内部工具：带默认值与范围检查的整数/浮点环境变量解析
# ---------------------------------------------------------------------

def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw.strip())
    except ValueError:
        raise SystemExit(f"配置错误：环境变量 {name}={raw!r} 不是整数（有效范围 {lo}~{hi}）")
    if not (lo <= val <= hi):
        raise SystemExit(f"配置错误：环境变量 {name}={val} 超出有效范围 {lo}~{hi}")
    return val


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw.strip())
    except ValueError:
        raise SystemExit(f"配置错误：环境变量 {name}={raw!r} 不是数字（有效范围 {lo}~{hi}）")
    if not (lo <= val <= hi):
        raise SystemExit(f"配置错误：环境变量 {name}={val} 超出有效范围 {lo}~{hi}")
    return val


# ---------------------------------------------------------------------
# 对外配置项（导入即完成校验；配置非法立即 SystemExit 并给出人话说明）
# ---------------------------------------------------------------------

def _env_flag(name: str, default: bool) -> bool:
    """布尔环境变量：0/false/off/no（不分大小写）视为关，其余视为开。

    定义提前到首个布尔配置项之前——TLS12 此前按 bool(非空即真) 判断，
    设 0/false 也会开启，语义与直觉相反（2026-09-10 收敛到本函数）。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "off", "no")


# 监听地址/端口：优先取显式的 LSQC_HOST/LSQC_PORT（本机/内网演示的既定语义，
# 默认 127.0.0.1:8765）。两者都未显式设置、且存在标准 PORT 环境变量时，
# 回退到「0.0.0.0 + PORT」——云端发布沙箱只注入 PORT 并要求绑定 0.0.0.0，
# 此回退让同一套代码无需改动即可被发布成在线链接（本机行为不变）。
_HOST_RAW = os.environ.get("LSQC_HOST", "").strip()
_PORT_RAW = os.environ.get("LSQC_PORT", "").strip()
_std_port = os.environ.get("PORT", "").strip()
if not _HOST_RAW and not _PORT_RAW and _std_port:
    HOST = "0.0.0.0"
    PORT = _env_int("PORT", int(_std_port), 1, 65535)
else:
    HOST = _HOST_RAW or "127.0.0.1"
    PORT = _env_int("LSQC_PORT", 8765, 1, 65535)

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
TLS12 = _env_flag("DEEPSEEK_TLS12", False)   # 设 1/true 回退 TLS 1.2；0/false/off/no 显式关闭
BASE_URL = (os.environ.get("DEEPSEEK_BASE_URL", "").strip()
            or "https://api.deepseek.com")
# 默认模型（2026-09-10 对齐 DeepSeek V4 正式版）。空值取 DeepSeekClient.DEFAULT_MODEL，
# 不在此处硬编码字面量，避免与 llm_providers.py 预设表 / 前端兜底字面量三处漂移
# （三者一致性由 tests/test_benchmark_snapshot.py 断言锁定）。
# 说明：这是「滚动别名」，官方后台自动指向最新正式版，故不写带日期的快照名。
MODEL = os.environ.get("DEEPSEEK_MODEL", "").strip()

LLM_TIMEOUT_S = _env_float("LLM_TIMEOUT_S", 90.0, 5.0, 120.0)
LLM_RETRIES = _env_int("LLM_RETRIES", 2, 0, 5)
LLM_STRUCT_RETRIES = _env_int("LLM_STRUCT_RETRIES", 2, 0, 5)
# ERC 回灌修复轮数（llm.generate_circuit_json 消费）：默认 1 轮，
# 修复轮与生成轮共用同一预算模型——CallBudget 是唯一终止上限
LLM_ERC_RETRIES = _env_int("LLM_ERC_RETRIES", 1, 0, 3)
LLM_BUDGET = _env_int("LLM_BUDGET", 5, 1, 20)
LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 4096, 256, 32768)

MAX_QUERY_LEN = _env_int("MAX_QUERY_LEN", 500, 10, 10000)


# ERC 警告自动修复（autofix.py）：缺去耦电容 / 555 CTRL 悬空 → 自动附挂电容
AUTOFIX = _env_flag("LSQC_AUTOFIX", True)

# 日志级别（server / ai2kicad / benchmark 入口经 setup_logging() 统一生效）
_LOG_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
               "WARNING": logging.WARNING, "ERROR": logging.ERROR}
_raw_level = os.environ.get("LSQC_LOG_LEVEL", "INFO").strip().upper()
if _raw_level not in _LOG_LEVELS:
    raise SystemExit(
        f"配置错误：环境变量 LSQC_LOG_LEVEL={_raw_level!r} 不是 "
        f"DEBUG/INFO/WARNING/ERROR 之一")
LOG_LEVEL = _LOG_LEVELS[_raw_level]

# JSON 结构化日志写入文件（留空则只输出到 stdout）
LOG_FILE = os.environ.get("LSQC_LOG_FILE", "").strip()

# KiCad 官方校验（export.py）：子进程超时与可选的 cli 路径提示
KICAD_CLI_TIMEOUT_S = _env_float("KICAD_CLI_TIMEOUT_S", 90.0, 5.0, 600.0)
KICAD_CLI_PATH = os.environ.get("KICAD_CLI_PATH", "").strip()

# 每 IP 滑动窗口限流（server.py）：保护消耗 LLM 预算的端点，
# 0 = 关闭。默认 30 次/分钟对单人教学演示完全无感，局域网暴露时防刷 Key。
RATE_LIMIT = _env_int("LSQC_RATE_LIMIT", 30, 0, 100000)

# SSE 心跳间隔（server.py handle_generate）：注释行 ": ping" 保活，
# 防代理/网关把长时间无数据的流式连接掐断；EventSource 自动忽略注释行。
SSE_HEARTBEAT_S = _env_float("LSQC_SSE_HEARTBEAT_S", 15.0, 3.0, 120.0)

# /api/kicad 导出端点独立限流（server.py）：kicad-cli 子进程导出比 LLM
# 调用更重，独立配额防止单 IP 打满子进程池；0 = 关闭。
KICAD_RATE_LIMIT = _env_int("LSQC_KICAD_RATE_LIMIT", 20, 0, 100000)


def api_key_configured() -> bool:
    """API Key 是否已配置（benchmark.py / server.py 的环境检查共用）。"""
    return bool(API_KEY)


_LOG_CONFIGURED = False


def setup_logging() -> None:
    """按 LOG_LEVEL 配置根日志（幂等；重复调用只生效一次）。

    server / ai2kicad / benchmark 等入口在 main() 里调用；库模块直接
    logging.getLogger(__name__) 即可，无需感知配置时机。
    LSQC_LOG_FILE 非空时同时写 JSON 行日志文件。"""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    try:
        from core.observability import setup_logging as _setup
        _setup(level=logging.getLevelName(LOG_LEVEL), log_file=LOG_FILE)
    except ImportError:
        logging.basicConfig(
            level=LOG_LEVEL,
            format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%H:%M:%S")
    _LOG_CONFIGURED = True
