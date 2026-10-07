import type { CSSProperties } from 'react';
import type { SubtitleStyle } from '../services/apiService';

/**
 * One style, three renderers.
 *
 * The in-app overlay draws with CSS, the burn-in draws with Canvas2D, and the
 * server writes ASS — and all three have to look the same. So every size here
 * is derived from the same percentage-of-frame-height numbers the server uses;
 * anything absolute would break the agreement at the first resolution change.
 *
 * Reference frame height for the overlay is the VIDEO's displayed height, not
 * the container's: the video is letterboxed inside its box, and measuring
 * against the container would push the subtitles into the black bars whenever
 * the aspect ratios differ.
 */

export interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/*
 * ---------------------------------------------------------------------------
 * 标签（给 TTS 的指令，不是给观众看的字）
 * ---------------------------------------------------------------------------
 * 「多模态增强识别」会给每行打控制类标签（[sad] [whispers]…，见
 * config.EMOTION_CONTROL_TAGS）与音效类标签（[gasp] [laughing]…）；翻译时被要求
 * 原样保留，TTS 靠它们加情绪。它们**必须**跟着译文走，但**不该**出现在字幕上 ——
 * 观众看到的是 "[sarcastic] 我吃很多米饭"，不是台词的语气。
 *
 * 形状有两种：半角 `[tag]` 是配置里的规范写法；模型在中文提示下还会自作主张写成
 * 全角 `【讽刺】`。两种都要认。
 *
 * 只匹配"像标签"的括号内容，避免误伤正文：
 *   · 半角：以字母开头，只含字母/空格/连字符/撇号，≤ 24 字符
 *     → `[1]`、`[注]`、`[500]` 这类不会被删
 *   · 全角：内容 ≤ 8 字符且不含标点/换行（模型输出如 `【讽刺】`、`【laughing】`）
 */
const TAG_SOURCE = String.raw`\[[A-Za-z][A-Za-z '\-]{0,23}\]|【[^】\n，。！？；：、,.!?;:]{1,8}】`;
const TAG_RE = new RegExp(TAG_SOURCE, 'g');
/**
 * 光标左侧正好一个标签（不带 g，供"退格整块删"判断）。
 *
 * 允许尾随一个空格：模型输出的形状就是 `[sad] 台词`，退格时把这个分隔空格一起
 * 吃掉才叫"整块删"；行内标签吃掉尾随空格也不会破坏词间距，因为前面那个空格还在。
 */
export const SUBTITLE_TAG_RE = new RegExp(`(?:${TAG_SOURCE}) ?$`);

/** 文本里有没有标签（避免给无标签的行做多余的渲染工作）。 */
export function hasSubtitleTags(text: string): boolean {
  return Boolean(text) && (text.includes('[') || text.includes('【'));
}

/**
 * 去掉标签，供**字幕渲染**用（覆盖层、烧录、SRT/ASS 同一条规则）。
 *
 * 顺带收拾留下的空格：行首标签去掉后不留前导空格，行中标签去掉后不出现双空格。
 * 编辑器里的文本【不】经过这里 —— 那里是用户改标签的地方，必须原样保留。
 */
export function stripSubtitleTags(text: string): string {
  if (!hasSubtitleTags(text)) return text;
  return text
    .replace(TAG_RE, '')
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/^[ \t]+|[ \t]+$/g, '');
}

/** 切分成 普通文本 / 标签 交替的片段，供侧边栏把标签画成色块。 */
export function splitSubtitleTags(text: string): { text: string; isTag: boolean }[] {
  if (!hasSubtitleTags(text)) return [{ text, isTag: false }];
  const parts: { text: string; isTag: boolean }[] = [];
  let last = 0;
  for (const match of text.matchAll(new RegExp(TAG_SOURCE, 'g'))) {
    const start = match.index ?? 0;
    if (start > last) parts.push({ text: text.slice(last, start), isTag: false });
    parts.push({ text: match[0], isTag: true });
    last = start + match[0].length;
  }
  if (last < text.length) parts.push({ text: text.slice(last), isTag: false });
  return parts;
}

/**
 * Where a video of `videoW`x`videoH` actually lands inside a container of
 * `containerW`x`containerH` under `object-fit: contain`.
 *
 * Returns null until the video's intrinsic size is known.
 */
export function containRect(
  containerW: number,
  containerH: number,
  videoW: number,
  videoH: number,
): Rect | null {
  if (!containerW || !containerH || !videoW || !videoH) return null;
  const scale = Math.min(containerW / videoW, containerH / videoH);
  const width = videoW * scale;
  const height = videoH * scale;
  return {
    left: (containerW - width) / 2,
    top: (containerH - height) / 2,
    width,
    height,
  };
}

/** Percentage-derived pixel sizes for a frame of the given size. */
export function resolveMetrics(style: SubtitleStyle, width: number, height: number) {
  return {
    fontSize: Math.max(6, (height * style.font_size_percent) / 100),
    marginV: (height * style.margin_v_percent) / 100,
    marginH: (width * style.margin_h_percent) / 100,
    outline: (height * style.outline_width) / 1000,
    shadow: (height * style.shadow) / 1000,
  };
}

/**
 * CSS for the subtitle overlay placed over the video's displayed rect.
 *
 * The overlay is a full-frame flex box anchored to the top/centre/bottom edge,
 * which is how the ASS `Alignment` + `MarginV` pair behaves — the same two
 * values drive both, so the preview cannot disagree with the export.
 */
export function overlayContainerStyle(style: SubtitleStyle, rect: Rect): CSSProperties {
  const base: CSSProperties = {
    position: 'absolute',
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height,
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    pointerEvents: 'none',
    zIndex: 10,
    // Padding stands in for MarginH/MarginV so long lines never reach the edge.
    padding: `${rect.height * style.margin_v_percent / 100}px ${rect.width * style.margin_h_percent / 100}px`,
    boxSizing: 'border-box',
  };
  if (style.alignment === 'top') return { ...base, justifyContent: 'flex-start' };
  if (style.alignment === 'center') return { ...base, justifyContent: 'center' };
  return { ...base, justifyContent: 'flex-end' };
}

/** CSS for one line of subtitle text. */
export function overlayLineStyle(style: SubtitleStyle, rect: Rect): CSSProperties {
  const metrics = resolveMetrics(style, rect.width, rect.height);
  const px = (v: number) => `${v.toFixed(1)}px`;
  return {
    fontSize: px(metrics.fontSize),
    fontWeight: style.bold ? 700 : 400,
    /*
     * Same chain the burn-in uses (`ctx.font = … ${font_family}, sans-serif`),
     * so the preview and the burned file fail over the same way.
     *
     * Caveat worth knowing: the ASS track names ONE font and lets the player
     * resolve it, while this resolves against whatever is installed HERE. If
     * the named font is missing locally the preview falls back — which is
     * exactly what a viewer without that font will also see, so it is not a
     * lie, just not a promise.
     */
    fontFamily: `${style.font_family}, sans-serif`,
    color: style.primary_color,
    textAlign: 'center',
    lineHeight: 1.35,
    maxWidth: '100%',
    // The outline is drawn with text-shadow rather than -webkit-text-stroke so
    // it stays behind the glyphs and keeps the same silhouette as libass.
    textShadow: buildTextShadow(style, metrics.outline, metrics.shadow),
    ...(style.background === 'box'
      ? {
          backgroundColor: 'rgba(0, 0, 0, 0.5)',
          padding: `${(metrics.fontSize * 0.18).toFixed(1)}px ${(metrics.fontSize * 0.4).toFixed(1)}px`,
          borderRadius: px(metrics.fontSize * 0.18),
        }
      : null),
  };
}

function buildTextShadow(style: SubtitleStyle, outline: number, shadow: number): string {
  const layers: string[] = [];
  if (outline > 0) {
    const o = Math.max(0.5, outline);
    // Eight directions gives a closed outline, which is what keeps light text
    // readable over light footage — the same reason ASS draws an outline.
    for (let i = 0; i < 8; i += 1) {
      const angle = (Math.PI * 2 * i) / 8;
      layers.push(
        `${(Math.cos(angle) * o).toFixed(2)}px ${(Math.sin(angle) * o).toFixed(2)}px 0 ${style.outline_color}`,
      );
    }
  }
  if (shadow > 0) {
    layers.push(`0 ${Math.max(1, shadow).toFixed(1)}px ${(shadow * 2).toFixed(1)}px rgba(0,0,0,0.75)`);
  }
  return layers.join(', ') || 'none';
}

/**
 * Draw one cue onto a canvas sized `width`x`height`.
 *
 * Used by the browser burn-in. Deliberately mirrors `overlayContainerStyle`:
 * same alignment switch, same percentage maths, same outline idea — so the
 * burned result matches the preview the user approved.
 */
export function drawCueOnCanvas(
  ctx: CanvasRenderingContext2D,
  lines: string[],
  style: SubtitleStyle,
  width: number,
  height: number,
): void {
  if (!lines.length) return;
  const metrics = resolveMetrics(style, width, height);
  const lineHeight = metrics.fontSize * 1.35;
  const blockHeight = lineHeight * lines.length;

  let blockTop: number;
  if (style.alignment === 'top') blockTop = metrics.marginV;
  else if (style.alignment === 'center') blockTop = (height - blockHeight) / 2;
  else blockTop = height - metrics.marginV - blockHeight;

  ctx.save();
  ctx.font = `${style.bold ? 'bold ' : ''}${metrics.fontSize}px ${style.font_family}, sans-serif`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';

  const maxWidth = width - metrics.marginH * 2;
  const centreX = width / 2;

  lines.forEach((line, index) => {
    const y = blockTop + index * lineHeight;

    if (style.background === 'box') {
      const textWidth = Math.min(ctx.measureText(line).width, maxWidth);
      const padX = metrics.fontSize * 0.4;
      const padY = metrics.fontSize * 0.18;
      ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';
      ctx.fillRect(
        centreX - textWidth / 2 - padX,
        y - padY,
        textWidth + padX * 2,
        metrics.fontSize + padY * 2,
      );
    }

    if (metrics.outline > 0) {
      const o = Math.max(0.5, metrics.outline);
      ctx.strokeStyle = style.outline_color;
      ctx.lineWidth = o * 2;
      ctx.lineJoin = 'round';
      ctx.miterLimit = 2;
      ctx.strokeText(line, centreX, y, maxWidth);
    }
    if (metrics.shadow > 0) {
      ctx.shadowColor = 'rgba(0,0,0,0.75)';
      ctx.shadowBlur = metrics.shadow * 2;
      ctx.shadowOffsetY = Math.max(1, metrics.shadow);
    } else {
      ctx.shadowColor = 'transparent';
      ctx.shadowBlur = 0;
      ctx.shadowOffsetY = 0;
    }
    ctx.fillStyle = style.primary_color;
    ctx.fillText(line, centreX, y, maxWidth);
    ctx.shadowColor = 'transparent';
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
  });

  ctx.restore();
}

/** The cue covering `time`, or null. Mirrors what the overlay shows. */
export function cueAtTime<T extends { start: number; end: number }>(
  cues: T[],
  time: number,
): T | null {
  for (const cue of cues) {
    if (time >= cue.start && time < cue.end) return cue;
  }
  return null;
}
