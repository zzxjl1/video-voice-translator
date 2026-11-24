# 项目文档

重要且非显而易见的内容放在 `docs/`，不要只留在聊天记录里。

- [账号、Google 登录与工程归属](auth.md)
- [Whisper 本地 ASR 试验](asr-whisper-spike.md)
- [Piper 本地 TTS 试听页](tts-piper-spike.md)
- [配音方式：Remote / In-browser / Off](dubbing-modes.md)
- [计费：成本 / 倍率 / 余额](billing.md)
- 本地部署：见根目录 [LOCAL_DEPLOY.md](../LOCAL_DEPLOY.md)

约定：

- 新增认证、数据库、部署、外部服务、隐私相关改动时，同步更新对应文档。
- 文档只写配置项名称和占位值，不提交真实密钥。
- 路径、命令、环境变量以当前代码为准；改了行为就改文档。
