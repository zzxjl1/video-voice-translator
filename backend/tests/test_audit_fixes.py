"""
浏览器巡检 + 代码复核发现的问题的回归测试（2026-11-10）。

覆盖：Ctx.ui 参数冲突、确认事件乱序、导出重复下载、set_settings 校验与误导 note、
set_export_plan(burn)、SSE 序号播种、术语表对永久错误重试、错误分类。
"""
import asyncio, os, sys, tempfile, types
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from app import status, store
from app.bus import bus
from app.commands import registry as R   # 导入 app.commands 包即注册全部命令
from app.models import Segment, Speaker
from app.services import export_service, llm_service as L

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

VID = "auditfix00000000000000000000000"
src = os.path.join(TMP, "v.mp4"); open(src, "wb").write(b"\0")

def fresh():
    if store.exists(VID): store.delete(VID)
    store.create(VID, "v.mp4", src)
    def seed(s):
        s.target_language = "English"
        s.dubbing_mode = "off"
        s.stage = "ready"
        s.speakers = [Speaker("0", "Speaker 0")]
        s.segments = [Segment("a", "0", 0.0, 3.0, "原文一"),
                      Segment("b", "0", 3.0, 6.0, "原文二"),
                      Segment("c", "0", 6.0, 9.0, "原文三")]
        for seg in s.segments:
            seg.translated_text = "Hello"
            seg.tr_fp = status.expected_tr_fp(s, seg)
    store.mutate(VID, seed, actor="system", cmd="seed", summary="seed")

def run(name, args=None, actor="user"):
    return asyncio.run(R.execute(VID, name, args or {}, actor=actor))

def drain(video_id=VID):
    q = bus.subscribe(video_id, since=0)
    out = []
    while not q.empty(): out.append(q.get_nowait())
    bus.unsubscribe(video_id, q)
    return out

# ------------------------------------------------------------------ ① Ctx.ui 不再撞车
fresh()
q = bus.subscribe(VID, since=store.current_seq(VID))
r = run("request_browser_action", {"action": "separate_vocals", "reason": "想分离人声"})
ui = [e for e in (lambda: [q.get_nowait() for _ in range(q.qsize())])() if e.get("type") == "ui"]
bus.unsubscribe(VID, q)
check("① 浏览器动作命令不再 TypeError", r["ok"], r)
check("① ui 事件类型仍是 browser_action", ui and ui[0].get("action") == "browser_action", ui)
check("① 具体动作名放在 kind，不覆盖 action", ui and ui[0].get("kind") == "separate_vocals", ui)
check("① 两种动作都能请求", run("request_browser_action", {"action": "burn_subtitles"})["ok"])

# ------------------------------------------------------------------ ② 导出只下载一次
fresh()
old_export, old_avail = export_service.export_video, export_service.is_available
async def fake_export(video_id, emit=None, plan=None):
    return types.SimpleNamespace(container="mkv", path=src)
export_service.export_video, export_service.is_available = fake_export, lambda: True
try:
    q = bus.subscribe(VID, since=store.current_seq(VID))
    r_user = run("export_video", {"format": "styled"}, actor="user")
    after_user = [q.get_nowait() for _ in range(q.qsize())]
    bus.unsubscribe(VID, q)
    check("② UI 导出成功", r_user["ok"], r_user)
    check("② UI 导出不发 export_ready（不会重复下载）",
          not any(e.get("action") == "export_ready" for e in after_user), after_user)

    q = bus.subscribe(VID, since=store.current_seq(VID))
    r_ai = run("export_video", {"format": "styled"}, actor="ai")
    after_ai = [q.get_nowait() for _ in range(q.qsize())]
    bus.unsubscribe(VID, q)
    ready = [e for e in after_ai if e.get("action") == "export_ready"]
    check("② AI 导出成功", r_ai["ok"], r_ai)
    check("② AI 导出发一次 export_ready 且带容器类型",
          len(ready) == 1 and ready[0].get("container") == "mkv", ready)
finally:
    export_service.export_video, export_service.is_available = old_export, old_avail

# ------------------------------------------------------------------ ③ 目标语言校验
fresh()
bad = run("set_settings", {"target_language": "Klingon"})
check("③ 无效目标语言被拒", (not bad["ok"]) and "Klingon" in bad["error"], bad)
check("③ 拒绝后工程语言没变", store.require_state(VID).target_language == "English")
check("③ 合法语言可改", run("set_settings", {"target_language": "Japanese"}, actor="ai")["ok"])

# ------------------------------------------------------------------ ④ note 只说这次新增的待办
fresh()
# 工程本来就有过期译文，但这次只切一个与指纹无关的开关
def make_stale(s):
    s.segments[0].tr_fp = "stale-on-purpose"
store.mutate(VID, make_stale, actor="system", cmd="seed", summary="seed")
r = run("set_settings", {"mm_enhance": True}, actor="ai")
check("④ 无关开关不会声称译文已按新设置过期", r["ok"] and r["result"].get("note") is None, r)
check("④ todo 仍如实反映工程里原有的待办", r["result"]["todo"]["translate"] >= 1, r)
r2 = run("set_settings", {"target_language": "Chinese"}, actor="ai")
check("④ 真让译文过期时才给出重译提示",
      r2["ok"] and r2["result"].get("note") and "过期" in r2["result"]["note"], r2)

# ------------------------------------------------------------------ ⑤ burn 不能存成项目方案
fresh()
r = run("set_export_plan", {"format": "burn"})
check("⑤ set_export_plan 拒绝 burn", (not r["ok"]) and "burn" in r["error"], r)
check("⑤ 合法方案仍可设置", run("set_export_plan", {"format": "styled"})["ok"])
# 历史遗留：工程里已经存了 burn，导出要给中文可读提示而不是任务内部抛英文
def force_burn(s): s.subtitle_export = {"enabled": True, "format": "burn", "tracks": ["translated"]}
store.mutate(VID, force_burn, actor="system", cmd="seed", summary="seed")
export_service.export_video, export_service.is_available = fake_export, lambda: True
try:
    r = run("export_video", {})
    check("⑤ 已存的 burn 方案给可读的中文提示", (not r["ok"]) and "烧录" in r["error"], r)
finally:
    export_service.export_video, export_service.is_available = old_export, old_avail

# ------------------------------------------------------------------ ⑥ 重启后 SSE 序号不回退
fresh()
for i in range(1, 501):
    store._persist_event(VID, {"type": "change", "seq": i, "rev": i, "actor": "user",
                               "cmd": "x", "summary": "x", "ids": [], "undoable": True,
                               "patch": {}, "ts": i})
# 模拟新进程：清掉内存里的历史与 bus channel，只留落盘事件
store._history.clear(); bus._channels.clear()
check("⑥ 新进程里 bus 序号从 0 开始", bus.current_seq(VID) == 0)
store.current_seq(VID)                       # /events 里现在会先做这一步
q = bus.subscribe(VID, since=500)
bus.publish(VID, {"type": "chat", "kind": "delta", "id": "x", "text": "你好"})
e = q.get_nowait()
check("⑥ 播种后新事件 seq=501，不会被客户端去重丢弃", e["seq"] == 501, e)
bus.unsubscribe(VID, q)

# ------------------------------------------------------------------ ⑦ 术语表对 401 不重试 / 错误分类
import httpx
from openai import AuthenticationError, NotFoundError
L.config.LLM_RETRY_SECONDS = 0.01
calls = {"n": 0}
async def bad_key(prompt, **kw):
    calls["n"] += 1
    raise AuthenticationError(message="bad key",
                              response=httpx.Response(401, request=httpx.Request("POST", "https://x")), body=None)
old_chat = L._chat
L._chat = bad_key
d = tempfile.mkdtemp()
items = [{"id": f"s{i}", "text": f"原文 {i}"} for i in range(6)]   # >3 行 → 会抽术语表
g = asyncio.run(L._extract_glossary(d, items, d, video_id="probe"))
L._chat = old_chat
check("⑦ 401 时术语表只发 1 次请求", calls["n"] == 1, calls)
check("⑦ 术语表失败不阻断翻译", g == {})
n404 = NotFoundError(message="404", response=httpx.Response(404, request=httpx.Request("POST", "https://x")), body=None)
check("⑦ 404 不算鉴权失败（不误导去查 API key）", L._is_auth_error(n404) is False)
check("⑦ 404 仍属于永久错误（不重试）", L._is_permanent_error(n404) is True)

# ------------------------------------------------------------------ ⑧ 本地配音：AI 的 redub 不再被拒，改请页面合成
from app import jobs

def seed_browser():
    fresh()
    def to_browser(s):
        s.dubbing_mode = "browser"
        for seg in s.segments:
            seg.translated_text = "Hello"
            seg.tr_fp = status.expected_tr_fp(s, seg)
    store.mutate(VID, to_browser, actor="system", cmd="seed", summary="browser")

def ui_events(q):
    out = []
    while not q.empty():
        e = q.get_nowait()
        if e.get("type") == "ui": out.append(e)
    return out

seed_browser()
r = run("redub", {}, actor="ai")
check("⑧ 没有页面在线 → 明说做不了（而不是假装已请求）", (not r["ok"]) and "没有打开" in r["error"], r)

q = bus.subscribe(VID, since=store.current_seq(VID))
r = run("redub", {}, actor="ai")
ev = ui_events(q)
check("⑧ 浏览器模式 redub：成功，不再被拒", r["ok"], r)
check("⑧ 发出 browser_dub 界面意图，带上要合成的句子 id", len(ev) == 1 and ev[0]["action"] == "browser_dub" and ev[0]["line_ids"] == ["a", "b", "c"] and ev[0]["force"] is False, ev)
check("⑧ 返回值说明是异步的、没合成完（防止 AI 说\"已配好\"）", r["result"]["queued"] == 3 and r["result"]["async"] is True and "还没合成完" in r["result"]["note"], r["result"])
check("⑧ 没有起服务端配音任务", jobs.find(VID, kind="dub") is None)

r = run("redub", {"lines": [2]}, actor="ai")
ev = ui_events(q)
check("⑧ 指定行号：只请求那一句", r["ok"] and len(ev) == 1 and ev[0]["line_ids"] == ["b"] and r["result"]["lines"] == [2], (r, ev))

# 一句已经是最新：不带 force 不重做；带 force 才重做
seg_b = store.get_state(VID).segment("b")
p = status.tts_audio_path(VID, "b"); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(b"x")
def fp(s): s.segment("b").tts_fp = status.expected_tts_fp(s, s.segment("b"))
store.mutate(VID, fp, actor="system", cmd="seed", summary="b fresh")
ui_events(q)
r = run("redub", {"lines": [2]}, actor="ai")
check("⑧ 该句配音已是最新 → 不发事件，告诉 AI 没活可做", r["ok"] and r["result"]["queued"] == 0 and not ui_events(q), r)
r = run("redub", {"lines": [2], "force": True}, actor="ai")
ev = ui_events(q)
check("⑧ force → 照样重做，事件带 force=true", r["ok"] and len(ev) == 1 and ev[0]["line_ids"] == ["b"] and ev[0]["force"] is True, (r, ev))
r = run("redub", {}, actor="ai")
ev = ui_events(q)
check("⑧ 不给行号 = 所有缺口，已最新的那句不在其中", r["ok"] and len(ev) == 1 and ev[0]["line_ids"] == ["a", "c"], (r, ev))

# 译文缺失仍然拦（校验在分支之前，不因为改走页面就绕过）
def drop_tr(s): s.segment("c").translated_text = ""
store.mutate(VID, drop_tr, actor="system", cmd="seed", summary="no tr")
r = run("redub", {"lines": [3]}, actor="ai")
check("⑧ 译文缺失仍被拒，先 retranslate", (not r["ok"]) and "译文" in r["error"], r)
bus.unsubscribe(VID, q)

fresh()      # dubbing_mode = off
r = run("redub", {}, actor="ai")
check("⑧ 仅字幕模式仍被拒", (not r["ok"]) and "dubbing_mode" in r["error"], r)

# ------------------------------------------------------------------ ⑧b 确认：本地合成免费，不该让 AI 去问用户"会花钱"
seed_browser()
st_browser = store.get_state(VID)
def to_remote(s): s.dubbing_mode = "remote"
store.mutate(VID, to_remote, actor="system", cmd="seed", summary="remote")
st_remote = store.get_state(VID)
check("⑧b 远端配音：AI 的 redub 仍要确认（花钱）", R.needs_confirmation("redub", "ai", st_remote) == R.CONFIRM_COST)
check("⑧b 浏览器配音：AI 的 redub 不用确认（免费）", R.needs_confirmation("redub", "ai", st_browser) is None)
check("⑧b 不传状态时保持原行为（要确认）", R.needs_confirmation("redub", "ai") == R.CONFIRM_COST)
check("⑧b 其它花钱的命令不受影响", R.needs_confirmation("clone_voice", "ai", st_browser) == R.CONFIRM_COST
      and R.needs_confirmation("retranslate", "ai", st_browser) == R.CONFIRM_COST)
check("⑧b agent 在判断确认时传了工程状态", "needs_confirmation(name, \"ai\", store.get_state(video_id))" in open(os.path.join(BACKEND, "app", "agent.py")).read())
check("⑧b 事件总线：没人订阅 / 有人订阅 的区别", bus.has_subscribers("no-such-project") is False)
_q = bus.subscribe(VID, since=0); check("⑧b 订阅后 has_subscribers=True", bus.has_subscribers(VID) is True)
bus.unsubscribe(VID, _q); check("⑧b 退订后恢复 False", bus.has_subscribers(VID) is False)

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
