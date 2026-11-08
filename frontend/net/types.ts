/**
 * 与后端 `app/view.py` 字段一一对应的视图类型。
 *
 * 前端不自己算"这句配音是不是过期了"：`trStatus` / `ttsStatus` / `audioUrl` 都是服务端按
 * 指纹算好发来的。这里只描述形状。
 */

export type LineStatus = 'none' | 'missing' | 'stale' | 'fresh';
export type SegmentKind = 'speech' | 'music' | 'noise';
export type Stage = 'uploaded' | 'initializing' | 'ready' | 'failed';

export interface Line {
  id: string;
  /** 1 起的行号：人和 AI 共用的"第 N 句"。插入/拆分/合并后会变。 */
  line: number;
  speakerId: string;
  start: number;
  end: number;
  text: string;
  translation: string;
  reviewedText: string;
  kind: SegmentKind;
  muted: boolean;
  hidden: boolean;
  ttsVoice: string | null;
  ttsRate: number | null;
  ttsInstruction: string | null;
  trStatus: LineStatus;
  /** 译文看起来没被翻译（还是原文的文字）。 */
  trSuspect: boolean;
  ttsStatus: LineStatus;
  /** 仅当配音"新鲜"时才有；带 ?v=<指纹>，被覆盖重写后浏览器缓存自然失效。 */
  audioUrl: string | null;
  audioDuration: number | null;
}

export interface SpeakerView {
  id: string;
  name: string;
  voice: string | null;
  voiceLabel: string | null;
  cloned: boolean;
  lines: number;
}

export interface Settings {
  targetLanguage: string;
  accent: string | null;
  separationMode: string;
  enableVoiceClone: boolean;
  enableDubbing: boolean;
  mmEnhance: boolean;
  cloneSmartPick: boolean;
  customPrompt: string;
}

export interface MixPlanSegment {
  id: string;
  start: number;
  end: number;
  atempo: number;
  videoRate: number;
  trim: number | null;
  played: number;
}

export interface MixPlan {
  version: string;
  policyVersion: number;
  dubbed: boolean;
  bed?: { kind: 'background' | 'original'; gain: number; duck: [number, number][] };
  segments: MixPlanSegment[];
  voiceFadeS?: number;
  master: Record<string, number>;
}

export interface Mix {
  version: string;
  plan: MixPlan;
}

export interface SubtitleStyle {
  track: 'translated' | 'original' | 'bilingual';
  font_family: string;
  font_size_percent: number;
  primary_color: string;
  outline_color: string;
  outline_width: number;
  shadow: number;
  bold: boolean;
  background: 'none' | 'box';
  alignment: 'bottom' | 'center' | 'top';
  margin_v_percent: number;
  margin_h_percent: number;
  max_chars_per_line: number;
  max_lines: number;
  min_duration: number;
}

export interface JobInfo {
  id: string;
  kind: 'init' | 'translate' | 'dub' | 'clone' | 'export';
  label: string;
  actor: string;
  step: string | null;
  done: number;
  total: number;
  cancelling: boolean;
  error?: string | null;
  elapsedS: number;
}

export interface InitReport {
  lines?: number;
  speakers?: number;
  reviewAdjusted?: number;
  failedTranslation?: string[];
  failedDub?: string[];
  error?: string;
  settings?: Settings;
}

export interface Needs {
  translate: number;
  dub: number;
}

export interface ProjectView {
  id: string;
  rev: number;
  seq: number;
  /** 服务端当前会话 id（resync 时也能同步回来）。 */
  chatConv: string;
  filename: string;
  stage: Stage;
  initError: string | null;
  initReport: InitReport;
  settings: Settings;
  speakers: SpeakerView[];
  segments: Line[];
  subtitle: { style: Partial<SubtitleStyle>; export: Record<string, unknown> };
  needs: Needs;
  hasStems: boolean;
  mix: Mix | null;
  jobs: JobInfo[];
  undo: { seq: number; summary: string } | null;
  export: { container: string; sizeMb: number; url: string } | null;
  capabilities: { subtitle: Record<string, { available: boolean; reason?: string | null }>; export: boolean };
}

/** 增量：只含变了的部分（与后端 view.diff 对应）。 */
export interface ViewPatch {
  segments?: { upsert: Line[]; remove: string[]; order: string[] | null };
  stage?: Stage;
  initError?: string | null;
  initReport?: InitReport;
  settings?: Settings;
  speakers?: SpeakerView[];
  subtitle?: ProjectView['subtitle'];
  needs?: Needs;
  hasStems?: boolean;
  filename?: string;
  chatConv?: string;
  mix?: Mix | null;
}

// ----------------------------------------------------------------------------
// 事件
// ----------------------------------------------------------------------------

export type Actor = 'user' | 'ai' | 'system';

export interface ChangeEvent {
  type: 'change';
  seq: number;
  ts: number;
  actor: Actor;
  cmd: string;
  summary: string;
  rev: number;
  ids: string[];
  undoable?: boolean;
  undoes?: number;
  patch: ViewPatch;
}

export interface JobEvent {
  type: 'job';
  seq: number;
  ts: number;
  phase: 'started' | 'done' | 'failed' | 'cancelled';
  job: string;
  kind: JobInfo['kind'];
  label: string;
  actor: Actor;
  error?: string;
  elapsedS?: number;
}

/** 任务里的细粒度进度（只在内存里）：初始化弹窗的日志行就是它们。 */
export interface ProgressEvent {
  type: 'progress';
  seq: number;
  ts: number;
  job?: string;
  kind?: string;
  phase?: string;
  status?: string;
  [key: string]: unknown;
}

export interface UiEvent {
  type: 'ui';
  seq: number;
  ts: number;
  action: 'focus_line' | 'open_panel' | 'browser_action' | 'play_voice_preview' | 'export_ready';
  actor: Actor;
  [key: string]: unknown;
}

export type ChatRole = 'user' | 'assistant' | 'confirm' | 'error';

export interface ChatMessage {
  id: string;
  role: ChatRole;
  ts: number;
  text?: string;
  /** assistant：这一轮做了什么（给人看的一句话）。 */
  actions?: { name: string; label?: string; summary: string }[];
  /** assistant：欢迎消息（系统生成，不是模型）。 */
  system?: boolean;
  // confirm 卡片
  command?: string;
  description?: string;
  args?: Record<string, unknown>;
  level?: 'cost' | 'destroy';
  status?: 'pending' | 'approved' | 'rejected';
}

/** `conv` 是事件所属会话；前端据此过滤掉"切换会话前那一轮"的迟到事件。 */
type ChatEventBase = { type: 'chat'; seq: number; conv?: string };

export type ChatEvent = ChatEventBase & (
  | { kind: 'start'; id: string }
  | { kind: 'delta'; id: string; text: string }
  | { kind: 'message'; message: ChatMessage }
  | { kind: 'tool_call'; id: string; name: string; args: Record<string, unknown> }
  | { kind: 'tool_result'; id: string; name: string; ok: boolean; error: string | null }
  | { kind: 'confirm_resolved'; id: string; approved: boolean }
  | { kind: 'idle' }
);

/** 会话切换（静默写）：不是 change，不进 AI 的改动摘要，但要通知所有标签页。 */
export interface ConversationEvent {
  type: 'conversation';
  seq: number;
  ts?: number;
  conv: string;
}

export type ServerEvent =
  | ChangeEvent | JobEvent | ProgressEvent | UiEvent | ChatEvent | ConversationEvent;

export interface CommandResult<T = Record<string, unknown>> {
  ok: boolean;
  result?: T;
  error?: string;
  hint?: string;
  rev?: number | null;
}

// ----------------------------------------------------------------------------
// 保留的组件（落地页 / 字幕面板 / 导出弹窗）用的共享类型
// ----------------------------------------------------------------------------

export type SubtitleTrack = SubtitleStyle['track'];
export type SubtitleAlignment = SubtitleStyle['alignment'];
export type SubtitleBackground = SubtitleStyle['background'];
export type SubtitleStylePatch = Partial<SubtitleStyle>;
export type SubtitleExportFormat = 'off' | 'soft' | 'styled' | 'burn';

export interface SubtitleExportOptions {
  enabled?: boolean;
  format?: SubtitleExportFormat;
  tracks?: SubtitleTrack[];
  default_track?: SubtitleTrack;
  style?: SubtitleStylePatch;
}

export interface SubtitleCapability {
  available: boolean;
  reason: string | null;
  description: string;
}

/** 能力探测的键与导出计划的 `format` 不是同一组：srt 是单独下载的字幕文件。 */
export type SubtitleDeliveryOption = 'srt' | 'soft' | 'styled' | 'burn';
export type SubtitleCapabilities = Partial<Record<SubtitleDeliveryOption, SubtitleCapability>>;

export interface SubtitleCue {
  start: number;
  end: number;
  text: string;
  secondary: string;
  lines: string[];
}

/** 浏览器里跑的人声分离："client" 或 "off"。服务器不自带分离（没有 GPU）。 */
export type SeparationMode = 'client' | 'off';

export interface BackendAvailability {
  available: boolean;
  reason: string | null;
}

export type SeparationBackends = Partial<Record<SeparationMode, BackendAvailability>>;

export interface VoiceOption {
  id: string;
  label: string;
}

/**
 * 字幕默认样式：只用于服务端数据到达之前的第一帧。数值与 `config.py` 一致。
 * 之后一律以服务端为准（`/subtitles/capabilities` 带 default_style）。
 */
export const DEFAULT_SUBTITLE_STYLE: SubtitleStyle = {
  track: 'translated',
  font_family: 'Source Han Sans',
  font_size_percent: 4.5,
  primary_color: '#FFFFFF',
  outline_color: '#000000',
  outline_width: 2,
  shadow: 1,
  bold: true,
  background: 'box',
  alignment: 'bottom',
  margin_v_percent: 6,
  margin_h_percent: 5,
  max_chars_per_line: 18,
  max_lines: 2,
  min_duration: 0.8,
};

export const srtFilename = (videoId: string, track: SubtitleTrack): string => `${track}_${videoId.slice(0, 8)}.srt`;
