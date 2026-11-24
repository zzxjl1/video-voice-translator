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

| 后端 | 条件 | 模型下载 | 实测速度 |
| --- | --- | --- | --- |
| WebGPU + fp16 | `navigator.gpu` 有适配器 | 163 MB | ~8-10x 实时 |
| WASM + q8 | 其余（自动降级） | 92 MB | ~0.7x 实时 |

实测（Chrome / Apple M 系列 / WebGPU，同一个 worker，只换模型文件，预热后合成两句）：

| 模型文件 | 大小 | 合成 2.7 秒英语 | 合成 5 秒中文 | 结论 |
| --- | --- | --- | --- | --- |
| `model_fp16.onnx` | 163 MB | 0.33 s | 0.48 s | **采用** |
| `model.onnx`（fp32） | 325 MB | 0.33 s | 0.46 s | 同速，体积翻倍，不用 |
| `model_q8f16.onnx` | 86 MB | 31 s，**输出 23 秒** | 50 s，**输出 37 秒** | **输出是错的**：时长被拉长约 8 倍，也只有 0.7x |

- **不要用 `model_q8f16`**。之前的提交（`062c5d0`）写的"q8f16 实测 10.86x"是误测：`KokoroTTS.from_pretrained`
  只透传 `dtype` / `device`，**不透传 `model_file_name`**，当时请求的其实是默认的 `model.onnx`（fp32），
  所以才快。真正加载 q8f16 后，ort-web 的 WebGPU 上它既慢又出错音频。
- 纯 int8 的 `q8` 在 WebGPU 上没有 GPU 算子实现，会回落 CPU，速度等同 WASM；所以 WebGPU 用 fp16，
  WASM 用 q8（`model_quantized.onnx`）。
- 想让 transformers.js 选某个文件，只能用它认识的 dtype（fp16 → `_fp16`，q8 → `_quantized`）；写
  `model_file_name` 不会报错，只会悄悄加载别的文件。
- **只在引擎自己出错时才降级到 WASM**（会话创建失败、推理失败）。文本问题、音色不支持、文件取不到，
  降级也一样会失败，还白白多下 92 MB、之后慢约 15 倍，所以直接把错误报给用户。
- worker **串行处理请求**（单个 ORT 会话），每条消息（含下载进度）都带它所属请求的 `id`；
  `fillBrowserDubbing` 也串行：初始化页的自动配音和编辑器的「补配音」同时触发时，后者排队。

### 运行时不访问任何第三方站点

模型、tokenizer、54 个音色、espeak-ng 数据都由**本项目后端同源提供**（`GET /api/models/kokoro/*`，
要登录，支持 Range）。ONNX Runtime 的 wasm 由 Vite 打包进 `dist/assets/`。浏览器不再访问
HuggingFace、hf-mirror、jsDelivr，所以内网 / 国内网络 / 严格 CSP 下也能用，文件内容也由 sha256 钉死。

部署时下载一次（需要能访问 HF 或 hf-mirror、jsDelivr 的机器）：

```bash
python backend/scripts/fetch_kokoro_models.py            # 约 310 MB，已有且大小一致的跳过
python backend/scripts/fetch_kokoro_models.py --verify   # 只校验
```

文件放在 `backend/models/kokoro/`（已 gitignore），布局和 `manifest.json`（大小 + sha256）见脚本头注释。
缺文件时 `GET /api/models/kokoro` 返回 `available:false` 和缺失清单，worker 报"本地配音资源缺失，
请运行 fetch_kokoro_models.py"，而不是让用户去撞 404。

官方 kokoro-js 的注册表只有英语音色；其它语言由 worker 自行音素化后走 `generate_from_ids` 绕过
（54 个音色 bin 随 kokoro-js 的 npm 包分发，`generate_from_ids` 不校验音色名）。

### 许可

espeak-ng 是 **GPL-3.0**。`espeak/espeak-ng.js` 和 `espeak-ng.data`（fr/it/pt 用）是它的编译产物，
`backend/models/kokoro/espeak/COPYING` 随文件一起放着，请勿删除。英语走 kokoro-js 内置的
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

## 失败与重试

- 浏览器合成失败：界面提示具体句号，已上传的句子不会丢
- 切换 voice 或目标语言：旧指纹失效，重新补配音即可
- 没有本地音色的语言（de/vi/ja/ko/id）：不能选 In-browser，提示改用 Remote / Off

## 长短句

官方建议单次合成 100-200 token 最佳（上限 511）；<10-20 token 会明显变弱。
worker 按 450 token 打包句子、短句自动拼包；单句超过上限时二分重试，不截断。
