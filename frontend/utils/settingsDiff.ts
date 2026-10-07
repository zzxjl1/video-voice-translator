/**
 * 当前页面上的设置 与 工程里存的设置 的差异。
 *
 * 抽成独立模块而不是留在 App.tsx 里，是因为它决定一件有破坏性的事：差异非空
 * 时上传同一视频会**清空旧进度重跑**。这种判断值得能被单独验证，而不是埋在
 * 一个 3000 行的组件里靠人读。
 *
 * 只比"会改变处理结果"的字段 —— 换语言要重翻、换分离方式要重分离、换口音要
 * 重配配音。纯界面项（字幕样式等）不影响已经做出来的东西，不参与比较。
 *
 * 未设过的字段（工程还没跑过、或用户没选）按空值处理：两边都空即视为一致，
 * 否则一个刚上传、什么都没配的工程会被判成"设置不同"而反复触发清空重跑。
 */

export interface CurrentSettings {
  targetLanguage: string;
  targetAccent: string;
  separationMode: string;
  enableVoiceClone: boolean;
  mmEnhance: boolean;
  cloneSmartPick: boolean;
  customPrompt: string;
}

/** 分离方式的人话名（给用户看差异列表用）。 */
const SEPARATION_LABELS: Record<string, string> = {
  client: '浏览器分离',
  off: '不分离',
};

/**
 * 差异列表；**空数组 = 完全一样**。顺序固定，方便比对与测试。
 */
export function differFromProject(
  project: Record<string, unknown> | null | undefined,
  current: CurrentSettings,
): string[] {
  const p = project ?? {};
  const norm = (v: unknown) => (v ?? '').toString().trim();
  const onOff = (v: unknown) => (v ? '开' : '关');
  const diffs: string[] = [];

  if (norm(p.target_language) !== norm(current.targetLanguage)) {
    diffs.push(
      `语言 ${norm(p.target_language) || '(未设)'} → ${current.targetLanguage || '(未设)'}`
    );
  }
  if (norm(p.accent) !== norm(current.targetAccent)) {
    diffs.push(
      `口音 ${norm(p.accent) || '普通话'} → ${current.targetAccent || '普通话'}`
    );
  }
  if (norm(p.separation_mode) !== norm(current.separationMode)) {
    const before = SEPARATION_LABELS[norm(p.separation_mode)] ?? (norm(p.separation_mode) || '(未设)');
    const after = SEPARATION_LABELS[current.separationMode] ?? current.separationMode;
    diffs.push(`分离方式 ${before} → ${after}`);
  }
  if (Boolean(p.enable_voice_clone) !== current.enableVoiceClone) {
    diffs.push(`声音复刻 ${onOff(p.enable_voice_clone)} → ${onOff(current.enableVoiceClone)}`);
  }
  if (Boolean(p.mm_enhance) !== current.mmEnhance) {
    diffs.push(`多模态增强 ${onOff(p.mm_enhance)} → ${onOff(current.mmEnhance)}`);
  }
  if (Boolean(p.clone_smart_pick) !== current.cloneSmartPick) {
    diffs.push(`智能选材 ${onOff(p.clone_smart_pick)} → ${onOff(current.cloneSmartPick)}`);
  }
  if (norm(p.custom_prompt) !== norm(current.customPrompt)) {
    diffs.push('自定义翻译要求');
  }
  return diffs;
}
