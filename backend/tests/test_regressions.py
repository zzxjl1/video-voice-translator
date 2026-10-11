"""
回归测试：本轮修复的后端行为（不联网：TTS / ASR / LLM 都用桩替掉）。

运行：python3 tests/test_regressions.py
"""
import asyncio
import json
import os
import sys
import tempfile

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test")
os.environ.setdefault("LLM_API_KEY", "test")

from app import config, models  # noqa: E402

TMP = tempfile.mkdtemp()
config.DATA_DIR = TMP
models.config.DATA_DIR = TMP

from fastapi.testclient import TestClient  # noqa: E402

from app.http_range import parse_range  # noqa: E402
from app.main import app  # noqa: E402
from app.services import pipeline_service, run_registry, subtitle_service, tts_service  # noqa: E402

fails: list[str] = []


def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_project(vid, status="completed", segments=2, translated=True):
    d = os.path.join(TMP, vid)
    os.makedirs(d, exist_ok=True)
    src = os.path.join(d, "original_video.mp4")
    open(src, "wb").write(b"\0" * 64)
    write_json(os.path.join(d, "info.json"), {
        "video_id": vid, "file_path": src, "filename": "My Movie.mp4",
        "target_language": "Chinese", "status": status,
        "created_at": "2026-10-11T00:00:00+08:00",
    })
    asr = [
        {"id": f"seg-{i}", "speaker_id": "0", "speaker_label": "Speaker 0",
         "start_time": i * 3.0, "end_time": i * 3.0 + 2.5, "text": f"hello {i}"}
        for i in range(segments)
    ]
    write_json(os.path.join(d, "asr_result.json"), asr)
    if translated:
        write_json(os.path.join(d, "translation_result.json"),
                   [{"id": s["id"], "translatedText": f"你好 {s['id']}"} for s in asr])
    return d


client = TestClient(app)

# ---------------------------------------------------------------- ① Range 解析
check("① a-b", parse_range("bytes=0-9", 100) == (0, 9))
check("① a-（到末尾）", parse_range("bytes=90-", 100) == (90, 99))
check("① -n 取尾部 n 字节（旧实现读成从 0 开始）", parse_range("bytes=-10", 100) == (90, 99))
check("① -n 大于文件 → 整个文件", parse_range("bytes=-500", 100) == (0, 99))
check("① 越界 end 被夹住", parse_range("bytes=50-999", 100) == (50, 99))
check("① 乱写的头 → 忽略", parse_range("garbage", 100) is None and parse_range("bytes=1-2,5-6", 100) is None)
try:
    parse_range("bytes=500-600", 100)
    check("① 起点越界 → 416", False)
except Exception as e:
    check("① 起点越界 → 416", getattr(e, "status_code", None) == 416)

# ---------------------------------------------------------------- ② 字幕
check("② ASS 时间 59.999s 不再写出 60.00",
      subtitle_service._ass_time(59.999) == "0:01:00.00", subtitle_service._ass_time(59.999))
check("② ASS 时间 3661.5s", subtitle_service._ass_time(3661.5) == "1:01:01.50")
junk = subtitle_service.SubtitleStyle.from_dict(
    {"font_size_percent": "abc", "bold": "false", "max_lines": None, "shadow": float("nan")}
)
check("② 脏数字不再 500，回落默认值", junk.font_size_percent == 4.5 and junk.max_lines == 2 and junk.shadow == 1.0)
check("② 字符串 'false' 不再被读成 True", junk.bold is False)
ass = subtitle_service.to_ass(
    [subtitle_service.Cue(0, 2, "你好", "")], subtitle_service.SubtitleStyle().normalised(), 1920, 1080
)
check("② 半透明底与描边并存（两层样式）", "Style: Box," in ass and "Dialogue: 0," in ass and "Dialogue: 1," in ass)
check("② 描边按画面高度换算（1080p ≈ 2.16px），不是写死 2",
      "2.16" in ass, [l for l in ass.splitlines() if l.startswith("Style: Default")])

# ---------------------------------------------------------------- ③ 样式：增量保存 + 不被 save_state 覆盖
V1 = "regress1" + "0" * 24
d1 = make_project(V1)
r = client.put(f"/api/videos/{V1}/subtitles/style", json={"font_size_percent": 7.0})
check("③ PUT 样式 200", r.status_code == 200, r.text[:200])
r = client.put(f"/api/videos/{V1}/subtitles/style", json={"alignment": "top"})
st = r.json()["style"]
check("③ 第二次只发 alignment，字号不被重置回默认", st["alignment"] == "top" and st["font_size_percent"] == 7.0, st)

stale = models.load_state(V1)            # 管线开跑时读入的"旧副本"
client.put(f"/api/videos/{V1}/subtitles/style", json={"font_size_percent": 9.0})
stale.status = models.VideoStatus.COMPLETED
models.save_state(stale)                 # 管线随后写回自己的旧副本
check("③ 管线的旧副本写回，不会抹掉运行期间改的字幕样式",
      read_json(os.path.join(d1, "info.json"))["subtitle_style"]["font_size_percent"] == 9.0)

# ---------------------------------------------------------------- ④ 导出计划：样式来自工程；persist=false 真的不落盘
state = models.load_state(V1)
plan = __import__("app.services.export_service", fromlist=["x"]).resolve_plan(state)
check("④ 导出计划的样式 = 工程里保存的样式（不是配置默认值）", plan.style.font_size_percent == 9.0)
plan2 = __import__("app.services.export_service", fromlist=["x"]).resolve_plan(
    state, {"format": "off", "enabled": False})
check("④ patch 覆盖 format/enabled，样式仍沿用工程的",
      plan2.format == "off" and plan2.enabled is False and plan2.style.font_size_percent == 9.0)

# ---------------------------------------------------------------- ⑤ 说话人 id 统一为 speaker_id
d5 = make_project("regress5" + "0" * 24)
write_json(os.path.join(d5, "info.json"), {
    **read_json(os.path.join(d5, "info.json")),
    "speakers": [{"id": "Speaker 0", "name": "Speaker 0"}],      # 老工程的存法
})
st5 = models.load_state("regress5" + "0" * 24)
check("⑤ 老工程的 Speaker.id 迁到 speaker_id（音色/克隆映射的键）",
      [s.id for s in st5.speakers] == ["0"] and st5.speakers[0].name == "Speaker 0", st5.speakers)

# ---------------------------------------------------------------- ⑥ 译文：整表落盘，不被单行覆盖
V6 = "regress6" + "0" * 24
d6 = make_project(V6)
st6 = models.load_state(V6)
st6.segments[0].translated_text = "改过的译文"
models.save_translations(V6, st6.segments)
saved = {x["id"]: x["translatedText"] for x in read_json(os.path.join(d6, "translation_result.json"))}
check("⑥ save_translations 写全部行（不是只写传入的那一行）",
      saved == {"seg-0": "改过的译文", "seg-1": "你好 seg-1"}, saved)

r = client.put(f"/api/videos/{V6}/segments/seg-1/text", json={"text": "edited original"})
check("⑥ 保存原文 200", r.status_code == 200 and r.json()["saved"] is True, r.text[:200])
check("⑥ 原文写回 asr_result.json",
      {x["id"]: x["text"] for x in read_json(os.path.join(d6, "asr_result.json"))}["seg-1"] == "edited original")
r = client.put(f"/api/videos/{V6}/segments/seg-1/text", json={"translated_text": "手改译文"})
check("⑥ 保存译文后刷新（重新 load）仍在",
      models.load_state(V6).segments[1].translated_text == "手改译文")
r = client.put(f"/api/videos/{V6}/segments/..%2F..%2Fx/text", json={"text": "x"})
check("⑥ 非法 segment id → 4xx", r.status_code in (400, 404, 422), r.status_code)
r = client.put(f"/api/videos/{V6}/segments/nope/text", json={"text": "x"})
check("⑥ 不存在的行 → 404", r.status_code == 404)

# 改文本要作废旧配音
os.makedirs(os.path.join(d6, "tts"), exist_ok=True)
mp3 = os.path.join(d6, "tts", "seg-0.mp3")
open(mp3, "wb").write(b"\0" * 10)
write_json(os.path.join(d6, "tts_results.json"), {"seg-0": {"path": "tts/seg-0.mp3", "duration": 1.0}})
client.put(f"/api/videos/{V6}/segments/seg-0/text", json={"translated_text": "新的话"})
check("⑥ 改译文后旧配音被删，并从登记表摘掉",
      not os.path.exists(mp3) and "seg-0" not in read_json(os.path.join(d6, "tts_results.json")))

# ---------------------------------------------------------------- ⑦ 路径参数校验 / 不再因 GET 建目录
r = client.get(f"/api/videos/{V6}/tts/..%2Fsecret")
check("⑦ tts 路径穿越被拒", r.status_code in (400, 404), r.status_code)
ghost = "ghostxx0" + "0" * 24
client.get(f"/api/videos/{ghost}/voice-clone/0/preview")
client.get(f"/api/videos/{ghost}/export/download")
check("⑦ 对不存在的工程发 GET 不再凭空建目录", not os.path.exists(os.path.join(TMP, ghost)))
r = client.post(f"/api/videos/{ghost}/reset")
check("⑦ reset 不存在的工程 → 404，且不建目录",
      r.status_code == 404 and not os.path.exists(os.path.join(TMP, ghost)), r.status_code)

# ---------------------------------------------------------------- ⑧ reset 保留用户上传的文件名
V8 = "regress8" + "0" * 24
d8 = make_project(V8)
client.post(f"/api/videos/{V8}/reset")
check("⑧ reset 之后文件名仍是用户上传的名字", models.load_state(V8).filename == "My Movie.mp4",
      models.load_state(V8).filename)

# ---------------------------------------------------------------- ⑨ Range：后缀形式端到端
r = client.get(f"/api/videos/{V8}/video", headers={"Range": "bytes=-16"})
check("⑨ /video 后缀 Range → 206 且是尾部 16 字节",
      r.status_code == 206 and r.headers["content-range"] == "bytes 48-63/64" and len(r.content) == 16,
      (r.status_code, r.headers.get("content-range")))
r = client.get(f"/api/videos/{V8}/video", headers={"Range": "bytes=999-"})
check("⑨ 起点越界 → 416", r.status_code == 416, r.status_code)

# ---------------------------------------------------------------- ⑩ run_registry：重连唤醒旧读取端；结束不抛
async def registry_case():
    run = run_registry.start("regressA")
    q1 = run.subscribe()
    q2 = run.subscribe()                       # 刷新页面 / 另一个标签页接管
    first = q1.empty()                         # 旧队列不发结束标记（否则两个标签页互相抢流）
    await run.emit({"phase": "asr", "status": "started"})
    got = await asyncio.wait_for(q2.get(), 1)
    run.unsubscribe(q1)                        # 旧连接迟到的清理，不能清掉新队列
    still = run.queue is q2
    run.queue = None
    run.close()                                # 消费者已断开时结束：不能抛 AttributeError
    late = run.subscribe()                     # 结束之后才来的订阅者
    sentinel = await asyncio.wait_for(late.get(), 1)    # 先回放历史
    tail = await asyncio.wait_for(late.get(), 1)
    run_registry.finish("regressA")
    return first, got, still, sentinel, tail


first, got, still, replay, tail = asyncio.run(registry_case())
check("⑩ 被替换的旧队列不收结束标记（避免两个标签页互相抢流）", first is True)
check("⑩ 新队列收到事件", got.get("phase") == "asr")
check("⑩ 旧连接的 unsubscribe 不会清掉新队列", still is True)
check("⑩ 运行结束后的订阅者：回放历史后收到结束标记", replay.get("phase") == "asr" and tail is None)

# ---------------------------------------------------------------- ⑪ 管线：合成成功不再被报成"没有配音"
V11 = "regressB" + "0" * 24
d11 = make_project(V11, status="translated", segments=3)
src_audio = os.path.join(d11, "extracted_audio.wav")
open(src_audio, "wb").write(b"\0" * 64)


async def fake_synth(video_id, segment_id, text, voice=None, **kw):
    path = os.path.join(models.get_video_dir(video_id), "tts", f"{segment_id}.mp3")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(b"\0" * 32)
    return path, 2.0


tts_service.synthesize_speech = fake_synth
tts_service.assign_voice_for_speaker = lambda *a, **k: "voice-x"
events = []


async def collect(ev):
    events.append(ev)


asyncio.run(pipeline_service.run_pipeline(
    V11, "Chinese", "http://x", collect, separation_mode="off"))
final = models.load_state(V11)
check("⑪ 全部合成成功 → 状态 completed（以前恒为 error「N 段没有配音」）",
      final.status == models.VideoStatus.COMPLETED, (final.status, final.error_message))
check("⑪ 发出 done 事件，没有管线级 error",
      any(e.get("done") is True for e in events) and not any(e.get("error") and not e.get("phase") for e in events),
      [e for e in events if e.get("error")])
check("⑪ 三行配音都落了登记表", len(read_json(os.path.join(d11, "tts_results.json"))) == 3)

# 缺口：一行合成失败 → 仍应报错并点名
V12 = "regressC" + "0" * 24
d12 = make_project(V12, status="translated", segments=2)
open(os.path.join(d12, "extracted_audio.wav"), "wb").write(b"\0" * 64)


async def flaky_synth(video_id, segment_id, text, voice=None, **kw):
    if segment_id == "seg-1":
        raise RuntimeError("boom")
    return await fake_synth(video_id, segment_id, text, voice, **kw)


tts_service.synthesize_speech = flaky_synth
events.clear()
asyncio.run(pipeline_service.run_pipeline(V12, "Chinese", "http://x", collect, separation_mode="off"))
st12 = models.load_state(V12)
check("⑪ 真有一行失败 → error，且只点名失败的那一行",
      st12.status == models.VideoStatus.ERROR and "seg-1" in (st12.error_message or "")
      and "seg-0" not in (st12.error_message or ""), st12.error_message)

# ---------------------------------------------------------------- ⑫ 启动修复：进程中途死掉遗留的状态
V13 = "regressD" + "0" * 24
d13 = make_project(V13, status="synthesizing")
check("⑫ 启动修复把「合成中」遗留状态改成可续跑的 cancelled",
      models.repair_interrupted_states() >= 1 and models.load_state(V13).status == models.VideoStatus.CANCELLED)

# ---------------------------------------------------------------- ⑬ 缺口清单到达前端
r = client.get(f"/api/videos/{V12}/status")
check("⑬ /status 带上 failed_audio_ids（以前被响应模型丢掉）", "failed_audio_ids" in r.json())

# ---------------------------------------------------------------- ⑭ TTS 入参
r = client.post(f"/api/videos/{V6}/tts", json={"segment_id": "../../etc/x", "text": "hi"})
check("⑭ /tts 非法 segment_id → 422", r.status_code == 422, r.status_code)
r = client.post(f"/api/videos/{V6}/tts", json={"segment_id": "seg-0", "text": "x" * 5000})
check("⑭ /tts 超长文本 → 422", r.status_code == 422, r.status_code)
r = client.post(f"/api/videos/{V6}/tts", json={"segment_id": "nope", "text": "hi"})
check("⑭ /tts 不存在的行 → 404", r.status_code == 404, r.status_code)

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + '; '.join(fails)}")
sys.exit(1 if fails else 0)
