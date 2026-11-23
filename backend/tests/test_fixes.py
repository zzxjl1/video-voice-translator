"""
这一批修复的回归测试：鉴权与归属、浏览器配音（上传指纹 / 并发 / 模式校验）、克隆失败跳过、
设置校验、命令的模式守卫。

HEAD 上这几块一条测试都没有（`browser|auth|clone_speakers` 在 tests 里零命中），所以这里补齐。
外部服务（DashScope / Google）全部用桩；ffmpeg 是真的。
"""
import asyncio, os, subprocess, sys, tempfile
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from fastapi.testclient import TestClient
from app import jobs, status, store
from app.main import app
from app.commands import registry as R
from app.models import Segment, Speaker, tts_audio_path
from app.services import token_service, voice_clone_service as vc
from _auth import login, own

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

def tone(seconds: float, rate=22050) -> bytes:
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=f=330:d={seconds}:r={rate}", "-ac", "1", "-f", "wav", "-"],
                       check=True, capture_output=True)
    return p.stdout

src = os.path.join(TMP, "v.mp4"); open(src, "wb").write(b"\0")
VID = "fixes00000000000000000000000000"

def fresh(mode="browser", lang="English"):
    if store.exists(VID): store.delete(VID)
    store.create(VID, "v.mp4", src)
    def seed(s):
        s.target_language = lang; s.dubbing_mode = mode; s.stage = "ready"
        s.browser_voice = config.browser_voice_for_language(lang) if mode == "browser" else None
        s.speakers = [Speaker("0", "S0", voice="Emily_v3.1", browser_voice=config.browser_voice_for_language(lang)),
                      Speaker("1", "S1", voice="Brian_v3.1")]
        s.segments = [Segment("a", "0", 0.0, 4.0, "你好", translated_text="Hello there"),
                      Segment("b", "1", 4.0, 5.0, "好", translated_text="[laughing]"),       # 只有语气标签
                      Segment("c", "0", 6.0, 8.0, "再见", translated_text="Goodbye")]
        for g in s.segments: g.tr_fp = status.expected_tr_fp(s, g)
    store.mutate(VID, seed, actor="system", cmd="seed", summary="seed")
    return store.get_state(VID)

def run(name, args=None, actor="user"):
    return asyncio.run(R.execute(VID, name, args or {}, actor=actor))

# ====================================================================== ① 鉴权与归属
c = TestClient(app)
fresh()
check("① 未登录读工程 → 401", c.get(f"/api/projects/{VID}").status_code == 401)
user_a = login(c, "a@example.com")
check("① 登录但工程不是自己的 → 404（不是 403，防枚举）", c.get(f"/api/projects/{VID}").status_code == 404)
own(VID, user_a)
check("① 自己的工程 → 200", c.get(f"/api/projects/{VID}").status_code == 200)
cb = TestClient(app); login(cb, "b@example.com")
check("① 别人登录后访问我的工程 → 404", cb.get(f"/api/projects/{VID}").status_code == 404)

tok, _ = token_service.issue_token(VID, ttl=600)
anon = TestClient(app)
check("① 令牌能读 /audio（外部 ASR 取音频用）", anon.get(f"/api/projects/{VID}/audio?token={tok}").status_code != 401)
check("① 令牌能读 /audio/original", anon.get(f"/api/projects/{VID}/audio/original?token={tok}").status_code != 401)
check("① 令牌不能读工程本身", anon.get(f"/api/projects/{VID}?token={tok}").status_code == 401)
check("① 令牌不能读别的路径，即使段 id 恰好叫 audio（路由精确匹配，不是 URL 后缀）",
      anon.get(f"/api/projects/{VID}/tts/audio?token={tok}").status_code == 401
      and anon.get(f"/api/projects/{VID}/voice-preview/audio?token={tok}").status_code == 401)
tok_other, _ = token_service.issue_token("someoneelse" + "0" * 21, ttl=600)
check("① 别的工程的令牌读不了这个工程", anon.get(f"/api/projects/{VID}/audio?token={tok_other}").status_code == 401)
check("① 令牌不能用来写（POST）", anon.post(f"/api/projects/{VID}/commands/undo?token={tok}", json={"args": {}}).status_code == 401)

# ====================================================================== ② 上传：先登记 owner，失败不留孤儿
from app.auth import users as auth_users
real_assign = auth_users.assign_project
calls = {"released": 0}
real_release = auth_users.release_project
def counting_release(pid, uid): calls["released"] += 1; return real_release(pid, uid)
auth_users.release_project = counting_release
import app.routers.projects as P
real_create = P.store.create
def boom_create(*a, **k): raise OSError("disk full")
P.store.create = boom_create
up = TestClient(app, raise_server_exceptions=False); login(up, "up@example.com")
mp4 = os.path.join(TMP, "up.mp4"); open(mp4, "wb").write(b"unique-bytes-for-md5-" + os.urandom(16))
r = up.post("/api/projects", files={"file": ("up.mp4", open(mp4, "rb"), "video/mp4")})
P.store.create = real_create
check("② 建工程失败 → 500，且不泄露异常文本", r.status_code == 500 and "disk full" not in r.text, r.text[:100])
check("② 建工程失败 → 回滚 owner 登记（否则再传同一文件永远 409）", calls["released"] == 1, calls)
r = up.post("/api/projects", files={"file": ("up.mp4", open(mp4, "rb"), "video/mp4")})
check("② 再传同一个文件 → 成功", r.status_code == 200, r.text[:100])
auth_users.release_project = real_release

# ====================================================================== ③ 浏览器配音上传
fresh("browser")
c = TestClient(app); login(c, "a@example.com"); own(VID)
voice = config.browser_voice_for_language("English")
def upload(seg, wav, text, v):
    return c.post(f"/api/projects/{VID}/browser-tts/{seg}", files={"audio": ("x.wav", wav, "audio/wav")}, data={"text": text, "voice": v})

r = upload("a", tone(1.5), "Hello there", voice)
st = store.get_state(VID); sa = st.segment("a")
check("③ 上传：收下，写指纹、时长", r.status_code == 200 and sa.tts_fp and abs(sa.actual_duration - 1.5) < 0.1, (r.status_code, r.text[:100]))
check("③ 上传后这句是新鲜的", status.tts_status(st, sa) == status.FRESH)
check("③ 没有残留的临时文件", not [f for d, _, fs in os.walk(os.path.join(TMP, VID)) for f in fs if f.startswith(".browser-tts") or (f.startswith(".") and f.endswith(".mp3"))])

# 合成期间译文被改 → 按旧译文合成的音频不能被标成新鲜
run("set_translation", {"line": 3, "text": "See you later"})
r = upload("c", tone(1.0), "Goodbye", voice)
sc = store.get_state(VID).segment("c")
check("③ 合成期间译文被改 → 409 拒收", r.status_code == 409, (r.status_code, r.text[:80]))
check("③ 被拒的音频不落盘、不写指纹", not os.path.exists(tts_audio_path(VID, "c")) and not sc.tts_fp)
r = upload("c", tone(1.0), "See you later", voice)
check("③ 按新译文重传 → 收下", r.status_code == 200, r.text[:80])

check("③ 合成时用的音色和现在不一致 → 409", upload("a", tone(1.0), "Hello there", "zh_CN-huayan-medium").status_code == 409)
check("③ 缺少 text / voice 字段 → 422", c.post(f"/api/projects/{VID}/browser-tts/a", files={"audio": ("x.wav", tone(1.0), "audio/wav")}).status_code == 422)
check("③ 空音频 → 413", upload("a", b"", "Hello there", voice).status_code == 413)

# 时间槽适配：Piper 自然语速比槽长时，先压进混音器能接受的范围，不然句尾会被截掉
fresh("browser"); c = TestClient(app); login(c, "a@example.com"); own(VID)
r = upload("b", tone(1.0), "[laughing]", voice)      # b 是 1 秒槽、这里 1 秒：不该变速
check("③ 刚好在槽内的不变速", r.status_code == 200 and abs(r.json()["duration"] - 1.0) < 0.1, r.text[:80])
r = upload("a", tone(5.0), "Hello there", voice)      # a 是 4 秒槽，5 秒 → 应压到 ≈4 秒
d = r.json().get("duration", 0) if r.status_code == 200 else 0
check("③ 比时间槽长的：用 atempo 压进槽（保持音高）", r.status_code == 200 and 3.8 < d <= 4.1, (r.status_code, d))
r = upload("a", tone(8.0), "Hello there", voice)      # 8 秒/4 秒槽 = 2 倍，超过混音器上限
from app.services import mix_service
d = r.json().get("duration", 0) if r.status_code == 200 else 0
check("③ 超过混音器上限的：只压到上限，剩下的由混音器裁剪（不无限加速）", r.status_code == 200 and abs(d - 8.0 / mix_service.RATE_MAX) < 0.2, d)

# 并发传同一句：不互相覆盖临时文件
import concurrent.futures as cf
fresh("browser"); c = TestClient(app); login(c, "a@example.com"); own(VID)
with cf.ThreadPoolExecutor(4) as ex:
    rs = list(ex.map(lambda _: upload("a", tone(1.2), "Hello there", voice).status_code, range(4)))
st = store.get_state(VID)
check("③ 并发上传同一句：都成功，最终文件完整、状态新鲜", all(x == 200 for x in rs) and status.tts_status(st, st.segment("a")) == status.FRESH, rs)

# 浏览器模式之外不收
fresh("remote"); c = TestClient(app); login(c, "a@example.com"); own(VID)
check("③ 不是浏览器配音模式 → 409", upload("a", tone(1.0), "Hello there", voice).status_code == 409)

# ====================================================================== ④ 设置校验
fresh("browser", "English")
r = run("set_settings", {"target_language": "Japanese"})
check("④ 浏览器配音下只改目标语言到没有本地音色的语言 → 拒绝（否则前端合成必然失败、把人锁在初始化页）",
      not r["ok"] and "Piper" in r["error"], r)
check("④ 被拒后工程原样（语言没变、音色没被清）", store.get_state(VID).target_language == "English" and store.get_state(VID).speaker("0").browser_voice)
r = run("set_settings", {"target_language": "French"})
check("④ 切到有本地音色的语言 → 通过", r["ok"] and store.get_state(VID).browser_voice == config.browser_voice_for_language("French"), r)
r = run("set_settings", {"dubbing_mode": "remote", "target_language": "Japanese"})
check("④ 同时切到 remote + 日语 → 通过（remote 不需要本地音色）", r["ok"], r)
r = run("set_settings", {"dubbing_mode": "browser"})
check("④ 日语下再切 browser → 拒绝", not r["ok"])
fresh("browser", "English")
r = run("set_settings", {"browser_voice": "zh_CN-huayan-medium"})
check("④ 别的语言的音色写进设置 → 拒绝", not r["ok"] and "不是" in r["error"], r)
r = run("assign_browser_voices", {"voices": ["zh_CN-huayan-medium"]})
check("④ assign_browser_voices 带别的语言的音色 → 拒绝", not r["ok"], r)
r = run("set_line_browser_voice", {"lines": [1], "voice": "zh_CN-huayan-medium"})
check("④ set_line_browser_voice 带别的语言的音色 → 拒绝", not r["ok"], r)
r = run("set_line_browser_voice", {"lines": [1], "voice": "en_US-amy-medium"})
check("④ 同语言的音色 → 通过", r["ok"], r)
c = TestClient(app); login(c, "a@example.com"); own(VID)
r = c.post(f"/api/projects/{VID}/setup", json={"target_language": "English", "dubbing_mode": "browser", "browser_voice": "zh_CN-huayan-medium"})
check("④ setup 带别的语言的音色 → 400", r.status_code == 400, r.status_code)

# ====================================================================== ⑤ 命令的模式守卫
fresh("browser")
for name, args in (("clone_voice", {"speaker": "0"}), ("generate_voice_candidates", {"speaker": "0"}),
                   ("preview_voice", {"speaker": "0"}), ("preview_voice_candidates", {"speaker": "0"}),
                   ("use_voice_candidate", {"speaker": "0", "candidate": "x"}), ("delete_voice_candidates", {"speaker": "0"})):
    r = run(name, args)
    check(f"⑤ 浏览器配音下 {name} 被拒（否则会调 DashScope 花钱）", not r["ok"] and "remote" in r["error"], r)
r = run("set_dub_style", {"lines": [1], "rate": 1.2})
check("⑤ 浏览器配音下 set_dub_style 被拒并说清（语速 / 语气对本地 Piper 不生效，指引也不再自相矛盾）", not r["ok"] and "Piper" in r["error"], r)
fresh("off")
check("⑤ 仅字幕下 set_dub_style 被拒", not run("set_dub_style", {"lines": [1], "rate": 1.2})["ok"])

# ====================================================================== ⑥ 克隆失败 → 跳过这个声音
fresh("remote")
def seed_clone(s):
    s.enable_voice_clone = True
    # 说话人 0 素材够；说话人 1 只有 1 秒（< 3 秒中句，连读也只有一句）
    s.segments = [Segment("a", "0", 0.0, 5.0, "x", translated_text="t"), Segment("b", "0", 6.0, 11.0, "x", translated_text="t"),
                  Segment("c", "0", 12.0, 17.0, "x", translated_text="t"), Segment("d", "1", 30.0, 30.5, "x", translated_text="t")]
store.mutate(VID, seed_clone, actor="system", cmd="seed", summary="seed")
os.makedirs(os.path.join(TMP, VID), exist_ok=True)
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=220:d=40:r=44100", "-ac", "1", os.path.join(TMP, VID, "vocals.wav")], check=True)

class FlakyEnroll:
    n = 0; fail_for = set()
    def create_voice(self, target_model, prefix, url, **k):
        FlakyEnroll.n += 1
        if "spk0" in prefix or prefix.endswith("0") and False: pass
        if FlakyEnroll.fail_for and FlakyEnroll.n in FlakyEnroll.fail_for: raise RuntimeError("DashScope 503")
        return f"voice-{FlakyEnroll.n}"
    def query_voice(self, voice_id): return {"status": "OK"}
vc.VoiceEnrollmentService = FlakyEnroll
config.CLONE_POLL_INTERVAL_S = 0
events = []
orig_emit = jobs._emit
jobs._emit = lambda vid, job, e: events.append(e) or orig_emit(vid, job, e)

async def go(): await jobs.clone_speakers(VID, jobs.Job(id="j", video_id=VID, kind="init", label="t", actor="system"), "http://t")

FlakyEnroll.n = 0; FlakyEnroll.fail_for = {1, 2, 3}          # 说话人 0 的 3 次注册全失败
events.clear()
try:
    asyncio.run(go()); raised = False
except Exception:
    raised = True
st = store.get_state(VID)
check("⑥ 注册重试 3 次仍失败 → 不抛错、不中止初始化", not raised)
check("⑥ 这个说话人没有克隆音色（之后用系统音色）", st.speaker("0").clone is None)
sk = [e for e in events if e.get("status") == "skipped" and e.get("speaker_id") == "0"]
check("⑥ 发出 skipped 事件，原因 enroll_failed，带错误文本", len(sk) == 1 and sk[0].get("reason") == "enroll_failed" and "503" in sk[0].get("error", ""), sk)
check("⑥ 重试了 3 次", FlakyEnroll.n >= 3, FlakyEnroll.n)
check("⑥ 其余说话人照常处理（说话人 1 素材不足 → 也是跳过，不拖累别人）",
      any(e.get("status") == "skipped" and e.get("speaker_id") == "1" for e in events) or st.speaker("1").clone is not None, [e for e in events if e.get("speaker_id") == "1"])
check("⑥ 跳过后 ensure_speaker_voices 给他系统音色，配音能继续", (jobs.ensure_speaker_voices(VID), store.get_state(VID).speaker("0").voice)[1])
check("⑥ 没有任何一个事件带 fatal（不再中止）", not any(e.get("fatal") for e in events), [e for e in events if e.get("fatal")])

FlakyEnroll.n = 0; FlakyEnroll.fail_for = {1}                 # 第 1 次失败、第 2 次成功：重试有效
events.clear(); asyncio.run(go())
st = store.get_state(VID)
check("⑥ 第一次失败、重试成功 → 拿到克隆音色", st.speaker("0").clone and st.speaker("0").clone["voice_id"].startswith("voice-"), st.speaker("0").clone)
check("⑥ 重试过程有 retrying 事件", any(e.get("status") == "retrying" for e in events))
jobs._emit = orig_emit

check("⑥ 素材不足是类型化异常，不靠中文子串判断", issubclass(vc.NoCloneMaterial, RuntimeError))
def only_blips(st):
    st.segments = [Segment("z", "1", 30.0, 30.2, "x", translated_text="t")]      # 0.2 秒：连"够长的语音"都算不上
store.mutate(VID, only_blips, actor="system", cmd="seed", summary="blips")
try:
    asyncio.run(vc.pick_plan(VID, store.get_state(VID).segments, "1", None)); ok = False
except vc.NoCloneMaterial:
    ok = True
except Exception:
    ok = False
check("⑥ pick_plan 没素材 → 抛 NoCloneMaterial", ok)

# ====================================================================== ⑦ 智能挑选的音频地址带令牌
from app.commands import voices as V
url = V._audio_url(VID)
check("⑦ omni 取音频的地址带签名令牌（没有它会 401，智能挑选必失败）", "?token=" in url and url.split("?token=")[1], url)
check("⑦ 令牌绑定这个工程且有效", token_service.verify_token(url.split("?token=")[1], VID))

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)


def test_fixes():
    assert not fails, fails
