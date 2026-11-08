"""
「生成配音」总开关（仅字幕模式）：不合成配音、不做克隆、导出时原声比特级直通。
走新架构的入口：setup → 初始化任务 → 导出命令。ASR / 翻译用桩，TTS 若被调用立即失败。
"""
import os, subprocess, sys, tempfile, time
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from fastapi.testclient import TestClient
from app import jobs, status, store
from app.main import app
from app.services import asr_service, llm_service, mix_service, tts_service

fails = []
def check(n, cond, d=None):
    print(("PASS " if cond else "FAIL ") + n + (f" [{d}]" if d is not None and not cond else ""))
    if not cond: fails.append(n)

async def fake_transcribe(*a, **k):
    return [{"id": "seg-a", "speaker_id": "0", "start_time": 0.5, "end_time": 2.5, "text": "hello there"}]
async def fake_translate(video_id, segments, lang, **k):
    return [{"id": s["id"], "translatedText": "你好呀"} for s in segments], []
def forbidden(*a, **k):
    raise AssertionError("synthesize_speech 在仅字幕模式下被调用了")

asr_service.transcribe_video = fake_transcribe
llm_service.translate_script = fake_translate
tts_service.synthesize_speech = forbidden
asr_service.extract_audio = lambda s, d: subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", s, "-vn", "-ar", "16000", "-ac", "1", d], check=True)

SRC = os.path.join(TMP, "in.mp4")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x120:d=4",
                "-f", "lavfi", "-i", "sine=frequency=330:duration=4", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", SRC], check=True)

with TestClient(app) as c:
    vid = c.post("/api/projects", files={"file": ("in.mp4", open(SRC, "rb"), "video/mp4")}).json()["id"]
    r = c.post(f"/api/projects/{vid}/setup", json={
        "target_language": "Chinese", "enable_dubbing": False,
        "enable_voice_clone": True, "separation_mode": "client",       # 这两个在仅字幕模式下必须被忽略
    })
    check("① setup 接受", r.status_code == 200, r.text)
    for _ in range(200):
        if not jobs.active(vid): break
        time.sleep(0.05)
    time.sleep(0.2)
    st = store.get_state(vid)

    check("① 初始化完成，阶段 ready", st.stage == "ready", (st.stage, st.init_error))
    check("① enable_dubbing=False，且克隆 / 分离被强制关掉",
          st.enable_dubbing is False and st.enable_voice_clone is False and st.separation_mode == "off",
          (st.enable_dubbing, st.enable_voice_clone, st.separation_mode))
    check("① 译文照做（字幕需要文本）", st.segments[0].translated_text == "你好呀")
    check("① 没有配音产物", not os.path.exists(os.path.join(TMP, vid, "tts")))
    check("① 配音状态是 none（选择，不是缺口）", status.tts_status(st, st.segments[0]) == status.NONE)
    check("① 待办里没有配音这一项", status.needs(st)["dub"] == 0)
    check("① 没有给说话人分配音色（用不着）", all(sp.voice is None for sp in st.speakers))

    # ② 混音计划：dubbed=False，不报错
    plan = mix_service.build_mix_plan(st)
    check("② plan dubbed=False 且不报错", plan.get("dubbed") is False and plan["segments"] == [])

    # ③ redub 被明确拒绝，并指路
    r = c.post(f"/api/projects/{vid}/commands/redub", json={"args": {}}).json()
    check("③ 仅字幕模式下 redub 被拒，并指路", (not r["ok"]) and "enable_dubbing" in r["error"], r)

    # ④ 导出：原声直通（-c:a copy）→ 音频流参数与源一致
    r = c.post(f"/api/projects/{vid}/commands/export_video", json={"args": {}})
    check("④ 导出成功", r.json()["ok"], r.text)
    def aparams(path):
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                              "stream=codec_name,sample_rate,bit_rate", "-of", "csv=p=0", path], capture_output=True, text=True)
        return out.stdout.strip()
    from app.services import export_service
    path, _ = export_service.find_export(vid)
    check("④ 音频流与源一致（copy 而非重编码）", aparams(st.file_path) == aparams(path), (aparams(st.file_path), aparams(path)))

    # ⑤ 打开开关：配音回来，且只补缺的
    async def ok_synth(video_id, segment_id, text, voice, speech_rate=None, target_duration=None, instruction=None):
        p = tts_service.tts_audio_path(video_id, segment_id); os.makedirs(os.path.dirname(p), exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=2", "-ar", "24000", p], check=True)
        return p, 2.0
    tts_service.synthesize_speech = ok_synth
    r = c.post(f"/api/projects/{vid}/commands/set_settings", json={"args": {"enable_dubbing": True}}).json()
    check("⑤ 打开配音开关", r["ok"] and store.get_state(vid).enable_dubbing)
    check("⑤ 待办里出现 1 句要配音", status.needs(store.get_state(vid))["dub"] == 1)
    r = c.post(f"/api/projects/{vid}/commands/redub", json={"args": {}}).json()
    st2 = store.get_state(vid)
    check("⑤ redub 之后配音新鲜，混音计划走正常分支",
          r["ok"] and status.tts_status(st2, st2.segments[0]) == status.FRESH and mix_service.build_mix_plan(st2).get("dubbed") is True, r)

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败'}")
sys.exit(1 if fails else 0)


def test_dubbing_switch():
    assert not fails, fails
