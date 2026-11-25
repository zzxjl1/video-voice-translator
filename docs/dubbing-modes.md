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

`frontend/features/kokoro/` 里跑在一个专用 Web Worker（主线程不卡）。**WebGPU 和 WASM 用同一个模型文件**
`model_int8.onnx`（92 MB）：

| 后端 | 条件 | 实测速度（Chrome / Apple M 系列） |
| --- | --- | --- |
| WebGPU | `navigator.gpu` 有适配器 | **~11-14x 实时**（fp16 同机 14-18x，慢约 20%） |
| WASM | 其余（自动降级） | ~0.8x 实时（单线程） |

### 为什么是"改过图的 int8"

官方的 int8 模型（`model_quantized.onnx`）在 WebGPU 上和 WASM 一样慢：它的每个线性层 / 卷积都是
`DynamicQuantizeLinear → MatMulInteger / ConvInteger → Cast → Mul`，ORT 的 WebGPU 后端没有这几个算子的
GPU 实现，整批回落 CPU。`backend/scripts/kokoro_int8.py` 只改图、不改权重：

- 148 个 `MatMulInteger` → `MatMul`，87 个 `ConvInteger` → `Conv`，权重在图里仍是 int8（文件和官方 int8 一样大）。
- 反量化用 `Cast → Sub → Mul`，输入全是常量，ORT 建会话时折叠成浮点权重；激活不再量化，所以数值比官方 int8 更接近 fp32。
- 4 个词嵌入查表后的反量化也同样替换。`DynamicQuantizeLSTM`（6 个）保留，它在 WebGPU 上走 CPU，不影响正确性。
- **不能用 `DequantizeLinear`**：ORT 的 WebGPU 后端对它的一部分变体会算错（最小模型实测：scale 是标量的整类都错，
  误差可到 5e7；scale 是向量时有的对有的错），onnxruntime-web 1.31.1 和 1.33 dev 都一样，同一个文件在 WASM 上完全正确。
  Kokoro 里两种都有，整个模型在 WebGPU 上输出垃圾（整段全零是其中一种表现）。`Cast/Sub/Mul` 在所有情形下都对。
  详细的变体表和上游线索见 `backend/scripts/kokoro_int8.py` 的文件头。
- 反量化节点的输入全是常量，会被常量折叠：WebGPU profiling 里整个模型的 `Sub` kernel 只有原图自带的 68 个，
  我们加的 169 个都没执行，推理时没有额外开销。
- 数值核对（真实模型、同一输入）：WebGPU 与 WASM 的输出长度相同、RMS 相同、包络相关 0.9999；逐点波形相关约 0.93，
  这是声码器里大相位正弦项在两个后端上浮点累加顺序不同造成的相位差，不是反量化错误。

实测（真实浏览器，同一个 worker，只换模型文件，预热后合成一句英语和一句中文）：

| 模型 | 大小 | WebGPU 英语 / 中文 | 音频 |
| --- | --- | --- | --- |
| **`model_int8`（改图版）** | **92 MB** | 2.67 s / 235 ms = **11x**，4.33 s / 314 ms = **14x** | 正常 |
| `model_fp16` | 163 MB | 2.70 s / 192 ms = 14x，4.97 s / 282 ms = 18x | 正常 |
| 官方 `model_quantized`（int8） | 92 MB | ~0.8x（算子回落 CPU） | 正常 |
| 只做 `DequantizeLinear` 替换的版本 | 92 MB | 10x | **错误（我们这份是全零静音）** |
| `model_q8f16` | 86 MB | 31 s 才出 **23 秒**音频（时长被拉长约 8 倍） | **错误** |

为什么只提交这一个：GitHub 单文件上限 100 MB，fp16（163 MB）推不上去；改图 int8 只比 fp16 慢约 20%，却小一半。

> 历史：提交 `062c5d0` 声称 "q8f16 实测 10.86x" 是误测。`KokoroTTS.from_pretrained` 只透传 `dtype` / `device`，
> **不透传 `model_file_name`**，当时请求的其实是默认的 `model.onnx`（fp32），所以才快。`engine.ts` 因此绕开
> `from_pretrained`，自己组装 model + tokenizer，并由测试保证加载的就是 `model_int8`。

- **只在引擎自己出错时才降级到 WASM**（会话创建失败、推理失败）。文本问题、音色不支持、文件取不到，
  降级也一样会失败，还白白慢约 15 倍，所以直接把错误报给用户。
- worker **串行处理请求**（单个 ORT 会话），每条消息（含下载进度）都带它所属请求的 `id`；
  `fillBrowserDubbing` 也串行：初始化页的自动配音和编辑器的「补配音」同时触发时，后者排队。
- 想提速 WASM 兜底：给页面加 COOP/COEP 响应头开启多线程，同一个 int8 文件实测从 ~0.8x 到 ~2.5x。
  目前没有开（需要改 nginx 和 Vite 的响应头，且所有跨域资源都要带 CORP）。

### 模型资源随代码库提供，运行和部署都不联网

`backend/models/` 下**一个模型一个文件夹**：

```text
backend/models/
  uvr-mdx-net-inst-hq-3/    人声分离（64 MB）+ 它的参数表（download_checks / mdx_model_data / vr_model_data.json）
  kokoro-82m/               本地配音（133 MB）
    config.json, tokenizer.json, tokenizer_config.json
    onnx/model_int8.onnx    92 MB，改图后的 int8（见上）
    voices/*.bin            应用里能选到的 42 个音色（中 8 / 英 28 / 法 1 / 意 2 / 葡 3），每个 0.5 MB
    espeak/                 espeak-ng.js / .data（fr/it/pt 音素化，24 MB）+ COPYING（GPL-3.0）
    manifest.json           每个文件的大小和 sha256
```

这些文件**已经提交进代码库**（不再 gitignore），部署不需要下载。后端同源提供（`GET /api/models/kokoro/*`，
要登录，支持 Range）；ONNX Runtime 的 wasm 由 Vite 打包进 `dist/assets/`。浏览器不访问 HuggingFace、
hf-mirror、jsDelivr，所以内网 / 国内网络 / 严格 CSP 下也能用。

维护者要升级模型、增减音色、或怀疑文件损坏时：

```bash
pip install onnx numpy
python backend/scripts/prepare_kokoro_models.py --verify   # 只校验现有文件（sha256），不联网
python backend/scripts/prepare_kokoro_models.py            # 下载缺的、转换模型、写 manifest
```

缺文件时 `GET /api/models/kokoro` 返回 `available:false` 和缺失清单，worker 报"本地配音资源缺失"，
而不是让用户去撞 404。

**改了模型内容，必须同时换文件名**（`model_int8` → `model_int8_2`），并改前端
`features/kokoro/assets.ts` 的 `KOKORO_MODEL_FILE` 和 `prepare_kokoro_models.py` 的 `MODEL_FILE`：
transformers.js 按 URL 缓存模型文件、不会重新校验，同名换文件会让老用户一直用旧模型。
`scripts/test-kokoro.mjs` 检查两个名字一致、onnx/ 里只有这一个模型。

官方 kokoro-js 的注册表只有英语音色；其它语言由 worker 自行音素化后走 `generate_from_ids` 绕过
（音色 bin 来自 kokoro-js 的 npm 包，`generate_from_ids` 不校验音色名）。

> 仓库体积：每次**更换**这些大文件，git 历史都会永久多出一份（模型 92 MB、espeak data 24 MB）。
> 所以升级模型是低频操作；日常改动不要动 `backend/models/`。

### 许可

espeak-ng 是 **GPL-3.0**。`espeak/espeak-ng.js` 和 `espeak-ng.data`（fr/it/pt 用）是它的编译产物，
`backend/models/kokoro-82m/espeak/COPYING` 随文件一起放着，请勿删除。英语走 kokoro-js 内置的
`phonemizer`（同为 espeak-ng 的 WASM 构建）。商用分发前请自行确认合规。Kokoro 模型本身 Apache-2.0，
misaki 的 G2P 表 Apache-2.0。

### 音素化（G2P）

| 语言 | 路径 |
| --- | --- |
| 中文 | `zhG2P.ts`：misaki 中文 G2P 表的 JS 移植（声调 →↗↓↘），加上文本前端：数字 / 百分号 / 金额 / 时间 / 年份读法（`100`→一百，`2020-2024`→逐位读年份，`2`+量词→两），**英文单词（品牌名、缩写）交给英文 espeak**，不会把原字母塞进音素串 |
| 英语 | kokoro-js 内置 espeak `en-us`（含数字、缩写归一化） |
| fr / it / pt | 全量 espeak-ng WASM（`@echogarden/espeak-ng-emscripten`，24 MB 数据，同源提供），音素过滤到 Kokoro 词表 |
| 日语 | **不可用**：模型有 5 个音色，但 espeak 的 ja 输出是语言切换噪声，JS 生态没有 OpenJTalk 级 G2P |

中文移植的已知边界：一/不变调用自然变调，和 Python misaki 的字典驱动变调在少数句子上不同；多音字靠
pinyin-pro 的句内上下文判断。词边界按**位置**判断（曾经按字符建表，同一个字出现在相邻两个词里就错位）。

### 分句（`text.ts`）

不是"见 `.!?` 就断"：`thirdweb.com`、`3.5`、`v2.0`、`Dr. Smith`、`e.g.`、`J. Smith`、`Wait...` 里的点都不是
句号。超过上限（中文 70 字 / 其它 250 字符）的句子在离中点最近的子句处切开，音素超出模型 511 token 窗口时
递归二分，**不再让 tokenizer 悄悄截掉句尾**。

## 浏览器配音语言矩阵

| 目标语言 | 音色数 | 备注 |
| --- | --- | --- |
| Chinese | 8 | 官方评级全部 D（训练数据薄），音质见试听对比 |
| English | 28 | af_heart 官方 A 级 |
| French | 1 | ff_siwis B− |
| Italian | 2 | |
| Portuguese | 3 | 仅巴葡 |
| German / Vietnamese / Japanese / Korean / Indonesian | 0 | Kokoro 不支持或浏览器没有可用 G2P；只能 Remote 或 Off |

**这张表只有两份**，且由测试比对：前端 `frontend/features/kokoro/catalog.ts` 的 `LANGUAGES`（默认音色 +
前缀）和后端 `backend/app/config.py` 的 `BROWSER_LANGUAGES`。`scripts/test-kokoro.mjs` 会导入后端配置逐项
比较，改一边忘了另一边直接测试失败。音色 id 是 Kokoro 自己的命名（`zf_xiaobei`、`af_heart`……），存在
`project.json`；后端只按前缀校验，完整音色目录在前端。

音色列表按官方评级从好到坏排序（A 在前，未评级最后）；多说话人的自动分配按这个顺序，默认音色排第一。

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

## 初始化之后出现的缺口：谁来补

本地配音在**用户的浏览器里**合成，服务端自己合成不了。初始化那一次是前端自动跑的（`App.tsx` 的 effect，
去重 key 是 `id:mode:lang:accent`，同一个工程只跑一次）。之后再出现缺口——AI 重译了几句、把歌标回语音、
用户改了译文——有三条路径来补：

| 谁触发 | 怎么补 |
| --- | --- |
| 用户 | 顶栏「补」/ 行菜单「重新配音」→ 编辑器直接调 `fillBrowserDubbing` |
| AI | `redub`（见下） |
| 初始化页 | 只在初始化阶段自动跑一次 |

**AI 的 `redub`**：以前在浏览器模式下直接报错"请在页面里生成"，AI 只能让用户自己去点。现在：

1. 先做和远端一样的校验（行号、非语音/静音、译文缺失/过期）——校验不因为改走页面就被绕过。
2. 算出真正需要合成的句子（缺失/过期；`force` 时是全部指定行），**没有就直接说"没活可干"**，不发事件。
3. 没有页面连着这个工程的事件流（`bus.has_subscribers`）→ 明说做不了，而不是假装已请求。
4. 否则给页面发界面意图 `browser_dub`（`line_ids` + `force`），编辑器收到后和点「补」走同一条路径合成并上传。

返回值里写明 `async: true`、`queued: N`，并提醒"命令返回时还没合成完，不要说已配好"；AI 之后看概览里还差多少句配音来确认。
合成失败的句子由页面提示用户（和手动点「补」一致）。

确认策略：远端 `redub` 花钱，AI 调用要先问用户；浏览器模式本地合成免费，不问（`Command.confirm_unless`）。

页面端的并发：同一时间只合成一批（`fillBrowserDubbing` 内部排队，进队时重新取工程状态，已做完的句子自然跳过）。
人手重复点击在有批次进行时直接拒绝；**AI 的请求不拒绝**（它已经被告知"已请求"，悄悄丢掉会让缺口一直留着），
排在当前批次后面。

已知限制：

- 界面意图是广播的，同一个工程开着多个标签页时每个都会收到，会各自合成一遍（结果等价，只是重复耗 GPU）。
- 页面没连着时 AI 的请求会被拒；之后用户回来点「补」即可。
- 界面意图只有一个槽位，同一瞬间到达两个意图时后到的会覆盖前面的（`redub` 只发一个事件，不会自己触发这种情况）。

## 失败与重试

- 浏览器合成失败：界面提示具体句号，已上传的句子不会丢
- 切换 voice 或目标语言：旧指纹失效，重新补配音即可
- 没有本地音色的语言（de/vi/ja/ko/id）：不能选 In-browser，提示改用 Remote / Off

## 长短句

官方建议单次合成 100-200 token 最佳（上限 511）；<10-20 token 会明显变弱。
worker 按 450 token 打包句子、短句自动拼包；单句超过上限时二分重试，不截断。

## 导出音频链路（采样率 / 声道）

混音与成片统一在 **44.1 kHz 立体声**，AAC 192k（`EXPORT_SAMPLE_RATE` / `EXPORT_CHANNELS` / `EXPORT_AUDIO_BITRATE`）。

为什么是 44.1 kHz：人声分离模型（UVR MDX）工作在 44.1 kHz，分离出的 `background.wav`
因此必然是 44.1 kHz 立体声——它是整条链路里**唯一的高保真源**（远端 TTS 22.05 kHz、
浏览器 Kokoro 24 kHz 都比它窄）。混音取同一采样率，垫底轨零重采样直通；人声轨无论
选哪个采样率都要升一次，没有差别。以前是 24 kHz：所有音源在混音阶段就被削到 12 kHz
带宽，成片再升到 48 kHz 也补不回来。

声道：**配音轨与保留原声轨保持单声道**（人声居中，1D memmap），在 mux 时用
`aformat=channel_layouts=stereo` 展开成 L=R；**垫底轨保留立体声**（`build_ducked_bed`
按 `(帧, 声道)` 流式处理，同一时刻各声道同一增益，duck 不会把声像扭向一边）。
以前垫底被 `-ac 1` downmix，声场塌成一点，左右反相成分还会互相抵消——成片"闷、扁"的主因。

代价（已实测）：中间 WAV 约 ×3.7（2 小时视频垫底轨 ~2.5 GB 临时文件，导完即删）、
导出 CPU 时间约 +10–20%、成片音频码流 160k→192k。响度目标与限幅逻辑不变
（限幅器仍在重采样之后），真工程实测 LUFS −16.0 正中目标。

回归断言：`test_master.py` ④ 成片 `channels=2`、采样率=混音采样率、`build_ducked_bed`
输出立体声、左右反相垫底经处理后 L/R 仍有差异（防 downmix 回归）。
