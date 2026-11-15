"""
非语言声的保留、词级时间戳、能量候选、规则拦截层、重叠字幕。

这一批功能的共同点：**不依赖模型表现**。它们是确定性的 —— 同样的音轨和同样的行，永远得到同样的结果 ——
所以每一条都能用真实数据（合成音轨、真实 ASR 基线）验证，而不是"看起来对"。

  ① ASR 句子 → 行（词级时间戳）、旁路文件、分句与边界
  ② 能量候选：帧级边界、减掉台词、跨 60 秒块的声音不被切断、真实样本上 3/3 命中且零误报
  ③ kind=vocal：混音条目、预览地址、预览接口、导出时真的把原声放回时间轴（频谱验证）
  ④ 规则拦截层：只拦 AI；放行 / 改写 / 拒绝
  ⑤ 重叠字幕合成两行
"""
import asyncio, dataclasses, io, json, os, subprocess, sys, tempfile, wave
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

import numpy as np
from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from app import asr_words, status, store, view
from app.commands import registry as R
from app.models import Segment, Speaker, VideoState, video_dir_path
from app.services import asr_service, audio_energy as AE, export_service, mix_service
from app.services import subtitle_service as SS

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

def brief(res):
    """失败详情里别带整段音频。"""
    if isinstance(res, dict) and isinstance(res.get("result"), dict):
        return {**res, "result": {k: v for k, v in res["result"].items() if k != "_audio"}}
    return res

def write_wav(path, x, rate):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())

def tone(rate, n_samples, freq, db=-20.0):
    t = np.arange(n_samples) / rate
    return np.sin(2 * np.pi * freq * t) * 10 ** (db / 20)

def track(rate, seconds, bursts, floor_db=-60.0, seed=1):
    """底噪 + 若干 (起, 止, 频率) 的音调段。"""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(int(rate * seconds)) * 10 ** (floor_db / 20)
    for a, b, f in bursts:
        i, j = int(a * rate), int(b * rate)
        x[i:j] += tone(rate, j - i, f)
    return x

def ffmpeg_mp3(path, freq, seconds):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=f={freq}:d={seconds}", "-ar", "24000",
                    "-c:a", "libmp3lame", path], check=True)

# ============================================================================ ① ASR → 行 / 旁路文件
sent = {"begin_time": 1040, "end_time": 3840, "text": "大家好，欢迎收看。", "speaker_id": 1, "sentence_id": 1,
        "words": [{"begin_time": 1040, "end_time": 1640, "text": "大家好", "punctuation": "，", "confidence": 1.0},
                  {"begin_time": 1960, "end_time": 2440, "text": "欢迎", "punctuation": "", "confidence": 1.0},
                  {"begin_time": 2440, "end_time": 3000, "text": "收看", "punctuation": "。", "confidence": 1.0}]}
line = asr_service.sentence_to_line(sent, "seg-x")
check("① 句子转行：时间换成秒、说话人转字符串、带词级时间戳",
      line["start_time"] == 1.04 and line["end_time"] == 3.84 and line["speaker_id"] == "1" and len(line["words"]) == 3
      and line["words"][0] == {"b": 1.04, "e": 1.64, "t": "大家好", "p": "，"}, line)
bad = asr_service.sentence_to_line({"begin_time": 0, "end_time": 1000, "text": "x",
                                    "words": [{"text": "缺时间"}, {"begin_time": "bad", "end_time": 1}, None, {"begin_time": 0, "end_time": 500, "text": "好"}]}, "s")
check("① 坏词（缺时间、格式不对、null）被跳过，不影响这一行", [w["t"] for w in bad["words"]] == ["好"], bad)
check("① 没有 words 字段也行（旧返回格式）", asr_service.sentence_to_line({"begin_time": 0, "end_time": 1, "text": "x"}, "s")["words"] == [])

VID = "vocalkeep00000000000000000000000"
src = os.path.join(TMP, "v.mp4"); open(src, "wb").write(b"\0")
def fresh():
    if store.exists(VID): store.delete(VID)
    store.create(VID, "v.mp4", src)
    def seed(s):
        s.target_language = "English"
        s.speakers = [Speaker("0", "Speaker 0", voice="Emily_v3.1"), Speaker("1", "Speaker 1", voice="Brian_v3.1")]
        s.segments = [
            Segment("a", "0", 1.0, 3.0, "你好，今天天气不错", translated_text="Hello, nice weather"),
            Segment("b", "1", 3.5, 6.0, "是啊，我们出去走走吧", translated_text="Yes let's go out"),
            Segment("c", "0", 9.0, 11.0, "好的", translated_text="OK"),
        ]
        s.stage = "ready"
    store.mutate(VID, seed, actor="system", cmd="seed", summary="seed")

def words_for(sid, text, spans):
    return {"text": text, "words": [{"b": a, "e": b, "t": t, "p": p} for a, b, t, p in spans]}

fresh()
os.makedirs(video_dir_path(VID), exist_ok=True)
asr_words.save(VID, [{"id": "a", "text": "你好，今天天气不错", "words": [
    {"b": 1.0, "e": 1.6, "t": "你好", "p": "，"}, {"b": 1.9, "e": 2.4, "t": "今天", "p": ""}, {"b": 2.4, "e": 3.0, "t": "天气不错", "p": ""}]},
    {"id": "nowords", "text": "x", "words": []}])
seg_a = store.get_state(VID).segment("a")
cl = asr_words.clauses(VID, seg_a)
check("① 旁路文件：按标点切成分句（2 个）", cl and [c.text for c in cl] == ["你好，", "今天天气不错"], cl)
check("① 分句时间 = 首词起、末词止", cl[0].t0 == 1.0 and cl[0].t1 == 1.6 and cl[1].t0 == 1.9 and cl[1].t1 == 3.0)
check("① 切点取两个分句之间空隙的中点（不切到词里）", asr_words.boundaries(cl) == [(1, 1.75)], asr_words.boundaries(cl))
check("① 取第 1~2 分句的时间窗", asr_words.window(cl, 1, 2) == (1.0, 3.0) and asr_words.window(cl, 2, 2) == (1.9, 3.0))
check("① 分句编号不合法 → None（不猜）", asr_words.window(cl, 0, 1) is None and asr_words.window(cl, 2, 1) is None and asr_words.window(cl, 1, 9) is None)
check("① 没有词级信息的行不存", "nowords" not in asr_words.load(VID))
def edit_a(s): s.segment("a").text = "你好，今天天气很好"
store.mutate(VID, edit_a, actor="user", cmd="edit", summary="e")
check("① 行的原文被改过 → 查不到词级信息（不会拿旧的时间套新的字）", asr_words.clauses(VID, store.get_state(VID).segment("a")) is None)
check("① 没有旁路文件 → 空、不抛", asr_words.load("nosuchproject000000000000000000000") == {})
check("① normalize 去掉标点和空白", asr_words.normalize("你好， 今天 天气！") == "你好今天天气")

# ============================================================================ ② 能量候选
class L:                                    # 够用的"行"：只有 candidates 要的字段
    def __init__(self, i, s, e): self.id, self.start_time, self.end_time = f"l{i}", s, e

fresh()
D = video_dir_path(VID)
write_wav(os.path.join(D, "extracted_audio.wav"), track(16000, 14.0, [(1.0, 3.0, 300), (3.5, 6.0, 400), (7.0, 7.8, 500), (9.0, 11.0, 300)]), 16000)
lines = [L(1, 1.0, 3.0), L(2, 3.5, 6.0), L(3, 9.0, 11.0)]
c = AE.find_candidates(VID, lines)
check("② 台词之外的声音是候选：恰好 1 个（7.0–7.8），台词本身不是", c is not None and len(c) == 1 and abs(c[0]["start"] - 7.0) <= 0.05 and abs(c[0]["end"] - 7.8) <= 0.05, c)
check("② 候选带位置：在第 2 句之后", c[0]["after_line"] == 2 and c[0]["after_id"] == "l2", c[0])
check("② 候选的数字是普通 float（能直接进 JSON）", type(c[0]["start"]) is float and json.dumps(c))
check("② 减掉台词：把台词去掉，同一段声音就整块成为候选",
      len(AE.find_candidates(VID, [])) == 4)
write_wav(os.path.join(D, "extracted_audio.wav"), track(16000, 14.0, [(1.0, 3.0, 300), (3.1, 3.3, 500)]), 16000)
c = AE.find_candidates(VID, [L(1, 1.0, 3.0)])
check("② 短于 0.3 秒的残余不算候选（紧挨台词的 0.2 秒尾巴）", c == [], c)
write_wav(os.path.join(D, "extracted_audio.wav"), track(16000, 14.0, [(1.0, 3.0, 300), (3.2, 4.2, 500)]), 16000)
c = AE.find_candidates(VID, [L(1, 1.0, 3.0)])
check("② 紧贴台词的声音：台词的部分被抠掉，只剩自己的（以前会和台词连成一块）",
      len(c) == 1 and 3.15 <= c[0]["start"] <= 3.3 and abs(c[0]["end"] - 4.2) <= 0.05, c)
write_wav(os.path.join(D, "extracted_audio.wav"), track(16000, 14.0, [(2.0, 2.15, 500)]), 16000)
check("② 短促的咔哒（0.15 秒）不算", AE.find_candidates(VID, []) == [])
write_wav(os.path.join(D, "extracted_audio.wav"), track(16000, 14.0, [(2.0, 2.4, 500), (2.5, 2.9, 500)]), 16000)
c = AE.find_candidates(VID, [])
check("② 间隔 0.1 秒的两声并成一段（笑声的“哈、哈”）", len(c) == 1 and abs(c[0]["start"] - 2.0) <= 0.05 and abs(c[0]["end"] - 2.9) <= 0.05, c)

# 跨块：读音轨是按 60 秒一块读的，横跨块边界的声音不能被切成两段
write_wav(os.path.join(D, "extracted_audio.wav"), track(16000, 130.0, [(59.4, 60.6, 500)]), 16000)
c = AE.find_candidates(VID, [])
check("② 横跨 60 秒块边界的声音是完整的一段（59.4–60.6）", len(c) == 1 and abs(c[0]["start"] - 59.4) <= 0.05 and abs(c[0]["end"] - 60.6) <= 0.05, c)
check("② 音轨时长读对（130 秒）", abs(AE.audio_seconds(VID) - 130.0) <= 0.05, AE.audio_seconds(VID))

write_wav(os.path.join(D, "extracted_audio.wav"), track(16000, 14.0, [(1.0, 3.0, 300)]), 16000)
check("② 有声窗口的有声比例高、静音窗口为 0", AE.active_fraction(VID, 1.2, 2.8) > 0.9 and AE.active_fraction(VID, 6.0, 8.0) == 0.0)
write_wav(os.path.join(D, "extracted_audio.wav"), track(16000, 14.0, [(6.0, 8.0, 300)]), 16000)
check("② 音轨换了内容，结果跟着变（缓存按文件修改时间失效）", AE.active_fraction(VID, 1.2, 2.8) == 0.0 and AE.active_fraction(VID, 6.2, 7.8) > 0.9)
os.remove(os.path.join(D, "extracted_audio.wav"))
check("② 没有音轨 → None，不是空列表（“没有证据”和“没有候选”要分开）",
      AE.find_candidates(VID, []) is None and AE.active_fraction(VID, 0, 1) is None and AE.audio_seconds(VID) is None)
open(os.path.join(D, "extracted_audio.wav"), "wb").write(b"not a wav")
check("② 音轨损坏 → None，不抛", AE.find_candidates(VID, []) is None)
write_wav(os.path.join(D, "vocals_asr.wav"), track(16000, 5.0, []), 16000)
check("② is_separated：有 vocals_asr.wav 才算分离过", AE.is_separated(VID))
os.remove(os.path.join(D, "vocals_asr.wav"))
check("② 没有 vocals_asr.wav → 未分离", not AE.is_separated(VID))

# 聚焦窗口：留余量，碰到邻居就停
fresh(); D = video_dir_path(VID)
write_wav(os.path.join(D, "extracted_audio.wav"), track(16000, 14.0, [(1.0, 3.0, 300), (3.2, 4.0, 500), (8.0, 9.0, 300)]), 16000)
fw_lines = [L(1, 1.0, 3.0), L(2, 8.0, 9.0)]
lo, hi = AE.focus_window(VID, fw_lines, fw_lines[1])
check("② 聚焦窗口：没有邻居挡着（最近的声音在 4.0 秒，离得很远）→ 前后各留满 0.3 秒", abs(lo - 7.7) < 1e-6 and abs(hi - 9.3) < 1e-6, (lo, hi))
lo, hi = AE.focus_window(VID, fw_lines, fw_lines[0])
check("② 聚焦窗口：紧挨着一段没被台词覆盖的声音（3.2 秒起）→ 终点停在那里，不把它剪进来", abs(hi - 3.2) <= 0.06 and abs(lo - 0.7) < 1e-6, (lo, hi))
nb = [L(1, 1.0, 3.0), L(2, 3.1, 5.0), L(3, 5.2, 6.0)]
check("② 聚焦窗口：紧挨着别的台词 → 在那句的边界停下（3.0~3.1 之间，不压过去）", AE.focus_window(VID, nb, nb[1])[0] == 3.0 and AE.focus_window(VID, nb, nb[1])[1] == 5.2, AE.focus_window(VID, nb, nb[1]))
check("② 聚焦窗口：永远包住这一行本身，起点不小于 0", (lambda w: w[0] <= 1.0 and w[1] >= 3.0)(AE.focus_window(VID, nb, nb[0])) and AE.focus_window(VID, [L(1, 0.1, 1.0)], L(1, 0.1, 1.0))[0] == 0.0)
ov = [L(1, 1.0, 5.0), L(2, 2.0, 6.0)]
w = AE.focus_window(VID, ov, ov[0])
check("② 聚焦窗口：重叠的两行互不是对方的边界（那就是要一起听的内容）", w[0] <= 1.0 and w[1] >= 5.0, w)

# 真实样本（合成音轨 + 真实 ASR 基线）：3 个非语言声全部命中、零误报
SAMPLE = os.path.join(os.path.dirname(BACKEND), "samples", "problem_video")
if os.path.exists(os.path.join(SAMPLE, "speech_stem.flac")) and os.path.exists(os.path.join(SAMPLE, "asr_baseline", "asr_vocals.json")):
    fresh(); D = video_dir_path(VID)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", os.path.join(SAMPLE, "speech_stem.flac"), "-ac", "1", "-ar", "16000",
                    os.path.join(D, "vocals_asr.wav")], check=True)
    truth = json.load(open(os.path.join(SAMPLE, "truth.json"), encoding="utf-8"))
    asr = json.load(open(os.path.join(SAMPLE, "asr_baseline", "asr_vocals.json"), encoding="utf-8"))["parsed_lines"]
    segs = [Segment(l["id"], "0", l["start_time"], l["end_time"], l["text"]) for l in asr]
    cands = AE.find_candidates(VID, segs)
    want = [e for e in truth["events"] if e["category"] == "nonverbal_human" and e["id"] in truth["expected"]["uncovered_voice"]]
    check("② 真实样本：3 个 ASR 没转写的非语言声（笑、叹、句尾笑）全部找到", len(cands) == 3 == len(want), cands)
    check("② 真实样本：每个候选的边界和真值差不到 0.1 秒",
          all(abs(c["start"] - e["start"]) < 0.1 and abs(c["end"] - e["end"]) < 0.1 for c, e in zip(cands, want)), (cands, [(e["start"], e["end"]) for e in want]))
    check("② 真实样本：零误报（每个候选都对应一个真值）", all(any(min(c["end"], e["end"]) - max(c["start"], e["start"]) > 0.05 for e in want) for c in cands))
    check("② 真实样本：位置对（第 3 句后两个、第 7 句后一个——末尾的笑不再并进前一句）", [c["after_line"] for c in cands] == [3, 3, 7], [c["after_line"] for c in cands])
else:
    print("SKIP ② 真实样本（samples/problem_video 不在）")

# ============================================================================ ③ kind=vocal
def mk_state(with_bg=True, with_vocals=True, segs=None, dubbing=True):
    vid = "vocalmix00000000000000000000000"
    d = os.path.join(TMP, vid); os.makedirs(d, exist_ok=True)
    for name in ("background.wav", "vocals.wav"):
        p = os.path.join(d, name)
        if os.path.exists(p): os.remove(p)
    if with_bg: write_wav(os.path.join(d, "background.wav"), track(44100, 20.0, []), 44100)
    if with_vocals: write_wav(os.path.join(d, "vocals.wav"), track(44100, 20.0, [(5.0, 6.5, 880)]), 44100)
    mp = os.path.join(TMP, "mixsrc.mp4"); open(mp, "ab").close()
    st = VideoState(video_id=vid, file_path=mp, filename="v.mp4", target_language="English")
    st.enable_dubbing = dubbing
    st.segments = segs if segs is not None else []
    return st, d

def speech(sid, s, e, d):
    sg = Segment(sid, "0", s, e, "x", translated_text="y")
    p = os.path.join(d, "tts", f"{sid}.mp3"); ffmpeg_mp3(p, 330, 1.0)
    sg.audio_path, sg.actual_duration = p, 1.0
    return sg

st, d = mk_state()
st.segments = [speech("s1", 1.0, 2.0, d), Segment("v1", "0", 5.0, 6.5, "（笑声）", kind="vocal")]
plan = mix_service.build_mix_plan(st)
ents = {e["id"]: e for e in plan["segments"]}
check("③ 分离过：vocal 行有混音条目，来源是人声轨、原速、不裁剪", ents.get("v1", {}).get("source") == "vocals" and ents["v1"]["atempo"] == 1.0
      and ents["v1"]["trim"] is None and ents["v1"]["videoRate"] == 1.0 and ents["v1"]["played"] == 1.5, ents.get("v1"))
check("③ 普通配音条目的形状不变（没有 source 字段，计划版本号不被无关地改动）", "source" not in ents["s1"])
check("③ 垫底是背景轨，不压低（保留的原声不会让垫底被压）", plan["bed"]["kind"] == "background" and plan["bed"]["duck"] == [])
st.segments[1].muted = True
check("③ 静音的 vocal 行不保留（用户可以把笑声去掉）", all(e["id"] != "v1" for e in mix_service.build_mix_plan(st)["segments"]))
st.segments[1].muted = False
v_a = mix_service.build_mix_plan(st)["version"]
st.segments[1].end_time = 6.0
check("③ vocal 行改了时间窗，计划版本号跟着变（播放端据此重取）", mix_service.build_mix_plan(st)["version"] != v_a)
st.segments[1].end_time = 6.5

st2, d2 = mk_state(with_bg=False)
st2.segments = [speech("s1", 1.0, 2.0, d2), Segment("v1", "0", 5.0, 6.5, "（笑声）", kind="vocal")]
p2 = mix_service.build_mix_plan(st2)
check("③ 没分离（垫底是整条原声）：不保留，原声里本来就有，再叠一份会重复", p2["bed"]["kind"] == "original" and all(e["id"] != "v1" for e in p2["segments"]))
check("③ 没分离时 vocal 行不让原声垫底被压低（压低的只有配音的 1~2 秒）",
      all(abs(t - 5.0) > 0.5 and abs(t - 6.5) > 0.5 for t, _ in p2["bed"]["duck"]), p2["bed"]["duck"])
st3, d3 = mk_state(with_vocals=False)
st3.segments = [speech("s1", 1.0, 2.0, d3), Segment("v1", "0", 5.0, 6.5, "（笑声）", kind="vocal")]
check("③ 有背景轨但没有人声轨：不保留，也不报错", all(e["id"] != "v1" for e in mix_service.build_mix_plan(st3)["segments"]))
st4, d4 = mk_state(segs=[Segment("v1", "0", 5.0, 6.5, "（笑声）", kind="vocal")])
try:
    mix_service.build_mix_plan(st4); raised = False
except RuntimeError:
    raised = True
check("③ 只有保留的原声、没有任何配音：不算“有配音”，照旧拒绝导出", raised)
st5, d5 = mk_state(dubbing=False, segs=[Segment("v1", "0", 5.0, 6.5, "（笑声）", kind="vocal")])
p5 = mix_service.build_mix_plan(st5)
check("③ 仅字幕模式：原声直通，不做任何保留条目", p5["dubbed"] is False and p5["segments"] == [])

# 视图
st, d = mk_state()
st.segments = [speech("s1", 1.0, 2.0, d), Segment("v1", "0", 5.0, 6.5, "（笑声）", kind="vocal")]
check("③ 视图：vocal 行带预览地址（含时间窗做缓存标记）", view.segment_view(st, st.segments[1], 1)["audioUrl"] == f"/api/projects/{st.video_id}/keep/v1?v=5.000-6.500"
      and view.segment_view(st, st.segments[1], 1)["kind"] == "vocal", view.segment_view(st, st.segments[1], 1)["audioUrl"])
st.segments[1].muted = True
check("③ 视图：静音的 vocal 行没有预览地址", view.segment_view(st, st.segments[1], 1)["audioUrl"] is None)
st.segments[1].muted = False
os.remove(os.path.join(d, "vocals.wav"))
check("③ 视图：没有人声轨时没有预览地址", view.segment_view(st, st.segments[1], 1)["audioUrl"] is None)

# 预览接口（真实 HTTP）
from fastapi.testclient import TestClient
from app.main import app
fresh(); D = video_dir_path(VID)
def seed_vocal(s):
    s.segments.append(Segment("v1", "0", 7.0, 8.5, "（笑声）", kind="vocal"))
store.mutate(VID, seed_vocal, actor="user", cmd="t", summary="t")
client = TestClient(app); P = f"/api/projects/{VID}"
r = client.get(f"{P}/keep/v1")
check("③ 预览接口：没做过人声分离 → 404 并说明原因", r.status_code == 404 and "分离" in r.json()["detail"], (r.status_code, r.text[:80]))
write_wav(os.path.join(D, "background.wav"), track(44100, 14.0, []), 44100)
write_wav(os.path.join(D, "vocals.wav"), track(44100, 14.0, [(7.0, 8.5, 880)]), 44100)
r = client.get(f"{P}/keep/v1")
w = wave.open(io.BytesIO(r.content)) if r.status_code == 200 else None
check("③ 预览接口：返回 wav，时长就是这一行的时间窗（1.5 秒），wav 头的长度是对的",
      r.status_code == 200 and r.headers["content-type"] == "audio/wav" and abs(w.getnframes() / w.getframerate() - 1.5) < 0.02, (r.status_code, w and w.getnframes() / w.getframerate()))
x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float)
peak = np.argmax(np.abs(np.fft.rfft(x * np.hanning(len(x))))) * w.getframerate() / len(x)
check("③ 预览接口：切的是这一行的窗口（频谱峰在 880 Hz，不是别处的静音）", abs(peak - 880) < 15, peak)
check("③ 预览接口：普通台词行没有可保留的原声 → 404", client.get(f"{P}/keep/a").status_code == 404)
check("③ 预览接口：不存在的行 → 404", client.get(f"{P}/keep/nope").status_code == 404)
check("③ 预览接口：不安全的 id → 拒绝", client.get(f"{P}/keep/..%2Fetc").status_code in (400, 404))
def mute_v(s): s.segment("v1").muted = True
store.mutate(VID, mute_v, actor="user", cmd="t", summary="t")
check("③ 预览接口：静音的行 → 404", client.get(f"{P}/keep/v1").status_code == 404)

# 导出：原声真的被放回时间轴（用频谱验证位置和内容，不是只看条目存在）
st, d = mk_state()
st.segments = [speech("s1", 1.0, 2.0, d), Segment("v1", "0", 5.0, 6.5, "（笑声）", kind="vocal")]
plan = mix_service.build_mix_plan(st)
tl_path = export_service.build_dubbed_timeline(st.video_id, st, 10.0, plan)
w = wave.open(tl_path); rate = w.getframerate(); tl = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float); w.close()
def peak_hz(a, b):
    seg = tl[int(a * rate): int(b * rate)]
    return np.argmax(np.abs(np.fft.rfft(seg * np.hanning(len(seg))))) * rate / len(seg)
def rms(a, b): return float(np.sqrt(np.mean(tl[int(a * rate): int(b * rate)] ** 2)))
check("③ 导出时间轴：配音在 1~2 秒（330 Hz）", abs(peak_hz(1.1, 1.9) - 330) < 15 and rms(1.1, 1.9) > 500, (peak_hz(1.1, 1.9), rms(1.1, 1.9)))
check("③ 导出时间轴：保留的原声在 5~6.5 秒（来自人声轨，880 Hz）", abs(peak_hz(5.1, 6.4) - 880) < 20 and rms(5.1, 6.4) > 500, (peak_hz(5.1, 6.4), rms(5.1, 6.4)))
check("③ 导出时间轴：其余地方是静音", rms(3.0, 4.5) < 50 and rms(7.0, 9.0) < 50, (rms(3.0, 4.5), rms(7.0, 9.0)))
check("③ 导出时间轴：保留的原声没被变速（长度就是 1.5 秒，不多不少）",
      abs(np.count_nonzero(np.abs(tl[int(4.9 * rate): int(7.0 * rate)]) > 200) / rate - 1.5) < 0.1)

# 命令：mark_non_speech / insert_line
fresh()
def run(name, args=None, actor="ai"): return asyncio.run(R.execute(VID, name, args or {}, actor=actor))
todo_before = run("get_overview")["result"]["todo"]["translate"]
r = run("mark_non_speech", {"lines": [3], "kind": "vocal"}, actor="user")
stt = store.get_state(VID)
check("③ mark_non_speech 能标 vocal", r["ok"] and stt.segments[2].kind == "vocal", r)
check("③ vocal 行：不翻译、不配音、不进待办（待办少了这一行）",
      status.tr_status(stt, stt.segments[2]) == status.NONE and status.tts_status(stt, stt.segments[2]) == status.NONE
      and run("get_overview")["result"]["todo"]["translate"] == todo_before - 1, (todo_before, run("get_overview")["result"]["todo"]))
check("③ vocal 行不出字幕", all("好的" != c.text for c in SS.build_cues(stt.segments, SS.default_style(), probe_audio=False)))
r = run("insert_line", {"after_line": 2, "start": 6.5, "end": 7.5, "text": "（笑声）", "kind": "vocal"}, actor="user")
stt = store.get_state(VID)
check("③ insert_line kind=vocal：不需要说话人，借前一句的", r["ok"] and stt.segments[2].kind == "vocal" and stt.segments[2].speaker_id == stt.segments[1].speaker_id, r)
check("③ insert_line 默认还是要说话人", not run("insert_line", {"after_line": 0, "start": 0, "end": 1, "text": "x"}, actor="user")["ok"])
check("③ 返回里带 kind", r["result"]["kind"] == "vocal")

# ============================================================================ ④ 规则拦截层
def gfresh(with_audio=True, separated=False):
    fresh(); d = video_dir_path(VID)
    if with_audio:
        write_wav(os.path.join(d, "extracted_audio.wav"),
                  track(16000, 14.0, [(1.0, 3.0, 300), (3.5, 6.0, 400), (7.0, 7.8, 500), (9.0, 11.0, 300)]), 16000)
    asr_words.save(VID, [{"id": "a", "text": "你好，今天天气不错", "words": [
        {"b": 1.0, "e": 1.6, "t": "你好", "p": "，"}, {"b": 1.9, "e": 2.4, "t": "今天", "p": ""}, {"b": 2.4, "e": 3.0, "t": "天气不错", "p": ""}]}])
    return d

gfresh()
r = run("insert_line", {"after_line": 2, "start": 4.0, "end": 5.0, "text": "（咳嗽）", "kind": "vocal"})
check("④ vocal 窗口压在别人说话上 → 拒绝，原因点明漏出原声，建议改用标签", not r["ok"] and "第 2 句" in r["error"] and "人声" in r["error"] and "[cough]" in r.get("hint", ""), r)
check("④ …被拒绝时一行都没插进去", len(store.get_state(VID).segments) == 3)
check("④ 同样的参数，用户自己做 → 不拦（规则只拦 AI）",
      run("insert_line", {"after_line": 2, "start": 4.0, "end": 5.0, "text": "（咳嗽）", "kind": "vocal"}, actor="user")["ok"])

gfresh()
r = run("insert_line", {"after_line": 2, "start": 6.8, "end": 8.1, "text": "（笑声）", "kind": "vocal"})
stt = store.get_state(VID)
check("④ 空隙里的 vocal：放行，起止吸附到检测到的声音边界（7.0–7.8），说明写进结果",
      r["ok"] and abs(stt.segments[2].start_time - 7.0) <= 0.05 and abs(stt.segments[2].end_time - 7.8) <= 0.05
      and any("吸附" in n for n in r["result"].get("guard_notes", [])), brief(r))
r = run("insert_line", {"after_line": 3, "start": 6.9, "end": 7.9, "text": "（笑声）", "kind": "vocal"})
check("④ 同一处再插一次 → 拒绝（那里已经被刚插入的行覆盖，幂等）", not r["ok"] and "已经被" in r["error"], r)

gfresh()
r = run("insert_line", {"after_line": 2, "start": 12.0, "end": 13.0, "text": "（笑声）", "kind": "vocal"})
check("④ 静音里的 vocal → 拒绝，建议先 find_uncovered_sounds", not r["ok"] and "没有" in r["error"] and "find_uncovered_sounds" in r.get("hint", ""), r)
r = run("insert_line", {"after_line": 2, "start": 7.0, "end": 16.0, "text": "（笑声）", "kind": "vocal"})
check("④ 过长的 vocal（9 秒）→ 拒绝，提示这更像一整句话", not r["ok"] and "整句话" in r["error"], r)
r = run("insert_line", {"after_line": 2, "start": 6.0, "end": 6.4, "text": "（笑声）", "kind": "vocal"})
check("④ 台词之间的静音里 vocal → 拒绝", not r["ok"], r)
r = run("insert_line", {"after_line": 2, "start": 20.0, "end": 21.0, "text": "x", "speaker": "0"})
check("④ 起点超出音频长度 → 拒绝并说明音频多长", not r["ok"] and "14" in r["error"], r)
r = run("insert_line", {"after_line": 2, "start": 12.2, "end": 13.5, "text": "凭空多出来的一句", "speaker": "0"})
check("④ 补一句话但那里是静音 → 拒绝（不能凭空造）", not r["ok"] and "几乎没有声音" in r["error"], r)
r = run("insert_line", {"after_line": 2, "start": 7.0, "end": 7.8, "text": "被漏掉的一句", "speaker": "0"})
check("④ 补一句话、那里有声音 → 放行", r["ok"] and store.get_state(VID).segments[2].text == "被漏掉的一句", r)

gfresh()
write_wav(os.path.join(video_dir_path(VID), "extracted_audio.wav"),
          track(16000, 14.0, [(1.0, 3.0, 300), (3.5, 6.0, 400), (9.0, 11.0, 300), (11.1, 12.0, 500)]), 16000)
r = run("insert_line", {"after_line": 3, "start": 10.9, "end": 12.2, "text": "（笑声）", "kind": "vocal"})
stt = store.get_state(VID)
check("④ AI 给的大概窗口压到旁边台词的边上（差 0.1 秒）：先吸附到声音边界，不因此被拒绝",
      r["ok"] and abs(stt.segments[3].start_time - 11.1) <= 0.06 and abs(stt.segments[3].end_time - 12.0) <= 0.06, brief(r))
gfresh()
write_wav(os.path.join(video_dir_path(VID), "extracted_audio.wav"),
          track(16000, 14.0, [(1.0, 3.0, 300), (3.5, 6.0, 400), (9.0, 11.0, 300), (11.1, 12.0, 500)]), 16000)
r = run("insert_line", {"after_line": 3, "start": 9.5, "end": 11.4, "text": "（笑声）", "kind": "vocal"})
check("④ 窗口大部分压在台词上、只蹭到一点声音边界 → 仍然拒绝，原因是会漏出原声", not r["ok"] and "第 3 句" in r["error"] and "人声" in r["error"], r)

gfresh(with_audio=False)
r = run("insert_line", {"after_line": 2, "start": 6.0, "end": 7.0, "text": "（笑声）", "kind": "vocal"})
check("④ 没有音轨：没有证据就不拦（只剩和台词重叠这条不需要音轨的规则）", r["ok"], r)
r = run("insert_line", {"after_line": 0, "start": 12.0, "end": 13.0, "text": "话", "speaker": "0"})
check("④ 没有音轨时补话也放行", r["ok"], r)

# split_line
gfresh()
r = run("split_line", {"line": 1, "text_first": "你好，", "text_second": "今天天气不错", "at_seconds": 1.7})
stt = store.get_state(VID)
check("④ 拆句：给的切点离分句边界很近 → 对齐到边界（1.75），说明写进结果",
      r["ok"] and abs(stt.segments[1].start_time - 1.75) < 0.01 and any("对齐" in n for n in r["result"].get("guard_notes", [])), brief(r))
gfresh()
r = run("split_line", {"line": 1, "text_first": "你好，今天", "text_second": "天气不错", "at_seconds": 2.6})
check("④ 拆句：切点离任何分句边界都远 → 拒绝，列出这一句的分句边界", not r["ok"] and "1.75" in r["error"], r)
check("④ …被拒绝时这一行没动", len(store.get_state(VID).segments) == 3 and store.get_state(VID).segments[0].end_time == 3.0)
gfresh()
r = run("split_line", {"line": 1, "text_first": "你好，", "text_second": "今天天气不错"})
stt = store.get_state(VID)
check("④ 拆句：没给时间，前半句恰好是第 1 分句 → 用词级时间戳定（1.75），不再按字数估",
      r["ok"] and abs(stt.segments[1].start_time - 1.75) < 0.01 and any("词级" in n for n in r["result"].get("guard_notes", [])), brief(r))
gfresh()
r = run("split_line", {"line": 1, "text_first": "你好，今", "text_second": "天天气不错"})
check("④ 拆句：没给时间，前半句不是整分句 → 沿用按字数估算（行为不变）", r["ok"] and "guard_notes" not in r["result"], brief(r))
gfresh()
store.mutate(VID, lambda s: setattr(s.segment("a"), "text", "你好，今天天气很好"), actor="user", cmd="e", summary="e")
r = run("split_line", {"line": 1, "text_first": "你好，", "text_second": "今天天气很好", "at_seconds": 2.6})
check("④ 拆句：这一行的原文被改过，词级时间戳作废 → 规则跳过，按原样执行", r["ok"] and "guard_notes" not in r["result"], brief(r))
gfresh()
r = run("split_line", {"line": 1, "text_first": "你好，", "text_second": "今天天气不错", "at_seconds": 2.6}, actor="user")
check("④ 拆句：用户自己给的切点原样采用（不拦）", r["ok"] and abs(store.get_state(VID).segments[1].start_time - 2.6) < 0.01, brief(r))

# mark_non_speech vocal
gfresh()
r = run("mark_non_speech", {"lines": [3], "kind": "vocal"})
check("④ 把 2 秒的独立短行标成 vocal → 放行", r["ok"], r)
gfresh()
store.mutate(VID, lambda s: s.segments.append(Segment("long", "0", 12.0, 21.0, "很长的一段", kind="speech")), actor="user", cmd="t", summary="t")
r = run("mark_non_speech", {"lines": [4], "kind": "vocal"})
check("④ 把 9 秒的行标成 vocal → 拒绝（这是话）", not r["ok"] and "整句话" in r["error"], r)
gfresh()
store.mutate(VID, lambda s: s.segments.append(Segment("over", "0", 2.0, 3.8, "压在别人上面", kind="speech")), actor="user", cmd="t", summary="t")
r = run("mark_non_speech", {"lines": [4], "kind": "vocal"})
check("④ 把和别的台词重叠的行标成 vocal → 拒绝（会漏出原声）", not r["ok"] and "重叠" in r["error"], r)
r = run("mark_non_speech", {"lines": [4], "kind": "vocal"}, actor="user")
check("④ 用户自己标 → 不拦", r["ok"], r)
gfresh()
r = run("mark_non_speech", {"lines": [3], "kind": "music"})
check("④ 标 music / noise 不受 vocal 规则影响", r["ok"] and "guard_notes" not in r["result"], r)
r = run("mark_non_speech", {"lines": [99], "kind": "vocal"})
check("④ 行号不存在：规则不插手，由命令自己报错", not r["ok"] and "99" in r["error"], r)

# find_uncovered_sounds
gfresh()
r = run("find_uncovered_sounds")
check("④ find_uncovered_sounds：返回台词之外的声音（1 个）、带位置，没做分离时带警告",
      r["ok"] and r["result"]["count"] == 1 and r["result"]["sounds"][0]["after_line"] == 2 and "没做人声分离" in r["result"].get("warning", ""), brief(r))
write_wav(os.path.join(video_dir_path(VID), "vocals_asr.wav"), track(16000, 14.0, [(1.0, 3.0, 300), (3.5, 6.0, 400), (7.0, 7.8, 500), (9.0, 11.0, 300)]), 16000)
check("④ 做过分离就没有那条警告", "warning" not in run("find_uncovered_sounds")["result"])
gfresh(with_audio=False)
check("④ 没有音轨 → 清楚的报错", not run("find_uncovered_sounds")["ok"])
check("④ 它是只读命令，AI 能调", any(t["function"]["name"] == "find_uncovered_sounds" for t in R.tool_specs()) and R.get("find_uncovered_sounds").readonly)

# ============================================================================ ⑤ 重叠字幕
def sub_seg(sid, s, e, tr, orig="原文", **kw):
    return Segment(sid, "0", s, e, orig, translated_text=tr, **kw)
sty = SS.default_style()
cues = SS.build_cues([sub_seg("a", 19.71, 22.57, "I think this is great."), sub_seg("b", 20.01, 23.14, "No, I disagree.")], sty, probe_audio=False)
check("⑤ 两个人同时说：合成一条字幕，每人一行，前面加 “- ”",
      len(cues) == 1 and cues[0].lines(sty) == ["- I think this is great.", "- No, I disagree."], [c.lines(sty) for c in cues])
check("⑤ 这条字幕的起止覆盖两个人（起点取最早，终点取最晚）", abs(cues[0].start - 19.71) < 1e-6 and abs(cues[0].end - 23.14) < 1e-6)
check("⑤ 前一个人不会被截短（以前会在后一个人开口前 0.04 秒就消失）", cues[0].end >= 23.14 - 1e-6)
srt = SS.to_srt(cues, sty)
check("⑤ SRT 里是一块、两行", srt.count("-->") == 1 and "- I think this is great.\n- No, I disagree." in srt, srt)
cues = SS.build_cues([sub_seg("a", 0.0, 3.0, "First."), sub_seg("b", 2.9, 5.0, "Second.")], sty, probe_audio=False)
check("⑤ 只重叠 0.1 秒（ASR 边界误差）不合并，仍按原来的规则截断", len(cues) == 2 and cues[0].end < 2.9, [(c.start, c.end) for c in cues])
cues = SS.build_cues([sub_seg("a", 0.0, 3.0, "First."), sub_seg("b", 3.0, 5.0, "Second.")], sty, probe_audio=False)
check("⑤ 首尾相接的两句不受影响", len(cues) == 2 and cues[0].text == "First." and cues[1].text == "Second.")
cues = SS.build_cues([sub_seg("a", 0.0, 6.0, "Long one."), sub_seg("b", 1.0, 2.0, "Mid."), sub_seg("c", 3.0, 4.0, "Late.")], sty, probe_audio=False)
check("⑤ 三个人连环重叠：合成一条、三行", len(cues) == 1 and len(cues[0].lines(sty)) == 3, [c.lines(sty) for c in cues])
cues = SS.build_cues([sub_seg("a", 0.0, 3.0, "Mine."), sub_seg("b", 1.0, 3.0, "Hidden.", hidden=True)], sty, probe_audio=False)
check("⑤ 隐藏的那一行不参与合并（只剩一条普通字幕）", len(cues) == 1 and cues[0].text == "Mine." and cues[0].forced_lines is None)
cues = SS.build_cues([sub_seg("a", 0.0, 3.0, "Mine."), sub_seg("b", 1.0, 3.0, "Music?", kind="music")], sty, probe_audio=False)
check("⑤ 非语音行（音乐、vocal）不进字幕，也不参与合并", len(cues) == 1 and cues[0].text == "Mine.")
cues = SS.build_cues([sub_seg("a", 0.0, 4.0, "Hi there."), sub_seg("b", 1.0, 4.0, "Hey.")] + [sub_seg("c", 5.0, 7.0, "Later.")], sty, probe_audio=False)
check("⑤ 合并后的下一条字幕照常（前一条合并字幕截在它开始之前）", len(cues) == 2 and cues[0].end <= 5.0 - 0.04 + 1e-6 and cues[1].text == "Later.", [(c.start, c.end) for c in cues])
bil = dataclasses.replace(sty, track="bilingual")
cues = SS.build_cues([sub_seg("a", 0.0, 3.0, "Hello.", orig="你好"), sub_seg("b", 1.0, 3.0, "Hi.", orig="嗨")], bil, probe_audio=False)
check("⑤ 双语轨道：每个人的译文在前、原文在后，各占一行", cues[0].lines(bil) == ["- Hello.", "- Hi.", "- 你好", "- 嗨"], cues[0].lines(bil))
ordered_cues = SS.build_cues([sub_seg("b", 1.0, 3.0, "Second."), sub_seg("a", 0.0, 2.0, "First.")], sty, probe_audio=False)
check("⑤ 输入顺序乱也按开始时间排：先说的在上面一行", ordered_cues[0].lines(sty) == ["- First.", "- Second."], ordered_cues[0].lines(sty))
check("⑤ 合成的字幕在预览接口的形状里有 lines（前端按它画），text 为空", ordered_cues[0].lines(sty) and ordered_cues[0].text == "")
orig_sty = dataclasses.replace(sty, track="original")
cues = SS.build_cues([sub_seg("a", 0.0, 3.0, "x", orig="[sad]")], orig_sty, probe_audio=False)
check("⑤ 原文只有语气标签的行（画面上没有字可显示）不出空字幕", cues == [], cues)
cues = SS.build_cues([sub_seg("a", 0.0, 3.0, "Hello.", orig="你好"), sub_seg("b", 1.0, 3.0, "x", orig="[sad]")], orig_sty, probe_audio=False)
check("⑤ 重叠的两行里有一行没有可显示的文字 → 退化成普通字幕，没有空的 “- ” 行",
      len(cues) == 1 and cues[0].text == "你好" and cues[0].forced_lines is None and "- " not in "".join(cues[0].lines(orig_sty)), [c.lines(orig_sty) for c in cues])

# ============================================================================ ⑥ 端到端：真实样本 → AI 经规则层插入 → 真导出 → 从成片里测
# 样本：合成的两条轨（理想的人声轨 / 背景轨）+ 真实 ASR 的输出。AI 只给"大概位置"，
# 起止由规则层按检测到的声音边界对齐；然后真的导出一次，再把这三个 vocal 行静音导出一次做对照，
# 比较"成片里这三处相对配音的响度"——只有保留原声的那次，这三处才有笑声和叹息。
if os.path.exists(os.path.join(SAMPLE, "speech_stem.flac")) and os.path.exists(os.path.join(SAMPLE, "asr_baseline", "asr_vocals.json")):
    import shutil
    E2E = "vocale2e00000000000000000000000"
    if store.exists(E2E): store.delete(E2E)
    sample_mp4 = os.path.join(TMP, "sample.mp4"); shutil.copy(os.path.join(SAMPLE, "problem_sample.mp4"), sample_mp4)
    store.create(E2E, "sample.mp4", sample_mp4)
    ed = video_dir_path(E2E)
    def conv(src_name, out_name, rate):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", os.path.join(SAMPLE, src_name) if src_name != "MP4" else sample_mp4,
                        "-ac", "1", "-ar", str(rate), os.path.join(ed, out_name)], check=True)
    conv("speech_stem.flac", "vocals.wav", 44100); conv("speech_stem.flac", "vocals_asr.wav", 16000)
    conv("background_stem.flac", "background.wav", 44100); conv("MP4", "extracted_audio.wav", 16000)
    asr = json.load(open(os.path.join(SAMPLE, "asr_baseline", "asr_vocals.json"), encoding="utf-8"))["parsed_lines"]
    def seed_e2e(st):
        st.target_language = "English"; st.stage = "ready"; st.enable_dubbing = True
        st.speakers = [Speaker("0", "Speaker 0", voice="Emily_v3.1")]
        st.segments = [Segment(l["id"], "0", l["start_time"], l["end_time"], l["text"], translated_text=f"English line {i}")
                       for i, l in enumerate(asr, 1)]
        for sg in st.segments: sg.tr_fp = status.expected_tr_fp(st, sg)
    store.mutate(E2E, seed_e2e, actor="system", cmd="seed", summary="seed")
    for sg in store.get_state(E2E).segments:
        from app.models import tts_audio_path
        ffmpeg_mp3(tts_audio_path(E2E, sg.id), 330, 1.5)
    def mark_dubbed(st):
        for sg in st.segments: sg.tts_fp = status.expected_tts_fp(st, sg); sg.actual_duration = 1.5
    store.mutate(E2E, mark_dubbed, actor="system", cmd="dub", summary="dub")
    def ai(name, args=None): return asyncio.run(R.execute(E2E, name, args or {}, actor="ai"))

    # AI 只凭 find_uncovered_sounds 的结果和大概位置插入
    found = ai("find_uncovered_sounds")
    check("⑥ AI 先问有哪些没被台词覆盖的声音：3 个，已做过分离所以没有“结果偏多”的警告",
          found["ok"] and found["result"]["count"] == 3 and "warning" not in found["result"], brief(found))
    plan_ins = [(3, 11.0, 12.7, "（笑声）"), (4, 12.9, 14.3, "（叹气）"), (9, 32.2, 33.5, "（笑声）")]
    notes = []
    for after, a, b, label in plan_ins:
        r = ai("insert_line", {"after_line": after, "start": a, "end": b, "text": label, "kind": "vocal"})
        check(f"⑥ AI 在第 {after} 句后插入 {label}：放行", r["ok"], brief(r))
        notes += r.get("result", {}).get("guard_notes", [])
    check("⑥ 三处起止都被规则层对齐到了检测到的声音边界（给的是大概位置）", len(notes) == 3 and all("吸附" in n for n in notes), notes)
    es = store.get_state(E2E)
    vocal = [(i + 1, g) for i, g in enumerate(es.segments) if g.kind == "vocal"]
    truth_nv = [e for e in truth["events"] if e["category"] == "nonverbal_human" and e["id"] in truth["expected"]["uncovered_voice"]]
    check("⑥ 对齐后的窗口和真值差不到 0.1 秒", len(vocal) == 3 and all(abs(g.start_time - e["start"]) < 0.1 and abs(g.end_time - e["end"]) < 0.1
          for (_, g), e in zip(vocal, truth_nv)), [(g.start_time, g.end_time) for _, g in vocal])
    check("⑥ 再问一次：没有剩下的了（插入的行把候选覆盖掉，幂等）", ai("find_uncovered_sounds")["result"]["count"] == 0)
    c2 = TestClient(app)
    keep_urls = [view.segment_view(es, g, i)["audioUrl"] for i, g in enumerate(es.segments) if g.kind == "vocal"]
    check("⑥ 三行都有预览地址，接口真的返回对应时长的 wav",
          all(u and (lambda r: r.status_code == 200 and abs(wave.open(io.BytesIO(r.content)).getnframes() / config.EXPORT_SAMPLE_RATE - (g.end_time - g.start_time)) < 0.03)(c2.get(u))
              for u, (_, g) in zip(keep_urls, vocal)), keep_urls)
    mp = mix_service.build_mix_plan(store.get_state(E2E))
    check("⑥ 混音计划：7 句配音 + 3 条保留原声", sum(1 for e in mp["segments"] if e.get("source") == "vocals") == 3 and len(mp["segments"]) == 10, len(mp["segments"]))

    def export_levels():
        res = asyncio.run(export_service.export_video(E2E))
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", res.path, "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                             capture_output=True, check=True).stdout
        y = np.frombuffer(pcm, dtype=np.float32).astype(np.float64)
        def db(a, b):
            seg_ = y[int(a * 16000): int(b * 16000)]
            return 20 * np.log10(np.sqrt(np.mean(seg_ ** 2)) + 1e-9)
        ref = db(1.2, 3.6)                                     # 第 1 句的配音：用它做参照，消掉响度归一带来的整体增益差
        return {"laugh1": db(11.3, 12.4) - ref, "sigh": db(13.15, 14.05) - ref, "laugh2": db(32.4, 33.2) - ref,
                "music_only": db(24.0, 25.5) - ref, "wind_only": db(15.0, 15.5) - ref}
    kept = export_levels()
    for _, g in vocal:
        asyncio.run(R.execute(E2E, "set_muted", {"lines": [store.get_state(E2E).segments.index(store.get_state(E2E).segment(g.id)) + 1], "muted": True}, actor="user"))
    dropped = export_levels()
    for k in ("laugh1", "sigh", "laugh2"):
        # 实测：笑声 +10.8 dB、叹息 +4.3 dB、句尾笑声 +3.9 dB（它压在背景音乐下面）；不保留时差值是 0.0。门槛放在 3 dB。
        check(f"⑥ 成片里 {k} 处：保留原声比不保留响 ≥ 3 dB（{kept[k]:.1f} vs {dropped[k]:.1f}，都是相对配音）", kept[k] - dropped[k] >= 3.0, (kept[k], dropped[k]))
    check("⑥ 别处没被动到：只有背景音乐 / 只有风声的地方，两次成片的相对响度差不到 1.5 dB",
          abs(kept["music_only"] - dropped["music_only"]) < 1.5 and abs(kept["wind_only"] - dropped["wind_only"]) < 1.5, (kept, dropped))
else:
    print("SKIP ⑥ 端到端（samples/problem_video 不在）")

# ============================================================================ ⑦ 初始化时自动标出 ASR 漏掉的非语言声
# 只有这一类自动应用：边界是音轨能量定的（确定），结论只有一种。其余（太长、数量异常）只写进报告、
# 由聊天里的 AI 提示，人来判断。
from app import agent, jobs
from app.services import sound_scan
from app.commands import guards as G

class FJ:                                       # 任务的最小替身：step_sounds 只用 check()
    def check(self): pass

def sfresh(sounds, separated=True, dur=20.0, lines=None):
    """sounds / lines: [(起, 止)]。人声轨上台词是 300 Hz，没被覆盖的声音是 500 Hz。"""
    lines = lines if lines is not None else [(1.0, 3.0), (3.5, 6.0), (9.0, 11.0)]
    fresh(); d = video_dir_path(VID)
    def seed(st):
        st.segments = [Segment(f"l{i}", str(i % 2), a, b, f"原文{i}", translated_text=f"tr{i}") for i, (a, b) in enumerate(lines)]
        st.sounds_scanned = False
    store.mutate(VID, seed, actor="system", cmd="seed", summary="seed")
    bursts = [(a, b, 300) for a, b in lines] + [(a, b, 500) for a, b in sounds]
    x = track(16000, dur, bursts)
    if separated:
        write_wav(os.path.join(d, "vocals_asr.wav"), x, 16000)
        write_wav(os.path.join(d, "vocals.wav"), track(44100, dur, bursts), 44100)
        write_wav(os.path.join(d, "background.wav"), track(44100, dur, []), 44100)
    else:
        write_wav(os.path.join(d, "extracted_audio.wav"), x, 16000)

def step():
    return asyncio.run(jobs.step_sounds(VID, FJ()))

# ---- 基本：分离过 → 不长的自动标，位置和说话人对，可撤销
sfresh([(0.0, 0.5), (7.0, 7.8), (8.1, 8.6), (12.0, 13.0)])
before_ids = [g.id for g in store.get_state(VID).segments]
rep = step()
stt = store.get_state(VID)
kinds = [g.kind for g in stt.segments]
check("⑦ 4 处没被台词覆盖的声音全部标成 vocal，并插在正确的位置（句首 / 第 2 句后两处 / 第 3 句后）",
      kinds == ["vocal", "speech", "speech", "vocal", "vocal", "speech", "vocal"], kinds)
vs = [g for g in stt.segments if g.kind == "vocal"]
check("⑦ 起止是检测到的声音边界，标签是中性的“非语言声”（没有模型就不知道是笑还是叹，不乱猜）",
      all(abs(g.start_time - a) <= 0.06 and abs(g.end_time - b) <= 0.06 for g, (a, b) in zip(vs, [(0.0, 0.5), (7.0, 7.8), (8.1, 8.6), (12.0, 13.0)]))
      and all(g.text == sound_scan.LABEL for g in vs), [(g.start_time, g.end_time, g.text) for g in vs])
check("⑦ 说话人借前一句的（句首的借第一个说话人）；同一个位置的两段按时间排",
      [g.speaker_id for g in vs] == ["0", "1", "1", "0"] and vs[1].start_time < vs[2].start_time,
      [(g.speaker_id, g.start_time) for g in vs])
check("⑦ 原来的行一个没动、顺序没变", [g.id for g in stt.segments if g.kind == "speech"] == before_ids
      and all(g.translated_text for g in stt.segments if g.kind == "speech"))
check("⑦ 小结：track=vocals、已标记 4 处（带 id）、没有待确认的",
      rep["track"] == "vocals" and len(rep["applied"]) == 4 and rep["pendingCount"] == 0 and rep["applied"][0]["id"] in {g.id for g in vs}, rep)
check("⑦ 扫描标记已置位", stt.sounds_scanned is True)
check("⑦ 插入的行不和任何台词重叠，且都通过 AI 规则层对 vocal 窗口的检查（两套规则一致）",
      all(not G._speech_overlapping(stt, g.start_time, g.end_time, set()) for g in vs))
und = store.can_undo(VID)
check("⑦ 可撤销，且是一条记录（系统的其它改动不可撤销，所以最近一条就是它）", und and "自动标出 4 处" in und["summary"], und)
store.undo(VID, actor="user")
stt = store.get_state(VID)
check("⑦ 撤销：4 行全部移除，原来的行（含译文）原样还在", [g.kind for g in stt.segments] == ["speech"] * 3 and all(g.translated_text for g in stt.segments))
check("⑦ 撤销不会把扫描标记退回 False（否则再跑初始化会把用户刚撤销的又加回来）", stt.sounds_scanned is True)
rep2 = step()
check("⑦ 撤销之后再跑一遍：不会重新加（尊重用户的决定），小结里也不再算“待确认”",
      [g.kind for g in store.get_state(VID).segments] == ["speech"] * 3 and rep2["applied"] == [] and rep2["pendingCount"] == 0, rep2)
check("⑦ 这一遍没有产生新的可撤销记录（扫描标记是静默、不可撤销的）", store.can_undo(VID) is None, store.can_undo(VID))

# ---- 规则：什么时候不自动
sfresh([(7.0, 8.0), (12.0, 18.0)], dur=40.0)
rep = step(); stt = store.get_state(VID)
check("⑦ 超过 4 秒的片段不自动标（更像被漏掉的一句话）：只标了短的那个，长的留作待确认",
      [g.kind for g in stt.segments].count("vocal") == 1 and rep["pendingCount"] == 1 and rep["pending"][0]["reason"] == "long", rep)
check("⑦ 待确认的带位置（在哪一行之后）和起止，给聊天提示用", rep["pending"][0]["afterId"] == "l2" and abs(rep["pending"][0]["start"] - 12.0) <= 0.06, rep["pending"])

sfresh([(7.0 + 0.8 * i, 7.4 + 0.8 * i) for i in range(21)], dur=40.0, lines=[(1.0, 3.0), (3.5, 6.0)])   # 台词都在声音前面，21 处全在台词之外
rep = step()
check("⑦ 数量异常（21 处 > 20）：一处都不自动标（多半是这一段整体没识别好），全部待确认",
      [g.kind for g in store.get_state(VID).segments] == ["speech"] * 2 and rep["pendingCount"] == 21 and {p["reason"] for p in rep["pending"]} == {"too_many"}, rep["pendingCount"])
check("⑦ 报告里最多列 20 段，但总数是真的（21）", len(rep["pending"]) == 20 and rep["pendingCount"] == 21)
check("⑦ 这种情况也算扫描过了（不会每次初始化都重新判断）", store.get_state(VID).sounds_scanned is True)

sfresh([(7.0, 10.0), (11.0, 14.0), (15.0, 18.0)], dur=20.0, lines=[(1.0, 3.0), (3.5, 6.0)])
rep = step()
check("⑦ 没被台词覆盖的声音占全片 45%（超过 25%）：一处都不自动标，即使每段都不长",
      [g.kind for g in store.get_state(VID).segments] == ["speech"] * 2 and {p["reason"] for p in rep["pending"]} == {"too_many"} and rep["pendingCount"] == 3, rep)

sfresh([(7.0, 7.8), (12.0, 13.0)], separated=False)
rep = step(); stt = store.get_state(VID)
check("⑦ 没做人声分离：不自动标（候选里混着风声、音乐，原声本来就在垫底里），报告里记成 no_stem",
      [g.kind for g in stt.segments] == ["speech"] * 3 and rep["track"] == "original" and {p["reason"] for p in rep["pending"]} == {"no_stem"} and rep["pendingCount"] == 2, rep)
check("⑦ 没分离时不置扫描标记（之后分离了再跑初始化，还能自动标）", stt.sounds_scanned is False)
sfresh([(7.0, 7.8)])
os.remove(os.path.join(video_dir_path(VID), "background.wav"))
rep = step()
check("⑦ 有人声轨但没有背景轨（导出时没法保留）：不自动标", [g.kind for g in store.get_state(VID).segments] == ["speech"] * 3 and rep["pending"][0]["reason"] == "no_stem", rep)
sfresh([(7.0, 7.8)])
os.remove(os.path.join(video_dir_path(VID), "vocals_asr.wav"))
write_wav(os.path.join(video_dir_path(VID), "extracted_audio.wav"), track(16000, 20.0, [(1.0, 3.0, 300), (3.5, 6.0, 300), (9.0, 11.0, 300), (7.0, 7.8, 500)]), 16000)
rep = step()
check("⑦ 有全频带人声和背景轨、但 ASR 用的人声轨不在（候选来自混音，里面有背景声）：不自动标",
      rep["pendingCount"] == 1 and [g.kind for g in store.get_state(VID).segments] == ["speech"] * 3 and rep["pending"][0]["reason"] == "no_stem", rep)
sfresh([])
rep = step()
check("⑦ 没有任何漏掉的声音：什么都不改，也没有可撤销记录", rep["applied"] == [] and rep["pendingCount"] == 0 and store.can_undo(VID) is None, (rep, store.can_undo(VID)))
fresh()
rep = step()
check("⑦ 没有音轨：返回 track=none，不抛", rep["track"] == "none" and rep["applied"] == [])

# ---- 健壮：失败只降级；取消要往外传；计划之后状态变了要按最新的算
sfresh([(7.0, 7.8)])
orig_classify = sound_scan.classify
def boom(*a, **k): raise RuntimeError("音轨坏了")
sound_scan.classify = boom
try:
    rep = step()
finally:
    sound_scan.classify = orig_classify
check("⑦ 扫描失败只降级（返回空），不会让初始化失败", rep == {} and [g.kind for g in store.get_state(VID).segments] == ["speech"] * 3)
class Cancelled(FJ):
    def check(self): raise jobs.JobCancelled()
try:
    asyncio.run(jobs.step_sounds(VID, Cancelled())); passed = False
except jobs.JobCancelled:
    passed = True
check("⑦ 任务被取消时取消要继续往外传（不能被“只降级”吞掉）", passed)
sfresh([(7.0, 7.8), (12.0, 13.0)])
plan = sound_scan.classify(VID, store.get_state(VID))
def cover_first(st): st.segments.append(Segment("late", "0", 6.9, 7.9, "用户补了一句", translated_text="x"))
store.mutate(VID, cover_first, actor="user", cmd="insert", summary="用户插入")
rep = sound_scan.apply(VID, plan)
check("⑦ 计划算完之后用户又插了一句盖住了第一处：按最新状态重算，只标剩下的那一处（不会重复覆盖）",
      [g.kind for g in store.get_state(VID).segments].count("vocal") == 1 and len(rep["applied"]) == 1 and abs(rep["applied"][0]["start"] - 12.0) <= 0.06, rep)

# ---- 欢迎消息：说清标了什么、没处理什么
sfresh([(7.0, 7.8), (12.0, 13.0), (14.0, 19.0)], dur=40.0)
rep = step()
store.mutate(VID, lambda st: setattr(st, "init_report", {"sounds": rep}), actor="system", cmd="init_report", summary="r", undoable=False)
w = agent.welcome_text(VID)
check("⑦ 欢迎消息：说了自动标出 2 处、是第几句、导出时怎样、不对怎么办",
      "自动标出了 2 处" in w and "第 3、5 句" in w and "保留这段原声" in w and "告诉我是第几句" in w, w)
check("⑦ 欢迎消息：没自动处理的 1 处也提了——在哪、为什么、要不要听由用户决定",
      "另有 1 处声音没有台词覆盖、也没有自动处理" in w and "超过 4 秒" in w and "第 5 句之后 14.0–19.0 秒" in w and "确认后再改" in w, w)
check("⑦ 欢迎消息：自动标出的 vocal 行不再被“被标成了非语音”重复提一遍", "被标成了非语音" not in w, w)
sfresh([(7.0, 7.8)], separated=False)
rep = step()
store.mutate(VID, lambda st: setattr(st, "init_report", {"sounds": rep}), actor="system", cmd="init_report", summary="r", undoable=False)
w = agent.welcome_text(VID)
check("⑦ 没分离的工程：欢迎消息不提（候选是风声音乐，提了只是噪音）", "声音没有台词覆盖" not in w and "自动标出" not in w, w)
sfresh([(7.0, 7.8)])
rep = step()
store.mutate(VID, lambda st: setattr(st, "init_report", {"sounds": rep}), actor="system", cmd="init_report", summary="r", undoable=False)
store.mutate(VID, lambda st: [setattr(g, "kind", "speech") for g in st.segments if g.kind == "vocal"], actor="user", cmd="mark", summary="标回")
check("⑦ 用户已经把它标回语音：欢迎消息不再说“自动标出了”（不拿过期的事实说话）", "自动标出" not in agent.welcome_text(VID))
check("⑦ 系统提示：这些要用户拍板，AI 先听、告诉用户、同意后才改", "要用户拍板" in agent.SYSTEM_PROMPT and "不要自己批量插入或删除" in agent.SYSTEM_PROMPT)

# ---- 端到端：真实样本 + 真实 ASR 输出，走完整的 initialize（HTTP、真任务、真 store），只桩掉网络
if os.path.exists(os.path.join(SAMPLE, "speech_stem.flac")) and os.path.exists(os.path.join(SAMPLE, "asr_baseline", "asr_vocals.json")):
    import shutil, time
    from fastapi.testclient import TestClient
    from app.services import asr_service as ASR, llm_service as LLM, tts_service as TTS
    AID = "autosnds000000000000000000000000"
    if store.exists(AID): store.delete(AID)
    mp4 = os.path.join(TMP, "auto.mp4"); shutil.copy(os.path.join(SAMPLE, "problem_sample.mp4"), mp4)
    store.create(AID, "auto.mp4", mp4)
    ad = video_dir_path(AID)
    for src_name, out_name, rate in (("speech_stem.flac", "vocals.wav", 44100), ("background_stem.flac", "background.wav", 44100)):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", os.path.join(SAMPLE, src_name), "-ac", "1", "-ar", str(rate), os.path.join(ad, out_name)], check=True)
    real = json.load(open(os.path.join(SAMPLE, "asr_baseline", "asr_vocals.json"), encoding="utf-8"))["raw_transcripts"][0]["transcripts"][0]["sentences"]
    seen = {"translate": [], "tts": []}
    async def fake_asr(video_id, video_path, audio_path, url):
        return [ASR.sentence_to_line(x, f"seg-r{i}") for i, x in enumerate(real, 1)]
    async def fake_translate(video_id, segments, target_language, emit=None, text_overrides=None, custom_prompt="", length_hint=None):
        seen["translate"] += [x["id"] for x in segments]
        return [{"id": x["id"], "translatedText": f"[en] {x['text']}"} for x in segments], []
    async def fake_synth(video_id, segment_id, text, voice, speech_rate=None, target_duration=None, instruction=None):
        seen["tts"].append(segment_id)
        p = TTS.tts_audio_path(video_id, segment_id); os.makedirs(os.path.dirname(p), exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=1.5", "-ar", "24000", p], check=True)
        return p, 1.5
    ASR.transcribe_video, LLM.translate_script, TTS.synthesize_speech = fake_asr, fake_translate, fake_synth
    ASR.extract_audio = lambda src, dst: subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vn", "-ar", "16000", "-ac", "1", dst], check=True)

    def wait_init(timeout=30):
        t = time.time()
        while time.time() - t < timeout:
            if not any(j["kind"] == "init" for j in jobs.active_snapshot(AID)): return True
            time.sleep(0.05)
        return False

    with TestClient(app) as cc:
        r = cc.post(f"/api/projects/{AID}/setup", json={"target_language": "English", "enable_dubbing": True})
        check("⑦ 端到端：setup 启动初始化", r.status_code == 200, r.text)
        check("⑦ 端到端：等初始化结束", wait_init()); time.sleep(0.3)
        v = cc.get(f"/api/projects/{AID}").json()
        segs = v["segments"]
        check("⑦ 端到端：阶段 ready，共 10 行 = 7 句台词 + 3 处非语言声", v["stage"] == "ready" and len(segs) == 10 and [x["kind"] for x in segs].count("vocal") == 3, (v["stage"], [x["kind"] for x in segs]))
        check("⑦ 端到端：vocal 在笑声（第 3 句后两处）和句尾笑声（第 7 句后）的位置，与真值相差不到 0.1 秒",
              [x["kind"] for x in segs] == ["speech"] * 3 + ["vocal", "vocal"] + ["speech"] * 4 + ["vocal"]
              and all(abs(x["start"] - e["start"]) < 0.1 and abs(x["end"] - e["end"]) < 0.1 for x, e in zip([x for x in segs if x["kind"] == "vocal"], truth_nv)),
              [(x["kind"], x["start"], x["end"]) for x in segs])
        check("⑦ 端到端：vocal 行没有被翻译、没有被配音（省钱：不给非语言声花一分钱）",
              not any(i in seen["translate"] for i in [x["id"] for x in segs if x["kind"] == "vocal"])
              and not any(i in seen["tts"] for i in [x["id"] for x in segs if x["kind"] == "vocal"]) and len(seen["tts"]) == 7, seen)
        check("⑦ 端到端：台词行译文、配音都正常（新鲜）", all(x["trStatus"] == "fresh" and x["ttsStatus"] == "fresh" for x in segs if x["kind"] == "speech"))
        check("⑦ 端到端：vocal 行带预览地址（保留原声）", all(x["audioUrl"] for x in segs if x["kind"] == "vocal"))
        mp = v["mix"]["plan"]
        check("⑦ 端到端：混音计划里 7 句配音 + 3 条保留原声", sum(1 for e in mp["segments"] if e.get("source") == "vocals") == 3 and len(mp["segments"]) == 10)
        rpt = v["initReport"]["sounds"]
        check("⑦ 端到端：初始化报告里有这一项（已标记 3 处，没有待确认）", len(rpt["applied"]) == 3 and rpt["pendingCount"] == 0 and rpt["track"] == "vocals", rpt)
        msgs = cc.get(f"/api/projects/{AID}/chat").json()["messages"]
        welcome = next(m["text"] for m in msgs if m.get("system"))
        check("⑦ 端到端：聊天第一条消息说了：自动标出 3 处、第 4、5、10 句、保留原声", "自动标出了 3 处" in welcome and "第 4、5、10 句" in welcome and "保留这段原声" in welcome, welcome)
        check("⑦ 端到端：聊天里的 AI 能看到这条消息（它在会话历史里，下一轮就在上下文里）", any(m.get("system") and "自动标出了 3 处" in m["text"] for m in msgs))

        # 再跑初始化：幂等，不重复插入
        n_before = len(segs)
        cc.post(f"/api/projects/{AID}/commands/run_initialize", json={"args": {}}); wait_init(); time.sleep(0.3)
        check("⑦ 端到端：再跑一遍初始化，行数不变（不重复标）", len(cc.get(f"/api/projects/{AID}").json()["segments"]) == n_before)

        # 撤销 → 3 行消失，译文配音还在；再跑初始化不加回来
        r = cc.post(f"/api/projects/{AID}/undo")
        v = cc.get(f"/api/projects/{AID}").json()
        check("⑦ 端到端：撤销之后只剩 7 句台词，译文配音一个没丢", r.status_code == 200 and len(v["segments"]) == 7
              and all(x["trStatus"] == "fresh" and x["ttsStatus"] == "fresh" for x in v["segments"]), (r.text[:120], len(v["segments"])))
        cc.post(f"/api/projects/{AID}/commands/run_initialize", json={"args": {}}); wait_init(); time.sleep(0.3)
        check("⑦ 端到端：撤销后再跑初始化，不会把用户撤销的又加回来", len(cc.get(f"/api/projects/{AID}").json()["segments"]) == 7)

        # 新的转写 → 新的扫描
        store.mutate(AID, lambda st: setattr(st, "segments", []), actor="user", cmd="clear", summary="清空")
        cc.post(f"/api/projects/{AID}/commands/run_initialize", json={"args": {}}); wait_init(); time.sleep(0.3)
        v = cc.get(f"/api/projects/{AID}").json()
        check("⑦ 端到端：转写被清掉重来（新的转写）：重新扫描、重新标出 3 处", len(v["segments"]) == 10 and [x["kind"] for x in v["segments"]].count("vocal") == 3, len(v["segments"]))

        # 后面某一步失败：标注和报告不丢
        store.mutate(AID, lambda st: setattr(st, "segments", []), actor="user", cmd="clear", summary="清空")
        orig_review = jobs.step_review
        async def review_boom(*a, **k): raise RuntimeError("审听炸了")
        jobs.step_review = review_boom
        try:
            cc.post(f"/api/projects/{AID}/commands/run_initialize", json={"args": {}}); wait_init(); time.sleep(0.3)
        finally:
            jobs.step_review = orig_review
        v = cc.get(f"/api/projects/{AID}").json()
        check("⑦ 端到端：初始化后面一步失败，已经标出的 3 行还在，报告里也有（放在转写之后而不是末尾的原因）",
              [x["kind"] for x in v["segments"]].count("vocal") == 3 and len(v["initReport"].get("sounds", {}).get("applied", [])) == 3
              and "审听炸了" in (v["initReport"].get("error") or ""), (v["initReport"].get("error"), [x["kind"] for x in v["segments"]]))
else:
    print("SKIP ⑦ 端到端（samples/problem_video 不在）")

# ============================================================================ 收尾
print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
