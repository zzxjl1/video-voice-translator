"""
会话相关 HTTP 端点：纯读不写盘、空会话可选回、running 按会话、POST /chat 锁定 conv。
之前这类端点只有 store 层测试，路由上的 404 / 参数传递都没覆盖。
"""
import os, sys, tempfile, json
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from fastapi.testclient import TestClient
from app import agent, store
from app.main import app
from app.models import Segment

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

VID = "chathttp00000000000000000000000"
f = os.path.join(TMP, "v.mp4"); open(f, "wb").write(b"\0")
store.create(VID, "v.mp4", f)
p = f"/api/projects/{VID}"
c = TestClient(app)
from _auth import login, own
login(c); own(VID)

# ------------------------------------------------------------------ ① 纯读不写盘
r0 = store.require_state(VID).rev
r = c.get(f"{p}/chat")
check("① GET /chat 200 且带 conv", r.status_code == 200 and bool(r.json()["conv"]), r.json())
r = c.get(f"{p}/chats")
check("① GET /chats 200 且当前会话在列表里", r.status_code == 200
      and any(x["id"] == r.json()["current"] for x in r.json()["conversations"]), r.json())
check("① 两个纯读 GET 都没有改 rev", store.require_state(VID).rev == r0, store.require_state(VID).rev)

# ------------------------------------------------------------------ ② 新对话 / 切换 / 空会话
before = c.get(f"{p}/chats").json()["current"]
r = c.post(f"{p}/chats")
check("② POST /chats 返回新会话 id", r.status_code == 200 and r.json()["conv"] != before, r.json())
new_conv = r.json()["conv"]
lst = c.get(f"{p}/chats").json()
check("② 新建会话可枚举（能选回来）", any(x["id"] == new_conv for x in lst["conversations"]), lst)
r = c.get(f"{p}/chat", params={"conv": new_conv})
check("② GET /chat?conv=<新会话> 200，且带一条默认消息",
      r.status_code == 200 and len(r.json()["messages"]) == 1
      and "初始化" in r.json()["messages"][0].get("text", ""), r.json()["messages"])
check("② 切回旧会话成功", c.post(f"{p}/chats/select", json={"conv": before}).status_code == 200)
check("② 切回后 current 正确", c.get(f"{p}/chats").json()["current"] == before)
check("② 不存在的会话 404", c.post(f"{p}/chats/select", json={"conv": "nope"}).status_code == 404)
check("② GET /chat?conv=不存在 404", c.get(f"{p}/chat", params={"conv": "nope"}).status_code == 404)

# ------------------------------------------------------------------ ③ 会话切换的 rev 连续性
r0 = store.require_state(VID).rev
c.post(f"{p}/chats")
check("③ 切会话不推进 rev", store.require_state(VID).rev == r0, (r0, store.require_state(VID).rev))
store.mutate(VID, lambda s: s.segments.append(Segment("a", "0", 0, 1, "x")), actor="user", cmd="edit", summary="改了一句")
rows = [json.loads(l) for l in open(os.path.join(TMP, VID, "events.jsonl")) if l.strip()]
conv_events = [e for e in rows if e.get("type") == "conversation"]
change_revs = [e["rev"] for e in rows if e.get("type") == "change"]
check("③ 会话切换发了 conversation 事件并落盘", bool(conv_events))
check("③ 下一条 change 的 rev 连续（客户端不会 resync）", change_revs and change_revs[-1] == r0 + 1, change_revs)

# ------------------------------------------------------------------ ④ running 按会话
chat = agent.chat_for(VID)
chat.running, chat.conv = True, new_conv
check("④ 请求别的会话时 running=false", c.get(f"{p}/chat", params={"conv": before}).json()["running"] is False)
check("④ 请求该会话时 running=true", c.get(f"{p}/chat", params={"conv": new_conv}).json()["running"] is True)
chat.running, chat.conv = False, ""

# ------------------------------------------------------------------ ⑥ 新建会话带默认消息 / 删除会话
r = c.post(f"{p}/chats")
check("⑥ POST /chats 200", r.status_code == 200, r.text)
new_conv = r.json()["conv"]
welcome = c.get(f"{p}/chat", params={"conv": new_conv}).json()["messages"]
check("⑥ 新会话里有一条初始化完成后的默认消息",
      len(welcome) == 1 and "初始化" in welcome[0].get("text", ""), welcome)
r = c.post(f"{p}/chats")
check("⑥ 连续新建两个会话都各自带默认消息",
      len(c.get(f"{p}/chat", params={"conv": r.json()["conv"]}).json()["messages"]) == 1)
to_delete = r.json()["conv"]
r = c.delete(f"{p}/chats/{to_delete}")
check("⑥ DELETE /chats/{conv} 200 且返回新的当前会话", r.status_code == 200 and r.json()["conv"], r.text)
check("⑥ 删掉的会话不在列表里", to_delete not in [x["id"] for x in c.get(f"{p}/chats").json()["conversations"]])
check("⑥ 删掉的会话 GET /chat?conv= 返回 404", c.get(f"{p}/chat", params={"conv": to_delete}).status_code == 404)
check("⑥ 重复删除 404", c.delete(f"{p}/chats/{to_delete}").status_code == 404)

# ------------------------------------------------------------------ ⑤ POST /chat 锁定 conv
seen = []
async def fake_handle(video_id, text, conv=None):
    seen.append((text, conv))
original = agent.handle_user_message
agent.handle_user_message = fake_handle
try:
    r = c.post(f"{p}/chat", json={"text": "锁定会话测试"})
    check("⑤ POST /chat 200", r.status_code == 200, r.text)
    # TestClient 里 create_task 在该请求结束后可能尚未调度，等一下
    import time
    for _ in range(20):
        if seen: break
        time.sleep(0.02)
    check("⑤ 传给 agent 的 conv 是请求进入时的当前会话",
          seen and seen[0][1] == c.get(f"{p}/chats").json()["current"], (seen, c.get(f"{p}/chats").json()["current"]))
finally:
    agent.handle_user_message = original

# ------------------------------------------------------------------ ⑦ 停止/确认必须在事件循环线程里执行
# cancel_chat / resolve_confirmation 会 set asyncio.Event / Future。从线程池里 set 不会唤醒
# 正在等待的协程（要拖到下一个事件才醒），所以这两个端点必须是 async def。
# 这里不看"端点是不是 async"这个形式，而是直接量：被调用时在哪个线程。
import asyncio, threading
import httpx

async def where_do_they_run():
    loop_thread = threading.get_ident()
    seen = {}
    orig_cancel, orig_resolve = agent.cancel_chat, agent.resolve_confirmation
    agent.cancel_chat = lambda vid: seen.__setitem__("stop", threading.get_ident())
    agent.resolve_confirmation = lambda vid, cid, ok: seen.__setitem__("confirm", threading.get_ident()) or True
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", cookies=dict(c.cookies)) as ac:
            r1 = await ac.post(f"{p}/chat/stop")
            r2 = await ac.post(f"{p}/chat/confirm", json={"id": "x", "approve": True})
        return loop_thread, seen, (r1.status_code, r2.status_code)
    finally:
        agent.cancel_chat, agent.resolve_confirmation = orig_cancel, orig_resolve

loop_thread, ran_in, codes = asyncio.run(where_do_they_run())
check("⑦ 两个端点都返回 200", codes == (200, 200), codes)
check("⑦ POST /chat/stop 在事件循环线程里执行 cancel_chat", ran_in.get("stop") == loop_thread, (ran_in, loop_thread))
check("⑦ POST /chat/confirm 在事件循环线程里执行 resolve_confirmation", ran_in.get("confirm") == loop_thread, (ran_in, loop_thread))

c.close()
print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
