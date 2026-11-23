# Piper 本地 TTS 试听页

独立试验，**没有集成到应用**。用现有工程的中文译文和远程 TTS 音频，逐句对比：

- Remote Qwen TTS：工程里已有的 `tts/seg-*.mp3`
- Piper local：`zh_CN-huayan-medium` ONNX，本地 CPU 生成

## 运行

```bash
cd backend
../.venv/Scripts/python -m pip install piper-tts
../.venv/Scripts/python -m piper.download_voices zh_CN-huayan-medium \
  --download-dir data/tts-test/voices
../.venv/Scripts/python scripts/tts_piper_test.py \
  --project-id d24990f5f9d1da0df883566547e41a6f
```

生成：

```text
backend/data/tts-test/index.html
backend/data/tts-test/remote/*.mp3
backend/data/tts-test/piper/*.wav
```

启动试听页：

```bash
../.venv/Scripts/python -m http.server 9200 --bind 127.0.0.1 \
  --directory data/tts-test
```

然后打开 http://127.0.0.1:9200/。

## 声音

- voice: `zh_CN-huayan-medium`
- 模型约 63 MB
- 采样率 22.05 kHz
- CPU 合成，短句基本即时

## 限制

- Piper 不支持现有 Qwen 的**音色克隆**
- 不支持 Qwen 的**情绪/语气标签**
- 固定 voice，不是原说话人音色
- 本次只测中文；其他语言要换对应 voice
- 这是听感对比，不改工程数据，也不参与导出
