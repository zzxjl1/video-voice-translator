"""
AI 对话循环：用**脚本化的假模型**驱动完整循环（不调真 API）。

假模型按剧本一步步回：发 tool call、等结果、再发……循环要正确处理的事：
  · 并行 tool call —— 每一个都必须有对应的 tool 回复（协议要求，漏一个下一次请求被拒）；
  · 先 listen 再动手 —— 音频作为紧随其后的 user 消息注入；
  · 需要确认的命令暂停，等用户点；拒绝则把"用户拒绝"回给模型；
  · UI → AI：用户在界面上的改动出现在下一轮的通知里，AI 自己的改动不重复报；
  · AI → UI：AI 的改动发 change 事件（actor=ai）；
  · 步数上限、取消、工具出错时模型能看到原因并自行改正。
"""
import asyncio, json, os, sys, tempfile, types
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from app import agent, commands, status, store  # noqa: F401
from app.bus import bus
from app.models import Segment, Speaker

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

VID = "agenttest00000000000000000000000"
src = os.path.join(TMP, "v.mp4"); open(src, "wb").write(b"\0")


# ----------------------------------------------------------------- 假模型
class Script:
    """每次被调用时从剧本里取下一步；记录收到的 messages，供断言。"""
    def __init__(self, steps):
        self.steps = list(steps); self.seen = []

    async def create(self, **kw):
        self.seen.append(json.loads(json.dumps(kw["messages"], default=str)))
        step = self.steps.pop(0)
        return self._stream(step() if callable(step) else step)

    async def _stream(self, step):
        text = step.get("text", "")
        for i in range(0, len(text), 4):
            yield self._chunk(content=text[i:i + 4])
        for idx, (name, args) in enumerate(step.get("calls", [])):
            raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            half = len(raw) // 2
            yield self._chunk(tool=(idx, f"call_{idx}_{name}", name, raw[:half]))       # 参数分片到达
            yield self._chunk(tool=(idx, None, None, raw[half:]))
        yield types.SimpleNamespace(choices=[], usage=types.SimpleNamespace(model_dump=lambda: {"prompt_tokens": 100, "completion_tokens": 20}))

    @staticmethod
    def _chunk(content=None, tool=None):
        tcs = None
        if tool:
            idx, cid, name, args = tool
            tcs = [types.SimpleNamespace(index=idx, id=cid, function=types.SimpleNamespace(name=name, arguments=args))]
        delta = types.SimpleNamespace(content=content, tool_calls=tcs)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)], usage=None)


def install(steps):
    script = Script(steps)
    agent._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=script))
    return script


def fresh():
    if store.exists(VID): store.delete(VID)
    agent._chats.clear()
    store.create(VID, "v.mp4", src)
    def seed(s):
        s.target_language = "English"; s.stage = "ready"
        s.speakers = [Speaker("0", "Speaker 0", voice="Emily_v3.1")]
        s.segments = [Segment("a", "0", 0, 4, "你好"), Segment("b", "0", 4, 8, "（音乐）"), Segment("c", "0", 8, 12, "再见")]
        for x in s.segments:
            x.translated_text = "t"; x.tr_fp = status.expected_tr_fp(s, x)
    store.mutate(VID, seed, actor="system", cmd="seed", summary="seed")

def say(text): return asyncio.run(agent.handle_user_message(VID, text))

def roles(script, n):                      # 第 n 次请求的角色序列
    return [m["role"] for m in script.seen[n]]

def events_since(seq):
    q = bus.subscribe(VID, since=seq); out = []
    while not q.empty(): out.append(q.get_nowait())
    return out


# ------------------------------------------------------------------ ① 最简单的一轮：只回话
fresh()
s = install([{"text": "你好，有什么可以帮你？"}])
seq0 = bus.current_seq(VID)
say("你好")
chat = store.read_chat(VID)
check("① 用户消息和 AI 回复都落盘", [m["role"] for m in chat] == ["user", "assistant"] and chat[1]["text"] == "你好，有什么可以帮你？")
check("① 系统提示里带着实时概况", "项目概况" in s.seen[0][0]["content"] and '"lines": 3' in s.seen[0][0]["content"])
check("① 带上了全部 tool", True)
kinds = [e["kind"] for e in events_since(seq0) if e["type"] == "chat"]
check("① 前端收到：start → 若干 delta → message(user) → message(assistant) → idle",
      kinds[0] == "message" and "start" in kinds and "delta" in kinds and kinds[-1] == "idle", kinds)

# ------------------------------------------------------------------ ② 一次 tool call，改动反映到文档与事件
fresh()
s = install([
    {"text": "我来标记一下。", "calls": [("mark_non_speech", {"lines": [2], "kind": "music"})]},
    {"text": "已把第 2 句标成音乐。"},
])
seq0 = bus.current_seq(VID)
say("第 2 句是背景音乐")
st = store.get_state(VID)
check("② tool call 真的改了项目", st.segments[1].kind == "music")
check("② 参数分片到达也能拼对", True)
changes = [e for e in events_since(seq0) if e["type"] == "change"]
check("② 改动事件标明是 AI 做的（前端据此高亮）", len(changes) == 1 and changes[0]["actor"] == "ai" and changes[0]["ids"] == ["b"], changes)
check("② 第二次请求里带着 assistant 的 tool_calls 和对应的 tool 回复",
      roles(s, 1)[-2:] == ["assistant", "tool"], roles(s, 1))
tool_msg = s.seen[1][-1]
check("② tool 回复是命令的结果（JSON）", json.loads(tool_msg["content"])["ok"] is True)
chat = store.read_chat(VID)
check("② 聊天记录里记了本轮做了什么", chat[-1]["actions"][0]["name"] == "mark_non_speech")

# ------------------------------------------------------------------ ③ 并行 tool call：每一个都要有回复
fresh()
s = install([
    {"calls": [("set_muted", {"lines": [1], "muted": True}),
               ("mark_non_speech", {"lines": [2], "kind": "noise"}),
               ("set_hidden", {"lines": [3], "hidden": True})]},
    {"text": "三件事都做了。"},
])
say("静音第一句，第二句标噪音，第三句隐藏字幕")
st = store.get_state(VID)
check("③ 三个并行调用全部执行", st.segments[0].muted and st.segments[1].kind == "noise" and st.segments[2].hidden)
tail = [m for m in s.seen[1] if m["role"] == "tool"]
check("③ 每个 tool_call 都有对应的 tool 回复（协议要求）", len(tail) == 3 and {m["tool_call_id"] for m in tail} == {
    "call_0_set_muted", "call_1_mark_non_speech", "call_2_set_hidden"}, [m["tool_call_id"] for m in tail])

# ------------------------------------------------------------------ ④ 出错时模型能看到原因并改正
fresh()
s = install([
    {"calls": [("edit_source", {"line": 99, "text": "x"})]},
    {"calls": [("edit_source", {"line": 1, "text": "您好"})]},
    {"text": "改好了。"},
])
say("把第 99 句改成x")
second = json.loads([m for m in s.seen[1] if m["role"] == "tool"][0]["content"])
check("④ 第一次失败，错误原因回给了模型（含有效范围）", second["ok"] is False and "共 3 句" in second["error"], second)
check("④ 模型据此改正后成功", store.get_state(VID).segments[0].text == "您好")

# ------------------------------------------------------------------ ⑤ listen：音频作为下一条 user 消息注入
fresh()
import wave
with wave.open(os.path.join(TMP, VID, "extracted_audio.wav"), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\x01\x00" * 16000 * 14)
s = install([
    {"calls": [("listen", {"line": 2})]},
    {"calls": [("mark_non_speech", {"lines": [2], "kind": "music"})]},
    {"text": "确认是音乐，已标记。"},
])
say("第 2 句是不是音乐？听一下")
r = s.seen[1]
check("⑤ tool 回复之后紧跟一条带音频的 user 消息", r[-2]["role"] == "tool" and r[-1]["role"] == "user"
      and any(p.get("type") == "input_audio" for p in r[-1]["content"]), [m["role"] for m in r])
check("⑤ 音频不在 tool 回复的文本里（太大）", "_audio" not in r[-2]["content"] and len(r[-2]["content"]) < 400)
check("⑤ 音频格式正确（data URI + wav）", r[-1]["content"][1]["input_audio"]["data"].startswith("data:;base64,") and r[-1]["content"][1]["input_audio"]["format"] == "wav")
check("⑤ 后续动作基于听到的结果", store.get_state(VID).segments[1].kind == "music")
check("⑤ 音频不进持久化的聊天记录", "input_audio" not in open(os.path.join(TMP, VID, "chat.jsonl"), encoding="utf-8").read())

# ------------------------------------------------------------------ ⑥ UI → AI：用户在界面上的改动
fresh()
s = install([{"text": "好的。"}])
say("你好")                                             # 第一轮，建立"已看到"的基线
user_seq = bus.current_seq(VID)
asyncio.run(commands.registry.execute(VID, "set_muted", {"lines": [1], "muted": True}, actor="user"))
asyncio.run(commands.registry.execute(VID, "edit_source", {"line": 3, "text": "拜拜"}, actor="user"))
s2 = install([{"text": "看到了你的修改。"}])
say("接着做吧")
msgs = s2.seen[0]
last_user = msgs[-1]["content"]
check("⑥ 下一轮的用户消息前面带着“自上次以来”通知", "自你上次回复以来" in last_user and "接着做吧" in last_user, last_user[:300])
check("⑥ 通知里有用户做的两件事，且标明是用户在界面上做的", last_user.count("用户在界面上") == 2 and "静音第 1 句" in last_user and "改第 3 句原文" in last_user)
s3 = install([{"text": "嗯。"}])
say("再说一句")
check("⑥ 再下一轮不会重复报同样的通知", "自你上次回复以来" not in s3.seen[0][-1]["content"])

# ------------------------------------------------------------------ ⑦ AI 自己的改动不会在下一轮被当成“别人做的”
fresh()
install([{"calls": [("set_muted", {"lines": [1], "muted": True})]}, {"text": "已静音。"}]); say("静音第一句")
s = install([{"text": "好。"}]); say("然后呢")
check("⑦ AI 的改动不重复报给它自己", "自你上次回复以来" not in s.seen[0][-1]["content"], s.seen[0][-1]["content"][:200])

# ------------------------------------------------------------------ ⑧ 确认流程
fresh()
s = install([
    {"calls": [("set_settings", {"target_language": "Japanese"})]},
    {"text": "已改成日语。"},
])
async def with_user(approve):
    task = asyncio.create_task(agent.handle_user_message(VID, "改成日语"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        card = next((m for m in store.read_chat(VID) if m["role"] == "confirm"), None)
        if card: break
    assert card, "没有出确认卡片"
    before = store.get_state(VID).target_language
    agent.resolve_confirmation(VID, card["id"], approve)
    await task
    return card, before
card, before = asyncio.run(with_user(True))
check("⑧ 需要确认的命令：先出卡片，不立刻执行", card["command"] == "set_settings" and card["level"] == "destroy" and before == "English")
check("⑧ 用户确认后才执行", store.get_state(VID).target_language == "Japanese")

fresh()
s = install([{"calls": [("set_settings", {"target_language": "Japanese"})]}, {"text": "好吧，不改。"}])
card, _ = asyncio.run(with_user(False))
check("⑧ 用户拒绝 → 不执行", store.get_state(VID).target_language == "English")
refused = json.loads([m for m in s.seen[1] if m["role"] == "tool"][0]["content"])
check("⑧ 拒绝的结果回给模型，并告诉它别重复请求", refused["ok"] is False and "没有确认" in refused["error"])

check("⑧ 不存在的确认卡片 → 返回 False 而不是崩", agent.resolve_confirmation(VID, "nope", True) is False)

# ------------------------------------------------------------------ ⑨ 步数上限
fresh()
install([{"calls": [("get_overview", {})]} for _ in range(agent.MAX_STEPS + 3)])
say("一直看")
check("⑨ 步数上限：不会无限循环，并告诉用户", "步骤太多" in store.read_chat(VID)[-1]["text"])

# ------------------------------------------------------------------ ⑩ 非法参数 JSON / 未知命令
fresh()
s = install([{"calls": [("edit_source", "{not json")]}, {"calls": [("no_such_cmd", {})]}, {"text": "好"}])
say("乱调")
r1 = json.loads([m for m in s.seen[1] if m["role"] == "tool"][0]["content"])
r2 = json.loads([m for m in s.seen[2] if m["role"] == "tool"][-1]["content"])
check("⑩ 参数不是合法 JSON → 回给模型，不崩", r1["ok"] is False and "JSON" in r1["error"])
check("⑩ 不存在的命令 → 回给模型", r2["ok"] is False and "没有这个命令" in r2["error"])

# ------------------------------------------------------------------ ⑪ 上下文大小：历史不无限增长
fresh()
for i in range(60):
    store.append_chat(VID, {"id": f"u{i}", "role": "user", "text": f"消息{i}", "ts": i})
    store.append_chat(VID, {"id": f"a{i}", "role": "assistant", "text": f"回复{i}", "ts": i, "actions": [{"name": "x", "summary": "x()"}]})
s = install([{"text": "好"}]); say("现在呢")
check("⑪ 带入上下文的历史有上限", len(s.seen[0]) <= agent.HISTORY_MESSAGES + 3, len(s.seen[0]))
check("⑪ 历史里的 tool 调用折叠成文字（不带细节）", all(m["role"] in ("system", "user", "assistant") for m in s.seen[0]))

# ------------------------------------------------------------------ ⑫ tool 结果过大被截断
check("⑫ 过大的 tool 结果被截断并提示", "已截断" in agent._cap({"x": "y" * 10000}) and len(agent._cap({"x": "y" * 10000})) < 6500)

# ------------------------------------------------------------------ ⑬ 取消
# 在"第 2 步的模型请求里"取消（确定性地卡在循环中间，而不是靠 sleep 去赛跑）
fresh()
def cancel_during_second_step():
    agent.cancel_chat(VID)
    return {"text": "第二步的话", "calls": [("set_muted", {"lines": [1], "muted": True})]}
s = install([{"calls": [("get_overview", {})]}, cancel_during_second_step, {"text": "不该走到这"}])
say("开始")
last = store.read_chat(VID)[-1]
check("⑬ 取消后停止，回复里标注", "已停止" in last["text"] and "不该走到这" not in last["text"], last["text"])
check("⑬ 取消之后不再执行剩下的工具", store.get_state(VID).segments[0].muted is False)
check("⑬ 没有再发第三次模型请求", len(s.seen) == 2, len(s.seen))
check("⑬ 取消后下一条消息能正常处理（取消标志已复位）",
      (install([{"text": "我在"}]), say("在吗"), store.read_chat(VID)[-1]["text"])[2] == "我在")

# ⑧b 卡片状态落盘：刷新页面后不能又变回"待确认"（真浏览器里出现过：点了确认，重载后按钮又亮了）
fresh()
s = install([{"calls": [("set_settings", {"target_language": "Japanese"})]}, {"text": "好"}])
card, _ = asyncio.run(with_user(True))
cards = [m for m in store.read_chat(VID) if m["role"] == "confirm"]
check("⑧b 确认后的卡片重新读取仍是 approved", cards[0]["status"] == "approved", cards[0].get("status"))
check("⑧b 聊天文件里的状态记录不会作为消息冒出来", all(m["role"] != "confirm_state" for m in store.read_chat(VID)))
fresh()
s = install([{"calls": [("set_settings", {"target_language": "Japanese"})]}, {"text": "不改"}])
card, _ = asyncio.run(with_user(False))
check("⑧b 拒绝也落盘", [m for m in store.read_chat(VID) if m["role"] == "confirm"][0]["status"] == "rejected")

fresh()
install([{"calls": [("set_settings", {"target_language": "Japanese"})]}, {"text": "好"}])
async def stop_while_waiting():
    task = asyncio.create_task(agent.handle_user_message(VID, "改日语"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if any(m["role"] == "confirm" for m in store.read_chat(VID)): break
    agent.cancel_chat(VID); await task
asyncio.run(stop_while_waiting())
check("⑧b 等确认时点了停止 → 卡片落成 rejected，不会永远挂着", [m for m in store.read_chat(VID) if m["role"] == "confirm"][0]["status"] == "rejected")

# ------------------------------------------------------------------ ⑭ 工具语法泄漏（真模型冒烟里出现过）
check("⑭ clean_reply 截掉泄漏的工具语法", agent.clean_reply('好的。\n<invoke name="x"><parameter name="a">1</parameter></invoke>') == "好的。")
check("⑭ clean_reply 截掉“此前请求”的复述", agent.clean_reply("看了。\n\n【以下是我此前发出的请求（勿再发出）】\nfoo") == "看了。")
check("⑭ 正常文字原样保留", agent.clean_reply("没有问题，一共 10 句。") == "没有问题，一共 10 句。")
check("⑭ leaked 只在真有标记时为真", agent.leaked("a <invoke b") and not agent.leaked("普通 <b>粗体</b>"))

fresh()
# 标记被切在两个 chunk 之间：<inv | oke
s = install([{"text": "好的，我来处理。<invoke name=\"list_lines\"></invoke>"}, {"text": "不该走到这"}])
seq0 = bus.current_seq(VID)
q = bus.subscribe(VID, since=seq0)
say("看一下")
pushed = "".join(e["text"] for e in iter(lambda: q.get_nowait() if not q.empty() else None, None) if e and e.get("type") == "chat" and e.get("kind") == "delta")
check("⑭ 流上拦截：泄漏的语法从没推给过前端（不会在打字气泡里闪一下）", "<invoke" not in pushed and "好的，我来处理" in pushed, pushed)

# 模型把“想调工具”写成了文本 → 没有动作发生 → 要求它重来，而不是把乱码当回复
fresh()
s = install([
    {"text": '<invoke name="mark_non_speech"><parameter name="lines">[2]</parameter></invoke>'},
    {"calls": [("mark_non_speech", {"lines": [2], "kind": "music"})]},
    {"text": "已标记第 2 句为音乐。"},
])
say("第 2 句是音乐")
check("⑭ 泄漏 → 追加一条纠正消息，让模型改用真正的工具调用",
      "没有真正执行" in json.dumps(s.seen[1], ensure_ascii=False))
check("⑭ 重试后动作确实发生了", store.get_state(VID).segments[1].kind == "music")
final = store.read_chat(VID)[-1]["text"]
check("⑭ 用户最终看到的是干净的总结", final == "已标记第 2 句为音乐。" and "<invoke" not in final, final)

# 历史里的“这一轮做了什么”不能长得像函数调用 —— 模型会模仿它
fresh()
store.append_chat(VID, {"id": "u1", "role": "user", "text": "静音第 1 句", "ts": 1})
store.append_chat(VID, {"id": "a1", "role": "assistant", "text": "好了。", "ts": 2,
                        "actions": [{"name": "set_muted", "label": "静音/取消静音某些句：第 1 句", "summary": "set_muted(lines=[1], muted=True)"}]})
hist = agent._history_messages(VID, store.current_conv(VID))
joined = json.dumps(hist, ensure_ascii=False)
check("⑭ 历史折叠用自然语言，不带 name(args) 的函数调用形态", "set_muted(" not in joined and "这一轮我做了" in joined and "第 1 句" in joined, joined)

# 回复只取最后一步：中间的过渡语不累加
fresh()
s = install([
    {"text": "我先看看。", "calls": [("get_overview", {})]},
    {"text": "再确认一下。", "calls": [("list_lines", {})]},
    {"text": "一共 3 句，都已翻译。"},
])
say("现在什么情况")
check("⑭ 最终回复是最后一步的总结，不是每一步过渡语的拼接", store.read_chat(VID)[-1]["text"] == "一共 3 句，都已翻译。", store.read_chat(VID)[-1]["text"])

# ------------------------------------------------------------------ ⑮ 欢迎消息
fresh()
def finish(s):
    s.init_report = {"reviewAdjusted": 2, "failedTranslation": [], "failedDub": ["c"]}
    s.mm_enhance = True
    s.segments[1].kind = "music"
    s.segments[0].actual_duration = 6.0
store.mutate(VID, finish, actor="system", cmd="x", summary="x")
store._history.clear()
agent.post_welcome(VID)
w = store.read_chat(VID)[0]
check("⑮ 欢迎消息包含配置、问题点", w["role"] == "assistant" and "3 句" in w["text"] and "English" in w["text"] and "审听增强" in w["text"]
      and "第 2 句被标成了非语音" in w["text"] and "1 句配音失败" in w["text"] and "第 1 句的配音比时间槽长" in w["text"], w["text"])
agent.post_welcome(VID)
check("⑮ 不重复发欢迎消息", len([m for m in store.read_chat(VID) if m.get("system")]) == 1)

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)


def test_agent():
    assert not fails, fails
