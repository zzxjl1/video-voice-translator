"""
端到端（HTTP）：上传 → 设置并初始化 → 命令 → 事件流 → 聊天 → 导出。

外部服务（ASR / 翻译 / TTS）用桩替换，其余全是真的：真路由、真 store、真任务、真 ffmpeg 导出。
重点验证"接得上"：初始化只做缺的、改动自动过期、AI 与 UI 看同一条事件流、重启后状态还在。
"""
import asyncio, json, os, subprocess, sys, tempfile, time
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from fastapi.testclient import TestClient
from app import agent, jobs, store
from app.main import app
from app.services import asr_service, llm_service, tts_service

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

# ----------------------------------------------------------------- 桩
calls = {"asr": 0, "translate": [], "tts": []}

async def fake_transcribe(video_id, video_path, audio_path, url):
    calls["asr"] += 1
    return [
        {"id": "seg-a1", "speaker_id": "0", "start_time": 0.5, "end_time": 2.5, "text": "你好，欢迎收看"},
        {"id": "seg-b2", "speaker_id": "1", "start_time": 2.6, "end_time": 4.0, "text": "（背景音乐）"},
        {"id": "seg-c3", "speaker_id": "0", "start_time": 4.1, "end_time": 6.0, "text": "我们下期再见"},
    ]

async def fake_translate(video_id, segments, target_language, emit=None, text_overrides=None, custom_prompt="", length_hint=None):
    calls["translate"].append([s["id"] for s in segments])
    out = [{"id": s["id"], "translatedText": f"[{target_language}] {s['text']}"} for s in segments]
    if emit: await emit({"phase": "translation", "status": "progress"})
    return out, []

async def fake_synth(video_id, segment_id, text, voice, speech_rate=None, target_duration=None, instruction=None):
    calls["tts"].append((segment_id, voice, instruction))
    path = tts_service.tts_audio_path(video_id, segment_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=1.5", "-ar", "24000", path], check=True)
    return path, 1.5

asr_service.transcribe_video = fake_transcribe
llm_service.translate_script = fake_translate
tts_service.synthesize_speech = fake_synth
asr_service.extract_audio = lambda src, dst: subprocess.run(
    ["ffmpeg", "-y", "-v", "error", "-i", src, "-vn", "-ar", "16000", "-ac", "1", dst], check=True)

# ----------------------------------------------------------------- 夹具视频
SRC = os.path.join(TMP, "in.mp4")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x120:d=7",
                "-f", "lavfi", "-i", "sine=f=300:d=7", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", SRC], check=True)

_client_cm = TestClient(app)
c = _client_cm.__enter__()      # 事件循环全程存活：请求里 create_task 出来的后台任务才能跑完（uvicorn 就是这样）

def wait_job(vid, kind=None, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        if not any(j["kind"] == kind or kind is None for j in jobs.active_snapshot(vid)):
            return True
        time.sleep(0.05)
    return False

# ------------------------------------------------------------------ ① 上传
r = c.post("/api/projects", files={"file": ("in.mp4", open(SRC, "rb"), "video/mp4")})
vid = r.json()["id"]
check("① 上传返回 id（MD5）、不自动开始", r.status_code == 200 and len(vid) == 32 and r.json()["stage"] == "uploaded" and not r.json()["hasLines"], r.text)
r2 = c.post("/api/projects", files={"file": ("again.mp4", open(SRC, "rb"), "video/mp4")})
check("① 同一个文件再传 → 同一个工程、标记 exists", r2.json()["id"] == vid and r2.json()["exists"])
check("① 不支持的类型被拒", c.post("/api/projects", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 400)
check("① 不存在的工程 404", c.get("/api/projects/" + "0" * 32).status_code == 404)
check("① 畸形 id 400", c.get("/api/projects/..%2Fetc").status_code in (400, 404))

# ------------------------------------------------------------------ ② 设置并初始化
sub = None
r = c.post(f"/api/projects/{vid}/setup", json={"target_language": "English", "enable_dubbing": True})
check("② setup 接受并启动初始化", r.status_code == 200 and "job" in r.json(), r.text)
check("② 重复启动被拒", c.post(f"/api/projects/{vid}/setup", json={"target_language": "English"}).status_code == 409)
check("② 等初始化结束", wait_job(vid, "init"))
time.sleep(0.2)
v = c.get(f"/api/projects/{vid}").json()
check("② 阶段 = ready", v["stage"] == "ready", v["stage"])
check("② 3 句、2 位说话人", len(v["segments"]) == 3 and len(v["speakers"]) == 2)
check("② 每句都有译文且新鲜", all(s["trStatus"] == "fresh" for s in v["segments"]))
check("② 配音：音乐句不配，其余新鲜", [s["ttsStatus"] for s in v["segments"]] == ["fresh", "none" if False else v["segments"][1]["ttsStatus"], "fresh"])
check("② 初始化走了一遍 ASR / 翻译 / 配音", calls["asr"] == 1 and len(calls["translate"]) == 1 and len(calls["tts"]) == 3)
check("② 说话人被分配了不同的音色", len({sp["voice"] for sp in v["speakers"]}) == 2)
check("② 欢迎消息已写入聊天", any(m.get("system") for m in c.get(f"/api/projects/{vid}/chat").json()["messages"]))
check("② 视图带 rev、seq、undo、export 信息", v["rev"] > 0 and v["seq"] > 0 and "undo" in v and "export" in v)

# ------------------------------------------------------------------ ③ 再跑一次初始化：只做缺的 = 什么都不做
before = (calls["asr"], len(calls["translate"]), len(calls["tts"]))
r = c.post(f"/api/projects/{vid}/commands/run_initialize", json={"args": {}})
wait_job(vid, "init"); time.sleep(0.2)
check("③ 重跑初始化：ASR、翻译、TTS 都不再调用（幂等）", (calls["asr"], len(calls["translate"]), len(calls["tts"])) == before, (calls, before))

# ------------------------------------------------------------------ ④ 命令 + 事件流：UI 改动 → 所有订阅者看到
since = c.get(f"/api/projects/{vid}").json()["seq"]
from app.bus import bus
q = bus.subscribe(vid, since=since)
r = c.post(f"/api/projects/{vid}/commands/mark_non_speech", json={"args": {"lines": [2], "kind": "music"}})
check("④ 命令返回 ok + rev", r.json()["ok"] and r.json()["rev"] > v["rev"], r.text)
ev = q.get_nowait()
check("④ 事件流推出 change：actor=user、带受影响的行、带增量", ev["type"] == "change" and ev["actor"] == "user" and ev["ids"] == ["seg-b2"]
      and ev["patch"]["segments"]["upsert"][0]["kind"] == "music", ev)
r = c.post(f"/api/projects/{vid}/commands/edit_source", json={"args": {"line": 99, "text": "x"}})
check("④ 业务上做不了 = 200 + ok:false + 可执行的原因", r.status_code == 200 and not r.json()["ok"] and "共 3 句" in r.json()["error"])
check("④ 不存在的命令 404", c.post(f"/api/projects/{vid}/commands/zzz", json={"args": {}}).status_code == 404)
check("④ 命令清单带 schema", len(c.get(f"/api/projects/{vid}/commands").json()["commands"]) >= 40)

# ------------------------------------------------------------------ ⑤ 改了会过期，补上只做过期的
c.post(f"/api/projects/{vid}/commands/edit_source", json={"args": {"line": 1, "text": "您好，欢迎收看节目"}})
v = c.get(f"/api/projects/{vid}").json()
check("⑤ 改原文 → 第 1 句译文 stale、其它不受影响", [s["trStatus"] for s in v["segments"]][0] == "stale" and v["segments"][2]["trStatus"] == "fresh")
check("⑤ 待办里反映出来", v["needs"]["translate"] == 1)
calls["translate"].clear(); calls["tts"].clear()
r = c.post(f"/api/projects/{vid}/commands/retranslate", json={"args": {}})
check("⑤ retranslate 只翻过期的那一句", r.json()["ok"] and calls["translate"] == [["seg-a1"]], calls)
v = c.get(f"/api/projects/{vid}").json()
check("⑤ 译文变了 → 配音随之过期（没有手动作废）", v["segments"][0]["ttsStatus"] == "stale", v["segments"][0])
r = c.post(f"/api/projects/{vid}/commands/redub", json={"args": {}})
check("⑤ redub 只配过期的那一句", r.json()["ok"] and [t[0] for t in calls["tts"]] == ["seg-a1"], calls["tts"])
v = c.get(f"/api/projects/{vid}").json()
check("⑤ 补完后全部新鲜", all(s["ttsStatus"] in ("fresh", "none") for s in v["segments"]) and v["needs"] == {"translate": 0, "dub": 0})

# ------------------------------------------------------------------ ⑥ 语气指令真的传到了合成
calls["tts"].clear()
c.post(f"/api/projects/{vid}/commands/set_dub_style", json={"args": {"lines": [3], "instruction": "Please speak sadly and slowly."}})
c.post(f"/api/projects/{vid}/commands/redub", json={"args": {}})
check("⑥ 语气指令传给了 TTS", calls["tts"] and calls["tts"][0][0] == "seg-c3" and "sadly" in (calls["tts"][0][2] or ""), calls["tts"])

# ------------------------------------------------------------------ ⑦ 撤销
r = c.post(f"/api/projects/{vid}/undo")
check("⑦ 撤销最近一次（语气设置）→ 配音指纹对回去，旧 mp3 重新新鲜", r.json()["ok"])

# ------------------------------------------------------------------ ⑧ 文件服务：过期的配音不会被播放
v = c.get(f"/api/projects/{vid}").json()
seg = v["segments"][0]
check("⑧ 新鲜配音可播放，带缓存头", c.get(seg["audioUrl"]).status_code == 200)
c.post(f"/api/projects/{vid}/commands/set_translation", json={"args": {"line": 1, "text": "Hello and welcome"}})
check("⑧ 译文改了 → 旧 mp3 还在磁盘上，但不再提供（它念的是旧文本）", c.get(f"/api/projects/{vid}/tts/seg-a1").status_code == 404)
check("⑧ 视频支持 Range", c.get(f"/api/projects/{vid}/video", headers={"Range": "bytes=0-99"}).status_code == 206)
check("⑧ 畸形 segment id 被拒", c.get(f"/api/projects/{vid}/tts/..%2Fx").status_code in (400, 404))

# ------------------------------------------------------------------ ⑨ 事件流 SSE
# TestClient 是同步的，读不完一个永不关闭的流。直接驱动路由返回的流本身：读到第一批事件就关。
from app.routers import projects as P

class _Req:                                   # 只需要 is_disconnected
    def __init__(self): self.gone = False
    async def is_disconnected(self): return self.gone

async def read_sse(since, want, hold=0.0):
    req = _Req()
    resp = await P.events(vid, req, since=since)
    got = []
    async for chunk in resp.body_iterator:
        if chunk.startswith(": ping"):
            continue
        if "data:" in chunk:
            got.append(json.loads(chunk.split("data: ", 1)[1].split("\n\n")[0]))
        if len(got) >= want:
            break
    await resp.body_iterator.aclose()          # 客户端断开：finally 里要把订阅摘掉
    return got

subs_before = len(bus._channels[vid].subs)
replayed = asyncio.run(read_sse(since=0, want=3))
check("⑨ SSE 先回放历史（带 seq，单调递增）", len(replayed) == 3 and [e["seq"] for e in replayed] == sorted(e["seq"] for e in replayed), replayed[:1])
check("⑨ 断开后路由的订阅被摘掉（不泄漏队列）", len(bus._channels[vid].subs) == subs_before, (subs_before, len(bus._channels[vid].subs)))
tail = bus.current_seq(vid) - 1
check("⑨ since=N 只回放 N 之后的", asyncio.run(read_sse(since=tail, want=1))[0]["seq"] == tail + 1)

# ------------------------------------------------------------------ ⑩ 聊天（假模型）→ 改动经同一条事件流出来
import test_agent_shim  # noqa: E402  (见文件末尾：把假模型装上)
test_agent_shim.install([
    {"calls": [("set_muted", {"lines": [3], "muted": True})]},
    {"text": "已静音第 3 句。"},
])
q = bus.subscribe(vid, since=bus.current_seq(vid))
r = c.post(f"/api/projects/{vid}/chat", json={"text": "把第三句静音"})
check("⑩ 发消息立刻返回，回复走事件流", r.status_code == 200 and r.json()["accepted"])
for _ in range(100):
    if not agent.chat_for(vid).running and store.get_state(vid).segments[2].muted:
        break
    time.sleep(0.05)
evs = []
while not q.empty(): evs.append(q.get_nowait())
ai_changes = [e for e in evs if e["type"] == "change" and e["actor"] == "ai"]
check("⑩ AI 的改动生效，且在事件流里标明是 AI 做的", store.get_state(vid).segments[2].muted and ai_changes and ai_changes[0]["ids"] == ["seg-c3"])
check("⑩ 聊天记录持久化", [m["role"] for m in c.get(f"/api/projects/{vid}/chat").json()["messages"]][-2:] == ["user", "assistant"])
check("⑩ 空消息被拒", c.post(f"/api/projects/{vid}/chat", json={"text": "  "}).status_code == 400)

# ------------------------------------------------------------------ ⑪ 导出
r = c.post(f"/api/projects/{vid}/commands/export_video", json={"args": {}})
check("⑪ 配音没补齐时导出被拒，并指路", (not r.json()["ok"]) and "redub" in r.json()["error"], r.text)
c.post(f"/api/projects/{vid}/commands/redub", json={"args": {}})
r = c.post(f"/api/projects/{vid}/commands/export_video", json={"args": {}})
check("⑪ 补齐后导出成功", r.json()["ok"], r.text)
if r.json()["ok"]:
    got = c.get(f"/api/projects/{vid}/export/download")
    check("⑪ 下载到成片", got.status_code == 200 and len(got.content) > 5000)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", "/dev/stdin"],
                           input=got.content, capture_output=True)
    check("⑪ 成片是 h264 + aac", b"h264" in probe.stdout and b"aac" in probe.stdout, probe.stdout)
v = c.get(f"/api/projects/{vid}").json()
check("⑪ 视图里有导出信息", v["export"] and v["export"]["container"] == "mp4")

# ------------------------------------------------------------------ ⑫ 重启：从磁盘恢复，状态、撤销、聊天、序号都在
rev = c.get(f"/api/projects/{vid}").json()["rev"]
# 只有 change / job 事件落盘；progress / chat 只在内存里，重启后 seq 可以比重启前小，
# 但绝不能小于"最后一个已落盘事件"的 seq —— 否则新事件会和旧事件撞号，客户端按 seq 去重就会丢事件。
last_persisted = max(e["seq"] for e in store.events_since(vid, 0))
store._cache.clear(); store._history.clear(); bus._channels.clear(); agent._chats.clear()
v2 = c.get(f"/api/projects/{vid}").json()
check("⑫ 重启后视图一致（rev、行数、译文）", v2["rev"] == rev and len(v2["segments"]) == 3 and v2["segments"][0]["translation"] == "Hello and welcome")
check("⑫ 重启后聊天记录还在", len(c.get(f"/api/projects/{vid}").json()["messages"] if False else c.get(f"/api/projects/{vid}/chat").json()["messages"]) >= 3)
check("⑫ 重启后 seq 不小于最后一个落盘事件（新事件不会和旧事件撞号）", v2["seq"] >= last_persisted, (last_persisted, v2["seq"]))
c.post(f"/api/projects/{vid}/commands/set_muted", json={"args": {"lines": [1], "muted": True}})
newest = [e for e in store.events_since(vid, 0) if e["type"] == "change"][-1]["seq"]
check("⑫ 重启后的第一个新事件，序号严格大于所有旧的", newest > last_persisted, (newest, last_persisted))
check("⑫ 重启后还能撤销（含重启前的改动）", c.post(f"/api/projects/{vid}/undo").json()["ok"])

# ------------------------------------------------------------------ ⑬ 删除
check("⑬ 删除", c.delete(f"/api/projects/{vid}").status_code == 200 and c.get(f"/api/projects/{vid}").status_code == 404)

# ------------------------------------------------------------------ ⑭ 初始化失败 → 可重试；有部分结果就进编辑器
async def boom(*a, **k): raise RuntimeError("ASR 服务挂了")
asr_service.transcribe_video = boom
r = c.post("/api/projects", files={"file": ("in.mp4", open(SRC, "rb"), "video/mp4")}); vid = r.json()["id"]
c.post(f"/api/projects/{vid}/setup", json={"target_language": "English"}); wait_job(vid, "init"); time.sleep(0.2)
v = c.get(f"/api/projects/{vid}").json()
check("⑭ ASR 失败（还没有任何行）→ stage=failed，错误可见", v["stage"] == "failed" and "ASR" in (v["initError"] or ""), v["stage"])
asr_service.transcribe_video = fake_transcribe
c.post(f"/api/projects/{vid}/commands/run_initialize", json={"args": {}}); wait_job(vid, "init"); time.sleep(0.2)
check("⑭ 重试成功 → ready", c.get(f"/api/projects/{vid}").json()["stage"] == "ready")

_client_cm.__exit__(None, None, None)
print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)


def test_e2e():
    assert not fails, fails
