# -*- coding: utf-8 -*-
"""
llm_providers.py —— 多模型提供商预设表（OpenAI 兼容接口）
==========================================================
智绘电路的 LLM 客户端（core/llm.py 的 DeepSeekClient）说的是 OpenAI 兼容
协议（POST {base}/chat/completions，Bearer 鉴权，response_format json_mode），
因此换提供商 = 换 (base_url, api_key, model) 三元组，协议层零改动。

本模块集中维护内置预设，供：
* GET /api/providers —— 前端多级按钮（一级提供商 → 二级模型）的数据源
* POST /api/llm_config —— 按 provider id 解析 base_url（预设的 base_url
  由服务端给出，客户端只传 provider id，不伪造地址）

安全约定：
* api_key 一律由用户在浏览器端输入 → POST /api/llm_config 上报 →
  只活在服务端内存（TTL 过期），不落盘、不进 URL、不打日志；
* DeepSeek 保留服务端环境变量 DEEPSEEK_API_KEY 供 key 的默认路径
  （界面选 DeepSeek 且服务端已配 key 时无需再输）；
* custom 提供商的 base_url 允许 http:// 仅当指向本机回环
  （127.0.0.1/localhost/[::1]）——本地 Ollama 等自建兼容端点场景；
  公网地址一律强制 https://。

json_mode 兼容性说明（列入预设的模型均已确认支持 response_format）：
* 各家 JSON mode 均要求 prompt 含 "json" 字样（GEN/EXTRACT/两套修复模板
  已满足）；DeepSeek 之外的兼容端点不下发 thinking 字段（参数名各家不同，
  避免被拒收）。
* 思维链默认常开的模型会让推理 token 吃掉 max_tokens、正文为空——本项目
  的 JSON 结构化链路会整轮失败（DeepSeek V4 实录），故带思维链的旗舰
  只作备选并在标签注明，推荐位留给非思考/可关思考的型号，宁可少列。
* 下线模型不得列入（调用直接 404 → 生成链路首轮即报错再兜底）：
  DeepSeek deepseek-chat/reasoner、Kimi kimi-k2 / moonshot-v1 / kimi-k2.5、
  qwen-turbo 等。

DeepSeek 现状对齐（2026-09-11 官方 Pricing 页）
----------------------------------------------
官方模型 ID：

  deepseek-flash   → DeepSeek-V4.1-Flash（当前唯一主推；JSON/工具/视觉均支持）
  deepseek-v4-pro  → 2026-09-14 起有序退役，请求将改投 V4.1 Flash

遗留别名 deepseek-v4-flash / deepseek-v4-flash-vision-exp 仍可受理，
但已退役、按 Flash 计费，不再列入推荐位。

其他提供商（2026-09-11 官网核对）
--------------------------------
通义百炼：qwen3.8-flash / qwen3.7-plus / qwen3.8-max（滚动别名 qwen-flash/qwen-plus 仍可用）。
智谱 GLM：glm-5.3（旗舰）/ glm-5.3-flash（多模态）/ glm-4.7-flash（免费）/ glm-4.5-air。
Kimi：kimi-k3（旗舰，始终推理）/ kimi-k2.6 / kimi-k2.7-code。
OpenAI：gpt-5.6-chat-latest（非思考 Instant）/ gpt-5.6（推理旗舰）/
        gpt-5.5-mini（便宜）。

关键差异（直接影响本项目的结构化链路）：
* thinking 默认开启的型号，本项目对 DeepSeek 官方端点默认下发
  {"thinking":{"type":"disabled"}}；其他兼容端点不下发 thinking。
* 视觉实验型号是唯一收图的：纯文本模型收到 image 内容块会返回 400。
"""

PROVIDERS = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        # 该提供商可由服务端环境变量供 key（前端据此决定是否免输 Key）
        "env_key": "DEEPSEEK_API_KEY",
        "models": [
            # 官方现行 ID：deepseek-flash = V4.1-Flash（2026-09-11 Pricing）
            {"id": "deepseek-flash",
             "label": "deepseek-flash（V4.1 Flash，推荐）"},
            {"id": "deepseek-v4-pro",
             "label": "deepseek-v4-pro（将退役，改投 V4.1 Flash）"},
        ],
    },
    {
        "id": "qwen",
        "name": "通义千问 · 阿里",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": [
            # 百炼官方推荐三件套（2026-09-11 文本生成页）
            {"id": "qwen3.7-plus", "label": "qwen3.7-plus（均衡·Agent，推荐）"},
            {"id": "qwen3.8-flash", "label": "qwen3.8-flash（快/省）"},
            {"id": "qwen3.8-max", "label": "qwen3.8-max（旗舰）"},
        ],
    },
    {
        "id": "glm",
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": [
            {"id": "glm-5.3", "label": "glm-5.3（旗舰，推荐）"},
            {"id": "glm-5.3-flash", "label": "glm-5.3-flash（多模态）"},
            {"id": "glm-4.7-flash", "label": "glm-4.7-flash（免费文本）"},
            {"id": "glm-4.5-air", "label": "glm-4.5-air（轻量）"},
        ],
    },
    {
        "id": "kimi",
        "name": "Kimi · 月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "models": [
            # k3 始终推理（reasoning token 计入输出）；k2.6 适合 JSON 结构化
            {"id": "kimi-k2.6", "label": "kimi-k2.6（快/稳，推荐）"},
            {"id": "kimi-k3", "label": "kimi-k3（旗舰·始终推理）"},
            {"id": "kimi-k2.7-code", "label": "kimi-k2.7-code（代码/多模态）"},
        ],
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": [
            {"id": "gpt-5.6-chat-latest", "label": "gpt-5.6-chat（非思考·快，推荐）"},
            {"id": "gpt-5.6", "label": "gpt-5.6（旗舰·带推理）"},
            {"id": "gpt-5.5-mini", "label": "gpt-5.5-mini（便宜）"},
        ],
    },
    {
        # 自定义：用户自填 base_url + model（本地 Ollama / 其他兼容网关）
        "id": "custom",
        "name": "自定义 OpenAI 兼容端点…",
        "base_url": None,
        "models": [],
    },
]

_BY_ID = {p["id"]: p for p in PROVIDERS}

# 自定义 base_url 中允许使用 http://（而非 https://）的回环主机
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def get_provider(provider_id: str):
    """按 id 取预设；未知 id 返回 None。"""
    return _BY_ID.get(provider_id)


def public_catalog(deepseek_env_configured: bool) -> dict:
    """GET /api/providers 的响应负载（无任何敏感信息）。

    deepseek_env_configured 让前端知道：选 DeepSeek 时可免输 Key
    （服务端环境变量已供 key），其他提供商一律需要用户自己的 Key。"""
    return {
        "providers": [
            {
                "id": p["id"],
                "name": p["name"],
                # base_url 不透出（除 custom 外用户无需感知；少一个伪造面）
                "custom": p["id"] == "custom",
                "models": p["models"],
            }
            for p in PROVIDERS
        ],
        "deepseek_env_key": bool(deepseek_env_configured),
    }


def validate_custom_base_url(url: str) -> str:
    """校验自定义 base_url，返回规范化结果（去尾斜杠）；非法抛 ValueError。

    规则：必须 http(s)://；http:// 仅允许本机回环（Ollama 等本地端点）；
    长度 ≤200；不得含 query/fragment（避免参数注入到请求路径）。"""
    import urllib.parse
    url = (url or "").strip()
    if not url or len(url) > 200:
        raise ValueError("base_url 为空或超过 200 字符")
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("base_url 必须以 http:// 或 https:// 开头")
    if not u.hostname:
        raise ValueError("base_url 缺少主机名")
    if u.scheme == "http" and u.hostname.lower() not in _LOOPBACK_HOSTS:
        raise ValueError("http:// 仅允许本机回环地址（127.0.0.1/localhost）；"
                         "公网端点请用 https://")
    if u.query or u.fragment:
        raise ValueError("base_url 不得携带 query 或 #fragment")
    return url.rstrip("/")


def validate_model_name(model: str) -> str:
    """校验模型名（1~100 字符，限定字符集），返回原值；非法抛 ValueError。"""
    import re
    model = (model or "").strip()
    if not (1 <= len(model) <= 100):
        raise ValueError("model 为空或超过 100 字符")
    if not re.fullmatch(r"[A-Za-z0-9._:/\-]+", model):
        raise ValueError("model 含非法字符（仅允许字母数字 . _ : / -）")
    return model


def validate_api_key(key: str) -> str:
    """校验 API Key（1~256 字符、可打印 ASCII），返回原值；非法抛 ValueError。"""
    key = (key or "").strip()
    if not (1 <= len(key) <= 256):
        raise ValueError("api_key 为空或超过 256 字符")
    if any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("api_key 含不可见字符")
    return key
