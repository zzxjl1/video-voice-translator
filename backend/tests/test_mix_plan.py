"""mix_service.build_mix_plan 的行为单测（纯数据，不碰 ffmpeg）。"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/
from app.models import Segment, VideoState
from app.services import mix_service as M

def make_state(segs, with_bg=True, tmp=None, with_source=True):
    vid = "mixtest000000000000000000000000"
    d = os.path.join(tmp, vid); os.makedirs(d, exist_ok=True)
    # 源视频：生产里必然存在（file_path 是上传时的绝对路径），夹具也给真文件
    src = os.path.join(tmp, "video.mp4")
    if with_source:
        open(src, "ab").close()
    elif os.path.exists(src):
        os.remove(src)
    state = VideoState(video_id=vid, file_path=src,
                       filename="v.mp4", target_language="Chinese")
    state.segments = segs
    bg = os.path.join(d, "background.wav")
    if with_bg:
        open(bg, "wb").write(b"\0" * 44)
    elif os.path.exists(bg):
        os.remove(bg)          # 同一目录复用：上个用例的产物必须清掉
    return state

def seg(sid, s, e, dur=None, muted=False, audio=True):
    d = os.path.join(tmp, "mixtest000000000000000000000000")
    sg = Segment(id=sid, speaker_id="S1", speaker_label="S1", start_time=s, end_time=e,
                 text="x", translated_text="y", muted=muted)
    if audio:
        # 生产里 audio_path 是绝对路径（load_state 从 tts_results.json 还原时
        # join 了 video_dir），且文件必须真实存在 —— 生成器按生产契约检查。
        path = os.path.join(d, "tts", f"{sid}.mp3")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(b"\0" * 16)
        sg.audio_path = path
        if dur is not None:
            sg.actual_duration = dur
    return sg

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond: fails.append(name)

tmp = tempfile.mkdtemp()
# get_video_dir → video_dir_path → config.DATA_DIR（运行时读取），patch 这里
from app import config
config.DATA_DIR = tmp

# ① 分离产物存在 → bed=background，无下压曲线
st = make_state([seg("a", 0.4, 2.6, 2.2), seg("b", 3.0, 5.4, 2.5)], with_bg=True, tmp=tmp)
plan = M.build_mix_plan(st)
check("① bed=background 且无 duck 曲线", plan["bed"]["kind"] == "background" and plan["bed"]["duck"] == [])
check("① 背景增益 = -1dB", abs(plan["bed"]["gain"] - M.db_to_gain(-1.0)) < 1e-4, plan["bed"]["gain"])

# ② 无分离产物 → bed=original，带下压断点
st2 = make_state([seg("a", 2.0, 4.0, 2.0)], with_bg=False, tmp=tmp)
plan2 = M.build_mix_plan(st2)
check("② bed=original", plan2["bed"]["kind"] == "original")
bp = plan2["bed"]["duck"]
check("② 断点形状：attack→duck→release",
      len(bp) == 4 and abs(bp[1][1] - M.db_to_gain(-24.0)) < 1e-4
      and abs(bp[0][0] - (2.0 - M.DUCK_ATTACK_S)) < 1e-6
      and abs(bp[3][0] - (4.0 + M.DUCK_RELEASE_S)) < 1e-6, bp)

# ③ 相邻相接的槽合并（不产生中间抬起）
st3 = make_state([seg("a", 0.0, 2.0, 2.0), seg("b", 2.0, 4.0, 2.0)], with_bg=False, tmp=tmp)
bp3 = M.build_mix_plan(st3)["bed"]["duck"]
# 两个槽合并成一个 [0,4]：t=0 处"attack 起点"与"duck 起点"重合 → 塌成
# 一个点并保留 duck 值（说话从 0s 就开始了，不需要 attack），共 3 个断点
check("③ 相邻槽合并为 3 个断点", len(bp3) == 3 and abs(bp3[0][1] - M.db_to_gain(-24.0)) < 1e-4, bp3)

# ④ atempo：actual/slot 夹在 [0.8, 1.25]
st4 = make_state([seg("slow", 0.0, 2.0, 2.6), seg("fast", 2.0, 4.0, 1.4), seg("fit", 4.0, 6.0, 2.0)], with_bg=True, tmp=tmp)
segs4 = {e["id"]: e for e in M.build_mix_plan(st4)["segments"]}
check("④ 超槽 → atempo=1.25 且裁剪", segs4["slow"]["atempo"] == 1.25 and segs4["slow"]["trim"] == 2.0, segs4["slow"])
check("④ 不足 → atempo=0.8 不裁", segs4["fast"]["atempo"] == 0.8 and segs4["fast"]["trim"] is None, segs4["fast"])
check("④ 恰好 → 1.0 不裁", segs4["fit"]["atempo"] == 1.0 and segs4["fit"]["trim"] is None)

# ⑤ videoRate：音频吸收不了的余量才给视频
#    slow: ideal=1.3, atempo=1.25 → videoRate=1.3/1.25=1.04
# ideal=1.3、atempo=1.25 → 音频吸收后还差 1.04 个槽 → 视频放慢 0.9615
check("⑤ videoRate = atempo/ideal（视频放慢让槽变长）",
      abs(segs4["slow"]["videoRate"] - 1.25/1.3) < 1e-3, segs4["slow"]["videoRate"])

# ⑥ muted 不进 plan；缺 actual_duration 的会被现量补写（这里文件不存在→按自然速度+裁剪并告警）
st6 = make_state([seg("m", 0.0, 2.0, 2.0, muted=True), seg("k", 2.0, 4.0, None)], with_bg=True, tmp=tmp)
plan6 = M.build_mix_plan(st6)
ids = [e["id"] for e in plan6["segments"]]
check("⑥ muted 段不进 plan", "m" not in ids and "k" in ids)
check("⑥ 缺时长 → atempo=1.0 裁到槽", plan6["segments"][0]["atempo"] == 1.0 and plan6["segments"][0]["trim"] == 2.0)

# ⑦ version 随内容变化；policyVersion 变化也会变
v1 = plan["version"]
st.segments[0].end_time = 2.7
v2 = M.build_mix_plan(st)["version"]
check("⑦ 内容变了 version 变", v1 != v2)

# ⑧ 没有任何配音 → 响亮报错
try:
    M.build_mix_plan(make_state([], with_bg=True, tmp=tmp))
    check("⑧ 无配音 → 报错", False)
except RuntimeError as e:
    check("⑧ 无配音 → 报错", "配音" in str(e))

# ⑨ 无背景且源文件不存在 → 响亮报错（C 那条死路）
try:
    M.build_mix_plan(make_state([seg("a", 0.0, 2.0, 2.0)], with_bg=False, tmp=tmp, with_source=False))
    check("⑨ 无垫底 → 报错", False)
except RuntimeError as e:
    check("⑨ 无垫底 → 报错", "垫底" in str(e))

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)


def test_mix_plan():
    """pytest 入口：直接复用上面的脚本式断言（进程内只跑一次）。"""
    # 脚本本体在 import 时执行完毕；到这里只要没有 sys.exit(1) 就是通过
    assert not fails, fails
