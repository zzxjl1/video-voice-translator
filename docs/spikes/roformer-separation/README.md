# Spike：人声分离换 Roformer（浏览器 WebGPU）—— 调研与实测记录

> 状态：**跑通、测过、未接入**。现有 MDX 够用，Roformer 在能测的样本上只赢 0.9 dB，不值 150 MB + 重写 DSP。
> 但路已经铺好了：依赖、模型、spike 页、对比脚本都在，想捡起来半天能接。
>
> 日期：2026-11-26。决定搁置，等有更多真实素材或用户反馈"克隆有伴奏味"时再评估。

## 一句话结论

| | 现在 MDX-Net Inst_HQ_3 | 候选 Roformer student |
|---|---|---|
| 大小 | 64 MB | 213 MB（fp16）+ 6 MB 图 |
| 公开 SDR（人声） | ~8.5 | ~10.2（教师 11，蒸馏 −0.8） |
| 实测速度（浏览器 WebGPU, M 系列） | ~7x 实时 | **~9x 实时** |
| 实测质量（真伴奏视频） | 基准 | 背景轨人声残留好 **0.9 dB**；重构误差差 11 dB |
| 接入成本 | 已接 | 钉 ORT 1.30 + 换 DSP 层 + 150 MB 首次下载 |

SDR +1.7 dB 在分离领域算"明显"，但在我们的链路里**听不出来**：成片里背景轨被 duck 到 −10~−20 dB，分离 artifacts 被掩蔽；ASR 有 omni 审听兜底。唯一能受益的是**克隆参考音频更干净**——这是真素材上 0.9 dB 的那个差距。

## 调研过的选项（别重查）

**浏览器端**（HF 上 `webgpu` + `separation` 全扫过）：≤300 MB 的**只有** [musetric Duality student](https://huggingface.co/musetric/mel-band-roformer-duality-kara2-student-onnx)（Apache-2.0）。其它 Roformer 的 ONNX 都 600–970 MB。**没有人做 int8**——transformer 结构量化掉点大，社区走的是蒸馏。

**API**：最便宜且对口的 [Music.AI](https://music.ai/pricing/) "Cinematic stems"（对白/音乐/音效）$0.05/分钟 → 30 分钟 ¥11，是整条流水线收入（¥3.15）的 3.5 倍，且音频出境。LALAL.AI ¥14、Moises ¥26。**没有商业上可行的 API**；Replicate 自部署 bs-roformer 约 ¥1.9/30 分钟（账单是标价 2 倍：冷启动/加载/I/O 都计费），但要运维+出境。企业版可考虑 Music.AI。

## 实测数据

样本：用户提供的 18.8 秒 TikTok 视频（真人声 + 真伴奏，混音 −9.3 dB，背景比人声响）。mono 44.1 kHz：

| 指标 | MDX | Roformer | 说明 |
|---|---|---|---|
| 人声轨 RMS | −16.1 | −16.5 | 相当 |
| 人声 6–10k 相对混音 | −2.1 | −2.9 | 齿擦音区都保留 |
| 人声轨静音段残留 | −47.8 | −48.3 | 相当 |
| **说话段背景轨人声残留** | −5.7 | **−6.6** | Roformer 略干净 |
| 重构误差 mix−(v+b) | **−40.3** | −29.2 | Roformer 丢更多能量（判成"谁都不属于"） |

试听页（7 个播放器并排）：`~/Desktop/分离对比_MDX_vs_Roformer/index.html`（含全部 wav，约 23 MB，没进 git）。

**两个坑，别再踩**：
- `problem_video/speech_stem.flac` 是 `make_sample.py` **合成**的（人声 = 混音 − 衰减背景），不是 MDX 输出。拿它当基准会得到 −54 dB 重构误差 / −120 dB 残留这种任何模型都比不过的数字。
- 合成共振峰人声（testsrc2 样本）8 kHz 以上没内容，Roformer 会把它当噪声削掉——显示"高频 −17 dB"，那是正确行为不是缺陷。**测高频必须用真人声。**

## 怎么捡起来

### 依赖（已装，未提交）

```jsonc
// frontend/package.json
"dependencies": {
  "@musetric/ai": "0.1.2",        // MIT，736 KB；separation 路径不依赖 transformers.js（peer 只是 whisper 用的）
  "onnxruntime-web": "1.30.0",     // 必须钉 1.30.0，见下
},
"overrides": { "onnxruntime-web": "$onnxruntime-web" }   // 和 transformers 3.8.1（自带 1.22-dev）去重
```

`npm install --legacy-peer-deps`（它声明 peer `@huggingface/transformers@4.3.2`，我们用 3.8.1；separation 路径实际不碰 transformers，所以安全）。

**ORT 必须 1.30.0**：musetric 锁这个版本。1.31.1 下 WebGPU EP 报
`There's no data transfer registered for copying tensors from Device:[DeviceId:0] to Device:[DeviceId:1]`
——ORT 自己 requestAdapter 创建了第二个 device。Kokoro 在 1.30 下**未验证**，接入前要跑一次 `scripts/test-kokoro.mjs`。

### 模型（500 MB，gitignore 了，`public/roformer-model/`）

```bash
cd frontend/public/roformer-model
for f in duality_kara2_student_core_t1100.onnx duality_kara2_student_core_t1100.onnx.data; do
  curl -L -o $f "https://huggingface.co/musetric/mel-band-roformer-duality-kara2-student-onnx/resolve/main/$f"
done
```

`loadSeparationModel` 会校验 sha256（`node_modules/@musetric/ai/.../pin.json`），文件名不能改。

### 核心调用（spike.html 里跑通的）

```js
import * as sep from '@musetric/ai/separation';

if (!await sep.separationSupported()) throw new Error('需 WebGPU');   // 内置显存门槛检测

const [model, modelData] = await Promise.all([
  fetch('/roformer-model/duality_kara2_student_core_t1100.onnx').then(r => r.arrayBuffer()).then(b => new Uint8Array(b)),
  fetch('/roformer-model/duality_kara2_student_core_t1100.onnx.data').then(r => r.arrayBuffer()).then(b => new Uint8Array(b)),
]);
const loaded = await sep.loadSeparationModel({ model, modelData });  // 注意：会 transfer 这两个 buffer

// 源：44.1 kHz 立体声 Float32Array[]（用现有 decodeAudio 即可）
const stems = { instrumental: [], lead: [], backing: [] };
const scales = await sep.separateTrack({
  ...sep.separationTrack,          // { geometry: { sampleRate: 44100, chunkSamples }, channels: 2, masks: 2 }
  source: {
    frames: audio.channels[0].length,
    peak: async () => 1.0,        // decodeAudio 已归一化；否则要给真实峰值，它内部做 peakScale
    read: async (start, chans) => {   // chans: Float32Array[2]，按 start 填充
      for (let c = 0; c < chans.length; c++) {
        const src = audio.channels[c], n = chans[c].length;
        chans[c].set(start + n <= src.length ? src.subarray(start, start + n) : src.subarray(start));
      }
    },
  },
  loadModel: async () => loaded,
  report: (unit, count) => {},     // 进度
  commit: async ({ stems: s }) => {   // 按 unit 增量回调；s.* 是交错立体声 Float32Array
    for (const k of Object.keys(s)) stems[k].push(new Float32Array(s[k]));
  },
});
// scales 是每轨的归一化系数（peakScale）；peak() 给对了就不用乘回
await loaded.release();
```

输出：`instrumental` / `lead` / `backing` 三轨（MDX 只有两轨，backing 是 Roformer 多出来的——对配音场景没用，可以丢）。

### 接入要动的地方

1. `features/separation/separatorClient.js`：`separateAndUpload` 里换 `separateTrack` 调用；`separationSupported()` 替换现有 `navigator.gpu` 判断；上传 stems 时 `vocals` 用 `lead`（**仍按 (L+R)/2 混 mono**，见 vocals 修复），`background` 用 `instrumental`。
2. 模型分发：现在 MDX 走 `GET /api/models/separator/onnx`（token 门控 + Range）。500 MB 的 `.onnx` + `.onnx.data` 要走同一套，两个文件。
3. `config.SEPARATOR_MODEL_FILE` / `separation_capabilities()`：加一个模型选项，或直接替换。建议**保留 MDX 作回退**（集显可能跑不动 1.2 GB 显存峰值——5070 Ti 实测，手机/集显未测）。
4. 下载体积提示：64 MB → 241 MB，落地页文案要改。

### 复现步骤

```bash
# 1. 模型（见上）
# 2. 测试素材 → wav（headless Chromium 解不了 mp4 容器）
ffmpeg -i <video> -vn -ac 2 -ar 44100 -c:a pcm_s16le frontend/public/samples/test.wav
# 3. Roformer：起 vite，开 frontend/roformer-spike.html（需 node putserver.mjs 收输出到 /tmp/sep-out/）
# 4. MDX：ffmpeg -i test.wav -f f32le -ac 2 -ar 44100 /tmp/sample.f32
#         cd frontend && cp ../docs/spikes/roformer-separation/mdx_headless.mjs ./_r.mjs && node _r.mjs
# 5. 量化：backend/.venv/bin/python docs/spikes/roformer-separation/compare.py
```

## 本目录文件

- `spike.html` — 浏览器跑 Roformer 的最小页（从 `frontend/roformer-spike.html` 复制；放回 frontend 才能用 Vite 的 import）
- `mdx_headless.mjs` — Node 跑 MDX（对比基准）
- `putserver.mjs` — 收 spike 输出 wav 的小服务器（9312）
- `compare.py` — 量化对比

## 相关背景

- musetric 的 STFT/iSTFT/FFT 全在 WGSL 里（GPU 做），这是它快的原因；ORT 只跑网络核心，输入输出是 `stft_repr [1,2050,1100,2]` 和 mask。
- `@musetric/ai` 源码：github.com/musetric/musetric `packages/ai/src/runtime/vocals/`（4 个 WGSL 共 ~150 行）+ `stftInference.ts`（294 行）。要自己移植的话就这些。
- 之前的分离音质分析：背景轨在成片里 duck −10~−20 dB；vocals 只喂 ASR/克隆，不进成片。
