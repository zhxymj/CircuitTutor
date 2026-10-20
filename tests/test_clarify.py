# -*- coding: utf-8 -*-
"""
test_clarify.py —— 需求澄清（判歧/合并/服务接口/CLI 追问）回归套件
==================================================================
覆盖四条线：
  1) 规则兜底判歧矩阵（清晰用例不得误伤，模糊用例必须命中）；
  2) LLM 结果规范化容错（坏形状安全降级，好形状保留并截断）；
  3) analyze 三路径（无 client→rules / LLM 好 JSON→llm / 坏 JSON·异常→rules）；
  4) 集成：server /api/clarify + /api/generate?c= 参数；CLI maybe_clarify 追问。

测试全程强制 config.API_KEY = ""（结束恢复），零 LLM 调用、可离线重放。
运行：python test_clarify.py
"""


# 测试位于 tests/ 子目录：先把项目根插入 sys.path 再 import 项目模块
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import http.client
import json
import threading
import urllib.parse

import ai2kicad as A
from core import clarify
from core import config
from core import llm
import server

PASS = 0


def check(name, cond):
    global PASS
    if not cond:
        raise AssertionError(f"失败：{name}")
    PASS += 1
    print(f"  ✔ {name}")


def has_question(result, needle):
    """判歧结果中是否存在题干含 needle 的问题。"""
    return any(needle in q["question"] for q in result["questions"])


def clear_of(result, needle):
    """该判歧结果不含 needle 相关提问（refined 限定词生效的判据）。"""
    return not has_question(result, needle)


class FakeClient:
    """假 DeepSeekClient：按预置返回固定文本或抛异常（不联网）。"""

    def __init__(self, raw=None, exc=None):
        self.raw, self.exc = raw, exc
        self.calls = 0

    def chat(self, *a, **k):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.raw


def main():
    saved_key = config.API_KEY
    config.API_KEY = ""          # 强制无 Key：analyze 必走规则兜底
    try:
        print("== 规则兜底判歧矩阵（清晰不得误伤 / 模糊必须命中）==")
        clear_cases = [
            "用5V电池经330Ω限流电阻点亮一个红色LED",
            "用555定时器做一个LED闪烁灯",      # 首页默认示例：绝不打扰
            "两个LED并联，共用一个限流电阻",
            "用12V继电器控制直流电机",          # 限定词+电压齐全
            "运放反相放大电路",
        ]
        for q in clear_cases:
            r = clarify.rule_analyze(q)
            check(f"清晰用例判 clear：{q}", r["status"] == "clear" and not r["questions"])

        r = clarify.rule_analyze("帮我画个电路")
        check("功能不明 -> clarify", r["status"] == "clarify")
        check("功能不明选项来自兜底模板",
              any("LED" in o or "555" in o for o in r["questions"][0]["options"]))

        r = clarify.rule_analyze("做一个电机控制电路")
        check("电机一词多义 -> 问电机类型",
              any("电机" in x["question"] for x in r["questions"]))
        check("功率负载缺供电 -> 问供电",
              any("供电" in x["question"] for x in r["questions"]))

        r = clarify.rule_analyze("用继电器控制灯泡")
        check("继电器缺供电 -> 问供电",
              r["status"] == "clarify"
              and any("供电" in x["question"] for x in r["questions"]))

        r = clarify.rule_analyze("直流电机调速")
        check("电机已限定 -> 不问电机类型",
              not any("指哪一种" in x["question"] for x in r["questions"]))
        check("但仍问供电（功率负载）",
              any("供电" in x["question"] for x in r["questions"]))

        r = clarify.rule_analyze("帮我用电机和开关做个东西")
        check("多歧义合并提问（电机+开关+供电）", len(r["questions"]) == 3)

        print("== 第④条：目录多候选歧义 ==")
        r = clarify.rule_analyze("放大器")
        check("裸词“放大器”判为需要澄清", r["status"] == "clarify")
        check("给出多候选提问（你想用哪一种）",
              any("你想用哪一种" in x["question"] for x in r["questions"]))
        opts = next(x["options"] for x in r["questions"] if "你想用哪一种" in x["question"])
        check("选项为得分前二的候选中文名（反相/同相比例放大器）",
              "反相比例放大器" in opts and "同相比例放大器" in opts)
        check("携带可读原因（含两类候选与分数）",
              any("都很接近" in x["reason"] for x in r["questions"]))
        from core.catalog import match_one_detail
        check("与 match_one_detail 判歧口径一致（同一查询同样 ambiguous）",
              match_one_detail("放大器")["ambiguous"] is True)
        check("满分明确需求“反相比例放大器”不澄清",
              clarify.rule_analyze("反相比例放大器")["status"] == "clear")
        check("长句假歧义回归：“用555定时器做一个LED闪烁灯”不澄清",
              clarify.rule_analyze("用555定时器做一个LED闪烁灯")["status"] == "clear")
        check("明确需求“画一个LED流水灯”不澄清",
              clarify.rule_analyze("画一个LED流水灯")["status"] == "clear")

        print("== 问题数量上限 ==")
        r = clarify.rule_analyze("帮我用电机和开关做个东西", max_questions=2)
        check("max_questions=2 生效", len(r["questions"]) == 2)
        r = clarify.rule_analyze("帮我用电机和开关做个东西")
        check("默认上限 3", len(r["questions"]) <= 3)
        r = clarify.rule_analyze("帮我用电机和开关做个东西", max_questions=0)
        check("max_questions=0 时不提问但仍返回 clear",
              r["status"] == "clear" and r["questions"] == [])
        r = clarify.rule_analyze("帮我用电机和开关做个东西", max_questions=1)
        check("max_questions=1 只保留第 1 条（电机歧义，顺序优先）",
              len(r["questions"]) == 1 and "电机" in r["questions"][0]["question"])

        print("== 问题/选项结构契约（前端与 CLI 共同依赖的字段形状） ==")
        r = clarify.rule_analyze("帮我用电机和开关做个东西")
        check("每个问题恰好含 question/options/reason 三键",
              all(set(q.keys()) == {"question", "options", "reason"}
                  for q in r["questions"]))
        check("question 与 reason 均为非空字符串",
              all(isinstance(q["question"], str) and q["question"].strip()
                  and q["reason"].strip() for q in r["questions"]))
        check("options 均为 2~MAX_OPTIONS 个非空字符串（可点选）",
              all(isinstance(q["options"], list)
                  and 2 <= len(q["options"]) <= clarify.MAX_OPTIONS
                  and all(isinstance(o, str) and o.strip() for o in q["options"])
                  for q in r["questions"]))
        check("同一问题内的选项无重复",
              all(len(set(q["options"])) == len(q["options"]) for q in r["questions"]))
        check("clear 结果不带问题（status 与 questions 自洽）",
              clarify.rule_analyze("用5V电池点亮LED")["questions"] == [])
        check("assumptions 字段恒存在且为空列表（占位契约不破）",
              all(clarify.rule_analyze(x)["assumptions"] == []
                  for x in ("用5V电池点亮LED", "帮我画个电路")))

        print("== 触发边界（防误伤 / 防漏报） ==")
        check("空白/纯空格需求 -> 必须触发功能不明（不得静默 clear）",
              clarify.rule_analyze("   ")["status"] == "clarify")
        check("空串需求 -> clarify",
              clarify.rule_analyze("")["status"] == "clarify")
        check("“一闪一闪的电路”无元件词 -> 功能不明（模板也不命中）",
              clarify.rule_analyze("一闪一闪的电路")["status"] == "clarify")
        check("「灯泡」已限定 → 灯歧义不触发（整句直接 clear）",
              clarify.rule_analyze("点亮一个灯泡")["status"] == "clear")
        check("「LED」已限定 → 灯歧义不触发",
              clarify.rule_analyze("点亮一个LED灯")["status"] == "clear")
        # —— 以下是 documented gap（当前行为记录，非期望行为）——
        # “指示灯”是 LED 的常见说法，但它不在 灯.refined 表里
        # （refined = led/LED/发光/灯泡/照明/数码），故仍会多余地问一次
        # “灯具体指什么”。本轮尺度为「只做不改行为的加固」，不改 refined
        # 表（会改变判歧输出），仅以断言锁定现状，避免日后无声漂移。
        check("[现状锁定]「指示灯」不在 灯.refined 表 → 仍会追问（已知多余问）",
              clarify.rule_analyze("点亮一个指示灯")["status"] == "clarify"
              and "“灯”" in clarify.rule_analyze("点亮一个指示灯")["questions"][0]["question"])
        check("[现状锁定]「led」在 refined 表内 → 不追问",
              "led" in next(t["refined"] for t in clarify._AMBIGUOUS_TERMS
                            if t["term"] == "灯"))
        check("「按钮」已限定 → 开关歧义不触发",
              clear_of(clarify.rule_analyze("按钮控制LED"), "“开关”"))
        check("「拨动开关」已限定 → 开关歧义不触发",
              clear_of(clarify.rule_analyze("拨动开关控制LED"), "“开关”"))
        check("「舵机」已限定 → 电机歧义不触发（refined 词生效）",
              not any("“电机”" in q["question"]
                      for q in clarify.rule_analyze("用舵机做个东西")["questions"]))
        check("「步进」已限定 → 电机歧义不触发",
              not any("“电机”" in q["question"]
                      for q in clarify.rule_analyze("步进电机")["questions"]))
        check("「直流」已限定 → 电机歧义不触发",
              not any("“电机”" in q["question"]
                      for q in clarify.rule_analyze("直流电机调速")["questions"]))
        check("供电线索“5V”存在 → 不再追问供电",
              not any("供电" in q["question"]
                      for q in clarify.rule_analyze("5V供电的直流电机调速")["questions"]))
        check("供电线索“USB”存在 → 不再追问供电",
              not any("供电" in q["question"]
                      for q in clarify.rule_analyze("USB给直流电机供电")["questions"]))
        check("供电线索“电池”存在 → 不再追问供电",
              not any("供电" in q["question"]
                      for q in clarify.rule_analyze("电池驱动直流电机")["questions"]))
        check("功率负载+无供电线索 → 必须追问供电（漏报防线）",
              any("供电" in q["question"]
                  for q in clarify.rule_analyze("用继电器控制直流电机")["questions"]))
        check("舵机（功率负载）+无供电线索 → 仍追问供电（refined 只管歧义、不管供电）",
              any("供电" in q["question"]
                  for q in clarify.rule_analyze("用舵机做个东西")["questions"]))
        check("LED 非功率负载 → 不问供电（LED 默认 5V 惯例）",
              not any("供电" in q["question"]
                      for q in clarify.rule_analyze("点亮一个LED")["questions"]))
        check("需求已含电源类元件（蓄电池）→ 不问供电",
              not any("供电" in q["question"]
                      for q in clarify.rule_analyze(
                          "用蓄电池给直流电机供电")["questions"]))
        check("判歧问题顺序稳定（同输入同输出，不受字典序影响）",
              [q["question"] for q in clarify.rule_analyze("帮我用电机和开关做个东西")["questions"]]
              == ["需求中的“电机”指哪一种？", "“开关”想用哪种实现？", "电路用什么供电？"])

        print("== merge_clarifications 行为契约 ==")
        m1 = clarify.merge_clarifications("做一个电机控制电路", [
            {"question": "电机指哪一种？", "answer": "舵机（角度控制）"}])
        check("合并格式：原需求 + 换行 + “补充说明：”前缀",
              m1.startswith("做一个电机控制电路\n补充说明："))
        check("问答以“问题——答案”拼接",
              "电机指哪一种？——舵机（角度控制）" in m1)
        m2 = clarify.merge_clarifications("做一个电机控制电路", [
            {"question": "电机指哪一种？", "answer": "舵机"},
            {"question": "供电？", "answer": "12V"}])
        check("多问答以“；”分隔", "；" in m2 and "舵机" in m2 and "12V" in m2)
        check("问答顺序与原列表一致（舵机在前、12V 在后）",
              m2.index("舵机") < m2.index("12V"))
        m3 = clarify.merge_clarifications("做一个电机控制电路", [
            {"question": "", "answer": "只有答案"}])
        check("问题为空时只并入答案文本（不留破折号）",
              m3.endswith("只有答案") and "——" not in m3)
        check("qa_pairs 非 list（None）原样返回",
              clarify.merge_clarifications("abc", None) == "abc")
        check("qa_pairs 为空列表原样返回",
              clarify.merge_clarifications("abc", []) == "abc")
        check("答案首尾空白被清理后并入",
              "5V" in clarify.merge_clarifications(
                  "abc", [{"question": "q", "answer": "  5V  "}]))
        check("答案内换行折叠为单行（不破坏下游行结构）",
              "\n补充说明：q——A B" in
              clarify.merge_clarifications("x", [{"question": "q", "answer": "A\nB"}]))
        m4 = clarify.merge_clarifications("x", [{"question": "q", "answer": "a" * 400}],
                                          max_len=100)
        check("截断发生在合并结果尾部（保留头部原需求）", m4.startswith("x\n补充说明："))
        check("截断长度严格不超过 max_len", len(m4) == 100)
        check("默认 max_len 对齐 config.MAX_QUERY_LEN",
              len(clarify.merge_clarifications(
                  "x" * 10, [{"question": "q", "answer": "a" * 900}]))
              <= config.MAX_QUERY_LEN)
        check("默认 max_len 实参即 config.MAX_QUERY_LEN",
              clarify.merge_clarifications.__defaults__[0] == 500
              and config.MAX_QUERY_LEN == 500)
        check("超长但未达 max_len 时不截断",
              clarify.merge_clarifications("abc", [{"question": "q", "answer": "z"}])
              == "abc\n补充说明：q——z")

        print("== 问答合并 merge_clarifications ==")
        m = clarify.merge_clarifications("做一个电机控制电路", [
            {"question": "电机指哪一种？", "answer": "舵机（角度控制）"},
            {"question": "供电？", "answer": ""},          # 空答案：不擅自假设
            {"question": "", "answer": "5V"},
            "junk",                                        # 非 dict 项跳过
        ])
        check("答案并入需求文本", "舵机" in m and "5V" in m)
        check("空答案问题不出现", "供电" not in m)
        check("原需求保留在头部", m.startswith("做一个电机控制电路"))
        check("无有效答复 -> 原样返回",
              clarify.merge_clarifications("abc", [{"question": "q", "answer": ""}]) == "abc")
        long_m = clarify.merge_clarifications("x", [{"question": "q", "answer": "a" * 400}],
                                              max_len=100)
        check("合并超长截断到 max_len", len(long_m) <= 100)

        print("== LLM 结果规范化容错 ==")
        check("非 dict -> None", clarify.normalize_llm_result("junk") is None)
        check("status 非法 -> None", clarify.normalize_llm_result({"status": "weird"}) is None)
        check("questions 非 list -> None",
              clarify.normalize_llm_result({"status": "clarify", "questions": "nope"}) is None)
        check("全部问题非法 -> None",
              clarify.normalize_llm_result({"status": "clarify", "questions": [{"x": 1}]}) is None)
        n = clarify.normalize_llm_result({"status": "clear", "extra": 1})
        check("clear 规范化", n == {"status": "clear", "questions": [], "assumptions": []})
        n = clarify.normalize_llm_result({"status": "clarify", "questions": [
            {"question": "电机指哪一种？",
             "options": ["直流", "舵机", "直流", 1, "x" * 100], "reason": "一词多义"},
            {"question": "  "},                       # 空问题剔除
            {"question": "供电？"},
            {"question": "频率？"},                     # 超出 cap 截断
        ]}, max_questions=3)
        check("非法项剔除 + 保留合法项", n is not None and len(n["questions"]) == 3)
        check("选项去重/去非字符串/限长",
              n["questions"][0]["options"] == ["直流", "舵机", "x" * 60])

        print("== analyze 三路径 ==")
        r = clarify.analyze("帮我画个电路")
        check("无 client -> rules 兜底",
              r["source"] == "rules" and r["status"] == "clarify")
        r = clarify.analyze("做一个电机控制电路", FakeClient(
            '{"status":"clarify","questions":[{"question":"电机指哪一种？",'
            '"options":["直流电机","舵机"],"reason":"一词多义"}]}'))
        check("LLM 好 JSON -> 直接采用（source=llm）",
              r["source"] == "llm" and r["status"] == "clarify"
              and r["questions"][0]["options"] == ["直流电机", "舵机"])
        r = clarify.analyze("做一个电机控制电路", FakeClient("这不是JSON"))
        check("LLM 坏 JSON -> 落规则兜底", r["source"] == "rules" and r["status"] == "clarify")
        r = clarify.analyze("用5V电池点亮LED", FakeClient(exc=RuntimeError("network down")))
        check("LLM 异常 -> 落规则兜底", r["source"] == "rules" and r["status"] == "clear")
        fc = FakeClient('{"status":"clear"}')
        budget = llm.CallBudget(max_calls=1)
        clarify.analyze("任意需求", fc, budget)
        check("判歧消耗独立预算 1 次", budget.used == 1 and fc.calls == 1)

        print("== CLARIFY_PROMPT 自检 ==")
        p = clarify.CLARIFY_PROMPT.format(max_q=3)
        check("format 通过且含 JSON 字样（json_mode 接口约束）", "JSON" in p)
        check("含最多问题数注入", "3" in p)

        print("== server 集成：/api/clarify 与 /api/generate?c= ==")
        server.Handler.log_message = lambda self, fmt, *a: None
        srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def post(path, obj):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                c.request("POST", path, json.dumps(obj),
                          {"Content-Type": "application/json"})
                resp = c.getresponse()
                body = resp.read()
                c.close()
                return resp.status, json.loads(body)

            st, r = post("/api/clarify", {"query": "做一个电机控制电路"})
            check("模糊需求 -> 200 clarify（rules）",
                  st == 200 and r["status"] == "clarify" and r["source"] == "rules"
                  and len(r["questions"]) >= 2)
            st, r = post("/api/clarify", {"query": "用555定时器做一个LED闪烁灯"})
            check("清晰需求 -> 200 clear", st == 200 and r["status"] == "clear")
            st, _ = post("/api/clarify", {"query": ""})
            check("空 query -> 400", st == 400)
            st, _ = post("/api/clarify", {"query": "x" * (config.MAX_QUERY_LEN + 1)})
            check("超长 query -> 400", st == 400)

            qa = json.dumps([{"question": "电机指哪一种？", "answer": "舵机"}],
                            ensure_ascii=False)
            path = ("/api/generate?q=" + urllib.parse.quote("做一个电机控制电路")
                    + "&c=" + urllib.parse.quote(qa))
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            c.request("GET", path)
            body = c.getresponse().read().decode("utf-8")
            c.close()
            check("部分 c 问答仍缺供电时继续澄清，不生成电路",
                  "event: circuit" not in body and "event: done" in body
                  and '"rejected": true' in body and '"questions": [' in body)

            qa = json.dumps([{"question": "供电电压？", "answer": "5V"}],
                            ensure_ascii=False)
            path = ("/api/generate?q=" + urllib.parse.quote("用470欧姆电阻串联红色LED做限流指示灯")
                    + "&c=" + urllib.parse.quote(qa))
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
            c.request("GET", path)
            body = c.getresponse().read().decode("utf-8")
            c.close()
            circuit_event = next((json.loads(block.split("data: ", 1)[1])
                                  for block in body.split("\n\n")
                                  if block.startswith("event: circuit\n")), None)
            check("完整 c 问答生成匹配模板并保留供电电压",
                  circuit_event is not None and "event: done" in body
                  and any(part["type"] == "battery" and "5" in part.get("attrs", {}).get("value", "")
                          for part in circuit_event["parts"]))

            path = "/api/generate?q=" + urllib.parse.quote("LED限流") + "&c=%7Bbad"
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            c.request("GET", path)
            resp = c.getresponse()
            body = resp.read().decode("utf-8")
            c.close()
            check("非法 c 静默忽略（不 500）",
                  resp.status == 200 and "event: circuit" in body)
        finally:
            srv.shutdown()

        print("== CLI maybe_clarify 追问 ==")
        q0, qa0 = A.maybe_clarify("做一个电机控制电路", no_clarify=True)
        check("--no-clarify 原样返回", q0 == "做一个电机控制电路" and qa0 == [])
        q1, qa1 = A.maybe_clarify("做一个电机控制电路", interactive=False)
        check("非 TTY 自动跳过（脚本/CI 不阻塞）", q1 == "做一个电机控制电路" and qa1 == [])

        answers = iter(["2", ""])          # 第 1 题选编号 2（舵机），第 2 题回车跳过
        q2, qa2 = A.maybe_clarify("做一个电机控制电路", interactive=True,
                                  verbose=False, input_fn=lambda _p: next(answers))
        check("编号选择并入需求", "舵机" in q2 and len(qa2) == 1)
        check("回车跳过不作答（不擅自假设）", all("供电" not in qa["question"] for qa in qa2))

        answers3 = iter(["自定义：步进电机", "12V"])
        q3, qa3 = A.maybe_clarify("做一个电机控制电路", interactive=True,
                                  verbose=False, input_fn=lambda _p: next(answers3))
        check("自由文本作答并入需求", "步进电机" in q3 and "12V" in q3 and len(qa3) == 2)

        def _eof(_p):
            raise EOFError
        q4, qa4 = A.maybe_clarify("做一个电机控制电路", interactive=True,
                                  verbose=False, input_fn=_eof)
        check("EOF 输入流结束按未作答处理（不炸）", q4 == "做一个电机控制电路" and qa4 == [])

        q5, qa5 = A.maybe_clarify("用555定时器做一个LED闪烁灯", interactive=True,
                                  verbose=False, input_fn=lambda _p: "1")
        check("清晰需求直接放行（不多余确认）", q5 == "用555定时器做一个LED闪烁灯" and qa5 == [])
    finally:
        config.API_KEY = saved_key

    print(f"\n全部通过 ✔  共 {PASS} 项断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
