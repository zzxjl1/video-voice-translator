"""
测试用的登录夹具。

鉴权上线后，所有 `/api/projects/{id}/...` 都要求登录并且工程属于当前账号。HTTP 级别的测试不该每个都
自己造用户、造会话，所以集中在这里：

    from _auth import login, own
    c = TestClient(app)
    login(c)                 # 造一个用户 + 会话，cookie 写进 c
    own(video_id)            # 把工程登记到这个用户名下

`login()` 返回用户 dict；`own()` 登记给最近一次 `login()` 的用户。
"""
import os, sys, tempfile

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

_last_user: dict | None = None


def _init_db_once(config):
    from app.auth import db
    if not getattr(config, "_test_auth_db_ready", False):
        # 每个测试进程一个独立的库，不碰 backend/data/auth.sqlite3
        config.AUTH_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="vvt-auth-"), "auth.sqlite3")
        db.init_db()
        config._test_auth_db_ready = True


def login(client, email: str = "tester@example.com"):
    """造一个用户和会话，把会话 cookie 设到 client 上。"""
    global _last_user
    from app import config
    from app.auth import sessions, users
    _init_db_once(config)
    profile = users.GoogleProfile(subject=f"sub-{email}", email=email, email_verified=True, name="Tester", picture=None) \
        if hasattr(users, "GoogleProfile") else None
    if profile is None:
        from app.auth.google import GoogleProfile
        profile = GoogleProfile(subject=f"sub-{email}", email=email, email_verified=True, name="Tester", picture=None)
    user = users.upsert_google(profile)
    # 测试用户预存一点积分：会花钱的命令有余额守卫，账本是空的就全被拦住了。
    # 想测"积分不足"的用例自己把余额扣到 0（见 test_billing.py）。
    from app.billing import service as _billing
    from app.billing.pricing import MICRO as _M
    if _billing.balance_points_micro(user["id"]) == 0:
        _billing.adjust(user["id"], 10_000 * _M, note="test seed", actor="test")   # 10000 积分
    token = sessions.create_session(user_id=user["id"], user_agent="test", ip="127.0.0.1")
    client.cookies.set(config.SESSION_COOKIE_NAME, token)
    _last_user = user
    return user


def own(video_id: str, user: dict | None = None) -> None:
    from app.auth import users
    u = user or _last_user
    assert u is not None, "先调用 login()"
    users.assign_project(video_id, u["id"])
