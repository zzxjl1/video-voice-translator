/**
 * 预览的音量：和导出用同一组数，但浏览器的 `HTMLMediaElement.volume` 只能 0..1，放大不了。
 *
 * 配音的增益、背景的增益都来自混音计划。其中大于 1 的（配音比原片这一句轻，要抬）在预览里放不出来，
 * 所以整体除以"最大的那个增益"，**各路之间的比例和导出完全一致**，只是整体音量低一点。
 * （导出还有一道母带：整条混音统一抬到目标响度。预览不做那一步 —— 它是一个全局的数，
 * 要渲染完整混音才量得出来，不在播放时算。所以预览和成片的差别只有一个整体音量。）
 */
import type { MixPlan } from '../net/types';

const clamp01 = (v: number) => Math.max(0, Math.min(1, v));

/** 整体缩放：把所有增益里最大的压到 1。没有大于 1 的就是 1（不放大）。 */
export function previewScale(plan: MixPlan | null): number {
  if (!plan) return 1;
  let top = Math.max(1, plan.bed?.gain ?? 1);
  for (const e of plan.segments ?? []) if (typeof e.gain === 'number' && e.gain > top) top = e.gain;
  return 1 / top;
}

/** 一句配音的预览音量：它自己的增益 × 整体缩放，夹到 0..1。 */
export function entryVolume(entry: { gain?: number } | undefined, scale: number): number {
  return clamp01((entry?.gain ?? 1) * scale);
}
