#!/usr/bin/env python3
"""
计费模块测试。重点不是"能算数"，而是几条会伤到钱的规则：

  · 用户价 = 成本 × 倍率，**接口任何地方都不返回成本**
  · 同一笔调用重复投递只记一次（幂等键）
  · 余额扣减与分录同一事务；余额不足时付费命令被拦
  · 成本算不出来时记 no_price（不猜 0 元、不扣钱、但要能被运营看到）
  · 本地配音 / 分离 / 导出记为免费
  · 人工调整走同一账本，留下可追溯的 adjustment

跑：backend/.venv/bin/python tests/test_billing.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DASHSCOPE_API_KEY", "test")
os.environ.setdefault("LLM_API_KEY", "test")

from app import config

TMP = tempfile.mkdtemp(prefix="vvt-billing-")
config.DATA_DIR = TMP
config.BILLING_DB_PATH = os.path.join(TMP, "billing.sqlite3")
config.AUTH_DB_PATH = os.path.join(TMP, "auth.sqlite3")
config.BILLING_ADMIN_EMAILS = ("admin@example.com",)

from app.auth import db as authdb
from app.billing import db as bdb, pricing, service as billing
from app.billing.pricing import MICRO

authdb.init_db()          # 账本记在账号上，得先有账号表
fails: list[str] = []


def check(name, cond, detail=None):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  {detail!r}"))
    if not cond:
        fails.append(name)


def user(email="u1@example.com"):
    from app.auth import users
    from app.auth.google import GoogleProfile
    return users.upsert_google(GoogleProfile(subject=f"sub-{email}", email=email,
                                             email_verified=True, name=email, picture=None))


# ------------------------------------------------------------------ ① 定价
tts_usage = {"input_tokens": 1_000_000, "output_tokens": 0}
cost = pricing.cost_micro("tts", "qwen-audio-3.1-tts-flash", tts_usage)
check("① TTS 百万输入 token 成本 = ¥1.5", cost == round(1.5 * MICRO), cost)

_, price, ratio = pricing.price_micro("tts", "qwen-audio-3.1-tts-flash", tts_usage,
                                      markups={"tts": 2.0})
check("① 用户价 = 成本 × 倍率", price == cost * 2 and ratio == 2.0, (price, ratio))
check("① 倍率按步骤取（没配的步骤回落到默认）",
      pricing.markup_for("clone", {"tts": 3.0}) == pricing.DEFAULT_MARKUP["clone"])
check("① 倍率可用 * 兜底", pricing.markup_for("asr", {"*": 1.7}) == 1.7)

# 与内部成本台账的价格表必须完全一致（否则"成本"和"对外定价"会漂）
from app.services import usage_service  # noqa: E402
check("① 与 services/usage_service 的成本价格表完全一致",
      usage_service._PRICE_TABLE == pricing.COST_TABLE,
      set(usage_service._PRICE_TABLE) ^ set(pricing.COST_TABLE))

try:
    pricing.cost_micro("tts", "no-such-model", {"input_tokens": 10})
    ok = False
except pricing.NoCost:
    ok = True
check("① 没价格表的模型 → NoCost（绝不猜一个价）", ok)

try:
    pricing.cost_micro("tts", "qwen-audio-3.1-tts-flash", {})
    ok = False
except pricing.NoCost:
    ok = True
check("① 按 token 计价但没拿到用量 → NoCost（不记成 0 元）", ok)

check("① 免费步骤成本 0（浏览器配音 / 分离 / 导出）",
      all(pricing.cost_micro(s, "any", {}) == 0 for s in ("browser_tts", "separation", "export")))

check("① 供应商自报金额优先于我们的计算",
      pricing.cost_micro("tts", "qwen-audio-3.1-tts-flash", tts_usage,
                         api_cost={"amount": 0.42, "currency": "CNY"}) == round(0.42 * MICRO))

# ------------------------------------------------------------------ ② 记账与余额
u = user()
uid = u["id"]
check("② 新账户余额 0", billing.balance_micro(uid) == 0)

billing.adjust(uid, 10 * MICRO, note="充值 ¥10", actor="test")
check("② 充值后余额 ¥10", billing.balance_micro(uid) == 10 * MICRO)

r1 = billing.charge(user_id=uid, project_id="p1", step="tts", model="qwen-audio-3.1-tts-flash",
                    usage={"input_tokens": 1_000_000, "output_tokens": 0}, detail="seg 1")
check("② 记一笔：用户价 ¥3（成本 1.5 × 2）", r1["price_micro"] == 3 * MICRO, r1)
check("② 余额被扣到 ¥7", billing.balance_micro(uid) == 7 * MICRO, billing.balance_micro(uid))

r_dup = billing.charge(user_id=uid, project_id="p1", step="tts", model="qwen-audio-3.1-tts-flash",
                       usage={"input_tokens": 1_000_000}, idempotency_key="k1")
r_dup2 = billing.charge(user_id=uid, project_id="p1", step="tts", model="qwen-audio-3.1-tts-flash",
                        usage={"input_tokens": 1_000_000}, idempotency_key="k1")
check("② 同一幂等键重复投递只记一次（第二次返回 None）", r_dup is not None and r_dup2 is None, r_dup2)
check("② 重复投递没有重复扣钱", billing.balance_micro(uid) == 7 * MICRO - 3 * MICRO, billing.balance_micro(uid))

# 缺价：不扣钱，但要记下来
r_np = billing.charge(user_id=uid, project_id="p1", step="asr", model="fun-asr",
                      usage={"input_tokens": 1})
check("② 缺价调用记 no_price 且不收钱",
      r_np["cost_source"] == "no_price" and r_np["price_micro"] == 0, r_np)
ps = billing.project_summary("p1")
check("② 工程账单里点出未定价的调用数（供运营看，不对外）", ps["unpriced_calls"] == 1, ps)

# 免费步骤
before = billing.balance_micro(uid)
r_free = billing.charge(user_id=uid, project_id="p1", step="browser_tts", model="kokoro",
                        usage={"input_tokens": 0})
check("② 免费步骤记 0 元且标 free，余额不变",
      r_free["cost_source"] == "free" and r_free["price_micro"] == 0
      and billing.balance_micro(uid) == before, r_free)

# ------------------------------------------------------------------ ③ 余额拦截
u2 = user("poor@example.com")
try:
    billing.require_balance(u2["id"], 1 * MICRO, "配音")
    ok = False
except billing.BillingError as e:
    ok = "余额不足" in str(e) and "充值" in str(e)
check("③ 余额不足 → BillingError，消息说清差多少、怎么办", ok)

billing.adjust(u2["id"], 2 * MICRO, note="充值", actor="test")
billing.require_balance(u2["id"], 1 * MICRO, "配音")
check("③ 够钱就放行", True)
check("③ 没有归属账号的工程不阻塞（user_id 为 None）", billing.require_balance(None, 10 * MICRO, "x") is None)

# 透支：钱花超了会变负数，下一次就被拦住（别让任务中途假死）
billing.charge(user_id=u2["id"], project_id="p2", step="tts",
               model="qwen-audio-3.1-tts-flash", usage={"input_tokens": 1_000_000})
check("③ 花超了余额变负（活已经干完了，不能事后拒付）", billing.balance_micro(u2["id"]) < 0,
      billing.balance_micro(u2["id"]))
try:
    billing.require_balance(u2["id"], 1, "再来一次")
    ok = False
except billing.BillingError:
    ok = True
check("③ 透支后下一次任务被拦", ok)

# ------------------------------------------------------------------ ④ 汇总
s = billing.summary(uid)
check("④ 账户汇总：余额 / 本月已用 / 按步骤",
      s["balance_micro"] == billing.balance_micro(uid) and s["month_spent_micro"] > 0
      and any(x["step"] == "tts" for x in s["by_step"]), s)
check("④ 汇总不含成本字段（只有用户价）",
      all("cost_micro" not in x for x in s["by_step"]) and "cost_micro" not in s)
entries = billing.recent_entries(uid, limit=10)
check("④ 明细行不含成本列", entries and all("cost_micro" not in e for e in entries), entries[:1])

# ------------------------------------------------------------------ ⑤ 接口：能查、要登录、不泄露成本
from fastapi.testclient import TestClient          # noqa: E402
from app.main import app                            # noqa: E402
from _auth import login as tlogin, own as town      # noqa: E402

c = TestClient(app)
check("⑤ 未登录 → 401", c.get("/api/billing/summary").status_code == 401)
check("⑤ 未登录查单价 → 401", c.get("/api/billing/prices").status_code == 401)

tlogin(c, "http@example.com")
r = c.get("/api/billing/summary")
body = r.json() if r.status_code == 200 else {}
check("⑤ 登录后能查账户汇总", r.status_code == 200 and "balance_yuan" in body, (r.status_code, body))
check("⑤ 汇总里没有任何成本字段", not any("cost" in k for k in body), list(body))

r = c.get("/api/billing/prices")
pj = r.json() if r.status_code == 200 else {}
clone = next((x for x in pj.get("per_unit", []) if x["step"] == "clone"), None)
# 成本 0.01 × 倍率 2 = 0.02
check("⑤ 单价接口给的是含倍率的用户价（克隆 ¥0.02）",
      r.status_code == 200 and clone and clone["price_yuan"] == 0.02, pj)

from app import store as _store          # noqa: E402
from app.models import Segment, Speaker  # noqa: E402
_PID = "billing00000000000000000000000001"
_store.create(_PID, "x.mp4", __file__)
def _seed(st):
    st.target_language = "Chinese"; st.dubbing_mode = "remote"; st.stage = "ready"
    st.speakers = [Speaker("0", "S0")]
    st.segments = [Segment("s1", "0", 0.0, 2.0, "hi", translated_text="你好", kind="speech")]
_store.mutate(_PID, _seed, actor="system", cmd="seed", summary="seed")
town(_PID)

r = c.get(f"/api/billing/projects/{_PID}")
check("⑤ 工程账单可查且不含成本", r.status_code == 200 and "total_yuan" in r.json()
      and not any("cost" in k for k in r.json()), r.text[:120])
check("⑤ 别人的工程 → 404（不可枚举）",
      (tlogin(c, "other@example.com"), c.get(f"/api/billing/projects/{_PID}").status_code)[1] == 404)

tlogin(c, "http@example.com")
r = c.get("/api/billing/entries")
check("⑤ 明细接口不含成本列",
      r.status_code == 200 and all("cost_micro" not in e for e in r.json()["entries"]), r.text[:120])

check("⑤ 非管理员不能调余额", c.post("/api/billing/adjust",
      json={"user_id": uid, "amount_yuan": 5, "note": "x"}).status_code == 403)
tlogin(c, "admin@example.com")
r = c.post("/api/billing/adjust", json={"user_id": uid, "amount_yuan": 5, "note": "后台充值"})
check("⑤ 管理员可以调余额，返回新余额", r.status_code == 200 and r.json()["balance_yuan"] > 0, r.text[:120])

# ------------------------------------------------------------------ ⑥ 计费数据落在独立库
check("⑥ 计费库是独立文件（不和 auth 混）", os.path.exists(config.BILLING_DB_PATH)
      and config.BILLING_DB_PATH != config.AUTH_DB_PATH)

print()
if fails:
    print(f"合计 {len(fails)} 项失败: {', '.join(fails)}")
else:
    print("合计 全部通过")
sys.exit(1 if fails else 0)


def test_billing():
    assert not fails, fails
