"""内置演示电路数据层：circuit_templates.json 加载 + 兜底模板路由表。

从 server.py 拆出（2026-09-09）：原先 ai2kicad.py 为了拿这两份数据
不得不 import 整个 HTTP 服务模块（耦合重、还触发全局静态缓存初始化）。
现在 server 与 ai2kicad 共用本模块；server 继续再导出同名属性，
对外接口（server.BUILTIN_TEMPLATES / server.FALLBACK_TEMPLATES）保持不变。
"""

import json
import os

# 数据文件在项目根的 data/ 目录（core/ 的上级）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")


def _load_demo_circuits() -> dict:
    """加载内置兜底模板；文件缺失/损坏时给出可定位的启动报错。"""
    path = os.path.join(_DATA_DIR, "circuit_templates.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(
            f"启动失败：无法加载内置模板 {path}（{type(e).__name__}: {e}）。"
            f"该文件随项目分发，请检查是否被移动或损坏。")


BUILTIN_TEMPLATES = _load_demo_circuits()

# 兜底模板路由：复用 catalog.py 的负向关键词匹配机制
# （仅在不走 LLM 时生效：未配置 Key / LLM 失败；有 Key 的正常生成仍走完整流水线）
FALLBACK_TEMPLATES = {
    "ne555_blink": {"aliases": ["555", "闪烁", "多谐振荡", "定时器"], "negative": []},
    "opamp_inv": {"aliases": ["反相", "比例放大", "放大器", "运放"],
                  "negative": ["同相", "跟随", "比较"]},
    # led_limit 加负向词：match_template 按子串满分、先到先得，若不加，
    # “两个LED并联”里的 “led” 会让 led_limit 抢走 parallel_led 的请求。
    "led_limit": {"aliases": ["led", "发光二极管", "限流"],
                  "negative": ["并联", "并行", "双", "两个", "多路"]},
    "pot_divider": {"aliases": ["电位器", "分压", "可调电压", "可调电阻", "调光"],
                    "negative": []},
    "button_buzzer": {"aliases": ["蜂鸣器", "报警", "喇叭"], "negative": []},
    "npn_switch": {"aliases": ["三极管", "npn", "开关电路"], "negative": ["pnp"]},
    "diode_protect": {"aliases": ["防反接", "反接保护", "整流"],
                      "negative": ["稳压", "发光"]},
    "zener_reg": {"aliases": ["稳压二极管", "齐纳"], "negative": []},
    # parallel_led 放在表末尾：与 pot_divider 等既有模板同分（都 1.0）时，
    # 既有模板先迭代先当选（严格大于才替换），“并联电阻分压”仍归 pot_divider。
    "parallel_led": {"aliases": ["并联", "并行", "两个led", "双led", "多路"],
                     "negative": ["串联"]},
    # ---- 拓扑芯片教学模板（2026-09-12 新增）----
    "current_mirror": {"aliases": ["电流镜", "镜像电流", "电流源镜像"],
                       "negative": []},
    "differential_amp": {"aliases": ["差分", "差动", "长尾对", "差分放大"],
                         "negative": []},
    "common_emitter": {"aliases": ["共射", "共发射极", "电压放大", "三极管放大"],
                       "negative": ["开关", "电流镜", "差分"]},
    "voltage_doubler": {"aliases": ["倍压", "倍压整流", "电压倍增"],
                        "negative": []},
    "integrator": {"aliases": ["积分", "积分器", "积分电路"],
                   "negative": ["微分"]},
    "schmitt_trigger": {"aliases": ["施密特", "迟滞比较", "波形整形"],
                        "negative": []},
    "peak_detector": {"aliases": ["峰值检测", "峰值检波", "包络检波"],
                      "negative": []},
    "voltage_reference": {"aliases": ["基准电压", "电压基准", "参考电压", "稳压管"],
                          "negative": ["限流", "放大"]},
    "current_source": {"aliases": ["恒流源", "恒流", "电流调节"],
                       "negative": ["电流镜"]},
    "crystal_osc": {"aliases": ["晶振", "晶体振荡", "皮尔斯", "时钟振荡"],
                    "negative": ["555"]},
}
