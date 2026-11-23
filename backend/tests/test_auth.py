"""
OAuth state 的一次性消费、会话 cookie 续期、回调兜底、限流、清理循环、迁移脚本的账号歧义保护。
Google 不可达：token 交换和 ID token 校验用桩。
"""
import os, subprocess, sys, tempfile, threading
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")
os.environ["GOOGLE_CLIENT_ID"] = "cid"; os.environ["GOOGLE_CLIENT_SECRET"] = "secret"

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
config.AUTH_DB_PATH = os.path.join(TMP, "auth.sqlite3")
config.AUTH_ENABLED = True
config.SESSION_COOKIE_SECURE = False        # TestClient 走 http：Secure cookie 不会被带回来
config.GOOGLE_CLIENT_ID, config.GOOGLE_CLIENT_SECRET = "cid", "secret"
from fastapi.testclient import TestClient
from app.auth import db, google, sessions, states, users
from app.main import app

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

db.init_db()

# ====================================================================== ① state 一次性消费
def mk(state="S1", browser="B1"):
    states.create_state(state=state, browser_token=browser, nonce="n", code_verifier="v", redirect_uri="http://r", return_to="/")

mk()
check("① 浏览器 cookie 不匹配 → 拿不到，且 state 没被烧掉", states.consume_state("S1", "WRONG") is None and states.consume_state("S1", "B1") is not None)
check("① 同一个 state 第二次 → None（一次性）", states.consume_state("S1", "B1") is None)
check("① 没有浏览器 cookie → None", (mk("S2"), states.consume_state("S2", None))[1] is None)
check("① 不存在的 state → None", states.consume_state("nope", "B1") is None)

# 并发：同一个 state，同一个浏览器，只有一个调用能拿到
mk("S3", "B3")
results = []
barrier = threading.Barrier(8)
def race():
    barrier.wait()
    results.append(states.consume_state("S3", "B3") is not None)
ts = [threading.Thread(target=race) for _ in range(8)]
[t.start() for t in ts]; [t.join() for t in ts]
check("① 8 个并发回调抢同一个 state：恰好 1 个成功（原子消费）", results.count(True) == 1, results)

# 过期的不能消费
mk("S4", "B4")
with db.db() as conn: conn.execute("UPDATE oauth_states SET expires_at = ? WHERE state_hash = ?", (db.now() - 5, states._hash("S4")))
check("① 过期的 state → None", states.consume_state("S4", "B4") is None)

# ====================================================================== ② 回调兜底：任何异常都回登录页，不是裸 500
c = TestClient(app, follow_redirects=False, base_url="http://localhost")
def start_flow():
    r = c.get("/api/auth/google/start?return_to=/")
    loc = r.headers["location"]
    from urllib.parse import urlparse, parse_qs
    return parse_qs(urlparse(loc).query)["state"][0], r

state, r = start_flow()
check("② start：302 到 Google，并种下浏览器 cookie", r.status_code == 302 and config.OAUTH_BROWSER_COOKIE in r.cookies)

real_exchange, real_verify, real_upsert = google.exchange_code, google.verify_id_token, users.upsert_google
google.exchange_code = lambda **k: {"id_token": "x"}
google.verify_id_token = lambda tok, nonce: google.GoogleProfile(subject="sub1", email="u@example.com", email_verified=True, name="U", picture=None)

def boom(profile): raise RuntimeError("database is locked")
users.upsert_google = boom
r = c.get(f"/api/auth/google/callback?code=c&state={state}")
check("② 回调里 sqlite 出错 → 302 回登录页（不是裸 500）", r.status_code == 302 and "auth_error=login_failed" in r.headers["location"], (r.status_code, r.headers.get("location")))
users.upsert_google = real_upsert

state, _ = start_flow()
google.exchange_code = lambda **k: (_ for _ in ()).throw(ValueError("Google returned a list"))      # 对方返回了意外形状
r = c.get(f"/api/auth/google/callback?code=c&state={state}")
check("② 对方返回意外形状 → 同样回登录页", r.status_code == 302 and "login_failed" in r.headers["location"], r.headers.get("location"))
google.exchange_code = lambda **k: {"id_token": "x"}

real_create = sessions.create_session
sessions.create_session = lambda **k: (_ for _ in ()).throw(RuntimeError("disk I/O error"))
state, _ = start_flow()
r = c.get(f"/api/auth/google/callback?code=c&state={state}")
check("② 建会话失败 → 回登录页", r.status_code == 302 and "login_failed" in r.headers["location"])
sessions.create_session = real_create

state, _ = start_flow()
r = c.get(f"/api/auth/google/callback?code=c&state={state}")
check("② 正常回调 → 302 回应用并种下会话 cookie", r.status_code == 302 and config.SESSION_COOKIE_NAME in r.cookies, (r.status_code, r.headers.get("location")))
check("② state 被烧掉：同一个回调重放 → invalid_state", c.get(f"/api/auth/google/callback?code=c&state={state}").headers["location"].endswith("auth_error=invalid_state"))
google.exchange_code, google.verify_id_token = real_exchange, real_verify

# ====================================================================== ③ 会话 cookie 滑动续期
token = c.cookies.get(config.SESSION_COOKIE_NAME)
r = c.get("/api/auth/me")
set_cookie = r.headers.get("set-cookie", "")
check("③ /me 已登录 → 重发会话 cookie（否则 cookie 固定 30 天，活跃用户也被登出）", config.SESSION_COOKIE_NAME in set_cookie and f"Max-Age={config.SESSION_DAYS * 86400}" in set_cookie, set_cookie[:120])
check("③ 续期的 cookie 属性：HttpOnly + SameSite=lax + Path=/", "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie.replace("samesite", "SameSite") and "Path=/" in set_cookie, set_cookie)
anon = TestClient(app)
check("③ /me 未登录 → 不发 cookie", "set-cookie" not in anon.get("/api/auth/me").headers)
r = anon.get("/api/auth/me")
check("③ /me 未登录 → user 为 null，不是 401（这是“没登录”的正常答案）", r.status_code == 200 and r.json()["user"] is None)

# ====================================================================== ④ 限流与清理
c2 = TestClient(app, follow_redirects=False)
limited = 0
for _ in range(config.TOKEN_ISSUE_LIMIT + 5):
    r = c2.get("/api/auth/google/start")
    if "rate_limited" in r.headers.get("location", ""): limited += 1
check("④ /google/start 未登录就能调：超过限额 → rate_limited（不能被拿来刷库）", limited >= 1, limited)

with db.db() as conn:
    conn.execute("INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen_at) VALUES ('dead', (SELECT id FROM users LIMIT 1), 1, 2, 1)")
db.cleanup_expired()
with db.db() as conn:
    alive = conn.execute("SELECT count(*) AS n FROM sessions WHERE token_hash = 'dead'").fetchone()["n"]
check("④ cleanup_expired 清掉过期会话", alive == 0)

import inspect
from app import main as M
check("④ 清理不只在启动时做：lifespan 里有周期任务", "_cleanup_loop" in inspect.getsource(M.lifespan) and "cleanup_task.cancel()" in inspect.getsource(M.lifespan))

# ====================================================================== ⑤ id_token 的 iat 留了时钟偏差
check("⑤ verify_id_token 带 leeway（服务器时钟比 Google 慢几秒也不该登录失败）", "leeway=" in inspect.getsource(google.verify_id_token))

# ====================================================================== ⑥ assign_project_owner 脚本
with db.db() as conn:
    for uid, email in (("u1", "dup@example.com"), ("u2", "DUP@example.com")):
        conn.execute("INSERT OR IGNORE INTO users(id, email, email_verified, created_at, updated_at) VALUES (?, ?, 1, 1, 1)", (uid, email))
env = {**os.environ, "AUTH_DB_PATH": config.AUTH_DB_PATH}
def script(*args, stdin=""):
    return subprocess.run([sys.executable, "scripts/assign_project_owner.py", *args], capture_output=True, text=True, env=env, input=stdin, cwd=BACKEND)
p = script("--email", "dup@example.com", "--project-id", "x" * 32)
check("⑥ 两个账号共用一个邮箱 → 拒绝，不替你选一个", p.returncode == 3 and "refusing to guess" in p.stderr, (p.returncode, p.stderr[:120]))
p = script("--email", "u@example.com", "--project-id", "../../etc/passwd")
check("⑥ 不是合法工程 id → 拒绝", p.returncode == 2, (p.returncode, p.stderr[:100]))
p = script("--email", "u@example.com", "--project-id", "a" * 32)
check("⑥ 不存在的工程 id → 拒绝（占位会让之后别人上传同一个文件得到 409）", p.returncode == 2 and "existing project" in p.stderr, (p.returncode, p.stderr[:100]))

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)


def test_auth():
    assert not fails, fails
