/**
 * 时间线的布局与取色：纯函数，不依赖 React / DOM，所以能在 node 里直接测。
 *
 * 时间线要回答三个问题：这一格是谁说的、它是不是台词、同时有几个人在说。
 *   · 台词用说话人的颜色（实心）；
 *   · 非台词（音乐 / 噪音 / 人声非语言）用各自的颜色 + 图标，并且是"描边"风格，和实心的台词一眼能分开；
 *     只有"导出时真的会保留这段原声"的人声非语言用实心 —— 它是唯一会出现在成片里的非台词；
 *   · 时间上重叠的行（两个人同时说话）分到不同的行（lane）里画，不叠在一起。
 */
import type { SegmentKind } from '../net/types';

export interface TimelineSegment {
  id: string;
  speakerId: string;
  start: number;
  end: number;
  kind: SegmentKind;
  muted: boolean;
  hidden: boolean;
  text: string;
  translation: string;
  /** 导出时会把这一段原声放回去：kind=vocal、没静音、做过人声分离（有预览地址就是这三个条件都满足）。 */
  kept: boolean;
}

export interface TimelineSpeaker {
  id: string;
  name: string;
}

/** 最多画几行。再多的重叠挤进最后一行（几乎不会出现，出现了也不该把时间线撑得很高）。 */
export const MAX_LANES = 3;
/** 重叠不足这么多秒不算重叠（ASR 的边界本来就有几十毫秒的误差）。 */
export const OVERLAP_EPS_S = 0.05;
/** 块宽到多少像素才画图标；再窄图标会被切成一个怪异的碎片。 */
export const ICON_MIN_PX = 14;
/** 块的最小像素宽度：一句 0.3 秒的咳嗽在 10 分钟的视频里只有 1 像素，鼠标根本指不到。 */
export const MIN_SPEECH_PX = 3;
export const MIN_NONSPEECH_PX = 7;

export interface KindMeta {
  label: string;
  /** 一句话说明，给 tooltip 用。 */
  hint: string;
  color: string;
}

/** 非台词的种类。颜色和脚本面板里的标签同属紫色系，但三种互相能分开。 */
export const KIND_META: Record<Exclude<SegmentKind, 'speech'>, KindMeta> = {
  music: { label: '音乐', hint: '不翻译、不配音、不出字幕；声音本来就在背景里', color: '#8b5cf6' },
  noise: { label: '噪音', hint: '不翻译、不配音、不出字幕；声音本来就在背景里', color: '#64748b' },
  vocal: { label: '人声非语言', hint: '笑、叹气、咳嗽等；不配音、不出字幕', color: '#db2777' },
};

export function isSpeech(seg: { kind: SegmentKind }): boolean {
  return seg.kind === 'speech';
}

/**
 * 给每个片段分一行：时间上互相重叠的放不同行。
 * 返回和输入同序的行号（0 起）以及总行数（至少 1，至多 MAX_LANES）。
 * 贪心：按开始时间排，放进第一个"已经结束"的行；都占着就开新行，开满了就放进最早结束的那一行。
 */
export function assignLanes(
  segs: ReadonlyArray<{ start: number; end: number }>,
): { lanes: number[]; count: number } {
  const lanes = new Array<number>(segs.length).fill(0);
  const order = segs.map((_, i) => i).sort((a, b) => segs[a].start - segs[b].start || segs[a].end - segs[b].end);
  const ends: number[] = [];
  for (const i of order) {
    const s = segs[i];
    let lane = ends.findIndex((e) => e <= s.start + OVERLAP_EPS_S);
    if (lane < 0) {
      if (ends.length < MAX_LANES) {
        lane = ends.length;
        ends.push(s.end);
      } else {
        lane = ends.indexOf(Math.min(...ends));
        ends[lane] = Math.max(ends[lane], s.end);
      }
    } else {
      ends[lane] = s.end;
    }
    lanes[i] = lane;
  }
  return { lanes, count: Math.max(1, ends.length) };
}

/**
 * 块在时间线上的位置（百分比）。保证最小像素宽度，并且不会伸出右边界。
 * `widthPx` 还没量到（0）时只给按比例算的宽度。
 */
export function blockGeometry(
  start: number,
  end: number,
  duration: number,
  widthPx: number,
  minPx: number,
): { left: number; width: number } {
  if (!(duration > 0)) return { left: 0, width: 0 };
  const s = Math.max(0, Math.min(duration, start));
  const e = Math.max(s, Math.min(duration, end));
  const minPct = widthPx > 0 ? (minPx / widthPx) * 100 : 0;
  const width = Math.max(((e - s) / duration) * 100, minPct);
  const left = Math.min((s / duration) * 100, Math.max(0, 100 - width));
  return { left, width };
}

export function minPxFor(seg: { kind: SegmentKind }): number {
  return isSpeech(seg) ? MIN_SPEECH_PX : MIN_NONSPEECH_PX;
}

/** 块宽（像素）够不够画图标。 */
export function showIcon(blockWidthPx: number): boolean {
  return blockWidthPx >= ICON_MIN_PX;
}

/** 块的主色：台词 = 说话人，非台词 = 它那一类的颜色。 */
export function colorOfSegment(seg: TimelineSegment, speakerColor: (speakerId: string) => string): string {
  return isSpeech(seg) ? speakerColor(seg.speakerId) : KIND_META[seg.kind as keyof typeof KIND_META]?.color ?? '#9ca3af';
}

/** 波形上每根柱子的颜色：这一刻落在哪个片段里就用它的颜色，没有就是灰。 */
export function barColors(
  count: number,
  duration: number,
  segs: ReadonlyArray<TimelineSegment>,
  speakerColor: (speakerId: string) => string,
  fallback = '#d1d5db',
): string[] {
  if (!count || !(duration > 0)) return [];
  const out = new Array<string>(count);
  for (let i = 0; i < count; i++) {
    const t = (i / count) * duration;
    const seg = segs.find((s) => t >= s.start && t <= s.end);
    out[i] = seg ? colorOfSegment(seg, speakerColor) : fallback;
  }
  return out;
}

/** tooltip 里的时间：一位小数（咳嗽、笑声只有零点几秒，整秒会显示成 "00:12 – 00:12"）。 */
export function formatSpan(start: number, end: number): string {
  // 先整体取整到 0.1 秒再拆分钟/秒：分开取整的话 59.96 秒会写成 "00:60.0"。
  const f = (t: number) => {
    const tenths = Math.max(0, Math.round(t * 10));
    const m = Math.floor(tenths / 600);
    const s = (tenths % 600) / 10;
    return `${String(m).padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`;
  };
  return `${f(start)} – ${f(end)}`;
}

/** 这个片段在 tooltip 里要带的小标签。 */
export function segmentFlags(seg: TimelineSegment): string[] {
  const out: string[] = [];
  if (seg.kind === 'vocal') {
    out.push(seg.kept ? '导出时保留这段原声' : seg.muted ? '已静音：导出时不放' : '没做人声分离：原声本来就在背景里');
  }
  if (seg.muted && seg.kind !== 'vocal') out.push('已静音');
  if (seg.hidden) out.push('字幕隐藏');
  return out;
}
