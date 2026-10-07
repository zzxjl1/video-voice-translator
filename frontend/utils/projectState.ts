import type { Speaker, TranscriptionSegment } from '../types';
import { getSpeakerColor } from './helpers';

/**
 * 服务端状态 → 界面状态 的转换，集中一处。
 *
 * 以前这些转换散在 App.tsx 的 5 个地方（会话恢复两份、上传"已存在"分支、分段
 * 重建……），字段少一个、名字写错一个都不会报错 —— 只是那一行的音频/静音/隐藏
 * 标记静默丢失。抽出来既去重，也能被单独验证。
 */

/** 后端阶段名 → 界面说法。与 pipeline_service 的 phase 名一一对应。 */
export const RUN_STEP_LABEL: Record<string, string> = {
  separation: '人声分离',
  asr: '语音识别',
  mm_enhance: '多模态增强（omni 正在听）',
  translation: '翻译',
  voice_clone: '声音复刻',
  tts: '语音合成',
};

/** 一个服务端分段 → 界面分段。 */
export function mapServerSegment(seg: any): TranscriptionSegment {
  return {
    id: seg.id,
    speakerId: seg.speaker_label,
    startTime: seg.start_time,
    endTime: seg.end_time,
    originalText: seg.text,
    translatedText: seg.translated_text || '',
    audioUrl: seg.audio_url || undefined,
    muted: (seg as { muted?: boolean }).muted ?? false,
    hidden: (seg as { hidden?: boolean }).hidden ?? false,
    status: (seg.translated_text ? 'ready' : 'pending') as TranscriptionSegment['status'],
  };
}

/** 服务端 speakers → 界面 Speaker（补上必填的颜色字段）。 */
export function mapServerSpeakers(spks: any[]): Speaker[] {
  return (spks ?? []).map(s => ({
    id: s.id,
    name: s.name,
    color: getSpeakerColor(s.id),
  }));
}

/**
 * 服务端没给说话人列表时，从分段里反推去重。
 *
 * `color` 是 Speaker 的必填字段：缺了它界面拿到 undefined 会渲染成透明/灰色。
 */
export function deriveSpeakers(segments: TranscriptionSegment[]): Speaker[] {
  const labels = Array.from(new Set(segments.map(s => s.speakerId)));
  return labels.map(label => ({
    id: label,
    name: label,
    color: getSpeakerColor(label),
  }));
}

/**
 * "这个工程上次没跑完"的说明文案；**已完成返回 null**（没什么要问的）。
 *
 * 未完成状态各有各的说法 —— 用户需要知道上次是失败了、被自己取消了，还是压根
 * 没开始，因为这三件事的下一步不一样。
 *
 * 【刻意不显示停在哪一步 / 内部状态名】：`cancelled_step` 是阶段名、`status` 是
 * 状态枚举（translated / transcribed / …），两者都是内部词汇。曾经这里会显示
 * "上次中断在：translated" —— 用户不知道那是什么，也无法据此做任何决定。要说明
 * 的只有"为什么需要你选下一步"，具体断点由服务器自己恢复时用，不必给人看。
 */
export function resumeOfferNote(data: any): string | null {
  if (data?.status === 'completed') return null;
  if (data?.status === 'error') return `上次处理失败：${data.error || '未知错误'}`;
  if (data?.status === 'uploaded') return '这个工程还没开始处理';
  if (data?.status === 'cancelled') return '上次已取消';
  return '上次没有跑完';
}
