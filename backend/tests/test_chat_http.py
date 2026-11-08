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
check("② 新建的空会话可枚举（能选回来）", any(x["id"] == new_conv and x["count"] == 0 for x in lst["conversations"]), lst)
check("② GET /chat?conv=<空会话> 200 且消息为空",
      c.get(f"{p}/chat", params={"conv": new_conv}).status_code == 200
      and c.get(f"{p}/chat", params={"conv": new_conv}).json()["messages"] == [])
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

c.close()
print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
