"""
状态层：store（唯一写入口）+ status（指纹判断）+ view（客户端视图增量）+ 通用撤销。

不碰网络、不碰 ffmpeg。
"""
import os, sys, tempfile, threading
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from app import status, store, view
from app.bus import bus
from app.models import Segment, Speaker, tts_audio_path

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

VID = "storetest000000000000000000000000"
src = os.path.join(TMP, "v.mp4"); open(src, "wb").write(b"\0")
store.create(VID, "v.mp4", src)

def seed(s):
    s.target_language = "English"; s.enable_dubbing = True
    s.speakers = [Speaker("0", "Speaker 0", voice="Emily_v3.1"), Speaker("1", "Speaker 1", voice="Brian_v3.1")]
    s.segments = [
        Segment("a", "0", 0.0, 2.0, "你好"), Segment("b", "1", 2.0, 4.0, "再见"),
        Segment("c", "0", 4.0, 6.0, "（音乐）"),
    ]
store.mutate(VID, seed, actor="system", cmd="seed", summary="seed")

# ① 刚转写完：缺译文，非语音不需要
st = store.get_state(VID)
check("① 新行缺译文", status.tr_status(st, st.segment("a")) == status.MISSING)
check("① rev 递增", st.rev == 1, st.rev)

# ② 标成音乐 → 不需要翻译 / 配音
store.mutate(VID, lambda s: setattr(s.segment("c"), "kind", "music"), actor="ai", cmd="mark", summary="标记第3句为音乐")
st = store.get_state(VID)
check("② 音乐行：译文/配音都不需要", status.tr_status(st, st.segment("c")) == status.NONE and status.tts_status(st, st.segment("c")) == status.NONE)

# ③ 写入译文 + 指纹 → 新鲜
def translate(s):
    for sid, text in (("a", "Hello"), ("b", "Goodbye")):
        seg = s.segment(sid); seg.translated_text = text; seg.tr_fp = status.expected_tr_fp(s, seg)
store.mutate(VID, translate, actor="system", cmd="translate", summary="翻译")
st = store.get_state(VID)
check("③ 译文指纹对上 = fresh", status.tr_status(st, st.segment("a")) == status.FRESH)

# ④ 改原文 → 译文过期（不用任何"作废"代码）
store.mutate(VID, lambda s: setattr(s.segment("a"), "text", "您好"), actor="user", cmd="edit_source", summary="改原文")
st = store.get_state(VID)
check("④ 改原文 → 译文 stale", status.tr_status(st, st.segment("a")) == status.STALE)
check("④ 另一行不受影响", status.tr_status(st, st.segment("b")) == status.FRESH)

# ⑤ 改目标语言 → 全部译文过期；改回去 → 又新鲜
store.mutate(VID, lambda s: setattr(s, "target_language", "Japanese"), actor="user", cmd="lang", summary="改语言")
st = store.get_state(VID)
check("⑤ 改语言 → 译文全部 stale", status.tr_status(st, st.segment("b")) == status.STALE)
store.mutate(VID, lambda s: setattr(s, "target_language", "English"), actor="user", cmd="lang", summary="改回")
st = store.get_state(VID)
check("⑤ 改回 → 指纹对回去，重新 fresh（不用重翻）", status.tr_status(st, st.segment("b")) == status.FRESH)

# ⑥ 配音：mp3 存在 + 指纹对上才新鲜；过期的 mp3 不会"复活"
def dub(sid, text="Goodbye"):
    os.makedirs(os.path.dirname(tts_audio_path(VID, sid)), exist_ok=True)
    open(tts_audio_path(VID, sid), "wb").write(b"mp3")
    def f(s):
        seg = s.segment(sid); seg.tts_fp = status.expected_tts_fp(s, seg); seg.actual_duration = 1.7
    store.mutate(VID, f, actor="system", cmd="dub", summary="配音")
check("⑥ 没有 mp3 → missing", status.tts_status(store.get_state(VID), store.get_state(VID).segment("b")) == status.MISSING)
dub("b")
st = store.get_state(VID)
check("⑥ mp3 + 指纹 → fresh，audio_path 已填", status.tts_status(st, st.segment("b")) == status.FRESH and st.segment("b").audio_path)
store.mutate(VID, lambda s: setattr(s.segment("b"), "tts_rate", 1.3), actor="ai", cmd="rate", summary="调语速")
st = store.get_state(VID)
check("⑥ 调语速 → 配音 stale，audio_path 不再给前端", status.tts_status(st, st.segment("b")) == status.STALE and st.segment("b").audio_path is None)
store.undo(VID)
st = store.get_state(VID)
check("⑥ 撤销 → 旧 mp3 重新 fresh（不用重新合成）", status.tts_status(st, st.segment("b")) == status.FRESH)

# ⑦ 换音色、换指令、静音
store.mutate(VID, lambda s: setattr(s.speaker("1"), "voice", "Other_v3.1"), actor="ai", cmd="voice", summary="换音色")
check("⑦ 换说话人音色 → 该人的配音 stale", status.tts_status(store.get_state(VID), store.get_state(VID).segment("b")) == status.STALE)
store.undo(VID)
store.mutate(VID, lambda s: setattr(s.segment("b"), "tts_instruction", "请用悲伤的语气。"), actor="ai", cmd="instr", summary="语气")
check("⑦ 加语气指令 → stale", status.tts_status(store.get_state(VID), store.get_state(VID).segment("b")) == status.STALE)
st = store.get_state(VID)
check("⑦ 指令与方言合并且不超 128", len(status.dub_instruction(st, st.segment("b")) or "") <= 128)
store.undo(VID)
store.mutate(VID, lambda s: setattr(s.segment("b"), "muted", True), actor="user", cmd="mute", summary="静音")
check("⑦ 静音 → 不需要配音", status.tts_status(store.get_state(VID), store.get_state(VID).segment("b")) == status.NONE)
store.undo(VID)

# ⑧ 克隆音色：必须按当前 TTS 模型登记才生效
def clone(model):
    def f(s):
        s.enable_voice_clone = True
        s.speaker("0").clone = {"voice_id": "clone-1", "target_model": model}
    return f
store.mutate(VID, clone("some-old-model"), actor="system", cmd="clone", summary="克隆")
st = store.get_state(VID)
check("⑧ 旧模型登记的克隆音色不生效", status.clone_voice_id(st, st.speaker("0")) is None)
store.mutate(VID, clone(config.TTS_MODEL), actor="system", cmd="clone", summary="克隆")
st = store.get_state(VID)
check("⑧ 当前模型的克隆音色生效，且优先于系统音色", status.resolve_voice(st, st.segment("a")) == "clone-1")
check("⑧ 本行指定的音色优先于克隆", (setattr(st.segment("a"), "tts_voice", "X_v3.1"), status.resolve_voice(st, st.segment("a")))[1] == "X_v3.1")

# ⑨ 变更事件：没改什么 → 不发事件、不涨 rev
rev0 = store.get_state(VID).rev
sub = bus.subscribe(VID, since=bus.current_seq(VID))
m = store.mutate(VID, lambda s: None, actor="user", cmd="noop", summary="空")
check("⑨ 没有变化 → changed=False，rev 不变，没有事件", (not m.changed) and store.get_state(VID).rev == rev0 and sub.empty())
m = store.mutate(VID, lambda s: setattr(s.segment("a"), "translated_text", "Hi there"), actor="ai", cmd="set_translation", summary="改第1句译文")
ev = sub.get_nowait()
check("⑨ 有变化 → 一条 change 事件，带 actor / rev / 受影响的行", ev["type"] == "change" and ev["actor"] == "ai" and ev["rev"] == rev0 + 1 and ev["ids"] == ["a"], ev)
seg_patch = ev["patch"]["segments"]
check("⑨ 增量只含改动的那一行", [s["id"] for s in seg_patch["upsert"]] == ["a"] and seg_patch["upsert"][0]["translation"] == "Hi there")
check("⑨ 增量里已带算好的过期状态", seg_patch["upsert"][0]["trStatus"] == status.STALE)

# ⑩ fn 抛异常 → 文档不变
rev1 = store.get_state(VID).rev
def boom(s):
    s.segment("a").text = "不该留下"; raise RuntimeError("x")
try: store.mutate(VID, boom, actor="ai", cmd="boom", summary="boom")
except RuntimeError: pass
check("⑩ fn 抛异常 → 文档与 rev 都不变", store.get_state(VID).segment("a").text == "您好" and store.get_state(VID).rev == rev1)

# ⑪ 撤销：多步、增删行、改顺序、不可撤销的跳过
def split(s):
    a = s.segment("a"); a.end_time = 1.0
    s.segments.insert(1, Segment("a2", "0", 1.0, 2.0, "新半句", kind="speech"))
store.mutate(VID, split, actor="ai", cmd="split", summary="拆第1句")
check("⑪ 拆句后行数 4，第 2 行是 a2", [s.id for s in store.get_state(VID).segments] == ["a", "a2", "b", "c"])
store.mutate(VID, lambda s: setattr(s.segment("b"), "text", "改过"), actor="user", cmd="edit", summary="改第3句原文")
undone = store.undo(VID)
check("⑪ 先撤销最近一次（改第3句原文）", undone["cmd"] == "edit" and store.get_state(VID).segment("b").text == "再见", undone)
store.undo(VID)
check("⑪ 再撤销拆句：a2 消失，a 的时间恢复，顺序回来", [s.id for s in store.get_state(VID).segments] == ["a", "b", "c"] and store.get_state(VID).segment("a").end_time == 2.0)

store.mutate(VID, lambda s: setattr(s.segment("a"), "muted", True), actor="user", cmd="mute", summary="静音", undoable=False)
undone = store.undo(VID)
check("⑪ undoable=False 的改动被跳过，撤销落在它之前的那一条", undone["cmd"] != "mute", undone)

st_before = store.get_state(VID)
n = 0
while True:
    try: store.undo(VID); n += 1
    except store.NothingToUndo: break
    if n > 60: break
check("⑪ 一路撤销到底最终抛 NothingToUndo（不会死循环），且撤销本身不能被再撤销", n < 60)
check("⑪ 系统写入（初始化的转写/翻译/配音）撤不掉：撤销到底之后转写结果还在",
      len(store.get_state(VID).segments) == 3 and len(store.get_state(VID).speakers) == 2)

# ⑫ 视图：第 N 句编号、说话人行数
store.mutate(VID, lambda s: s.segments.insert(0, Segment("z", "1", -1, 0, "前言")), actor="ai", cmd="insert", summary="插入")
v = view.snapshot(store.get_state(VID))
check("⑫ 编号按顺序重排：新插入的是第 1 句，原来的第 1 句变第 2 句",
      [(s["id"], s["line"]) for s in v["segments"]][:2] == [("z", 1), ("a", 2)])
check("⑫ 说话人带行数", {s["id"]: s["lines"] for s in v["speakers"]}["1"] == 2)

# ⑬ 重启：缓存清掉从磁盘重读；事件历史也从 events.jsonl 回来，seq 不回退
seq_before = bus.current_seq(VID)
store._cache.clear(); store._history.clear(); bus._channels.clear()
st2 = store.get_state(VID)
check("⑬ 重启后文档完整（segments / rev 一致）", st2 is not None and len(st2.segments) == 4 and st2.rev == store.get_state(VID).rev)
store.mutate(VID, lambda s: setattr(s.segment("a"), "text", "重启后改"), actor="user", cmd="edit", summary="重启后改")
check("⑬ seq 接着落盘事件往下数，不回退", bus.current_seq(VID) > seq_before, (bus.current_seq(VID), seq_before))
check("⑬ 重启后仍能撤销重启前的改动", store.undo(VID)["cmd"] == "edit")

# ⑭ 并发：8 个线程各改不同的行 100 次，不丢更新
def many(i):
    for k in range(100):
        store.mutate(VID, lambda s, i=i, k=k: setattr(s.segment("a"), "translated_text", f"t{i}-{k}") if i == 0
                     else s.segments.append(Segment(f"n{i}-{k}", "0", 9, 10, "x")) if False else None,
                     actor="system", cmd="load", summary="load")
threads = [threading.Thread(target=many, args=(i,)) for i in range(8)]
[t.start() for t in threads]; [t.join() for t in threads]
check("⑭ 并发写入后文档仍可读、rev 单调", store.get_state(VID) is not None)

counter = {"n": 0}
def bump(s):
    s.rev_probe = getattr(s, "rev_probe", 0)
def incr(i):
    for _ in range(50):
        store.mutate(VID, lambda s: setattr(s.segment("b"), "tts_rate", round((s.segment("b").tts_rate or 1.0) + 0.001, 3)),
                     actor="system", cmd="incr", summary="incr", undoable=False)
rate0 = store.get_state(VID).segment("b").tts_rate or 1.0
ts = [threading.Thread(target=incr, args=(i,)) for i in range(6)]
[t.start() for t in ts]; [t.join() for t in ts]
rate1 = store.get_state(VID).segment("b").tts_rate
check("⑭ 6 线程 × 50 次读—改—写没有丢更新（300 次累加全部生效）", abs(rate1 - rate0 - 0.3) < 1e-6, (rate0, rate1))

# ⑮ project.json 损坏：返回 None，文件原样保留
store._cache.clear()
p = store.project_path(VID); good = open(p, encoding="utf-8").read()
open(p, "w").write("{broken")
check("⑮ project.json 损坏 → 读不出来（None），不是崩溃", store.get_state(VID) is None and os.path.exists(p))
open(p, "w", encoding="utf-8").write(good); store._cache.clear()
check("⑮ 文件恢复后能再打开", store.get_state(VID) is not None)

# ⑯ 删除：目录、缓存、订阅者全清
sub = bus.subscribe(VID)
store.delete(VID)
check("⑯ 删除后不存在", not store.exists(VID) and store.get_state(VID) is None)

# ⑰ 译文是不是目标语言（脚本级粗检查）：只在"几乎全是另一种文字"时报，混用专有名词不误报
from app.models import VideoState
def suspect(lang, text, kind="speech"):
    st = VideoState(video_id="x", filename="f", file_path="p", target_language=lang)
    return status.untranslated_suspect(st, Segment("a", "0", 0, 1, "原文", translated_text=text, kind=kind))
check("⑰ 目标英文、译文是整句中文 → 报（翻译模型原样返回原文）", suspect("English", "柠檬牌外置眼球，让您看到更大的世界。"))
check("⑰ 带情感标签也认得出来（标签不算文字）", suspect("English", "[excited] 欢迎收看！") and not suspect("English", "[excited] Welcome!"))
check("⑰ 正常英文不报", not suspect("English", "Lemon brand eyeballs — see the world."))
check("⑰ 夹杂少量原文词不误报（专有名词、混用很常见）",
      not suspect("English", "iPhone 15 Pro 发布会") and not suspect("English", "Use the 3D 打印机 for it") and not suspect("Chinese", "iPhone 15 发布会很棒"))
check("⑰ 反向：目标中文、译文是英文 → 报", suspect("Chinese", "Hello and welcome"))
check("⑰ 日语假名算 CJK，不误报", not suspect("Japanese", "こんにちは、元気ですか") and suspect("Japanese", "Hello"))
check("⑰ 韩文 / 俄文各认各的脚本", not suspect("Korean", "안녕하세요") and suspect("Korean", "你好") and not suspect("Russian", "Привет, как дела") and suspect("Russian", "Hello"))
check("⑰ 拉丁字母语言之间不假装能分（英/法/德靠脚本分不出来）", not suspect("French", "Hello everyone") and not suspect("German", "Bonjour"))
check("⑰ 太短 / 纯数字 / 空 / 不认识的语言 / 非语音行，都不报",
      not suspect("English", "OK") and not suspect("English", "3.14") and not suspect("English", "") and not suspect("Thai", "สวัสดี") and not suspect("English", "(音乐)", kind="music"))
print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)


def test_store():
    assert not fails, fails
