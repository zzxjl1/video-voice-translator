"""「生成配音」总开关的行为验证：管线跳过合成、导出原声比特级直通。"""
import asyncio, json, os, subprocess, sys, tempfile

REPO = "/Users/edenfwu/Desktop/video-voice-translator"
sys.path.insert(0, os.path.join(REPO, "backend"))
os.chdir(os.path.join(REPO, "backend"))

from app import models
TMP = tempfile.mkdtemp(); models.config.DATA_DIR = TMP

from app.models import VideoState
from app.main import app
from app.services import pipeline_service, tts_service, export_service as E

VID = "dubswitch0000000000000000000000"
D = os.path.join(TMP, VID); os.makedirs(D, exist_ok=True)

def run(c):
    r = subprocess.run(c, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-300:]

run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x120:d=4",
     "-f", "lavfi", "-i", "sine=frequency=330:duration=4", "-shortest",
     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
     f"{D}/source.mp4"])
json.dump({"video_id": VID, "file_path": f"{D}/source.mp4", "filename": "s.mp4",
           "target_language": "Chinese", "status": "uploaded",
           "created_at": "2026-10-11T00:00:00+08:00"},
          open(f"{D}/info.json", "w", encoding="utf-8"), ensure_ascii=False)
json.dump([{"id": "seg-a", "speaker_id": "S1", "speaker_label": "S1",
            "start_time": 0.5, "end_time": 2.5, "text": "hello there"}],
          open(f"{D}/asr_result.json", "w", encoding="utf-8"), ensure_ascii=False)
json.dump([{"id": "seg-a", "translatedText": "你好呀"}],
          open(f"{D}/translation_result.json", "w", encoding="utf-8"), ensure_ascii=False)

fails = []
def check(n, cond, d=None):
    print(("PASS " if cond else "FAIL ") + n + (f" [{d}]" if d and not cond else ""))
    if not cond: fails.append(n)

events = []
async def emit(e): events.append(e)

# TTS 若被调用立即失败 —— 总开关必须让这一行 never 发生
def _forbidden(*a, **k):
    raise AssertionError("synthesize_speech 在仅字幕模式下被调用了")
orig_synth = tts_service.synthesize_speech
tts_service.synthesize_speech = _forbidden

# ① 管线：仅字幕模式
asyncio.run(pipeline_service.run_pipeline(
    video_id=VID, target_language="Chinese", server_url_base="http://127.0.0.1:9100",
    emit=emit, enable_dubbing=False,
))
tts_service.synthesize_speech = orig_synth

check("① 管线完成", events[-1].get("done") is True, events[-3:])
check("① 发出 tts skipped 事件",
      any(e.get("phase") == "tts" and e.get("status") == "skipped" for e in events))
check("① 完成事件带 skipped 标记",
      any(e.get("phase") == "tts" and e.get("status") == "done" and e.get("skipped") for e in events))
st = models.load_state(VID)
check("① 状态 completed 且 enable_dubbing=False",
      st.status.value == "completed" and st.enable_dubbing is False,
      (st.status.value, st.enable_dubbing))
check("① 没有配音产物", not os.path.exists(f"{D}/tts_results.json"))

# ② plan：dubbed=False 且不报错
from app.services import mix_service
plan = mix_service.build_mix_plan(models.load_state(VID))
check("② plan dubbed=False", plan.get("dubbed") is False and plan["segments"] == [])

# ③ 导出：原声直通（-c:a copy）→ 音频流参数与源一致、无重编码
out = asyncio.run(E.export_video(VID))
def aparams(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream=codec_name,sample_rate,bit_rate",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    return r.stdout.strip()
src, got = aparams(f"{D}/source.mp4"), aparams(out.path)
check("③ 音频流与源一致（copy 而非重编码）", src == got, f"src={src} got={got}")
check("③ 输出存在", os.path.exists(out.path) and os.path.getsize(out.path) > 10000)

# ④ 对照：配音模式不受影响（plan 走正常分支）
# 登记表与音频先就位，再 load（否则 segments 上没有 audio_path）
os.makedirs(f"{D}/tts", exist_ok=True)   # 仅字幕模式没有建过 tts/ 目录
_r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                     "-ar", "24000", f"{D}/tts/seg-a.mp3"], capture_output=True, text=True)
assert _r.returncode == 0, _r.stderr[-300:]
assert os.path.exists(f"{D}/tts/seg-a.mp3"), "测试夹具 mp3 没落盘"
json.dump({"seg-a": {"path": "tts/seg-a.mp3", "duration": 2.0}},
          open(f"{D}/tts_results.json", "w", encoding="utf-8"), ensure_ascii=False)
st2 = models.load_state(VID)
st2.enable_dubbing = True
print("  [debug] st2.enable_dubbing =", st2.enable_dubbing,
      "| audio_path =", st2.segments[0].audio_path,
      "| exists =", os.path.exists(st2.segments[0].audio_path or ""))
plan2 = mix_service.build_mix_plan(st2)
check("④ 开关打开 → plan dubbed=True 正常混音", plan2.get("dubbed") is True and plan2["segments"])

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败'}")
sys.exit(1 if fails else 0)
