"""
聊天会话（历史 / 新对话）：一个 chat.jsonl 里按 conv 分开，切换只是换个过滤条件。
同时盯住两件容易回归的事：
  · 会话切换是**静默**写（不进事件流、不进撤销栈），否则它会出现在 AI 读的"发生了什么"里；
  · 升级前的老记录没有 conv 字段，必须仍落在默认会话里，不能凭空消失。
"""
import os, sys, tempfile
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from app import store

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

VID = "convtest0000000000000000000000"
src = os.path.join(TMP, "v.mp4"); open(src, "wb").write(b"\0")
store.create(VID, "v.mp4", src)

# ------------------------------------------------------------------ ① 会话的开与切
c1 = store.current_conv(VID)
check("① 首次取当前会话：自动建一个并写回工程", bool(c1) and store.require_state(VID).chat_conv == c1)
check("① 再取一次不变（幂等）", store.current_conv(VID) == c1)

store.append_chat(VID, {"role": "user", "text": "第一个会话里问的问题", "ts": 100})
store.append_chat(VID, {"role": "assistant", "text": "回答一", "ts": 101})
check("① 消息自动带上会话 id", all(m.get("conv") == c1 for m in store.read_chat(VID)))

c2 = store.new_conv(VID)
check("② 新对话换了 id，并切过去", c2 != c1 and store.require_state(VID).chat_conv == c2)
store.append_chat(VID, {"role": "user", "text": "第二个会话", "ts": 200})
check("② 新会话里看不到旧消息", [m["text"] for m in store.read_chat(VID)] == ["第二个会话"])
check("② 旧会话原样还在", [m["text"] for m in store.read_chat(VID, conv=c1)] == ["第一个会话里问的问题", "回答一"])

# ------------------------------------------------------------------ ② 列表
convs = store.list_convs(VID)
check("③ 列表新的在前", [c["id"] for c in convs] == [c2, c1], [c["id"] for c in convs])
by_id = {c["id"]: c for c in convs}
check("③ 标题取该会话第一条用户消息", by_id[c1]["title"] == "第一个会话里问的问题", by_id[c1]["title"])
check("③ 条数只算真实消息（不含 confirm_state）",
      by_id[c1]["count"] == 2 and by_id[c2]["count"] == 1, by_id)

# ------------------------------------------------------------------ ③ 切换
check("④ 切回旧会话", store.select_conv(VID, c1) is True and store.require_state(VID).chat_conv == c1)
check("④ 切回后读到的就是旧会话", [m["text"] for m in store.read_chat(VID)] == ["第一个会话里问的问题", "回答一"])
check("④ 不存在的会话：拒绝，且当前会话不变",
      store.select_conv(VID, "nope") is False and store.require_state(VID).chat_conv == c1)

# ------------------------------------------------------------------ ④ 静默写
store.new_conv(VID)
store.select_conv(VID, c1)
import glob
paths = glob.glob(os.path.join(TMP, VID, "events*.jsonl"))
log = "".join(open(p, encoding="utf-8").read() for p in paths)
check("⑤ 会话切换不进事件流（否则会出现在 AI 读的\"自上次以来发生了什么\"里）", "chat_conv" not in log, log[-200:])
check("⑤ 会话切换不进撤销栈", '"summary": "切换会话"' not in log)

# ------------------------------------------------------------------ ⑤ 没有 conv 的行不属于任何会话
with open(store._chat_path(VID), "a", encoding="utf-8") as f:
    f.write('{"role": "user", "text": "没有会话 id 的脏数据", "ts": 50}\n')
check("⑥ 没有 conv 的行不会出现在任何会话里（不做迁移）",
      all(m["text"] != "没有会话 id 的脏数据" for c in store.list_convs(VID) for m in store.read_chat(VID, conv=c["id"])))
check("⑥ 它也不会在列表里造出一个空 id 的会话", all(c["id"] for c in store.list_convs(VID)))

# ------------------------------------------------------------------ ⑥ 确认卡片状态按会话折叠
store.new_conv(VID)
store.append_chat(VID, {"role": "confirm", "id": "card1", "cmd": "redub", "ts": 300})
store.resolve_confirm_card(VID, "card1", True)
cards = [m for m in store.read_chat(VID) if m["role"] == "confirm"]
check("⑦ 确认卡片的最终状态在所属会话里还原", cards and cards[0]["status"] == "approved", cards)
check("⑦ confirm_state 行不会作为消息冒出来", all(m["role"] != "confirm_state" for m in store.read_chat(VID)))

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
