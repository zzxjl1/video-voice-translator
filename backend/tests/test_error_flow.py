"""
上一波"翻译全挂却报成功"的故障链：任务失败要能传到命令的返回值、初始化失败的
报告要写回工程、欢迎消息要说明缺什么。全部用注入的假 LLM，不碰网络。
"""
import asyncio, os, sys, tempfile
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

import httpx
from openai import APIConnectionError, AuthenticationError

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from app import agent, jobs, store
from app.commands import registry as R
from app.models import Segment, Speaker

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

VID = "errflow000000000000000000000000"
src = os.path.join(TMP, "v.mp4"); open(src, "wb").write(b"\0")

def fresh():
    if store.exists(VID): store.delete(VID)
    store.create(VID, "v.mp4", src)
    def seed(s):
        s.target_language = "English"
        s.enable_dubbing = True
        s.speakers = [Speaker("0", "Speaker 0")]
        s.segments = [Segment("a", "0", 0.0, 4.0, "你好世界")]
        s.stage = "ready"
    store.mutate(VID, seed, actor="system", cmd="seed", summary="seed")

def run(name, args=None, actor="ai"):
    return asyncio.run(R.execute(VID, name, args or {}, actor=actor))

def req():
    return httpx.Request("POST", "https://api.example.com/chat")

# ------------------------------------------------------------------ ① 任务失败 → 命令返回 ok:false
fresh()
import app.services.llm_service as L
async def dead_net(*a, **kw):
    raise APIConnectionError(request=req())
L._chat = dead_net
ORIGINAL_RETRY = config.LLM_RETRY_SECONDS
ORIGINAL_CHAT = L._chat
config.LLM_RETRY_SECONDS = 0.01

r = run("retranslate", {})
check("① LLM 连不上 → retranslate 返回 ok:false（此前会把全军覆没报成 failed=0）",
      r["ok"] is False and "翻译任务失败" in (r.get("error") or ""), r)

r = run("redub", {})
check("① redub 在无译文时空跑不算失败（dub_lines 没活可干）", r["ok"] is True, r)

# ------------------------------------------------------------------ ② 鉴权错误：只发 1 次请求，报错指明方向
fresh()
auth_calls = {"n": 0}
async def no_key(*a, **kw):
    auth_calls["n"] += 1
    raise AuthenticationError(message="bad key", response=httpx.Response(401, request=req()), body=None)
L._chat = no_key
r = run("retranslate", {})
# 术语表 1 次 + 正文 1 次（正文在第一次 401 后立即停止，不再退避重试两次）
# 只有正文 1 次：1 行文本走缓存不抽术语表；永久性错误也不再走补翻轮。
check("② 401 只发 1 次请求（不重试、不补翻）", auth_calls["n"] == 1, auth_calls)
check("② 报错指明是鉴权问题", "鉴权" in (r.get("error") or ""), r.get("error"))

# ------------------------------------------------------------------ ③ 连接错误走重试，最终报错给出网络提示和端点
fresh()
conn_calls = {"n": 0}
async def flaky_net(*a, **kw):
    conn_calls["n"] += 1
    raise APIConnectionError(request=req())
L._chat = flaky_net
r = run("retranslate", {})
check("③ 连接错误被重试（正文 3 次 + 补翻轮）", conn_calls["n"] >= 4, conn_calls)
err = r.get("error") or ""
check("③ 最终报错说连接失败并带上端点", "连接失败" in err and config.LLM_BASE_URL in err, err)

# ------------------------------------------------------------------ ④ 初始化失败：欢迎消息说明缺什么、怎么补
fresh()
def fail_report(s):
    s.init_report = {"error": "翻译一条结果都没产出：连接失败 …", "lines": 1}
store.mutate(VID, fail_report, actor="system", cmd="init_report", summary="初始化未全部完成")
text = agent.welcome_text(VID)
check("④ 欢迎消息说明初始化没全部完成", "没有全部完成" in text, text)
check("④ 欢迎消息带上真实原因", "连接失败" in text, text)
check("④ 欢迎消息给出缺量与补齐路径", "缺 1 句译文" in text and "补齐" in text, text)

def ok_report(s):
    s.init_report = {"lines": 1}
store.mutate(VID, ok_report, actor="system", cmd="init_report", summary="初始化完成")
check("④ 正常完成时没有错误措辞", "没有全部完成" not in agent.welcome_text(VID))

# ------------------------------------------------------------------ ⑤ wrapper 不抛，job.error 记录原因
fresh()
async def boom(job):
    raise RuntimeError("炸了")
async def main():
    j = await jobs.run(VID, "translate", "翻译", "ai", boom)
    await j.task
    return j
j = asyncio.run(main())
check("⑤ wrapper 吞掉异常但 job.error 记下原因", j.error == "炸了" and j.task.exception() is None,
      (j.error, j.task.exception()))

# 跨 attempt 累积：第二次只补回缺的那句，前一次已拿到的行不能丢
fresh()
async def twice(prompt, **kw):
    twice.n += 1
    return ("[{\"id\": \"a\", \"translatedText\": \"A\"}]" if twice.n == 1
            else "[{\"id\": \"a\", \"translatedText\": \"A\"}, {\"id\": \"b\", \"translatedText\": \"B\"}]"), {}
twice.n = 0
L._chat = twice
import tempfile as _tf
sink = {}
res, missing = asyncio.run(L._translate_chunk([{"id": "a", "text": "x"}, {"id": "b", "text": "y"}], [], {}, "English",
                                              _tf.mkdtemp(), 7, error_sink=sink, log_name="测试"))
check("⑥ 跨 attempt 累积：补回缺的 id 时保留已有译文", res == {"a": "A", "b": "B"} and missing == set(), (res, missing))

# 还原被本脚本改过的全局（脚本式测试跑完进程就退出，但别给后续用例留坑）
config.LLM_RETRY_SECONDS = ORIGINAL_RETRY
L._chat = ORIGINAL_CHAT

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
