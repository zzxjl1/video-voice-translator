"""回归：/status 的 mix 字段与版本协商（TestClient，不占端口）。"""
import os, sys, tempfile, subprocess, json
REPO = "/Users/edenfwu/Desktop/video-voice-translator"
sys.path.insert(0, os.path.join(REPO, "backend")); os.chdir(os.path.join(REPO, "backend"))
from app import models
TMP = tempfile.mkdtemp(); models.config.DATA_DIR = TMP
from app.main import app
from fastapi.testclient import TestClient

VID = "statustest0000000000000000000000"
D = os.path.join(TMP, VID); os.makedirs(os.path.join(D, "tts"), exist_ok=True)

def run(c):
    r = subprocess.run(c, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-300:]

run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x120:d=4",
     "-f", "lavfi", "-i", "sine=frequency=200:duration=4", "-shortest",
     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", f"{D}/source.mp4"])
run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
     "-ar", "24000", f"{D}/tts/seg-a.mp3"])
run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=660:duration=4",
     "-ar", "24000", f"{D}/background.wav"])
json.dump({"video_id": VID, "file_path": f"{D}/source.mp4", "filename": "s.mp4",
           "target_language": "Chinese", "status": "completed",
           "created_at": "2026-10-11T00:00:00+08:00"},
          open(f"{D}/info.json", "w", encoding="utf-8"), ensure_ascii=False)
json.dump([{"id": "seg-a", "speaker_id": "S1", "speaker_label": "S1",
            "start_time": 0.5, "end_time": 2.5, "text": "a"}],
          open(f"{D}/asr_result.json", "w", encoding="utf-8"), ensure_ascii=False)
json.dump([{"id": "seg-a", "translatedText": "甲"}],
          open(f"{D}/translation_result.json", "w", encoding="utf-8"), ensure_ascii=False)
json.dump({"seg-a": {"path": "tts/seg-a.mp3", "duration": 2.0}},
          open(f"{D}/tts_results.json", "w", encoding="utf-8"), ensure_ascii=False)

c = TestClient(app)
fails = []
def check(n, cond, d=None):
    print(("PASS " if cond else "FAIL ") + n + (f" [{d}]" if d and not cond else ""))
    if not cond: fails.append(n)

r = c.get(f"/api/videos/{VID}/status")
mix = r.json().get("mix")
check("① /status 带 mix.plan", bool(mix and mix.get("plan")), mix and mix.get("version"))
plan = mix["plan"]
check("② plan 有 segments/bed/version",
      plan["segments"] and plan["bed"]["kind"] == "background" and plan["version"])
seg = plan["segments"][0]
check("③ 段条目字段齐（atempo/played/videoRate）",
      seg["atempo"] == 1.0 and seg["played"] == 2.0 and seg["videoRate"] == 1.0, seg)

r2 = c.get(f"/api/videos/{VID}/status", params={"mix_version": plan["version"]})
mix2 = r2.json().get("mix")
check("④ 版本命中 → 只回版本不回 plan",
      mix2 and mix2["version"] == plan["version"] and mix2["plan"] is None, mix2)

r3 = c.get(f"/api/videos/{VID}/status", params={"mix_version": "stale"})
check("⑤ 版本过期 → 重发 plan", r3.json()["mix"]["plan"] is not None)

# 没有配音的工程 → mix 为 null（降级），而不是 500
VID2 = "emptytest0000000000000000000000"
D2 = os.path.join(TMP, VID2); os.makedirs(D2, exist_ok=True)
json.dump({"video_id": VID2, "file_path": f"{D}/source.mp4", "filename": "e.mp4",
           "target_language": "Chinese", "status": "uploaded",
           "created_at": "2026-10-11T00:00:00+08:00"},
          open(f"{D2}/info.json", "w", encoding="utf-8"), ensure_ascii=False)
r4 = c.get(f"/api/videos/{VID2}/status")
check("⑥ 无配音工程 → mix=null 不 500", r4.status_code == 200 and r4.json()["mix"] is None,
      r4.status_code)

# plan 与导出执行端一致：同一生成器对同一 state 的两次产出 version 相同
from app.services import mix_service
st = models.load_state(VID)
v1 = mix_service.build_mix_plan(st)["version"]
v2 = mix_service.build_mix_plan(models.load_state(VID))["version"]
check("⑦ 生成器幂等（两次 version 相同）", v1 == v2 and v1 == plan["version"], (v1, v2))

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败'}")
sys.exit(1 if fails else 0)
