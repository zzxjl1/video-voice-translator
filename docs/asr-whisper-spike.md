# Whisper base 本地 ASR 试验

这是独立脚本，**没有集成到应用主流程**，只用于拿现有工程音频对比 Whisper 效果。

## 运行

```bash
cd backend
../.venv/Scripts/python -m pip install -r scripts/requirements-asr-test.txt
../.venv/Scripts/python scripts/asr_whisper_test.py --all --model base --device cpu --compute-type int8
```

说明：

- `faster-whisper` 固定配 `av<19`，因为 PyAV 19 移除了 `metadata_errors`，会让 faster-whisper 1.2.1 解码报错。
- 默认读取 `vocals_asr.wav`，没有则退到 `extracted_audio.wav`。
- 参考文本来自现有 `project.json`，只取 `kind=speech` 的行。
- 报告写到 `backend/data/whisper-test/`，已被 git 忽略。

## 本次结果

### Whisper base，CPU int8

| 工程 | 语言 | 音频 | 耗时 | RTF | 参考行 | Whisper 行 | 粗略 CER |
|---|---|---:|---:|---:|---:|---:|---:|
| 271ccc17 | zh | 112.4s | 8.07s | 0.072 | 10 | 23 | 32.9% |
| 44767c2a | en | 29.6s | 2.61s | 0.088 | 9 | 10 | 13.9% |
| 8eb14154 | en | 34.8s | 2.61s | 0.075 | 3 | 10 | 498%* |
| a00820db | en | 138.6s | 6.87s | 0.050 | 27 | 31 | 2.6% |
| d24990f5 | zh | 34.5s | 2.22s | 0.064 | 8 | 9 | 34.7% |
| e4f5674b | zh | 12.5s | 1.32s | 0.106 | 3 | 4 | 0% |

### Whisper small，CPU int8

| 工程 | 语言 | 音频 | 耗时 | RTF | 参考行 | Whisper 行 | 粗略 CER |
|---|---|---:|---:|---:|---:|---:|---:|
| 271ccc17 | zh | 112.4s | 22.65s | 0.202 | 10 | 27 | 27.3% |
| 44767c2a | en | 29.6s | 7.23s | 0.244 | 9 | 10 | 8.2% |
| 8eb14154 | en | 34.8s | 6.12s | 0.176 | 3 | 5 | 108%* |
| a00820db | en | 138.6s | 20.74s | 0.150 | 27 | 40 | 1.1% |
| d24990f5 | zh | 34.5s | 6.59s | 0.191 | 8 | 8 | 22.8% |
| e4f5674b | zh | 12.5s | 4.03s | 0.323 | 3 | 4 | 25%** |

\* 8eb14154 的音频里包含歌曲/演唱，Whisper 把歌词也转出来了，而现有工程只把 speech 行作为参考，所以 CER 不可比。

\** e4f5674b 的 small 输出是繁体字（`你幹啥` / `打針啊`），字符不匹配会放大 CER；语义本身是对的。

## 观察

- CPU int8 很快：base RTF 约 0.05～0.11，small 约 0.15～0.32。
- 英文长视频（a008）文本层面不错，但句子边界会合并；第一段有明显识别错误。
- 中文 base 准确率不够；small 把 271/d249 的 CER 从 32.9%/34.7% 降到 27.3%/22.8%，仍不够生产级。
- Whisper 不做说话人分离；参考里的 speaker 信息无法对齐。
- 音乐/歌曲/人声段落会污染结果，不能直接和现有 speech 行比较。
- small 会输出繁体中文，需要额外的简繁转换/提示词控制。


## 下一步

1. 用 `small` 再跑同一批，观察中文是否明显改善。
2. 单独测试 sherpa-onnx diarization，看说话人分离效果。
3. 再决定是否值得做浏览器 WebGPU 版本；浏览器模型还要考虑体积、内存和移动端。
