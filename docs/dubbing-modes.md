# 配音方式：Remote / In-browser / Off

工程设置里的 `dubbing_mode` 只有三个值：

```text
remote   远端 Qwen TTS；支持声音克隆、远端预制音色，按量计费
browser  浏览器本地 Kokoro；FREE，不克隆
off      不配音；只做转写/翻译/字幕，原声保持不变
```

本项目处于调试阶段，**不保留任何历史兼容**：旧工程不迁移，需要时直接删掉 `backend/data/<id>/` 重新上传。

工程设置存在 `project.json`，不在 SQLite。SQLite 只存账号、session、工程归属。

## UI

- 配音方式三选一：Remote / In-browser / Off
- In-browser 右上角有 `FREE`
- Off：隐藏 BGM 分离、声音克隆、智能选材、音色
- In-browser：隐藏声音克隆、智能选材；显示 `本地音色 · <目标语言>` 音色池
- 音色池列出 12 个、超出显示 `+N`；多说话人首次生成时自动给每位说话人分配不同 voice
- 生成后可在每一行的菜单里改这一句的 voice，或恢复跟随说话人
- 浏览器配音可用语言 = Kokoro 的语言矩阵 ∩ 目标语言（见下）；不可用的语言 In-browser 置灰

## 引擎（Kokoro-82M，24kHz，Apache-2.0）

`frontend/features/kokoro/` 里跑在一个专用 Web Worker（主线程不卡）：

| 后件 | 条件 | 模型下载 | 实测速度 |
| --- | --- | --- | --- |
| WebGPU + q8f16 | `navigator.gpu` 有适配器 | 86 MB（transformers.js 浏览器缓存） | ~10.9x 实时 |
| WASM + q8 | 其余（自动降级） | 92 MB | ~0.7x 实时 |

- q8f16 = int8 权重 + fp16 计算：权重解量化后 MatMul 走 GPU，与 fp16 同速且是
  发布里最小的文件。纯 int8 计算（dtype 'q8'）的 MatMulInteger 算子在 ort-web
  的 WebGPU EP 没有 GPU 实现，速度等同 WASM，不用。q8f16 不在 transformers.js
  的 dtype 映射表里，加载用 `dtype:'fp32' + model_file_name:'model_q8f16'`。
- WebGPU 初始化或生成失败时自动回退 WASM，一次会话内不再重试 WebGPU。
- 模型文件来自 HF；不可达时自动换 `hf-mirror.com` 镜像。
- 音色 bin（每个 0.5 MB，首次用到该音色时下载）来自 jsDelivr 的 kokoro-js 包。
- 官方 kokoro-js 的注册表只有英语音色；其它语言由 worker 自行音素化后走
  `generate_from_ids` 绕过：官方包的 54 个音色 bin 都已随 npm 包分发，`generate_from_ids`
  不校验音色名，只是注册表和音素化被硬编码成英语。

### 音素化（G2P）

| 语言 | 路径 |
| --- | --- |
| 中文 | `zhG2P.ts`：misaki（Apache-2.0）中文 G2P 表的 JS 移植，声调 →↗↓↘。已对拍：9 句工程台词 7 句与 Python misaki 逐字一致，2 句只差一/不变调（移植版用自然变调） |
| 英语 | kokoro-js 内置 espeak `en-us`（含数字归一化） |
| fr / it / pt | 全量 espeak-ng WASM（`@echogarden/espeak-ng-emscripten`，24 MB 数据从 jsDelivr 拉一次），音素过滤到 Kokoro 词表 |
| 日语 | **不可用**：模型有 5 个音色（jf_alpha 官方 C+），但 espeak 的 ja 输出是语言切换噪声，JS 生态没有 OpenJTalk 级 G2P |

## 浏览器配音语言矩阵（替换 Piper 后）

| 目标语言 | 音色数 | 备注 |
| --- | --- | --- |
| Chinese | 8 | 官方评级全部 D（训练数据薄），音质见试听对比 |
| English | 28 | af_heart 官方 A 级，显著优于 Piper |
| French | 1 | ff_siwis B−（与 Piper 旧 fr_FR-siwis 同源 SIWIS 数据） |
| Italian | 2 | |
| Portuguese | 3 | 仅巴葡（pt_PT 口音没了） |
| German / Vietnamese | 0 | **替换 Piper 的代价**：这两种语言失去浏览器配音，只能 Remote 或 Off |
| Japanese / Korean / Indonesian | 0 | Piper 时代就不可用；韩语/印尼语 Kokoro 也不支持 |

音色 id 是 Kokoro 自己的命名（`zf_xiaobei`、`af_heart`……），存在 `project.json`。
后端 `BROWSER_VOICE_PREFIX` 按前缀校验（zf_/zm_ 中文、af_/am_/bf_/bm_ 英语……），
`BROWSER_TTS_DEFAULT_VOICES` 是默认音色（Chinese→zf_xiaobei 等）。

## 音频路径

浏览器生成 WAV 后上传：

```text
POST /api/projects/{id}/browser-tts/{segment_id}
```

后端转成：

```text
backend/data/<id>/tts/<segment_id>.mp3
```

并写回 `actual_duration`、`tts_fp`。之后混音、时间线、导出完全复用 Remote TTS 的
现有路径。In-browser 不做声音克隆，也不支持 Smart selection。

## 失败与重试

- 浏览器合成失败：界面提示具体句号，已上传的句子不会丢
- 切换 voice 或目标语言：旧指纹失效，重新补配音即可
- 没有本地音色的语言（de/vi/ja/ko/id）：不能选 In-browser，提示改用 Remote / Off

## 长短句

官方建议单次合成 100-200 token 最佳（上限 511）；<10-20 token 会明显变弱。
worker 按 450 token 打包句子、短句自动拼包；单句超过上限时截断（字幕译文一般
不会触到）。
