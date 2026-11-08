/**
 * 项目状态：**唯一**的前端状态源，也是纯函数 —— 不碰 React、不碰网络。
 *
 *   state = applyPatch(state, patch)        增量应用（服务端 view.diff 算出来的）
 *   state = applyChange(state, event)       一条 change 事件：判断连续性，再应用
 *
 * 为什么前端不再有"自己算状态"的逻辑：旧版有 53 个 useState，本地维护服务器状态的副本，
 * 靠"先改本地、失败再回滚"去保持一致，单行重配音之后混音速率不刷新就是这么来的。
 * 现在的规则只有一条：**状态只由服务端推来的增量改变**。UI 的按钮发命令，AI 的动作也
 * 是命令，两者产生的变化沿着同一条事件流回来，所以同步是结构性的。
 *
 * 连续性：每个 change 事件带 `rev`。收到的 rev 恰好是 当前+1 才直接应用；
 * 否则说明漏了事件（断线、服务端重启），返回 `needsResync`，调用方整份重取。
 */
import type { ChangeEvent, Line, ProjectView, ViewPatch } from '../net/types';

export type ApplyResult =
  | { kind: 'applied'; state: ProjectView; changed: string[] }
  | { kind: 'stale' }                        // rev 已经见过：重复事件，忽略
  | { kind: 'resync' };                      // 漏了：需要整份重取

export function applyPatch(state: ProjectView, patch: ViewPatch): ProjectView {
  const next: ProjectView = { ...state };

  if (patch.segments) {
    const { upsert, remove, order } = patch.segments;
    const byId = new Map<string, Line>(state.segments.map((s) => [s.id, s]));
    for (const id of remove) byId.delete(id);
    for (const line of upsert) byId.set(line.id, line);

    if (order) {
      // 顺序（含"第 N 句"的编号）变了：按服务端给的整张 id 表重排
      next.segments = order.map((id) => byId.get(id)).filter((s): s is Line => !!s);
    } else {
      // 顺序没变：保持原顺序，新增的（理论上不会发生，因为新增必改顺序）追加在后
      const seen = new Set<string>();
      const kept = state.segments
        .filter((s) => byId.has(s.id))
        .map((s) => { seen.add(s.id); return byId.get(s.id)!; });
      next.segments = kept.concat(upsert.filter((s) => !seen.has(s.id)));
    }
  }

  // 其余顶层块：整块替换
  const blocks: (keyof ViewPatch)[] = [
    'stage', 'initError', 'initReport', 'settings', 'speakers', 'subtitle', 'needs', 'hasStems', 'filename',
  ];
  for (const key of blocks) {
    if (key in patch) (next as unknown as Record<string, unknown>)[key] = patch[key];
  }
  if ('mix' in patch) next.mix = patch.mix ?? null;
  return next;
}

/** 受影响的行 id（给界面做"刚被改过"的高亮）。 */
export function changedIds(event: ChangeEvent): string[] {
  return event.ids ?? [];
}

export function applyChange(state: ProjectView, event: ChangeEvent): ApplyResult {
  if (event.rev <= state.rev) return { kind: 'stale' };
  if (event.rev !== state.rev + 1) return { kind: 'resync' };
  const next = applyPatch(state, event.patch);
  next.rev = event.rev;
  return { kind: 'applied', state: next, changed: changedIds(event) };
}

// ---- 派生值（纯函数，界面直接用，不存） ---------------------------------------

export function speakerName(state: ProjectView, speakerId: string): string {
  return state.speakers.find((s) => s.id === speakerId)?.name ?? `Speaker ${speakerId}`;
}

const PALETTE = ['#DA7756', '#5B8DEF', '#3FA796', '#B06AB3', '#E0A030', '#6B7280', '#D1587A', '#4F9DA6'];

/** 说话人的颜色：按说话人在列表里的位置定，稳定且互不相同。 */
export function speakerColor(state: ProjectView, speakerId: string): string {
  const index = state.speakers.findIndex((s) => s.id === speakerId);
  return PALETTE[(index < 0 ? 0 : index) % PALETTE.length];
}

/** 这一刻的"当前句"：播放头所在的那一句（不含非语音）。 */
export function lineAt(state: ProjectView, t: number): Line | null {
  return state.segments.find((s) => t >= s.start && t < s.end) ?? null;
}

export function summarize(state: ProjectView) {
  const lines = state.segments;
  return {
    total: lines.length,
    translated: lines.filter((l) => l.trStatus === 'fresh').length,
    dubbed: lines.filter((l) => l.ttsStatus === 'fresh').length,
    needsTranslate: state.needs.translate,
    needsDub: state.needs.dub,
    suspect: lines.filter((l) => l.trSuspect).length,
  };
}
