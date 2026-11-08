"""
命令层：UI 和 AI 共用的动作。用真实的 store / status 驱动，不碰网络。

重点是三件事：
  1. 每个命令的**语义**（拆句、合并、标记非语音……联动靠指纹，不靠手动作废）；
  2. 出错时给调用方（AI）的话是**可执行的**（说清为什么、怎么改）；
  3. 注册表的结构性保证：校验、确认级别、tool schema 与实现一致。
"""
import asyncio, os, sys, tempfile
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from app import jobs, status, store
import app.commands  # noqa: F401  导入即注册全部命令
from app.commands import registry as R
from app.models import Segment, Speaker, tts_audio_path

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

VID = "cmdtest000000000000000000000000"
src = os.path.join(TMP, "v.mp4"); open(src, "wb").write(b"\0")

def fresh():
    if store.exists(VID): store.delete(VID)
    store.create(VID, "v.mp4", src)
    def seed(s):
        s.target_language = "English"
        s.speakers = [Speaker("0", "Speaker 0", voice="Emily_v3.1"), Speaker("1", "Speaker 1", voice="Brian_v3.1")]
        s.segments = [
            Segment("a", "0", 0.0, 4.0, "你好，今天天气不错", translated_text="Hello, nice weather"),
            Segment("b", "1", 4.0, 7.0, "是啊，我们出去走走吧"),
            Segment("c", "0", 7.0, 9.0, "（背景音乐）"),
            Segment("d", "1", 9.0, 12.0, "好的"),
        ]
        s.segments[0].tr_fp = status.expected_tr_fp(s, s.segments[0])
        s.stage = "ready"
    store.mutate(VID, seed, actor="system", cmd="seed", summary="seed")

def run(name, args=None, actor="ai"):
    return asyncio.run(R.execute(VID, name, args or {}, actor=actor))

# ------------------------------------------------------------------ ① 注册表结构
specs = R.tool_specs()
names = [t["function"]["name"] for t in specs]
check("① tool 列表与注册表一一对应", len(names) == len(set(names)) == len([c for c in R.all_commands() if c.ai]))
check("① 每个 tool 都有说明和参数 schema", all(t["function"]["description"] and t["function"]["parameters"]["type"] == "object" for t in specs))
check("① 需要确认的命令在说明里点明", all("确认" in t["function"]["description"] for t in specs
      if R.get(t["function"]["name"]).confirm != R.CONFIRM_NEVER))
check("① 只读命令不要求确认", all(R.needs_confirmation(c.name, "ai") is None for c in R.all_commands() if c.readonly))
check("① 用户自己点的按钮不需要再确认", R.needs_confirmation("set_settings", "user") is None)
check("① AI 调用 set_settings 需要确认（destroy）", R.needs_confirmation("set_settings", "ai") == R.CONFIRM_DESTROY)

# ------------------------------------------------------------------ ② 参数校验
fresh()
r = run("edit_source", {"line": 1})
check("② 缺必填参数 → 说清缺什么", not r["ok"] and "text" in r["error"], r)
r = run("edit_source", {"line": "x", "text": "y"})
check("② 类型错 → 说清类型", not r["ok"] and "整数" in r["error"], r)
r = run("edit_source", {"line": 1, "text": "y", "bogus": 1})
check("② 多余参数 → 列出有效参数", not r["ok"] and "bogus" in r["error"] and "line" in r["error"], r)
r = run("set_dub_style", {"lines": [1], "rate": 9})
check("② 超范围 → 说清范围", not r["ok"] and "2.0" in r["error"], r)
r = run("mark_non_speech", {"lines": [1], "kind": "weird"})
check("② 枚举外的值 → 列出可选", not r["ok"] and "music" in r["error"], r)
r = run("nope")
check("② 不存在的命令", not r["ok"] and "没有这个命令" in r["error"])

# ------------------------------------------------------------------ ③ 行号解析
r = run("edit_source", {"line": 99, "text": "x"})
check("③ 行号越界 → 说清有几句", not r["ok"] and "共 4 句" in r["error"], r)

# ------------------------------------------------------------------ ④ edit_source：译文靠指纹过期
r = run("edit_source", {"line": 1, "text": "您好，今天天气真不错"})
st = store.get_state(VID)
check("④ 改原文成功", r["ok"] and st.segments[0].text == "您好，今天天气真不错")
check("④ 译文因此过期（没有任何手动作废）", status.tr_status(st, st.segments[0]) == status.STALE)
check("④ 返回里提示下一步", "retranslate" in r["result"]["note"])
r = run("edit_source", {"line": 1, "text": "  "})
check("④ 空原文被拒，并指路", not r["ok"] and "mark_non_speech" in r["error"])

# ------------------------------------------------------------------ ⑤ split_line
fresh()
r = run("split_line", {"line": 1, "text_first": "你好", "text_second": "今天天气不错", "at_seconds": 1.5})
st = store.get_state(VID)
check("⑤ 拆成两句", r["ok"] and len(st.segments) == 5 and st.segments[0].text == "你好" and st.segments[1].text == "今天天气不错")
check("⑤ 时间被正确切开", st.segments[0].end_time == 1.5 and st.segments[1].start_time == 1.5 and st.segments[1].end_time == 4.0)
check("⑤ 后面的行号顺延（原来的第 2 句变第 3 句）", st.segments[2].id == "b")
check("⑤ 两半都需要重翻（旧译文是整句的）", status.tr_status(st, st.segments[0]) == status.STALE)
r = run("split_line", {"line": 1, "text_first": "你", "text_second": "好", "at_seconds": 0.05})
check("⑤ 切点太靠边被拒", not r["ok"] and "0.2" in r["error"], r)
r = run("split_line", {"line": 2, "text_first": "今", "text_second": "天", "second_speaker": "1"})
st = store.get_state(VID)
check("⑤ 不给时间 → 按字数比例估算，且指定后半句说话人", r["ok"] and st.segments[2].speaker_id == "1" and 1.5 < st.segments[2].start_time < 4.0)
check("⑤ 拆句可撤销，一步回到原样", (run("undo"), run("undo"), len(store.get_state(VID).segments))[2] == 4)

# ------------------------------------------------------------------ ⑥ merge_lines
fresh()
r = run("merge_lines", {"lines": [2, 4]})
check("⑥ 不相邻被拒", not r["ok"] and "相邻" in r["error"], r)
r = run("merge_lines", {"lines": [2, 3]})
check("⑥ 不同说话人被拒，并指路", not r["ok"] and "set_speaker" in r["error"], r)
run("set_speaker", {"lines": [3], "speaker": "1"})
r = run("merge_lines", {"lines": [2, 3]})
st = store.get_state(VID)
check("⑥ 合并：文本拼接、时间取并集、少一行", r["ok"] and len(st.segments) == 3 and st.segments[1].end_time == 9.0 and "背景音乐" in st.segments[1].text)

# ------------------------------------------------------------------ ⑦ mark_non_speech（你要的：音乐误识别）
fresh()
r = run("mark_non_speech", {"lines": [3], "kind": "music"})
st = store.get_state(VID)
check("⑦ 标成音乐", r["ok"] and st.segments[2].kind == "music")
check("⑦ 音乐行：不翻译、不配音", status.tr_status(st, st.segments[2]) == status.NONE and status.tts_status(st, st.segments[2]) == status.NONE)
check("⑦ 音乐行不进概览的待办", run("get_overview")["result"]["todo"]["translate"] == 2)
from app.services import subtitle_service as SS
check("⑦ 音乐行不出字幕", all("背景音乐" not in c.text for c in SS.build_cues(st.segments, SS.default_style(), probe_audio=False)))
from app.services import mix_service
check("⑦ 音乐行不进混音计划", all(e["id"] != "c" for e in mix_service._segment_entries(st)))
run("mark_non_speech", {"lines": [3], "kind": "speech"})
check("⑦ 标回语音就恢复", store.get_state(VID).segments[2].kind == "speech")

# ------------------------------------------------------------------ ⑧ set_speaker / rename
fresh()
r = run("set_speaker", {"lines": [4], "speaker": "2"})
st = store.get_state(VID)
check("⑧ 新建说话人并改归属", r["ok"] and r["result"]["created"] and st.segments[3].speaker_id == "2" and st.speaker("2"))
run("set_speaker", {"lines": [4], "speaker": "0"})
check("⑧ 没人用的说话人自动清掉", store.get_state(VID).speaker("2") is None)
r = run("set_speaker", {"lines": [4], "speaker": "0"})
r = run("rename_speaker", {"speaker": "Speaker 1", "name": "小明"})
check("⑧ 改名（按名字或 id 都能找到）", r["ok"] and store.get_state(VID).speaker("1").name == "小明")
r = run("rename_speaker", {"speaker": "nobody", "name": "x"})
check("⑧ 说话人不存在 → 列出现有的", not r["ok"] and "0" in r["error"] and "小明" in r["error"], r)

# ------------------------------------------------------------------ ⑨ 配音设置联动
fresh()
os.makedirs(os.path.dirname(tts_audio_path(VID, "a")), exist_ok=True); open(tts_audio_path(VID, "a"), "wb").write(b"x")
def mark_dubbed(s):
    a = s.segment("a"); a.tts_fp = status.expected_tts_fp(s, a); a.actual_duration = 3.0
store.mutate(VID, mark_dubbed, actor="system", cmd="dub", summary="dub")
check("⑨ 起点：第 1 句配音新鲜", status.tts_status(store.get_state(VID), store.get_state(VID).segments[0]) == status.FRESH)
r = run("set_dub_style", {"lines": [1], "instruction": "请用悲伤的语气慢慢说。"})
st = store.get_state(VID)
check("⑨ 设语气 → 该句配音过期", r["ok"] and status.tts_status(st, st.segments[0]) == status.STALE)
check("⑨ 语气指令进了合成指令", "悲伤" in (status.dub_instruction(st, st.segments[0]) or ""))
r = run("set_dub_style", {"lines": [1], "voice": "NotAVoice_v9"})
check("⑨ 不属于该语言的音色被拒，并指路", not r["ok"] and "list_voices" in r.get("hint", ""), r)
r = run("set_dub_style", {"lines": [1]})
check("⑨ 什么都不给被拒", not r["ok"] and "至少" in r["error"])
r = run("set_dub_style", {"lines": [1], "instruction": "", "rate": 0})
check("⑨ 空串/0 = 清除", store.get_state(VID).segments[0].tts_instruction is None and store.get_state(VID).segments[0].tts_rate is None)
check("⑨ 清除后指纹对回去 → 旧 mp3 又新鲜了", status.tts_status(store.get_state(VID), store.get_state(VID).segments[0]) == status.FRESH)

r = run("set_muted", {"lines": [1, 2], "muted": True})
check("⑨ 静音", r["ok"] and all(s.muted for s in store.get_state(VID).segments[:2]))
r = run("set_hidden", {"lines": [2], "hidden": True})
check("⑨ 隐藏", store.get_state(VID).segments[1].hidden)

# ------------------------------------------------------------------ ⑩ 音色
fresh()
r = run("set_speaker_voice", {"speaker": "0", "voice": "Bad_v1"})
check("⑩ 音色不属于该语言被拒", not r["ok"])
pool = config.voices_for_language("English")
r = run("set_speaker_voice", {"speaker": "0", "voice": pool[0]})
check("⑩ 指定系统音色", r["ok"] and store.get_state(VID).speaker("0").voice == pool[0] and r["result"]["lines_affected"] == 2)
r = run("set_speaker_voice", {"speaker": "0", "voice": ""})
check("⑩ 交回自动分配", r["ok"] and store.get_state(VID).speaker("0").voice in pool)
from app.services import voice_clone_service as vc
st = store.get_state(VID)
try:
    vc.validate_chosen(st.segments, "0", ["a", "zzz"]); ok = False
except ValueError as e:
    ok = "找不到" in str(e)
check("⑩ 克隆参考行：不存在的行被拒", ok)
try:
    vc.validate_chosen(st.segments, "0", ["b"]); ok = False
except ValueError as e:
    ok = "不属于" in str(e)
check("⑩ 克隆参考行：别人的行被拒", ok)
try:
    vc.validate_chosen(st.segments, "0", ["a"]); ok = True
except ValueError:
    ok = False
check("⑩ 克隆参考行：合格的通过", ok)

# ------------------------------------------------------------------ ⑪ 设置
fresh()
r = run("set_settings", {"target_language": "Japanese"})
st = store.get_state(VID)
check("⑪ 改目标语言：音色清空（旧语言的音色在新语言下会被拒）", r["ok"] and all(sp.voice is None for sp in st.speakers))
check("⑪ ……所有译文过期，待办里反映出来", r["result"]["todo"]["translate"] == 4)
r = run("set_settings", {"accent": "不存在话"})
check("⑪ 不支持的方言被拒并列出可选", not r["ok"] and "广东话" in r["error"])
r = run("set_settings", {"mm_enhance": True})
check("⑪ 改完没变化的设置 → unchanged", run("set_settings", {"mm_enhance": True})["result"].get("unchanged"))
check("⑪ 撤销设置变更", (run("undo"), store.get_state(VID).mm_enhance)[1] is False)

# ------------------------------------------------------------------ ⑫ 行锁：任务占用的行不能改
fresh()
job = jobs.Job(id="j1", video_id=VID, kind="dub", label="配音", actor="system", lines={"b"})
jobs._jobs.setdefault(VID, {})["j1"] = job
r = run("edit_source", {"line": 2, "text": "改一下"})
check("⑫ 被任务占用的行：拒绝，说明是哪个任务、怎么办", not r["ok"] and "配音" in r["error"] and "cancel_job" in r.get("hint", ""), r)
r = run("edit_source", {"line": 1, "text": "别的行不受影响"})
check("⑫ 其它行照常可改", r["ok"])
r = run("set_muted", {"lines": [2], "muted": True})
check("⑫ 静音/隐藏这类不碰产物的开关不受行锁限制", r["ok"])
jobs._jobs[VID].clear()

# ------------------------------------------------------------------ ⑬ 读命令
fresh()
ov = run("get_overview")["result"]
check("⑬ 概况：行数、说话人、待办", ov["lines"] == 4 and len(ov["speakers"]) == 2 and ov["todo"]["translate"] == 3)
ls = run("list_lines", {"filter": "untranslated"})["result"]
check("⑬ 筛选：未翻译的 3 句", ls["matched"] == 3 and [l["line"] for l in ls["lines"]] == [2, 3, 4])
ls = run("list_lines", {"speaker": "Speaker 1"})["result"]
check("⑬ 按说话人名字筛选", [l["line"] for l in ls["lines"]] == [2, 4])
ls = run("list_lines", {"limit": 2})["result"]
check("⑬ 分页提示", ls["matched"] == 4 and len(ls["lines"]) == 2 and "只显示" in ls["note"])
gl = run("get_lines", {"lines": [2]})["result"]["lines"][0]
check("⑬ 取完整内容带前后文", gl["previous"]["line"] == 1 and gl["next"]["line"] == 3)
check("⑬ 一次取太多被拒", not run("get_lines", {"lines": list(range(1, 14))})["ok"])
check("⑬ 搜索", run("search_lines", {"query": "天气"})["result"]["count"] == 1)
check("⑬ 概况够短（每轮都带，必须省）", len(str(ov)) < 1500, len(str(ov)))

# ------------------------------------------------------------------ ⑭ listen：切音频、超限、没有音频
r = run("listen", {"line": 1})
check("⑭ 还没音轨 → 说清原因", not r["ok"] and "音轨" in r["error"], r)
import wave
wav = os.path.join(TMP, VID, "extracted_audio.wav")
with wave.open(wav, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\x01\x00" * 16000 * 14)
r = run("listen", {"line": 2})
check("⑭ 切出音频，带时间范围", r["ok"] and r["result"]["_audio"] and "0:03" in r["result"]["listening"], r.get("error"))
import base64, io
w = wave.open(io.BytesIO(base64.b64decode(r["result"]["_audio"])))
check("⑭ 切出的是 5 秒（第 2 句 4~7 秒，前后各 1 秒）的 16k 单声道", w.getframerate() == 16000 and w.getnchannels() == 1 and abs(w.getnframes() / 16000 - 5.0) < 0.01)
check("⑭ 超过 60 秒被拒", not run("listen", {"start": 0, "end": 90})["ok"])
check("⑭ 超出音频长度被拒", not run("listen", {"start": 50, "end": 55})["ok"])

# ------------------------------------------------------------------ ⑮ 字幕样式
fresh()
r = run("set_subtitle_style", {"font_size_percent": 6, "primary_color": "#FFD400"})
st = store.get_state(VID)
check("⑮ 改字幕样式只动传入的项", r["ok"] and st.subtitle_style["font_size_percent"] == 6 and st.subtitle_style["outline_width"] == 2.0)
check("⑮ 超范围被拒", not run("set_subtitle_style", {"font_size_percent": 50})["ok"])
run("reset_subtitle_style")
check("⑮ 恢复默认", store.get_state(VID).subtitle_style == {})

# ------------------------------------------------------------------ ⑯ delete / 保护
fresh()
check("⑯ 不能删光所有句子", not run("delete_lines", {"lines": [1, 2, 3, 4]})["ok"])
r = run("delete_lines", {"lines": [3]})
check("⑯ 删一句，可撤销", r["ok"] and len(store.get_state(VID).segments) == 3 and (run("undo"), len(store.get_state(VID).segments))[1] == 4)

# ------------------------------------------------------------------ ⑰ 事件：来源标注
fresh()
from app.bus import bus
sub = bus.subscribe(VID, since=bus.current_seq(VID))
run("set_muted", {"lines": [1], "muted": True}, actor="ai")
run("set_muted", {"lines": [2], "muted": True}, actor="user")
evs = []
while not sub.empty(): evs.append(sub.get_nowait())
check("⑰ 每次改动都带来源，AI 能分辨哪些是用户在界面上做的",
      [e["actor"] for e in evs if e["type"] == "change"] == ["ai", "user"])
check("⑰ 事件摘要是人话", "静音第 1 句" in evs[0]["summary"], evs[0]["summary"])

# ------------------------------------------------------------------ ⑱ 译文没翻译：AI 和界面都看得到
fresh()
def untranslated(s):
    seg = s.segment("b"); seg.translated_text = "是啊，我们出去走走吧"; seg.tr_fp = status.expected_tr_fp(s, seg)   # 原样返回了原文
store.mutate(VID, untranslated, actor="system", cmd="translate", summary="翻译")
ov = run("get_overview")["result"]
check("⑱ 概况里单独点出“看起来没翻译”的句数（它的指纹是新鲜的，光看待办发现不了）", ov["looks_untranslated"] == 1 and ov["todo"]["translate"] == 2, ov)
row = run("get_lines", {"lines": [2]})["result"]["lines"][0]
check("⑱ 该行带警告，说明为什么", "不像English" in row.get("warning", ""), row)
from app import view
check("⑱ 视图里同样标出（界面可以打徽标）", [s["trSuspect"] for s in view.snapshot(store.get_state(VID))["segments"]] == [False, True, False, False])
check("⑱ 正常译文没有警告", "warning" not in run("get_lines", {"lines": [1]})["result"]["lines"][0])

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)


def test_commands():
    assert not fails, fails
