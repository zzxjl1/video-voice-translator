"""视图里的 mix 字段：版本协商（客户端带版本，没变就不重发整份计划）。真路由、真 ffmpeg。"""
import os, subprocess, sys, tempfile
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from fastapi.testclient import TestClient
from app import status, store, view
from app.main import app
from app.models import Segment, Speaker, tts_audio_path
from app.services import mix_service

fails = []
def check(n, cond, d=None):
    print(("PASS " if cond else "FAIL ") + n + (f" [{d}]" if d is not None and not cond else ""))
    if not cond: fails.append(n)

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True); assert r.returncode == 0, r.stderr[-300:]

VID = "mixstatus" + "0" * 23
D = os.path.join(TMP, VID); os.makedirs(os.path.join(D, "tts"), exist_ok=True)
SRC = os.path.join(D, "source.mp4")
run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x120:d=4", "-f", "lavfi", "-i", "sine=frequency=200:duration=4",
     "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", SRC])
run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=660:duration=4", "-ar", "24000", f"{D}/background.wav"])
run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ar", "24000", tts_audio_path(VID, "seg-a")])

store.create(VID, "s.mp4", SRC)
def seed(s):
    s.target_language = "Chinese"; s.stage = "ready"
    s.speakers = [Speaker("0", "S", voice="v_v3.1")]
    s.segments = [Segment("seg-a", "0", 0.5, 2.5, "a", translated_text="甲")]
    seg = s.segments[0]
    seg.tr_fp = status.expected_tr_fp(s, seg); seg.tts_fp = status.expected_tts_fp(s, seg); seg.actual_duration = 2.0
store.mutate(VID, seed, actor="system", cmd="seed", summary="seed")

with TestClient(app) as c:
    v = c.get(f"/api/projects/{VID}").json()
    mix = v["mix"]
    check("① 视图带 mix.plan", bool(mix and mix.get("plan")), mix and mix.get("version"))
    plan = mix["plan"]
    check("② plan 有 segments / bed / version", plan["segments"] and plan["bed"]["kind"] == "background" and plan["version"])
    seg = plan["segments"][0]
    check("③ 段条目字段齐（atempo / played / videoRate）", seg["atempo"] == 1.0 and seg["played"] == 2.0 and seg["videoRate"] == 1.0, seg)

    # 版本协商发生在 diff 层：版本相同 → 增量里不带 mix
    before = view.snapshot(store.get_state(VID))
    store.mutate(VID, lambda s: setattr(s.segments[0], "hidden", True), actor="user", cmd="hide", summary="hide")
    patch = view.diff(before, view.snapshot(store.get_state(VID)))
    check("④ 改了不影响混音的东西（隐藏字幕）→ 增量里不带 mix（版本没变）", "mix" not in patch, list(patch))

    before = view.snapshot(store.get_state(VID))
    store.mutate(VID, lambda s: setattr(s.segments[0], "muted", True), actor="user", cmd="mute", summary="mute")
    patch = view.diff(before, view.snapshot(store.get_state(VID)))
    check("⑤ 静音 → 混音计划变了 → 增量里带新版本", "mix" in patch, list(patch))

    # 没有任何可混的配音 → mix 为 None（降级），而不是 500
    VID2 = "emptytest" + "0" * 23
    store.create(VID2, "e.mp4", SRC)
    r = c.get(f"/api/projects/{VID2}")
    check("⑥ 没有配音的工程 → mix=None，不 500", r.status_code == 200 and r.json()["mix"] is None, r.status_code)

    # 计划与导出执行端是同一个生成器：同一个 state 两次产出版本相同
    st = store.get_state(VID)
    store.mutate(VID, lambda s: setattr(s.segments[0], "muted", False), actor="user", cmd="unmute", summary="unmute")
    st = store.get_state(VID)
    check("⑦ 生成器幂等（两次 version 相同）", mix_service.build_mix_plan(st)["version"] == mix_service.build_mix_plan(store.get_state(VID))["version"])
    check("⑦ 视图里的版本 = 生成器的版本", c.get(f"/api/projects/{VID}").json()["mix"]["version"] == mix_service.build_mix_plan(st)["version"])

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败'}")
sys.exit(1 if fails else 0)


def test_mix_status():
    assert not fails, fails
