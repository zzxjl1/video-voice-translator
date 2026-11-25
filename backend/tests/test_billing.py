#!/usr/bin/env python3
"""
计费模块测试。重点不是"能算数"，而是几条会伤到钱的规则：

  · 积分 = 成本（元） × 倍率 × 100；接口任何地方都不返回成本
  · 同一笔调用重复投递只记一次（幂等键）
  · 积分扣减与分录同一事务；积分不足时付费命令被拦
  · 成本算不出来时记 no_price（不猜 0 积分、不扣、但要能被运营看到）
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
# 前几节测的是"从 0 开始"的账本行为；新用户赠送留到 ⑥ 单独测。
config.BILLING_SIGNUP_BONUS_POINTS = 0

from app.auth import db as authdb
from app.billing import pricing, service as billing
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


def pts(pm: int) -> float:
    return pricing.as_points(pm)


# ------------------------------------------------------------------ ① 定价：积分 = 成本 × 倍率 × 100
one_yuan_of_tts = {"input_tokens": 666_667}      # ≈ ¥1.0 成本
cost = pricing.cost_micro("tts", "qwen-audio-3.1-tts-flash", one_yuan_of_tts)
check("① TTS 成本按 1.5 元/百万输入 token 算", abs(cost - 1_000_000) < 2_000, cost)

check("① 积分 = micro 元 × 倍率 × 100",
      pricing.points_micro(1_000_000, 2.0) == 200 * MICRO,
      pricing.points_micro(1_000_000, 2.0) / MICRO)
check("① 1 积分 = 0.01 元（倍率 1 时成本 1 元 = 100 积分）",
      pricing.points_micro(1_000_000, 1.0) == 100 * MICRO)

_, price, ratio = pricing.price_micro("tts", "qwen-audio-3.1-tts-flash", one_yuan_of_tts,
                                      markups={"tts": 2.0})
check("① 倍率 2：¥1 成本 → 200 积分", abs(price / MICRO - 200) < 1 and ratio == 2.0, price / MICRO)
check("① 倍率按步骤取（没配的步骤回落到默认）",
      pricing.markup_for("clone", {"tts": 3.0}) == pricing.DEFAULT_MARKUP["clone"])
check("① 倍率可用 * 兜底", pricing.markup_for("asr", {"*": 1.7}) == 1.7)

# 与内部成本台账的价格表必须完全一致
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
check("① 按 token 计价但没拿到用量 → NoCost（不记成 0）", ok)

check("① 免费步骤 0 成本（浏览器配音 / 分离 / 导出）",
      all(pricing.cost_micro(s, "any", {}) == 0 for s in ("browser_tts", "separation", "export")))

check("① 供应商自报金额优先于我们的计算",
      pricing.cost_micro("tts", "qwen-audio-3.1-tts-flash", one_yuan_of_tts,
                         api_cost={"amount": 0.42, "currency": "CNY"}) == round(0.42 * MICRO))

# ------------------------------------------------------------------ ② 记账与余额
u = user()
uid = u["id"]
check("② 新账户余额 0 积分", billing.balance_points_micro(uid) == 0)

billing.adjust(uid, 1_000 * MICRO, note="充值 1000 积分", actor="test")
check("② 充值后 1000 积分", billing.balance_points_micro(uid) == 1_000 * MICRO)

r1 = billing.charge(user_id=uid, project_id="p1", step="tts", model="qwen-audio-3.1-tts-flash",
                    usage=one_yuan_of_tts, detail="seg 1")
check("② 记一笔：¥1 成本 × 2 倍率 = 200 积分", abs(r1["points"] - 200) < 1, r1)
check("② 余额被扣到 800 积分", abs(pts(billing.balance_points_micro(uid)) - 800) < 1,
      pts(billing.balance_points_micro(uid)))

r_dup = billing.charge(user_id=uid, project_id="p1", step="tts", model="qwen-audio-3.1-tts-flash",
                       usage=one_yuan_of_tts, idempotency_key="k1")
r_dup2 = billing.charge(user_id=uid, project_id="p1", step="tts", model="qwen-audio-3.1-tts-flash",
                        usage=one_yuan_of_tts, idempotency_key="k1")
check("② 同一幂等键重复投递只记一次（第二次返回 None）", r_dup is not None and r_dup2 is None, r_dup2)
before = billing.balance_points_micro(uid)
billing.charge(user_id=uid, project_id="p1", step="tts", model="qwen-audio-3.1-tts-flash",
               usage=one_yuan_of_tts, idempotency_key="k1")
check("② 重复投递没有重复扣积分", billing.balance_points_micro(uid) == before)

# 缺价：不扣，但要记下来
r_np = billing.charge(user_id=uid, project_id="p1", step="asr", model="fun-asr",
                      usage={"input_tokens": 1})
check("② 缺价调用记 no_price 且不扣积分",
      r_np["cost_source"] == "no_price" and r_np["points_micro"] == 0, r_np)
ps = billing.project_summary("p1")
check("② 工程账单里点出未定价的调用数（供运营看，不对外）", ps["unpriced_calls"] == 1, ps)

# 免费步骤
before = billing.balance_points_micro(uid)
r_free = billing.charge(user_id=uid, project_id="p1", step="browser_tts", model="kokoro")
check("② 免费步骤 0 积分且标 free，余额不变",
      r_free["cost_source"] == "free" and r_free["points_micro"] == 0
      and billing.balance_points_micro(uid) == before, r_free)

# ------------------------------------------------------------------ ③ 余额拦截（单位是积分）
u2 = user("poor@example.com")
try:
    billing.require_balance(u2["id"], 100 * MICRO, "配音")
    ok = False
except billing.BillingError as e:
    ok = "积分不足" in str(e) and "充值" in str(e)
check("③ 积分不足 → BillingError，消息说清差多少、怎么办", ok)

billing.adjust(u2["id"], 200 * MICRO, note="充值", actor="test")
billing.require_balance(u2["id"], 100 * MICRO, "配音")
check("③ 够就放行", True)
check("③ 没有归属账号的工程不阻塞（user_id 为 None）",
      billing.require_balance(None, 10 * MICRO, "x") is None)

# 透支：用完会变负，下一次被拦（别让任务中途假死）
billing.charge(user_id=u2["id"], project_id="p2", step="tts",
               model="qwen-audio-3.1-tts-flash", usage={"input_tokens": 2_000_000})
check("③ 花超了余额变负（活已经干完了，不能事后拒付）", billing.balance_points_micro(u2["id"]) < 0,
      pts(billing.balance_points_micro(u2["id"])))
try:
    billing.require_balance(u2["id"], 1, "再来一次")
    ok = False
except billing.BillingError:
    ok = True
check("③ 透支后下一次任务被拦", ok)

# ------------------------------------------------------------------ ④ 汇总（单位积分，不含成本）
s = billing.summary(uid)
check("④ 账户汇总：余额 / 本月已用 / 按步骤",
      s["balance_points_micro"] == billing.balance_points_micro(uid) and s["month_points_micro"] > 0
      and any(x["step"] == "tts" for x in s["by_step"]), s)
check("④ 汇总不含成本字段（只有积分）",
      all("cost" not in k for x in s["by_step"] for k in x) and all("cost" not in k for k in s), s)
entries = billing.recent_entries(uid, limit=10)
check("④ 明细行不含成本列", entries and all("cost_micro" not in e for e in entries), entries[:1])

# ------------------------------------------------------------------ ⑤ 接口
from fastapi.testclient import TestClient          # noqa: E402
from app.main import app                            # noqa: E402
from _auth import login as tlogin, own as town      # noqa: E402

c = TestClient(app)
check("⑤ 未登录 → 401", c.get("/api/billing/summary").status_code == 401)
check("⑤ 未登录查单价 → 401", c.get("/api/billing/prices").status_code == 401)

tlogin(c, "http@example.com")
r = c.get("/api/billing/summary")
body = r.json() if r.status_code == 200 else {}
check("⑤ 登录后能查账户汇总（单位=积分）",
      r.status_code == 200 and "balance_points" in body and body.get("unit") == "points", body)
check("⑤ 汇总里没有任何成本字段", not any("cost" in k for k in body), list(body))

r = c.get("/api/billing/prices")
pj = r.json() if r.status_code == 200 else {}
clone = next((x for x in pj.get("per_unit", []) if x["step"] == "clone"), None)
# 成本 0.01 元 × 倍率 2 × 100 = 2 积分
check("⑤ 单价接口给的是含倍率的积分（克隆 2 积分）",
      r.status_code == 200 and clone and abs(clone["points"] - 2) < 1e-6, pj)

from app import store as _store          # noqa: E402
from app.models import Segment, Speaker  # noqa: E402
_PID = "billing00000000000000000000000001"
_store.create(_PID, "x.mp4", __file__)


def _seed(st):
    st.target_language = "Chinese"
    st.dubbing_mode = "remote"
    st.stage = "ready"
    st.speakers = [Speaker("0", "S0")]
    st.segments = [Segment("s1", "0", 0.0, 2.0, "hi", translated_text="你好", kind="speech")]


_store.mutate(_PID, _seed, actor="system", cmd="seed", summary="seed")
town(_PID)

r = c.get(f"/api/billing/projects/{_PID}")
check("⑤ 工程账单可查且不含成本",
      r.status_code == 200 and "total_points" in r.json()
      and not any("cost" in k for k in r.json()), r.text[:120])
check("⑤ 别人的工程 → 404（不可枚举）",
      (tlogin(c, "other@example.com"), c.get(f"/api/billing/projects/{_PID}").status_code)[1] == 404)

tlogin(c, "http@example.com")
r = c.get("/api/billing/entries")
check("⑤ 明细接口不含成本列",
      r.status_code == 200 and all("cost_micro" not in e for e in r.json()["entries"]), r.text[:120])

check("⑤ 非管理员不能调积分", c.post("/api/billing/adjust",
      json={"user_id": uid, "amount_points": 100, "note": "x"}).status_code == 403)
tlogin(c, "admin@example.com")
before_admin = billing.balance_points_micro(uid)
r = c.post("/api/billing/adjust", json={"user_id": uid, "amount_points": 500, "note": "后台充值"})
after_admin = billing.balance_points_micro(uid)
check("⑤ 管理员可以调积分，返回新余额", r.status_code == 200 and r.json()["balance_points"] > 0, r.text[:120])
check("⑤ 调整单位是积分：充 500 积分余额正好 +500",
      after_admin - before_admin == 500 * MICRO, (before_admin, after_admin))

# ------------------------------------------------------------------ ⑥ 新用户赠送积分
from app.auth import users as auth_users  # noqa: E402
from app.auth.google import GoogleProfile  # noqa: E402


def _signup(email, subject=None):
    return auth_users.upsert_google(GoogleProfile(
        subject=subject or f"sub-{email}", email=email, email_verified=True, name="N", picture=None))


config.BILLING_SIGNUP_BONUS_POINTS = 500
nu = _signup("bonus@example.com")
check("⑥ 新用户首次登录送 500 积分",
      abs(pts(billing.balance_points_micro(nu["id"])) - 500) < 1e-6,
      pts(billing.balance_points_micro(nu["id"])))

_same = _signup("bonus@example.com")           # 同一 Google subject 再登录
check("⑥ 再次登录不再送（总额仍是 500）",
      abs(pts(billing.balance_points_micro(nu["id"])) - 500) < 1e-6,
      pts(billing.balance_points_micro(nu["id"])))
check("⑥ 同一个账号对象", _same["id"] == nu["id"])

other = _signup("bonus2@example.com")
check("⑥ 另一个新用户各自送一次", abs(pts(billing.balance_points_micro(other["id"])) - 500) < 1e-6)

check("⑥ 重复调用赠送接口是幂等的（发过了就返回 None）",
      billing.grant_signup_bonus(nu["id"]) is None
      and abs(pts(billing.balance_points_micro(nu["id"])) - 500) < 1e-6)

# 并发首次登录：不能发两次（固定主键 + INSERT OR IGNORE）
import concurrent.futures as _cf  # noqa: E402
race = _signup("race@example.com")
with _cf.ThreadPoolExecutor(8) as ex:
    list(ex.map(lambda _: billing.grant_signup_bonus(race["id"]), range(8)))
check("⑥ 并发重复赠送只成功一次",
      abs(pts(billing.balance_points_micro(race["id"])) - 500) < 1e-6,
      pts(billing.balance_points_micro(race["id"])))

# 关掉赠送时不影响已有用户
config.BILLING_SIGNUP_BONUS_POINTS = 0
zero = _signup("nobonus@example.com")
check("⑥ 赠送设为 0 时新用户余额为 0", pts(billing.balance_points_micro(zero["id"])) == 0,
      pts(billing.balance_points_micro(zero["id"])))
config.BILLING_SIGNUP_BONUS_POINTS = 500

# ------------------------------------------------------------------ ⑦ 初始化费用估算
from app.billing import estimate as est  # noqa: E402

e = est.estimate_init_points(34, speakers=3)
# 校准基准（三个独立来源）：
#  · 账本一次干净初始化（34 秒 / 9 句 / 3 克隆，去掉 chat/选材/重跑）：LLM 关 thinking 后 ≈ 12
#  · omni 音频 6 token/秒（账本 prompt_tokens_details），30 分钟 ≈ 11
#  · TTS 官方实测 ¥0.0095/分钟音频 → 30 分钟 ≈ 58 积分；账本每次调用输出 ≈ 45 token
check("⑦ 34 秒视频估算贴近真实（≈12，允许 0.8–1.5×）", 12 * 0.8 <= e["points"] <= 12 * 1.5, e["points"])
e30 = est.estimate_init_points(1800, speakers=3)
check("⑦ 30 分钟 ≈ 230 积分（¥2.3）；LLM 是最大头，TTS 不是", 180 <= e30["points"] <= 300, e30["points"])
check("⑦ omni 30 分钟 ≈ 11（按音频秒，不是按句）", 8 <= e30["breakdown"]["omni"] <= 16, e30["breakdown"]["omni"])
check("⑦ TTS 30 分钟 ≈ 77（与官方 ¥0.0095/分钟一致）", 55 <= e30["breakdown"]["tts"] <= 100, e30["breakdown"]["tts"])
check("⑦ LLM 30 分钟 ≈ 130（已按关 thinking 估）", 110 <= e30["breakdown"]["llm"] <= 160, e30["breakdown"]["llm"])
e_off = est.estimate_init_points(34, dubbing_mode="off")
check("⑦ off 模式不估克隆与 TTS", "clone" not in e_off["breakdown"] and "tts" not in e_off["breakdown"], e_off["breakdown"])
e_br = est.estimate_init_points(34, dubbing_mode="browser")
check("⑦ browser 模式不估远端 TTS/克隆（本地免费）", "tts" not in e_br["breakdown"] and "clone" not in e_br["breakdown"], e_br["breakdown"])
e_nomm = est.estimate_init_points(34, mm_enhance=False)
check("⑦ 关掉审听不估 omni", "omni" not in e_nomm["breakdown"], e_nomm["breakdown"])
check("⑦ 估算随时长单调递增",
      est.estimate_init_points(600)["points_micro"] > est.estimate_init_points(60)["points_micro"] > est.estimate_init_points(34)["points_micro"])
check("⑦ 时长为 0 不炸", est.estimate_init_points(0)["points_micro"] >= 0)
# 固定 100 积分门槛对短片够用，但长视频会把它跑穿——按时长估才有意义
check("⑦ 10 分钟 ≈ 82（固定 100 够用）；30 分钟 ≈ 231（固定 100 不够）",
      est.estimate_init_points(600)["points_micro"] < 100 * MICRO < est.estimate_init_points(1800)["points_micro"],
      (est.estimate_init_points(600)["points"], est.estimate_init_points(1800)["points"]))


# 上传超长被拒 + 时长写进工程
from app import store as _store2  # noqa: E402
from app.models import video_dir_path as _vdp2  # noqa: E402
_LONG = "longvideo0000000000000000000000aa"
_SHORT = "shortvideo0000000000000000000000aa"
import subprocess as _sp  # noqa: E402
_old_max = config.MAX_DURATION_SECONDS
try:
    config.MAX_DURATION_SECONDS = 5
    _sp.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=8",
             "-f", "lavfi", "-i", "sine=f=440:d=8", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", f"{TMP}/long.mp4"], check=True)
    _sp.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=3",
             "-f", "lavfi", "-i", "sine=f=440:d=3", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", f"{TMP}/short.mp4"], check=True)
    tlogin(c, "uploader@example.com")
    with open(f"{TMP}/long.mp4", "rb") as f:
        r = c.post("/api/projects", files={"file": ("long.mp4", f, "video/mp4")})
    check("⑦ 超过时长上限的视频 → 413", r.status_code == 413 and "too long" in r.text, (r.status_code, r.text[:80]))
    with open(f"{TMP}/short.mp4", "rb") as f:
        r = c.post("/api/projects", files={"file": ("short.mp4", f, "video/mp4")})
    body = r.json() if r.status_code == 200 else {}
    check("⑦ 正常视频上传成功，返回时长与预估积分",
          r.status_code == 200 and body.get("duration") and body.get("estimatedPoints", {}).get("points", 0) > 0, body)
    vid = body.get("id")
    if vid:
        st = _store2.get_state(vid)
        check("⑦ 时长写进了工程文档", st.duration is not None and 2.5 < st.duration < 3.5, st.duration)
        r2 = c.post("/api/billing/estimate", json={"duration_seconds": st.duration, "dubbing_mode": "remote"})
        check("⑦ 估算接口：按设置返回积分与当前余额",
              r2.status_code == 200 and "points" in r2.json() and "balance_points" in r2.json(), r2.text[:120])
finally:
    config.MAX_DURATION_SECONDS = _old_max

# ------------------------------------------------------------------ ⑧ 其它
check("⑧ 计费库是独立文件（不和 auth 混）", os.path.exists(config.BILLING_DB_PATH)
      and config.BILLING_DB_PATH != config.AUTH_DB_PATH)
check("⑧ 确认卡预估：克隆按 2 积分/个",
      abs((billing.price_of("clone", pricing.ENROLLMENT_MODEL) or {}).get("points", 0) - 2) < 1e-6)

print()
if fails:
    print(f"合计 {len(fails)} 项失败: {', '.join(fails)}")
else:
    print("合计 全部通过")
sys.exit(1 if fails else 0)


def test_billing():
    assert not fails, fails
