"""
AI 对话循环：omni 通过 tool call 读取并修改项目，UI 的改动也会被它看到。

一轮用户消息的流程
------------------
  1. 把"自你上次回复以来发生了什么"（用户在界面上的操作、任务结束、报错）拼成一条
     system 通知，放在用户消息之前 —— 这就是 UI → AI 的同步。
  2. 带上概况（很短）+ 对话历史 + tool 列表，流式请求 omni。
  3. 文字增量实时推给前端；tool call 攒齐后执行（同一个注册表 `commands.execute`，
     和 UI 按钮走的是同一条路径）；结果回填，进入下一步，直到模型不再调用工具。
  4. 需要用户确认的命令（花钱/推翻已有工作）：暂停，在聊天里出一张确认卡片；用户点
     「确认」才执行，点「取消」就把"用户拒绝了"作为结果回给模型。
  5. AI 的每次改动都发 `change` 事件（actor=ai），前端据此高亮被改的行 —— AI → UI。

关键约束（P0 实测过）
--------------------
  · qwen3.8-omni-flash 支持 stream + tools + 并行 tool call + include_usage。
  · tool 结果只能是文本；`listen` 的音频要作为**紧随其后的一条 user 消息**注入
    （实测可行）。
  · 流式 + 音频在上下文里 + tools 同时存在时，模型可能先调 `listen` 而不是直接动手 ——
    这是正确的行为，循环要允许多步。
  · 每个 tool_call 都必须有对应的 tool 回复（并行调用时漏一个下一次请求会被拒）。
  · 思考模式必须关（`enable_thinking: False`），否则每步多等一两分钟。
"""
import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

from openai import AsyncOpenAI

from app import config, store, view
from app.bus import bus
from app.commands import registry as R
from app.commands.read import overview_data
from app.services import usage_service

logger = logging.getLogger(__name__)

MAX_STEPS = 12                   # 一轮用户消息里最多几次"模型 → 工具 → 模型"
HISTORY_MESSAGES = 40            # 带进上下文的历史消息数（再多就靠 get_* 工具按需读）
CONFIRM_TIMEOUT_S = 600          # 用户多久不点确认就当拒绝
TOOL_RESULT_CAP = 6000           # 单个 tool 结果最多多少字符，防止撑爆上下文

_client = AsyncOpenAI(
    api_key=config.DASHSCOPE_API_KEY, base_url=config.OMNI_BASE_URL, timeout=300, max_retries=0,
)
_THINKING_OFF = {"enable_thinking": False}


SYSTEM_PROMPT = """你是这个视频配音项目的 AI 助手，和用户一起把一段视频翻译成目标语言并配音。
你能听原视频的声音、读写项目里的每一句、调整翻译和配音，用户在界面上看到的一切你都能看到也都能改。

【分寸 —— 最重要】
- 只做用户这一次要求的事。发现了别的问题（还有句子没配音、译文还是原文、有疑似听错的字），**汇报并问用户要不要处理，
  不要自己动手**。用户只是在提问、查看、闲聊时，只用只读工具（get_*/list_*/search_*/listen），绝不调用会改东西或花钱的工具。
- 用户明确说了范围就严格按范围：说“第 3 句”就只动第 3 句，不顺手“一起补齐”。
- 拿不准用户想要什么、或者有两种合理理解时，先问。

【怎么工作】
- 句子用"第 N 句"（行号）称呼，和用户界面上显示的编号一致。插入/拆分/合并/删除之后行号会变，改完要重新看。
- 动手前先看：get_overview 看全貌，list_lines / get_lines 看具体句子。不要凭空猜内容。
- 判断"是不是音乐 / 有没有两个人同时说 / ASR 听错了什么"要用 listen 听，不要只看文字。
- 翻译和配音是两步：改了原文或译文，配音会过期，要 redub；改了语气/语速，配音也过期。
- 配音时长要和时间槽（slot_s）大致相当：dub_vs_slot 大于 1.15 说明配音太长（会被加速或裁掉），
  小于 0.8 说明太短。优先让译文更简短或调语速，而不是硬塞。
- 语气用 set_dub_style 的 instruction，写成一句自然的指令，**语言必须和译文一致**：译文是英文就写英文指令
  （如 "Speak with excitement, slightly faster."），译文是中文才写中文。写之前先看一眼译文是什么语言。
  译文里也可以直接带情感标签，如 [excited]。
- 会花钱的操作（批量配音、克隆、重新识别）和推翻已有工作的操作（改目标语言）会先请用户确认，
  你只管调用，别自己再问一遍。
- 改错了可以 undo。用户说"撤销/改回去"就调用它。

【沟通】
- 始终用用户的语言回答（用户说中文，整段回复都是中文，包括开头的过渡语；不要夹英文客套话）。
- 需要调用工具时，直接调用，不要先写一句“我来看看”再调用 —— 做完所有事之后再给一段总结。
- 绝不要把工具调用写成文字（不要输出 <invoke>、<parameter>、函数名(参数) 这类东西），只用真正的工具调用。
- 简洁，先说你做了什么、结果如何，再说需要用户决定的事。
- 你每次改动之后，用一句话说明改了哪一句、改成了什么，不要长篇大论。
- 如果用户在界面上刚做了某个操作（见"自上次以来"的通知），可以顺着它的意思继续，不用复述。
- 不确定时问用户，不要猜。做不到的事（比如 ffmpeg 不可用、浏览器才能做的分离）直接说明原因。
"""


class Chat:
    """一个工程一个对话。用户消息串行处理；确认卡片在等用户时整个对话暂停。"""

    def __init__(self, video_id: str):
        self.video_id = video_id
        self.lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future] = {}
        self.cancel = asyncio.Event()
        self.running = False


_chats: dict[str, Chat] = {}


def chat_for(video_id: str) -> Chat:
    return _chats.setdefault(video_id, Chat(video_id))


def forget(video_id: str) -> None:
    chat = _chats.pop(video_id, None)
    if chat:
        chat.cancel.set()
        for fut in chat.pending.values():
            if not fut.done():
                fut.set_result(False)


# ----------------------------------------------------------------------------
# 消息持久化与 UI 事件
# ----------------------------------------------------------------------------

def _msg(video_id: str, role: str, **fields) -> dict:
    """落盘一条聊天记录，并作为 chat 事件推给前端。"""
    record = {"id": uuid.uuid4().hex[:10], "role": role, "ts": time.time(), **fields}
    store.append_chat(video_id, record)
    bus.publish(video_id, {"type": "chat", "kind": "message", "message": record})
    return record


def _delta(video_id: str, message_id: str, text: str) -> None:
    bus.publish(video_id, {"type": "chat", "kind": "delta", "id": message_id, "text": text})


# ----------------------------------------------------------------------------
# 上下文拼装
# ----------------------------------------------------------------------------

def _last_seen_seq(video_id: str) -> int:
    """AI 上一次回复结束时看到的事件序号（存在对话记录里）。"""
    for m in reversed(store.read_chat(video_id, 400)):
        if m.get("role") == "assistant" and m.get("seen_seq") is not None:
            return int(m["seen_seq"])
    return 0


def changes_digest(video_id: str, since: int) -> str:
    """自 `since` 以来用户/系统做了什么 —— 给 AI 的一段简短通知。AI 自己的改动不重复报。"""
    lines = []
    for e in store.events_since(video_id, since):
        if e["type"] == "change" and e.get("actor") != "ai":
            who = "用户在界面上" if e.get("actor") == "user" else "系统"
            lines.append(f"- {who}：{e['summary']}")
        elif e["type"] == "job" and e.get("actor") != "ai":
            phase = {"started": "开始", "done": "完成", "failed": "失败", "cancelled": "被取消"}.get(e.get("phase"), e.get("phase"))
            err = f"（{e['error']}）" if e.get("error") else ""
            lines.append(f"- 任务「{e.get('label')}」{phase}{err}")
    if not lines:
        return ""
    if len(lines) > 25:
        lines = lines[:5] + [f"- ……中间还有 {len(lines) - 10} 条……"] + lines[-5:]
    return "【自你上次回复以来发生的事】\n" + "\n".join(lines)


def _history_messages(video_id: str) -> list[dict]:
    """把落盘的聊天记录还原成 API 消息。tool 调用只保留最近一轮的细节，更早的折叠成文字。"""
    records = store.read_chat(video_id, 400)
    out: list[dict] = []
    for m in records:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m.get("text", "")})
        elif role == "assistant":
            text = m.get("text", "")
            actions = m.get("actions") or []
            if actions:
                # 自然语言，不带函数调用的形态：模型会模仿历史里的样子，写成 `name(args)`
                # 的话它就会在回复里也输出这种东西（真模型冒烟实测过）。
                done = "；".join(a["label"] for a in actions if a.get("label"))
                text = (text + "\n" if text else "") + f"（这一轮我做了：{done}）"
            if text:
                out.append({"role": "assistant", "content": text})
    return out[-HISTORY_MESSAGES:]


def _context_block(video_id: str) -> str:
    state = store.require_state(video_id)
    ov = overview_data(state)
    return "【项目概况（实时）】\n" + json.dumps(ov, ensure_ascii=False)


# ----------------------------------------------------------------------------
# 流式调用与 tool 执行
# ----------------------------------------------------------------------------

async def _stream_step(video_id: str, messages: list[dict], message_id: str) -> tuple[str, list[dict], dict]:
    """一次流式请求。返回 (文字, tool_calls[], usage)。文字增量实时推送。"""
    stream = await _client.chat.completions.create(
        model=config.OMNI_MODEL, messages=messages, tools=R.tool_specs(), tool_choice="auto",
        stream=True, stream_options={"include_usage": True}, extra_body=_THINKING_OFF,
    )
    text, calls, usage = "", {}, {}
    sent = 0                      # 已经推给前端的字符数
    async for chunk in stream:
        if chunk.usage:
            usage = chunk.usage.model_dump()
        if not chunk.choices:
            continue
        d = chunk.choices[0].delta
        if d.content:
            text += d.content
            # 标记可能被切在两个 chunk 之间，所以对"累计文本"判断，并只推到标记之前。
            # 一旦出现泄漏标记就停止转发：打字中的气泡里不该闪过 `<invoke ...>`。
            safe = text
            if leaked(text):
                safe = text[:min(text.find(m) for m in _LEAK_MARKERS if m in text)]
            if len(safe) > sent:
                _delta(video_id, message_id, safe[sent:])
                sent = len(safe)
        for tc in d.tool_calls or []:
            c = calls.setdefault(tc.index, {"id": None, "name": "", "args": ""})
            c["id"] = tc.id or c["id"]
            if tc.function:
                c["name"] += tc.function.name or ""
                c["args"] += tc.function.arguments or ""
    ordered = [calls[i] for i in sorted(calls)]
    for c in ordered:
        c["id"] = c["id"] or f"call_{uuid.uuid4().hex[:8]}"
    return text, ordered, usage


_LEAK_MARKERS = ("<invoke", "<parameter", "</invoke", "<function_calls", "<tool_call", "【以下是我此前")


def clean_reply(text: str) -> str:
    """
    去掉模型偶尔漏到文字里的工具调用语法，只留人话。

    真模型在多步循环里偶尔会把"想调工具"写成文本（`<invoke name=...>`），而不是走
    tool_calls 通道 —— 用户会直接看到乱码。从第一个标记起整段截掉：标记之后都是语法
    残片，没有给人看的内容。
    """
    cut = min((text.find(m) for m in _LEAK_MARKERS if m in text), default=-1)
    return (text[:cut] if cut >= 0 else text).rstrip()


def leaked(text: str) -> bool:
    return any(m in text for m in _LEAK_MARKERS)


def _cap(result: Any) -> str:
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > TOOL_RESULT_CAP:
        text = text[:TOOL_RESULT_CAP] + f'…（已截断，共 {len(text)} 字符；缩小范围再取）'
    return text


async def _confirm(chat: Chat, video_id: str, call: dict, args: dict, level: str) -> bool:
    """出一张确认卡片并等待用户。超时/取消按拒绝处理。"""
    cmd = R.get(call["name"])
    card_id = uuid.uuid4().hex[:10]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    chat.pending[card_id] = fut
    _msg(video_id, "confirm", id=card_id, command=call["name"], description=cmd.description,
         args=args, level=level, status="pending")
    try:
        return bool(await asyncio.wait_for(fut, timeout=CONFIRM_TIMEOUT_S))
    except asyncio.TimeoutError:
        _settle_card(video_id, card_id, False)          # 超时按拒绝处理，卡片不能永远停在"待确认"
        return False
    finally:
        chat.pending.pop(card_id, None)


def _settle_card(video_id: str, card_id: str, approved: bool) -> None:
    """卡片的最终状态：落盘（刷新后还在）并通知前端。超时 / 取消导致的拒绝也走这里。"""
    store.resolve_confirm_card(video_id, card_id, approved)
    bus.publish(video_id, {"type": "chat", "kind": "confirm_resolved", "id": card_id, "approved": approved})


def resolve_confirmation(video_id: str, card_id: str, approve: bool) -> bool:
    chat = _chats.get(video_id)
    fut = chat.pending.get(card_id) if chat else None
    if fut is None or fut.done():
        return False
    fut.set_result(approve)
    _settle_card(video_id, card_id, approve)
    return True


async def _run_tool(chat: Chat, video_id: str, call: dict) -> tuple[dict, Optional[str]]:
    """执行一个 tool call。返回 (给模型的结果, 待注入的音频 base64)。"""
    name = call["name"]
    try:
        args = json.loads(call["args"] or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments 必须是对象")
    except (json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "error": f"参数不是合法的 JSON 对象：{e}"}, None

    level = R.needs_confirmation(name, "ai")
    if level:
        approved = await _confirm(chat, video_id, call, args, level)
        if not approved:
            return {"ok": False, "error": "用户没有确认这个操作，已取消。不要重复请求，问问用户想怎么办。"}, None

    bus.publish(video_id, {"type": "chat", "kind": "tool_call", "id": call["id"], "name": name, "args": args})
    outcome = await R.execute(video_id, name, args, actor="ai", call_id=call["id"])

    audio = None
    if outcome.get("ok") and isinstance(outcome.get("result"), dict) and "_audio" in outcome["result"]:
        audio = outcome["result"].pop("_audio")
    bus.publish(video_id, {"type": "chat", "kind": "tool_result", "id": call["id"], "name": name,
                           "ok": bool(outcome.get("ok")),
                           "error": outcome.get("error") if not outcome.get("ok") else None})
    return outcome, audio


def _action_summary(name: str, args: dict, outcome: dict) -> Optional[dict]:
    """给聊天记录用的一句话：只记会改东西的命令。`label` 是给人和模型看的自然语言。"""
    cmd = R.get(name)
    if cmd is None or cmd.readonly or not outcome.get("ok"):
        return None
    lines = args.get("lines") or ([args["line"]] if args.get("line") else [])
    where = f"第 {'、'.join(str(n) for n in lines[:6])} 句" if lines else ""
    label = f"{cmd.description.split('。')[0].split('（')[0]}{('：' + where) if where else ''}"
    brief = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
    return {"name": name, "label": label[:80], "summary": f"{name}({brief})"[:140]}


async def handle_user_message(video_id: str, text: str) -> None:
    """处理一条用户消息。串行：上一条没处理完时新消息排队。"""
    chat = chat_for(video_id)
    async with chat.lock:
        chat.running = True
        chat.cancel.clear()
        try:
            await _respond(chat, video_id, text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{video_id}] chat failed: {e}", exc_info=True)
            _msg(video_id, "error", text=f"出错了：{e}")
        finally:
            chat.running = False
            bus.publish(video_id, {"type": "chat", "kind": "idle"})


async def _respond(chat: Chat, video_id: str, user_text: str) -> None:
    since = _last_seen_seq(video_id)
    digest = changes_digest(video_id, since)
    _msg(video_id, "user", text=user_text)

    system = SYSTEM_PROMPT + "\n" + _context_block(video_id)
    messages: list[dict] = [{"role": "system", "content": system}]
    messages += _history_messages(video_id)[:-1]          # 最后一条就是刚落盘的用户消息，下面单独拼
    final_user = user_text if not digest else f"{digest}\n\n【用户说】\n{user_text}"
    messages.append({"role": "user", "content": final_user})

    actions: list[dict] = []
    answer = ""
    message_id = uuid.uuid4().hex[:10]
    bus.publish(video_id, {"type": "chat", "kind": "start", "id": message_id})

    stopped = False
    for step in range(MAX_STEPS):
        if chat.cancel.is_set():
            stopped = True
            break
        text, calls, usage = await _stream_step(video_id, messages, message_id)
        if usage:
            usage_service.record(video_id=video_id, step="omni", model=config.OMNI_MODEL,
                                 detail="chat", usage=usage)
        # 回复只取"最后一步"的话：中间每一步前的"我先看看……"是过渡语，累加起来读着像碎碎念。
        # 例外：中间步骤里有实质内容（比如先给了结论再去做别的）会随最终那段一起被模型复述。
        answer = clean_reply(text)
        if leaked(text) and not calls:
            # 模型想调工具却写成了文本 → 没有动作发生。让它重来一次，而不是把乱码当回复。
            messages.append({"role": "assistant", "content": answer or "（已重试）"})
            messages.append({"role": "user", "content": "你上一条把工具调用写成了文字，没有真正执行。"
                                                         "请用工具调用的方式再来一次；如果其实不需要调用工具，直接用中文回答。"})
            continue
        if not calls:
            break

        messages.append({
            "role": "assistant", "content": text or "",
            "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                           for c in calls],
        })
        injected: list[str] = []
        for c in calls:                       # 逐个执行：后一个可能依赖前一个的改动（行号会变）
            if chat.cancel.is_set():
                # 取消后不再动手，但每个 tool_call 仍要有回复（协议要求，也让记录自洽）
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": _cap({"ok": False, "error": "用户已停止，这个操作没有执行"})})
                continue
            outcome, audio = await _run_tool(chat, video_id, c)
            try:
                args = json.loads(c["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            act = _action_summary(c["name"], args, outcome)
            if act:
                actions.append(act)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": _cap(outcome)})
            if audio:
                injected.append(audio)
        for audio in injected:
            # tool 结果只能是文本：音频作为紧随其后的 user 消息注入（P0 实测可行）
            messages.append({"role": "user", "content": [
                {"type": "text", "text": "[listen 的结果：这段原视频的声音]"},
                {"type": "input_audio", "input_audio": {"data": f"data:;base64,{audio}", "format": "wav"}},
            ]})
        if chat.cancel.is_set():
            stopped = True
            break
        # 概况随改动变化：每一步都刷新 system 里那份，模型看到的永远是最新的
        messages[0] = {"role": "system", "content": SYSTEM_PROMPT + "\n" + _context_block(video_id)}
    else:
        answer += "\n（这一轮做的步骤太多了，先停在这里；告诉我接着做什么。）"
    if stopped:
        answer = (answer.rstrip() + "\n" if answer.strip() else "") + "（已停止）"

    seen = store.current_seq(video_id)
    _msg(video_id, "assistant", id=message_id, text=answer.strip(), actions=actions, seen_seq=seen)


def cancel_chat(video_id: str) -> None:
    chat = _chats.get(video_id)
    if chat:
        chat.cancel.set()
        for card_id, fut in list(chat.pending.items()):
            if not fut.done():
                fut.set_result(False)
                _settle_card(video_id, card_id, False)


# ----------------------------------------------------------------------------
# 初始化完成后的第一条消息
# ----------------------------------------------------------------------------

def welcome_text(video_id: str) -> str:
    """
    初始化报告 → 第一条消息。**不调用模型**：这条消息是确定的事实，不该有措辞上的
    不确定；也省一次调用。后面的对话才交给 omni。
    """
    state = store.require_state(video_id)
    rep = state.init_report or {}
    s = view.settings_view(state)
    lang = s.get("targetLanguage") or "目标语言"
    error = rep.get("error")
    parts = [f"初始化{'没有全部完成' if error else '完成'}：共 {len(state.segments)} 句，{len(state.speakers)} 位说话人，翻译成 **{lang}**"
             + (f"（{s['accent']}）" if s.get("accent") else "") + "。"]
    if error:
        # 报告只在失败时才有 error；缺多少直接按当前状态数（失败中途的报告是半份）。
        from app import status as _status
        needs = _status.needs(state)
        parts.append(
            f"中途出错：{error}。已完成的部分都在，缺 {needs['translate']} 句译文"
            + (f"、{needs['dub']} 句配音" if state.enable_dubbing else "")
            + "。跟我说「补齐」就会只补缺的，不用重跑。"
        )
    mode = []
    mode.append("配音" if state.enable_dubbing else "仅字幕（不配音）")
    if state.enable_dubbing and state.enable_voice_clone:
        mode.append("声音克隆" + ("（智能选材）" if state.clone_smart_pick else ""))
    if state.separation_mode == "client":
        mode.append("人声分离")
    if state.mm_enhance:
        adj = rep.get("reviewAdjusted")
        mode.append("审听增强" + (f"（纠正/标注了 {adj} 句）" if adj else ""))
    parts.append("本次配置：" + "、".join(mode) + "。")

    flags = []
    non = [i + 1 for i, x in enumerate(state.segments) if x.kind != "speech"]
    if non:
        flags.append(f"第 {'、'.join(map(str, non[:8]))} 句被标成了非语音")
    ftr, fdub = rep.get("failedTranslation") or [], rep.get("failedDub") or []
    if ftr:
        flags.append(f"{len(ftr)} 句翻译失败")
    if fdub:
        flags.append(f"{len(fdub)} 句配音失败")
    over = [i + 1 for i, x in enumerate(state.segments)
            if x.actual_duration and x.actual_duration / max(0.01, x.end_time - x.start_time) > 1.25]
    if over:
        flags.append(f"第 {'、'.join(map(str, over[:8]))} 句的配音比时间槽长很多")
    if flags:
        parts.append("需要留意：" + "；".join(flags) + "。")
    parts.append("你可以直接告诉我哪里要改，比如「第 3 句翻译太生硬」「第 5 句语气再激动一点」，我会直接改并重新配音。")
    return "\n\n".join(parts)


def post_welcome(video_id: str) -> None:
    """初始化成功后写入第一条消息（已经有消息就不重复）。"""
    if store.read_chat(video_id, 1):
        return
    _msg(video_id, "assistant", text=welcome_text(video_id), actions=[], seen_seq=store.current_seq(video_id), system=True)
