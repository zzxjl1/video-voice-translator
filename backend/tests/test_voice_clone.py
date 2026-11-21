"""
克隆参考样本的预设、拼接、候选音色命令。

不碰网络：DashScope 的 VoiceEnrollmentService 和 TTS 用桩。ffmpeg 是真的——拼接的时长、
采样率、是否循环重复，只有真跑一遍才知道。
"""
import asyncio, base64, io, os, subprocess, sys, tempfile, wave
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from app import status, store, view
from app.commands import registry as R
from app.models import Segment, Speaker, video_dir_path
from app.services import voice_clone_service as vc
from app.services import voice_presets as vp
from app.services import tts_service

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

# ------------------------------------------------------------------ 桩
class FakeEnroll:
    created, deleted = [], []
    def create_voice(self, target_model, prefix, url, language_hints=None, max_prompt_audio_length=None, **kw):
        FakeEnroll.created.append({"url": url, "max": max_prompt_audio_length, "model": target_model})
        return f"{target_model}-{prefix}-{len(FakeEnroll.created):04d}"
    def query_voice(self, voice_id):
        return {"status": "OK"}
    def delete_voice(self, voice_id):
        FakeEnroll.deleted.append(voice_id)
vc.VoiceEnrollmentService = FakeEnroll
synth_calls = []
tts_service._synth_audio = lambda model, voice, text, **kw: (synth_calls.append((voice, text)), b"mp3")[1]
config.CLONE_POLL_INTERVAL_S = 0

def seg(i, spk, a, b, text="A calm statement.", tr="一句平稳的话。", kind="speech"):
    return Segment(i, spk, a, b, text, translated_text=tr, kind=kind)

# 说话人 0：一段 24 秒的连读（四句，句间 ≤1.2s）+ 一句 1 秒碎片；说话人 1 只有一句 3 秒
S = [
    seg("s0", "0", 0.0, 5.0),
    seg("s1", "0", 6.0, 11.5, text="Is it really so?", tr="真的是这样吗？"),
    seg("s2", "0", 12.0, 18.0, tr="[deep and loud shouting] 大声喊叫的一句话，比较长一点。"),
    seg("s3", "0", 19.0, 24.0),
    seg("s4", "0", 30.0, 31.0, tr="短。"),
    seg("t0", "1", 25.0, 28.0, tr="另一个人说的话。"),
]

# ------------------------------------------------------------------ ① 候选池 / 素材量
pool = vp.pool_for(S, "0")
check("① 候选池：说话人 0 有 5 句，都够长", [s.id for s in pool] == ["s0", "s1", "s2", "s3", "s4"], [s.id for s in pool])
over = S + [seg("x", "1", 7.0, 9.0)]                       # 说话人 1 在 7~9 秒插话，压在 s1 上
check("① 与别人重叠的句子被淘汰（另一个人的也不留）",
      "s1" not in [s.id for s in vp.pool_for(over, "0")] and "x" not in [s.id for s in vp.pool_for(over, "1")])
check("① 非语音（音乐 / 笑声）不进候选",
      "m" not in [s.id for s in vp.pool_for(S + [seg("m", "0", 40, 45, kind="music")], "0")])
check("① 短于 0.3 秒的碎片不进候选", "f" not in [s.id for s in vp.pool_for(S + [seg("f", "0", 50, 50.2)], "0")])
m = vp.material(S, "0")
check("① 素材量：总量 22 秒，最长连续 24 秒（连读不被中间的间隔切断）", m["total"] == 22.5 and m["longest"] == 24.0 and m["lines"] == 5, m)
m1 = vp.material(S, "1")
check("① 素材量：说话人 1 只有 3 秒", m1["total"] == 3.0 and m1["longest"] == 3.0, m1)

# ------------------------------------------------------------------ ② 预设
p = vp.plan_continuous(S, "0")
check("② 连读：一段、不超过 20 秒、从连读开头取", len(p.clips) == 1 and p.clips[0][0] == 0.0 and p.seconds <= 20.0, p)
check("② 连读：覆盖的句子都是同一段连续说的", p.line_ids == ["s0", "s1", "s2"], p.line_ids)
p = vp.plan_long_one(S, "0")
check("② 单句长段：挑最长的完整句（6 秒）", p.line_ids == ["s2"], p.line_ids)
p = vp.plan_mid_concat(S, "0")
check("② 中句拼接：避开疑问句（句末上扬）", "s1" not in p.line_ids and len(p.line_ids) >= 2, p.line_ids)
check("② 中句拼接：语音 + 间隔不超过 20 秒", p.seconds <= vp.SAMPLE_MAX_DURATION, p.seconds)
check("② 中句拼接：按时间顺序", p.clips == sorted(p.clips))
try:
    vp.plan_mid_concat(S, "1"); ok = False
except vp.PresetUnavailable as e:
    ok = "中等长度" in str(e)
check("② 中句拼接：素材凑不够 → 说清原因", ok)
check("② 默认策略：素材够 → 中句拼接", vp.default_plan(S, "0").preset == "mid_concat")
check("② 默认策略：素材不够 → 退到连读 / 单句，不硬凑", vp.default_plan(S, "1").preset in ("continuous", "long_one"))
try:
    vp.default_plan(S, "9"); ok = False
except vp.PresetUnavailable:
    ok = True
check("② 默认策略：一句都没有 → 抛错", ok)
opts = {o["id"]: o for o in vp.preset_options(S, "1")}
check("② 预设清单：做不了的写明原因，能做的给出时长和行号",
      opts["mid_concat"]["available"] is False and opts["mid_concat"]["reason"]
      and opts["continuous"]["available"] and opts["continuous"]["seconds"] == 3.0 and opts["continuous"]["lines"] == [6], opts)
check("② 预设清单：smart 在有素材时可选", {o["id"]: o for o in vp.preset_options(S, "0")}["smart"]["available"])

# ------------------------------------------------------------------ ③ 固定试听句
check("③ 十种目标语言都有试听句", set(vp.PREVIEW_SENTENCES) == set(config.TARGET_LANGUAGES), set(config.TARGET_LANGUAGES) ^ set(vp.PREVIEW_SENTENCES))
check("③ 长度适合朗读（够听出语速和节奏，又不至于让人等太久）",
      all(30 <= len(t) <= 260 for t in vp.PREVIEW_SENTENCES.values()),
      {k: len(v) for k, v in vp.PREVIEW_SENTENCES.items()})
check("③ 都是一段两句话：句号处有自然停顿，能听出换气", all(t.count("。") + t.count(".") == 2 for t in vp.PREVIEW_SENTENCES.values()))
check("③ 按目标语言取", vp.preview_sentence("Chinese").startswith("这是一个视频翻译与配音平台")
      and vp.preview_sentence("English").startswith("This is a platform for video translation and dubbing"))
check("③ 没收录的语言退到英文，不会试听不了", vp.preview_sentence("Klingon") == vp.preview_sentence("English") == vp.preview_sentence(None))
check("③ 内容是平台介绍（用户最终会听到的成片旁白），不是随手一句生活用语",
      all(k in vp.preview_sentence("Chinese") for k in ("视频", "翻译", "配音", "字幕"))
      and all(k in vp.preview_sentence("English") for k in ("video", "translation", "dubbing", "subtitles")))
check("③ 试听句是平静的陈述句：没有问号 / 感叹号 / 情绪标签",
      all(not any(c in t for c in "?？!！[]") for t in vp.PREVIEW_SENTENCES.values()))
a = vp.preview_filename("v1", "你好"); b = vp.preview_filename("v1", "你好吗")
check("③ 试听文件名：同音色同句稳定，改了试听句就变", a == vp.preview_filename("v1", "你好") and a != b)
check("③ 试听文件名：不同音色不同文件", a != vp.preview_filename("v2", "你好"))


# ------------------------------------------------------------------ ④ 样本拼接（真 ffmpeg）
VID = "voicelab00000000000000000000000000"
src = os.path.join(TMP, "v.mp4"); open(src, "wb").write(b"\0")
def make_vocals():
    d = video_dir_path(VID); os.makedirs(d, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=40:sample_rate=44100",
                    "-ac", "1", os.path.join(d, "vocals.wav")], check=True)
def wav_info(path):
    w = wave.open(path); return w.getframerate(), w.getnchannels(), w.getnframes() / w.getframerate()

store.create(VID, "v.mp4", src); make_vocals()
out = os.path.join(TMP, "sample_multi.wav")
plan = vp.plan_mid_concat(S, "0")
secs = vc._ffmpeg_build_sample(os.path.join(video_dir_path(VID), "vocals.wav"), plan, out)
rate, ch, dur = wav_info(out)
check("④ 拼接：24kHz 单声道", (rate, ch) == (24000, 1), (rate, ch))
check("④ 拼接：时长 = 各段 + 0.5 秒间隔（不循环、不多不少）", abs(dur - plan.seconds) < 0.1 and abs(secs - plan.seconds) < 1e-6, (dur, plan.seconds))
out1 = os.path.join(TMP, "sample_one.wav")
short = vp.Plan("long_one", [(0.0, 3.0)], ["x"])
vc._ffmpeg_build_sample(os.path.join(video_dir_path(VID), "vocals.wav"), short, out1)
check("④ 素材不够 10 秒也不循环重复凑时长", abs(wav_info(out1)[2] - 3.0) < 0.1, wav_info(out1)[2])
try:
    vc._ffmpeg_build_sample(os.path.join(video_dir_path(VID), "vocals.wav"), vp.Plan("x", [], []), out1); ok = False
except RuntimeError:
    ok = True
check("④ 没有可截取的素材 → 报错而不是出空文件", ok)
# 淡入淡出：首尾样本接近 0（正弦波硬切会在边界有明显幅度）
w = wave.open(out); n = w.getnframes(); raw = w.readframes(n); w.close()
import array
samples = array.array("h"); samples.frombytes(raw)
check("④ 首尾淡入淡出（硬切会在拼接处咔哒）", abs(samples[0]) < 300 and abs(samples[-1]) < 300, (samples[0], samples[-1]))

# ------------------------------------------------------------------ ⑤ 注册参数
async def enroll():
    return await vc.enroll_voice(VID, "0", plan)
FakeEnroll.created.clear()
vid1 = asyncio.run(enroll())
call = FakeEnroll.created[0]
check("⑤ 注册显式传 max_prompt_audio_length=20（服务端默认只取前 10 秒）", call["max"] == 20.0, call["max"])
check("⑤ 样本以 data URI 交给 DashScope，不依赖公网地址", call["url"].startswith("data:audio/wav;base64,"))
embedded = base64.b64decode(call["url"].split(",", 1)[1])
check("⑤ 内嵌的是刚拼好的 24kHz wav", wave.open(io.BytesIO(embedded)).getframerate() == 24000)
check("⑤ 样本不超过官方 10MB 上限", len(embedded) < 10 * 1024 * 1024, len(embedded))
check("⑤ 注册返回 voice_id", vid1.startswith(config.TTS_MODEL))

# ------------------------------------------------------------------ ⑥ pick_plan
async def pick(**kw):
    return await vc.pick_plan(VID, S, "0", **kw)
check("⑥ 不指定预设：走默认（中句拼接）", asyncio.run(pick()).preset == "mid_concat")
check("⑥ 指定规则型预设", asyncio.run(pick(preset="continuous")).preset == "continuous")
try:
    asyncio.run(vc.pick_plan(VID, S, "1", "mid_concat")); ok = False
except RuntimeError as e:
    ok = "中等长度" in str(e)
check("⑥ 指定的预设做不了 → 报错，不悄悄换一个", ok)
from app.services import omni_service
async def omni_ok(*a, **k):
    omni_ok.kw = k; return ["s0", "s3"], ""
async def omni_fail(*a, **k):
    return None, "boom"
omni_service.pick_samples = omni_ok
pl = asyncio.run(pick(preset="smart", target_language="Chinese"))
check("⑥ smart：用 omni 挑的行，并把目标语言交给它", pl.preset == "smart" and pl.line_ids == ["s0", "s3"] and omni_ok.kw.get("target_language") == "Chinese", (pl, getattr(omni_ok, "kw", None)))
omni_service.pick_samples = omni_fail
try:
    asyncio.run(pick(preset="smart")); ok = False
except RuntimeError:
    ok = True
check("⑥ 用户明确选了 smart 而 omni 失败 → 报错", ok)
check("⑥ 初始化开了智能选材但 omni 失败 → 回退默认预设", asyncio.run(pick(smart=True)).preset == "mid_concat")

# ------------------------------------------------------------------ ⑦ 命令
def fresh():
    if store.exists(VID): store.delete(VID)
    store.create(VID, "v.mp4", src); make_vocals()
    def seed(s):
        s.target_language = "Chinese"; s.enable_dubbing = True; s.enable_voice_clone = True
        s.speakers = [Speaker("0", "Speaker 0", voice="Emily_v3.1"), Speaker("1", "Speaker 1", voice="Brian_v3.1")]
        s.segments = [Segment(x.id, x.speaker_id, x.start_time, x.end_time, x.text, translated_text=x.translated_text, kind=x.kind) for x in S]
        for g in s.segments: g.tr_fp = status.expected_tr_fp(s, g)
        s.stage = "ready"
    store.mutate(VID, seed, actor="system", cmd="seed", summary="seed")
def run(name, args=None, actor="user"):
    return asyncio.run(R.execute(VID, name, args or {}, actor=actor))

fresh()
omni_service.pick_samples = omni_ok
cm = R.get("generate_voice_candidates")
check("⑦ 生成候选会花钱：AI 调用要先确认", R.needs_confirmation("generate_voice_candidates", "ai") == R.CONFIRM_COST)
check("⑦ 删除候选：AI 调用要先确认", R.needs_confirmation("delete_voice_candidates", "ai") == R.CONFIRM_DESTROY)
check("⑦ 选用候选不花钱：不需要确认", R.needs_confirmation("use_voice_candidate", "ai") is None)

FakeEnroll.created.clear(); synth_calls.clear()
r = run("generate_voice_candidates", {"speaker": "0"})
st = store.get_state(VID); sp = st.speaker("0")
check("⑦ 默认生成所有能做的规则型预设（3 个）", r["ok"] and len(sp.clone_candidates) == 3 and len(FakeEnroll.created) == 3, r)
check("⑦ 候选记着预设、用了哪几句、时长（历史事实）",
      all(c["preset"] and c["lines"] and c["seconds"] and c["id"] for c in sp.clone_candidates), sp.clone_candidates)
check("⑦ 候选的样本各存各的文件，不互相覆盖",
      len({os.path.exists(vc.sample_path(VID, "0", c["id"])) and c["id"] for c in sp.clone_candidates}) == 3)
check("⑦ 所有候选念的是同一句固定的话（随目标语言，不看译文）",
      len({t for _, t in synth_calls}) == 1 and len(synth_calls) == 3 and synth_calls[0][1] == vp.preview_sentence("Chinese"), synth_calls)

r = run("generate_voice_candidates", {"speaker": "1", "presets": ["mid_concat"]})
check("⑦ 做不了的预设被拒并说明原因", not r["ok"] and "中等长度" in r["error"], r)
r = run("generate_voice_candidates", {"speaker": "1", "presets": ["nope"]})
check("⑦ 不存在的预设 → 参数校验拒绝", not r["ok"], r)

# 当前音色 + 试听
def set_current(s):
    s.speaker("0").clone = {"voice_id": "CUR", "target_model": config.TTS_MODEL, "created_at": 1,
                            "preset": "mid_concat", "lines": ["s0"], "seconds": 5.0}
store.mutate(VID, set_current, actor="system", cmd="seed", summary="cur")
n0 = len(synth_calls)
r = run("preview_voice_candidates", {"speaker": "0"})
check("⑦ 试听：当前音色没有试听就补上，已有的不重做（不重复花钱）", r["ok"] and r["result"]["synthesized"] == 1 and len(synth_calls) == n0 + 1, r)
r = run("preview_voice_candidates", {"speaker": "0"})
check("⑦ 再试听一次：全部已有，0 次合成", r["ok"] and r["result"]["synthesized"] == 0, r)
# 视图
sv = view.speaker_view(store.get_state(VID), store.get_state(VID).speaker("0"))
check("⑧ 视图：素材量现算", sv["material"]["total"] == 22.5 and sv["material"]["longest"] == 24.0, sv["material"])
check("⑧ 视图：当前音色和候选都带试听地址", sv["currentClone"]["previewUrl"] and all(c["previewUrl"] for c in sv["candidates"]), sv)
check("⑧ 视图：带固定试听句（随目标语言）", sv["previewText"] == vp.preview_sentence("Chinese"), sv["previewText"])
check("⑧ 视图：预设清单带可用性", any(o["id"] == "mid_concat" and o["available"] for o in sv["presetOptions"]))
check("⑧ 视图：候选带预设名和行号", all(c["presetLabel"] and c["lines"] for c in sv["candidates"]), sv["candidates"])
sv1 = view.speaker_view(store.get_state(VID), store.get_state(VID).speaker("1"))
check("⑧ 视图：没有克隆的说话人也不报错", sv1["currentClone"] is None and sv1["candidates"] == [] and sv1["previewText"], sv1)

# 选用
st = store.get_state(VID); sp = st.speaker("0")
target = next(c for c in sp.clone_candidates if c["preset"] == "continuous")
# 先让一句配音是新鲜的，选用之后应该变过期
def fresh_tts(s):
    g = s.segment("s0"); g.tts_fp = status.expected_tts_fp(s, g)
store.mutate(VID, fresh_tts, actor="system", cmd="seed", summary="tts")
open(status.tts_audio_path(VID, "s0"), "wb") if os.path.isdir(os.path.dirname(status.tts_audio_path(VID, "s0"))) else None
os.makedirs(os.path.dirname(status.tts_audio_path(VID, "s0")), exist_ok=True)
open(status.tts_audio_path(VID, "s0"), "wb").write(b"x")
check("⑨ 选用前：该句配音是新鲜的", status.tts_status(store.get_state(VID), store.get_state(VID).segment("s0")) == status.FRESH)
r = run("use_voice_candidate", {"speaker": "0", "candidate": target["id"]})
st = store.get_state(VID); sp = st.speaker("0")
check("⑨ 选用：当前音色换成候选", r["ok"] and sp.clone["voice_id"] == target["voice_id"] and sp.clone["preset"] == "continuous", r)
check("⑨ 选用：原来的音色退成候选，能切回来", any(c["voice_id"] == "CUR" and c.get("id") for c in sp.clone_candidates), sp.clone_candidates)
check("⑨ 选用：被选的不再重复出现在候选里", all(c["voice_id"] != target["voice_id"] for c in sp.clone_candidates))
check("⑨ 选用：不花钱，没有新建音色", len(FakeEnroll.created) == 3, len(FakeEnroll.created))
check("⑨ 选用：该说话人的配音自动过期（靠指纹，不靠手动作废）", status.tts_status(st, st.segment("s0")) == status.STALE)
check("⑨ 选用：告诉调用方影响几句、要 redub", r["result"]["lines_affected"] >= 1 and "redub" in r["result"]["note"], r)
r = run("use_voice_candidate", {"speaker": "0", "candidate": "nope"})
check("⑨ 选用不存在的候选 → 说清", not r["ok"] and "nope" in r["error"], r)
check("⑨ 选用可撤销", run("undo")["ok"] and store.get_state(VID).speaker("0").clone["voice_id"] == "CUR")

def lose_translations(st):
    for g in st.segments: g.translated_text = ""
store.mutate(VID, lose_translations, actor="system", cmd="seed", summary="no translation")
r = run("preview_voice_candidates", {"speaker": "0"})
check("⑦ 试听不依赖工程里有没有译文", r["ok"] and r["result"]["synthesized"] == 0, r)
r = run("preview_voice_candidates", {"speaker": "0", "line": 4})
check("⑦ 试听不再有“换一句”（固定句，参数不存在）", not r["ok"], r)

# 换了 TTS 模型的候选不能选
def stale_model(s):
    s.speaker("0").clone_candidates[0]["target_model"] = "old-model"
store.mutate(VID, stale_model, actor="system", cmd="seed", summary="old")
cid = store.get_state(VID).speaker("0").clone_candidates[0]["id"]
r = run("use_voice_candidate", {"speaker": "0", "candidate": cid})
check("⑨ 按别的 TTS 模型克隆的候选不能选", not r["ok"] and "模型" in r["error"], r)

# 手动指定参考句 → 一个“手动指定”候选
fresh()
FakeEnroll.created.clear()
r = run("generate_voice_candidates", {"speaker": "0", "reference_lines": [1, 4]})
cs = store.get_state(VID).speaker("0").clone_candidates
check("⑩ 手动指定参考句：只做这一个候选，预设记为 manual", r["ok"] and len(cs) == 1 and cs[0]["preset"] == "manual" and cs[0]["lines"] == ["s0", "s3"], r)
sv = view.speaker_view(store.get_state(VID), store.get_state(VID).speaker("0"))
check("⑩ 手动候选在视图里叫“手动指定”", sv["candidates"][0]["presetLabel"] == "手动指定", sv["candidates"])
r = run("generate_voice_candidates", {"speaker": "0", "reference_lines": [1, 6]})
check("⑩ 手动指定：别人说的句子被拒", not r["ok"] and "不属于" in r["error"], r)
r = run("generate_voice_candidates", {"speaker": "0", "reference_lines": [1, 2], "presets": ["continuous"]})
check("⑩ 手动指定 + 预设：一起做（2 个）", r["ok"] and len(store.get_state(VID).speaker("0").clone_candidates) == 3, r)

# 单句覆盖：这一句用某个候选音色
fresh()
run("generate_voice_candidates", {"speaker": "0", "presets": ["continuous", "long_one"]})
cands = store.get_state(VID).speaker("0").clone_candidates
c0, c1 = cands[0], cands[1]
r = run("set_dub_style", {"lines": [1], "clone_candidate": c0["id"]})
seg0 = store.get_state(VID).segment("s0")
check("⑩ 单句覆盖：这一句改用候选音色", r["ok"] and seg0.tts_voice == c0["voice_id"], r)
check("⑩ 单句覆盖：这句最终用的就是它（优先级高于说话人的当前音色）", status.resolve_voice(store.get_state(VID), seg0) == c0["voice_id"])
check("⑩ 单句覆盖：别的句不受影响", store.get_state(VID).segment("s3").tts_voice is None)
r = run("set_dub_style", {"lines": [1, 6], "clone_candidate": c0["id"]})
check("⑩ 单句覆盖：不能跨说话人", not r["ok"] and "同一个说话人" in r["error"], r)
r = run("set_dub_style", {"lines": [1], "clone_candidate": "nope"})
check("⑩ 单句覆盖：不存在的候选 → 说清", not r["ok"] and "nope" in r["error"], r)
r = run("set_dub_style", {"lines": [1], "clone_candidate": c0["id"], "voice": "x"})
check("⑩ 单句覆盖：voice 和 clone_candidate 只能给一个", not r["ok"], r)
r = run("delete_voice_candidates", {"speaker": "0", "candidate": c0["id"]})
check("⑩ 删除：还被某一句单独用着的候选不能删，并指出是哪一句", not r["ok"] and "第 1 句" in r["error"], r)
FakeEnroll.deleted.clear()
r = run("delete_voice_candidates", {"speaker": "0"})
left = store.get_state(VID).speaker("0").clone_candidates
check("⑩ 删除全部：跳过被占用的，只删没人用的", r["ok"] and [c["id"] for c in left] == [c0["id"]] and FakeEnroll.deleted == [c1["voice_id"]], (r, left))
run("set_dub_style", {"lines": [1], "voice": ""})
check("⑩ 清除单句音色后（回到跟随说话人）就能删了", run("delete_voice_candidates", {"speaker": "0"})["ok"]
      and store.get_state(VID).speaker("0").clone_candidates == [])

# 清理
fresh()
run("generate_voice_candidates", {"speaker": "0"})
store.mutate(VID, set_current, actor="system", cmd="seed", summary="cur")
before = len(store.get_state(VID).speaker("0").clone_candidates)
FakeEnroll.deleted.clear()
r = run("delete_voice_candidates", {"speaker": "0"})
sp = store.get_state(VID).speaker("0")
check("⑩ 清理：服务端音色一起删，释放额度", r["ok"] and len(FakeEnroll.deleted) == before and sp.clone_candidates == [], r)
check("⑩ 清理不动当前选用的音色", sp.clone and sp.clone["voice_id"] == "CUR")
check("⑩ 没有候选可删 → 说清", not run("delete_voice_candidates", {"speaker": "0"})["ok"])

# 单个克隆命令也走新流程
FakeEnroll.created.clear()
r = run("clone_voice", {"speaker": "0", "preset": "long_one"})
sp = store.get_state(VID).speaker("0")
check("⑪ clone_voice 指定预设 → 记下预设和参考句", r["ok"] and sp.clone["preset"] == "long_one" and sp.clone["lines"] == ["s2"], r)
r = run("clone_voice", {"speaker": "0", "reference_lines": [1, 4]})
sp = store.get_state(VID).speaker("0")
check("⑪ clone_voice 指定参考句 → 预设记为 manual", r["ok"] and sp.clone["preset"] == "manual" and sp.clone["lines"] == ["s0", "s3"], r)
check("⑪ 每次注册都带 max_prompt_audio_length", all(c["max"] == 20.0 for c in FakeEnroll.created))

# 旧工程（没有候选字段）能读出来
old = {"id": "0", "name": "Speaker 0", "voice": None, "clone": {"voice_id": "x", "target_model": config.TTS_MODEL, "created_at": 1}}
sp_old = Speaker.from_dict(old)
old["preview_line"] = "s3"        # 旧版存过的字段，现在已经没有了：读的时候忽略
check("⑫ 旧工程的说话人没有候选字段、带着已删除的旧字段也能读", Speaker.from_dict(old).clone_candidates == [] and not hasattr(Speaker.from_dict(old), "preview_line"))

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)


def test_voice_clone():
    assert not fails, fails
