# 账号、Google 登录与工程归属

## 范围

- 只支持 Google OAuth 登录，不做邮箱密码、不做第三方托管认证。
- 只做 Web，单机部署，SQLite。
- 上传、导出以及所有项目接口都要求登录。
- 只做个人项目：一个工程一个 owner，不做团队/共享。
- 暂不做账号删除、付费、配额。
- 不保存 Google access token / refresh token；只做身份登录。

## 代码位置

```text
backend/app/auth/
  db.py             # SQLite schema / connection
  google.py         # OAuth URL、code 换 token、ID token 校验
  sessions.py       # opaque session cookie
  states.py         # 一次性 OAuth state
  users.py          # users / identities / project_owners
  dependencies.py   # require_user / require_project_owner
  router.py         # /api/auth/*

backend/scripts/assign_project_owner.py
frontend/auth/AuthProvider.tsx
frontend/auth/AccountMenu.tsx
frontend/auth/AuthGate.tsx
frontend/auth/AuthErrorToast.tsx
frontend/legal/LegalPage.tsx   # /privacy、/terms
```

## 数据存储

账号数据在 SQLite：

```text
backend/data/auth.sqlite3
```

可通过 `AUTH_DB_PATH` 改路径；`backend/data/` 已被 git 忽略。

表：

- `users`：本地用户
- `identities`：`provider + issuer + subject`，目前只有 Google，但按多 provider 设计
- `sessions`：只存 session token 的 SHA-256
- `oauth_states`：state / nonce / PKCE verifier，一次性、短过期
- `project_owners`：`project_id -> owner_id`

工程本身仍是文件系统里的 `project.json` + 视频/音频文件；没有把工程文档迁进 SQLite。

## Google Cloud 配置

在 Google Cloud Console 创建 **Web application** OAuth Client。

### Authorized redirect URIs

```text
http://localhost:3100/api/auth/google/callback
https://video-voice-translator.idealbroker.cn/api/auth/google/callback
https://video-voice-translator.ideabroker.top/api/auth/google/callback
```

`.top` 上线后再启用对应域名。

### Scopes

```text
openid
email
profile
```

不需要 Gmail、Drive、YouTube 等权限。

### Authorized JavaScript origins

服务端 Authorization Code 流程不需要，留空即可。  
以后如果使用 Google One Tap / GIS 前端 SDK，再填 origin，不能带路径。

### Consent Screen

- User type：External
- 测试阶段把登录邮箱加入 **Test users**
- Privacy Policy：`https://video-voice-translator.idealbroker.cn/privacy`
- Terms：`https://video-voice-translator.idealbroker.cn/terms`
- 联系邮箱：`zzxjl1@hotmail.com`

## 环境变量

参考 `backend/.env.example`：

```env
APP_BASE_URL=https://video-voice-translator.idealbroker.cn
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
# 不填时 = APP_BASE_URL/api/auth/google/callback
GOOGLE_REDIRECT_URI=
AUTH_DB_PATH=
SESSION_DAYS=30
SESSION_TOUCH_SECONDS=3600
SESSION_COOKIE_SECURE=true
```

本地开发：

```env
APP_BASE_URL=http://localhost:3100
SESSION_COOKIE_SECURE=false
```

不要把 `backend/.env` 提交到 git；里面是真实 secret。

## 本地登录

1. 把 Google client ID/secret 填进 `backend/.env`
2. 重启本地 runner
3. 打开 **http://localhost:3100**，不要用 `127.0.0.1`

`127.0.0.1` 和 `localhost` 是两个 host，OAuth 绑定 cookie 不共享。前端在 `127.0.0.1` 点登录会自动跳到 `localhost`。

Google 登录页、token endpoint、JWKS 需要开发机/服务器能访问 Google。代码没有专门代理逻辑；`requests` 使用系统代理或 `HTTPS_PROXY` 等环境变量。

## Session 设计

不使用 JWT，使用服务端 opaque session：

```text
Cookie: vvt_session=<32 byte random>
HttpOnly; SameSite=Lax; Secure(生产); Path=/
```

- DB 只存 `sha256(token)`
- 登录时生成新 session；如果当前浏览器已有旧 session，先撤销
- 默认 30 天滑动过期：服务端最多每小时把 `expires_at` 往后推一次，同时前端每次加载调用 `/api/auth/me`，该接口会**重发** Set-Cookie，浏览器端的 cookie 过期时间才会跟着滑动（只推服务端、不重发 cookie 的话，活跃用户到第 30 天照样被登出）
- `last_seen_at` 最多每小时写一次，避免每请求写库
- 清理：启动时清一次，之后每小时清一次过期会话和 OAuth state
- 多设备/多浏览器各自一个 session，互不踢
- 退出只撤销当前 session
- 以后可加“退出所有设备”：`revoke_user_sessions(user_id)`

## API

```text
GET  /api/auth/google/start?return_to=/
GET  /api/auth/google/callback
GET  /api/auth/me
POST /api/auth/logout
```

OAuth 安全点：

- Authorization Code + PKCE
- `state` 一次性、10 分钟过期、绑定浏览器 cookie
- `nonce` 校验
- ID token 校验 `iss` / `aud` / `exp` / 签名 / `email_verified`
- 用 Google `sub` 作为外部唯一键，不用 email
- `return_to` 只允许站内相对路径
- 只使用 ID token 里的身份信息，丢弃 Google access token

## 工程权限
`projects.router` 和 `upload_router` 都要求 `require_user`。  
项目详情/媒体/导出等再经过 `require_project_access`：

- 无 owner 或 owner 不匹配：**404**，不返回 403
- 上传时校验已有 owner；同一文件属于其他账户返回 409
- 删除工程时同时删 `project_owners` 映射

旧匿名工程不做运行时兼容：没有 `project_owners` 记录的工程直接 404。

### 外部 ASR/克隆的音频读取

DashScope ASR、omni、音色克隆通过公网 URL 取 `/api/projects/<id>/audio`，带不了登录 cookie。  
这些 GET 音频请求改用短时签名 token：

```text
/api/projects/<id>/audio?token=<signed-token>
```

- token 绑定 `video_id`，默认有效期覆盖 ASR 最大等待时间
- 只允许 `/audio` 和 `/audio/original` 两个只读 GET
- 无 token 仍然 401；其他项目接口不能靠这个 token 访问


## 旧工程补 owner

先让目标账号登录一次，确保 `users` 里有该用户，然后在服务器/本地运行：

```bash
cd backend
python scripts/assign_project_owner.py --all-anonymous --email user@example.com --dry-run
python scripts/assign_project_owner.py --all-anonymous --email user@example.com
```

只分配没有 owner 的工程；已属于其他用户的工程会拒绝。  
脚本只写 `project_owners` 和 `auth.sqlite3`，不修改 `project.json`。

## 账户 UI

- 落地页/初始化页：语言地球图标右边是登录按钮或头像
- 编辑页：只在顶栏展开后、设置按钮旁边显示头像
- 折叠态仍然只有 `Show Tools`
- 未登录打开 `/?v=...` 会进入登录页
- 未登录点上传会先登录，回来后重新选文件
- 账户菜单：头像、名字、email、退出登录、Privacy/Terms

## 隐私与条款

```text
/privacy
/terms
```

当前是占位页，联系方式：

```text
zzxjl1@hotmail.com
```

Google OAuth Consent Screen 可直接用这两个 URL。正式上线前需要补齐/审阅隐私政策。

## 生产部署检查

1. 部署代码 + 重新构建 `frontend/dist`
2. 生产 `.env` 设置：
   - `APP_BASE_URL`
   - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`
   - `SESSION_COOKIE_SECURE=true`
   - `AUTH_DB_PATH`（默认 `backend/data/auth.sqlite3`）
3. 确保 `backend/data/` 持久化并有备份
4. `pip install -r backend/requirements.txt`（包含 `PyJWT[crypto]`）
5. Nginx 继续让 `/api/` 同源代理；cookie 依赖同源
6. 确保服务器能访问 `oauth2.googleapis.com` 和 `www.googleapis.com`
7. `.cn` / `.top` 各自有独立 host-only cookie；如需统一，做 301 到主域名
8. 运行旧工程补 owner 脚本（如果需要）

## 故障排查

### `signin failed` / `auth_error=invalid_state`

优先检查 host：

- 从 `127.0.0.1` 发起，但 redirect URI 是 `localhost`
- 两边 cookie 不共享，callback 找不到 `vvt_oauth`

用 `http://localhost:3100`；新版前端也会自动跳转。

其他可能：

- state 超过 10 分钟
- 浏览器禁用了 cookie
- 回调前清除了 cookie

### `access_denied`

Consent Screen 是 Testing，登录邮箱不在 Test users 里。

### `redirect_uri_mismatch`

Google Console 里的 redirect URI 和 `GOOGLE_REDIRECT_URI`/`APP_BASE_URL` 不完全一致；协议、host、port、路径都要一致。

### Google 接口超时

服务器无法访问 Google。检查出站代理/网络；代码没有写代理逻辑，`requests` 使用系统代理或环境变量。

## 当前已知限制

- 单机 SQLite；多实例前需要迁 Postgres 或共享会话存储
- 账号删除暂未实现，隐私页写的是人工联系
- 付费/配额未做
- 不保存 Google token，所以 Google 侧退出不会联动应用 session
- `.cn` 和 `.top` 是两个 host，登录态不共享
