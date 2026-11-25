# 字幕：断行、时间轴、样式、预设

对齐专业字幕软件（Netflix TTSG / Aegisub / 剪映专业版）的做法。服务端 `services/subtitle_service.py`
是权威：预览叠加、烧录、SRT、ASS 全部从 `build_cues()` 出来；前端 `utils/subtitleStyle.ts` 只负责把
同一套数字画出来。

## 断行（`services/subtitle_wrap.py`）

以前按显示宽度贪心切，会在词中间断、行首出现"了/的"。现在用动态规划在**断点**处切：

- **断点代价**：标点后 > 连词/介词前 > 词间 > CJK 逐字。行首不能是收尾标点或中文虚词
  （了/的/着/过/吗/呢…），行尾不能是开头标点；数字和单位（`450名`）不断开。
- **中文词边界**：`jieba` 分词（按需加载，只在断 CJK 文本时初始化；41 MB 词典，0.25s）。
  分词只用来**惩罚**在词中间断，分错的词不会造成硬约束。
- **两行平衡（金字塔）**：两行时优先选让两行长度接近的合法断点，不是第一行塞满。
- **`max_chars_per_line` 的单位没变**：仍是 CJK 格（1 格 = 1 汉字 = 2 拉丁字符）。18 格 = 18 汉字 = 36 字母。
  老工程的设置不受影响。内部按 Netflix TTSG 的宽度（拉丁 1、CJK 2）计算。

## 时间轴（`_apply_timing_rules`）

所有 cue 生成之后统一过一遍，顺序有讲究（先切再延伸，gap 在延伸之后，帧对齐最后）：

| 规则 | 字段 | 默认 | 说明 |
|---|---|---|---|
| 最短时长 | `min_duration` | 0.8s | 一闪而过 |
| **最长时长** | `max_duration` | 7s | 停太久观众走神；超过按行切成连续几条 |
| **阅读速度** | `max_cps` | 9 字/秒 | 超了按行切，每条的时间够读完。**只算译文**：双语里原文是参考，不该因为英文长就把中文切了 |
| **说完整留** | `extend_into_silence` | 0.5s | 话音一落字就没了很突兀；往后面的静音延伸，不超下一条 − gap，不超 max_duration |
| **条间间隔** | `min_gap` | 0.042s（≈1 帧@24fps） | 相邻两条不紧贴，切换才看得出来 |
| **帧对齐** | `fps` | 0（不对齐） | 起止取整到帧边界，不会在两帧之间切换；按视频帧率选 |

同一个源 cue 切出来的碎片共用 `group`：**不加 gap、不延伸**，无缝衔接（一句歌词/一句长译文切开，
中间不该闪一下空白）。

## 双语字幕

- 译文和原文**各自**最多 `max_lines` 行（一块最多 2×N 行），不是两种语言加起来 N 行。
- 原文行在译文行之后，**单独样式**：字号 82%、浅灰（`secondary_color` 可改）。
- ASS 里用两个 Style（`Default` / `Original`），行内 `{\rOriginal}` 切换。预览和烧录靠 `secondary_count`
  知道末尾几行是原文。

## 样式

`SubtitleStyle` 里和外观有关的全部进 ASS 的 `[V4+ Styles]`：

| 字段 | ASS | 备注 |
|---|---|---|
| `font_family` | Fontname | ASS Fontname 字段不能含逗号，写不了字体栈；libass 找不到时按 fontconfig 回退 |
| `font_size_percent` | Fontsize | 按视频高度百分比换算成 PlayRes 像素 |
| `primary_color` / `secondary_color` | PrimaryColour / SecondaryColour | |
| `outline_color` / `outline_width` / `shadow` | OutlineColour / Outline / Shadow | 千分比高度 |
| `bold` / `italic` | Bold / Italic | |
| `blur` | 行内 `{\blurN}` | 边缘柔化，代替硬描边；预览用 CSS `filter: blur()`，烧录用 `ctx.filter` |
| `spacing` | Spacing | 字间距；预览 `letter-spacing`，烧录 `ctx.letterSpacing` |
| `background: box` | 单独的 `Box` 样式（Layer 0） | ASS 一个样式只有一种边框色，底框和描边不能共存，所以分两层画 |

正文的 `{` `}` 一律转义成括号——ASS 里它们是覆盖标签边界，不转义等于让译文注入样式。

**字体可用性**：面板用 `document.fonts.check()` 探测本机有没有，下拉里标「本机没有」。预览和烧录
（Canvas2D）走 `fontStack()`：首选 + 同类回退（黑体→黑体）。注意 `Source Han Sans` 在 Mac/Win 都不预装，
大多数机器上看到的是回退效果。烧录是浏览器 Canvas 自己画，不是 libass——预览和烧录之间**是**一致的，
不一致的是 MKV/ASS 轨在观众机器上的解析（那是 ASS 格式的限制）。

## 预设（`services/subtitle_presets.py`）

一键到合规，选完仍可微调。预设**不动 `track`**。

| 预设 | 行宽 | 最短 | 最长 | 阅读速度 | 风格 |
|---|---|---|---|---|---|
| 默认 | 18 格 | 0.8s | 7s | 9 | 半透明底、粗体 |
| **Netflix** | 21 格（42 拉丁） | 5/6s | 7s | 8.5（17 拉丁 cps） | 无底框、细描边、正体 |
| YouTube | 20 格 | 0.8s | 6s | 9.5 | 无底框、粗黑边、字大 |
| 剪映综艺 | 16 格 | 0.8s | 5s | 9 | 大字、粗边、半透明底、位置高 |
| 广播/BBC | 19 格 | 1.0s | 7s | 7.5（≈160 wpm） | 底框、正体、节奏保守 |

命令：`apply_subtitle_preset(preset)`（AI 和面板共用）；`get_subtitle_style` 返回预设清单。

## 调试

```bash
cd backend && ./.venv/bin/python tests/test_subtitles.py     # 43 项 golden
```

**踩过的坑**：
- 把辅助函数插在 `@router.post` 和 handler 之间 → 装饰器套到了辅助函数上，预览接口 422。
  新加 helper 放装饰器**上面**。
- `problem_video/speech_stem.flac` 是合成的，不是任何模型的输出——别拿它当分离质量的基准。
- 双语阅读速度按"译文+原文"算会把合规的译文切掉（一条 2.36s 的字幕，中文 5.9 cps 合规，加英文就 18）。
