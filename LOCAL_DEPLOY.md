# 本地部署运行说明

## 结论

本地全功能已经跑通，**不需要 OSS，也不需要改服务器**。

DashScope 的 ASR、omni 审听和声音复刻都需要一个公网 URL 来下载音频。这里用一条
**临时 Cloudflare 隧道**把本地后端暴露出去，代码保持原样。

## 架构

```
浏览器
  -> http://127.0.0.1:3100        Vite dev server
  -> /api 代理 -> http://127.0.0.1:9100   FastAPI（只监听回环）

DashScope
  -> https://<随机>.trycloudflare.com      Cloudflare 临时隧道
  -> http://127.0.0.1:9101                本地只读过滤代理
  -> http://127.0.0.1:9100                本地后端
```

Cloudflare 只是隧道，不是项目依赖。本地 9101 的过滤代理只放行三个只读接口：

- `GET /api/health`
- `GET /api/projects/<32 位 id>/audio`
- `GET /api/projects/<32 位 id>/audio/original`
- `GET /api/projects/<32 位 id>/voice-sample/<speaker>`

其它路径一律 404，聊天、工程文档、上传、命令接口都不会通过公网暴露。

启动器会把当前隧道地址注入后端进程环境变量 `SERVER_URL_BASE`，所以 `backend/.env`
里的地址只是占位，不需要每次手改。

## 为什么不用 OSS

OSS 方案本身是可行的，我实测过：

- `getPolicy` + POST 到 `dashscope-file-mgr`，得到 `oss://...`
- ASR、omni、`create_voice` 带 `X-DashScope-OssResourceResolve: enable` 都能用
- 文件 48 小时有效、单文件上限 1 GB、与上传时的模型绑定

但它必须改应用代码：`asr_service.py`、`omni_service.py`、`voice_clone_service.py`、
`jobs.py`、`voices.py`、`config.py` 都要加分支。你明确要求不污染代码库，所以这次不用它。

Cloudflare 隧道能解决同一个问题，而且：

- 不改仓库代码
- 不改服务器 nginx、不加 SSH 公钥
- 不需要公网 IP / 域名

所以当前方案是隧道，不是 OSS。

如果以后 Cloudflare 不可用，再考虑 OSS 或服务器中继；那时需要接受相应的代码或服务器改动。

## 启动 / 停止

运行时文件在仓库外面：`%LOCALAPPDATA%\vvt-local`。

```powershell
# 启动：只读代理 + Cloudflare 隧道 + 后端 + 前端
python C:\Users\zzxjl\AppData\Local\vvt-local\run.py

# 停止
python C:\Users\zzxjl\AppData\Local\vvt-local\stop.py
```

启动后会打印：

- App：<http://127.0.0.1:3100/>
- API：<http://127.0.0.1:9100/api/health>
- ASR 回调：`https://<随机>.trycloudflare.com`

当前隧道地址也会写到 `%LOCALAPPDATA%\vvt-local\state.json`，日志在同目录的
`*.out.log` / `*.err.log`。

注意：Cloudflare 快速隧道的域名每次重启都会变，`run.py` 会自动把新地址传给后端；
不要手动把旧地址写死到 `.env`。

## 配置

`backend/.env` 已被 git 忽略，里面是线上同一套 DashScope / DeepSeek 密钥，
本地调用会计费到同一个账号；如果要分享代码，先轮换密钥。

需要保持稳定的只有：

```dotenv
SIGNING_SECRET=...     # 否则浏览器分离令牌在重启后失效
SERVER_URL_BASE=...    # 占位；实际由启动器注入当前隧道地址
```

## 已验证

用 `samples/problem_video/problem_sample.mp4`（34.5 秒）跑通：

- ASR：9 句、说话人分离正常，音频由 DashScope 通过 Cloudflare 隧道成功下载
- omni 审听：5 句被纠正/标注
- 声音复刻：1 个说话人克隆成功，TTS 正常（测试音色已删除）
- 配音：9 句全部 `fresh`
- 导出：MP4（H.264 + AAC + `mov_text` 字幕）成功
- 只读代理：非音频路径返回 404

测试工程和测试音色都已清理。

## 限制与注意事项

- Cloudflare 快速隧道在国内网络可能不稳定；断了就重新跑 `run.py`，会得到新地址。
- 隧道只影响 ASR、omni 审听、声音复刻；翻译、TTS、导出不依赖它。
- 隧道开着时，这三个只读音频接口对公网可达；不用时建议 `stop.py` 停掉。
- 后端仍然只监听 `127.0.0.1:9100`，局域网其它设备访问不到。
- 浏览器端人声分离需要 WebGPU/WASM，首次会下载约 64 MB 的 ONNX 模型。
- 仓库除本说明文档外没有其它改动（`git status` 可验证）。

## 如果不想用 Cloudflare

| 方案 | 结果 | 代价 |
| --- | --- | --- |
| 本地前端直连线上后端 | 不需要本地后端和隧道 | 后端改动无法本地验证 |
| 只跑本地前后端，停隧道 | UI / 翻译 / TTS / 导出正常 | ASR / omni / 声音复刻不可用 |
| 改用 OSS | 全功能，无需公网回调 | 必须改应用代码 |
| 服务器 nginx 中继 | 全功能 | 必须改服务器配置 |
