/**
 * API Service - calls the FastAPI backend
 */

export const API_BASE = '/api';

export interface UploadResult {
  video_id: string;
  filename: string;
  exists: boolean;
  status: string;
}

export interface SegmentData {
  id: string;
  speaker_label: string;
  start_time: number;
  end_time: number;
  text: string;
  translated_text?: string;
}

export interface TranslateResult {
  id: string;
  translated_text: string;
}

export interface TTSResult {
  audio_url: string;
  content_type: string;
}

/**
 * Upload a video file to the backend. Returns video_id (MD5).
 */
export async function uploadVideo(file: File): Promise<UploadResult> {
  const formData = new FormData();
  formData.append('file', file);

  const response = await fetch(`${API_BASE}/videos/upload`, {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Upload failed');
  }

  return response.json();
}

/**
 * Trigger transcription via Ali ASR. Returns segments.
 */
export async function transcribeVideo(videoId: string): Promise<SegmentData[]> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/transcribe`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Transcription failed');
  }

  const data = await response.json();
  return data.segments;
}

/**
 * Translate script segments via the backend LLM.
 *
 * `end_time` lets the backend size each line to the time slot it has to fill,
 * which keeps the dubbed audio aligned with the video.
 */
/** 静音 / 取消静音某一行（服务端持久化）。 */
export async function setSegmentMuted(
  videoId: string,
  segmentId: string,
  muted: boolean,
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/videos/${videoId}/segments/${segmentId}/mute`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ muted }),
    },
  );
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Mute failed');
  }
}


/**
 * 隐藏/取消隐藏一行的字幕。
 *
 * 隐藏 = 这一行不产生字幕（实时叠加、SRT、内嵌轨道都不出现它），但配音照常。
 * 与静音互补：静音是不出声，隐藏是不出字。
 */
export async function setSegmentHidden(
  videoId: string,
  segmentId: string,
  hidden: boolean,
): Promise<void> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/segments/${segmentId}/hide`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ hidden }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Failed to update hidden state');
  }
}


export async function translateScript(
  videoId: string,
  segments: { id: string; text: string; speaker_id: string; start_time: number; end_time?: number }[],
  targetLanguage: string = 'English',
  customPrompt: string = '',
  /** 单行重译的长度意图：「长一点」/「短一点」。 */
  lengthHint?: 'longer' | 'shorter',
): Promise<TranslateResult[]> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/translate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      target_language: targetLanguage,
      segments,
      custom_prompt: customPrompt,
      length_hint: lengthHint ?? null,
    }),
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Translation failed');
  }

  const data = await response.json();
  return data.translations;
}

/**
 * Synthesize speech on the backend.
 *
 * `options.targetDuration` makes the backend fit the line to its time slot:
 * it synthesizes, measures with ffprobe and re-synthesizes once at a corrected
 * `speech_rate` if the result misses by more than the tolerance.
 */
export async function synthesizeSpeech(
  videoId: string,
  segmentId: string,
  text: string,
  voice?: string,
  options?: { targetDuration?: number; targetLanguage?: string; accent?: string },
): Promise<TTSResult> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/tts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      segment_id: segmentId,
      text,
      voice,
      target_duration: options?.targetDuration,
      target_language: options?.targetLanguage,
      // Chinese dialect for this line. The backend checks it against its own
      // list and ignores it unless the language is Chinese, so sending it for
      // an English job is harmless rather than something to guard here.
      accent: options?.accent,
    }),
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'TTS failed');
  }

  return response.json();
}

/**
 * Live workflow state of a project, as reported by GET /status.
 *
 * The server owns this: which step the pipeline is in, how far through a
 * batch, and whether a stop has been requested. The client only renders it —
 * that is what makes a reload able to SHOW a run it did not start (and say
 * "already processing", instead of guessing from its own local state).
 */
export interface RunState {
  active: boolean;
  step: string | null;
  step_status: string | null;
  progress: number | null;
  total: number | null;
  /** 取消已请求，等当前 provider 请求跑完。 */
  cancelling: boolean;
  cancelled: boolean;
  error: string | null;
  started_at?: string;
  elapsed_s: number;
}


/**
 * Ask the server to cancel the live pipeline for this video.
 *
 * Cooperative and step-bounded: the in-flight provider request finishes, then
 * the pipeline stops and the project is left CANCELLED — resumable from the
 * last completed phase.
 */
export async function cancelProcessing(
  videoId: string,
): Promise<{ cancelling: boolean; step?: string | null; reason?: string }> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/cancel`, { method: 'POST' });
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Cancel failed');
  }
  return response.json();
}


/**
 * Get the current processing status of a video.
 */
export async function getVideoStatus(videoId: string) {
  const response = await fetch(`${API_BASE}/videos/${videoId}/status`);

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Status check failed');
  }

  return response.json();
}


export interface ExportResult {
  video_id: string;
  url: string;
  size_mb?: number;
  /** "mp4" or "mkv" — a styled subtitle track forces Matroska. */
  container?: string;
  filename?: string;
  subtitle_tracks?: SubtitleTrackInfo[];
}

/** One subtitle track that actually made it into the exported file. */
export interface SubtitleTrackInfo {
  track: SubtitleTrack;
  title: string;
  language: string;
  codec: string;
  default: boolean;
}

/**
 * Mux the dubbed audio (and optional subtitle tracks) into the video.
 * Cheap on the server: the video stream is copied and subtitles are embedded,
 * not burned, so even styled tracks cost one remux.
 *
 * The subtitle options are merged over the video's saved plan server-side, so
 * sending only the changed field leaves the rest intact.
 */
export async function exportVideo(
  videoId: string,
  subtitles?: SubtitleExportOptions,
): Promise<ExportResult> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/export`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // Always send a body: an empty object is treated as "use the saved plan".
    body: JSON.stringify(subtitles ? { subtitles } : {}),
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Export failed');
  }

  return response.json();
}


/** Absolute-ish URL for downloading an already exported video. */
export function getExportDownloadUrl(videoId: string): string {
  return `${API_BASE}/videos/${videoId}/export/download`;
}


/**
 * Backend used to split vocals from background music.
 *  - "client": the browser runs the MDX-Net ONNX model (WebGPU/WASM)
 *  - "api":    302.AI's demucs endpoint does it server-side (paid)
 *  - "off":    no separation
 * There is deliberately no self-hosted (PyTorch) option: the deployment has no
 * GPU and a local separator would just make the host swap.
 */
export type SeparationMode = 'client' | 'api' | 'off';

export interface BackendAvailability {
  available: boolean;
  /** Why the backend cannot be used, or null when it can. */
  reason: string | null;
}

/**
 * Per-backend availability as reported by the server. The UI builds its
 * backend picker from this instead of guessing, which is what used to produce
 * "no separation backend is available" while the browser backend was fine.
 */
export type SeparationBackends = Partial<Record<SeparationMode, BackendAvailability>>;

/**
 * Parameters of the vocal-separation model that runs in the browser.
 * The DSP constants live on the server so the client never hardcodes them.
 */
export interface SeparatorInfo {
  available: boolean;
  /** Default backend from the server config. */
  mode: SeparationMode;
  /** Whether separation should start switched on. */
  default_enabled: boolean;
  /** Per-backend availability, so the picker can render accurate options. */
  backends: SeparationBackends;
  filename: string;
  size_bytes: number;
  size_mb: number;
  fingerprint: string;
  download_url: string;
  params: {
    nFft: number;
    dimF: number;
    segmentSize: number;
    overlap: number;
    compensate: number;
    primaryStem: string;
    zeroLowBins: number;
    normalizationThreshold: number;
  };
  input_shape: number[];
}

export async function getSeparatorInfo(): Promise<SeparatorInfo> {
  const response = await fetch(`${API_BASE}/models/separator`);
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Separator model is unavailable');
  }
  return response.json();
}


export interface SeparatorToken {
  video_id: string;
  token: string;
  expires_in: number;
  model_download_url: string;
}

/**
 * Obtain a short-lived token authorising the client-compute endpoints for this
 * video (model download + stem upload). The server only issues one for a video
 * that already exists, and rate limits issuance per client.
 */
export async function getSeparatorToken(videoId: string): Promise<SeparatorToken> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/separator-token`, {
    method: 'POST',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Could not obtain a separator token');
  }
  return response.json();
}


export interface StemsResult {
  video_id: string;
  stems: { file: string; bytes: number }[];
  background_url: string;
}

/**
 * Upload the vocals/background WAV stems produced by the in-browser separator.
 * After this the pipeline behaves as if the server had separated the audio.
 */
export async function uploadStems(
  videoId: string,
  vocals: Blob,
  background: Blob,
  token?: string,
): Promise<StemsResult> {
  const form = new FormData();
  form.append('vocals', vocals, 'vocals.wav');
  form.append('background', background, 'background.wav');

  const headers: Record<string, string> = {};
  if (token) headers['X-Separator-Token'] = token;

  const response = await fetch(`${API_BASE}/videos/${videoId}/stems`, {
    method: 'POST',
    headers,
    body: form,
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Stem upload failed');
  }

  return response.json();
}


/**
 * Run the full server-side pipeline (separation → ASR → translation → TTS)
 * via SSE. Calls onEvent for each SSE message received.
 */
/**
 * 每次发起管线都打一行，够看清楚就行。
 *
 * 这里刻意不做"并发探测"（计数在飞请求、发现 >1 就警告）—— 那是给一个已经
 * 修掉的 bug 配的哨兵：重复发起是 StrictMode 把恢复 effect 跑了两遍，前端已
 * 用 ref 守卫堵住，后端也有"同工程重复请求=订阅"的守卫。多一层会误报的探测
 * 只是额外的复杂度，出问题时把这几行按时间顺序看一遍就明白了。
 *
 * 谁发起的、发出了几次：看 `[pipeline] POST` 的行数与相邻 `[startPipeline]
 * 来源=` 标签。
 */
function __stamp(): string {
  return new Date().toLocaleTimeString('zh-CN', { hour12: false });
}

export async function processVideo(
  videoId: string,
  targetLanguage: string,
  onEvent: (event: any) => void,
  options?: {
    separationMode?: SeparationMode;
    enableVoiceClone?: boolean;
    /** Chinese dialect for the dub, e.g. "广东话". Empty means Mandarin. */
    accent?: string;
    /** 语气模仿：TTS 前由 omni 听原声给译文注入情感/拟声标签。 */
    mmEnhance?: boolean;
    /** 智能选材：克隆参考素材由 omni 通过 tool call 挑选。 */
    cloneSmartPick?: boolean;
    /** 注入 DS 翻译的自定义要求。空串 = 默认行为。 */
    customPrompt?: string;
    // 这里刻意没有 export 开关：出片是用户按 Export 触发的独立请求
    // （POST /videos/{id}/export），不属于处理流程。
  },
): Promise<void> {
  const t0 = Date.now();
  console.log(
    `[pipeline ${__stamp()}] POST /process  video=${videoId.slice(0, 8)} lang=${targetLanguage}`,
    {
      separation_mode: options?.separationMode ?? 'client',
      enable_voice_clone: options?.enableVoiceClone ?? false,
      mm_enhance: options?.mmEnhance ?? false,
      clone_smart_pick: options?.cloneSmartPick ?? false,
      custom_prompt: (options?.customPrompt ?? '') || '(空)',
    }
  );

  const response = await fetch(`${API_BASE}/videos/${videoId}/process`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      target_language: targetLanguage,
      accent: options?.accent ?? '',
      // Which backend splits the vocals: "client" (browser, default), "api"
      // (302.AI) or "off". The server validates this against the backends it
      // can actually run and degrades to "off" if the choice is unusable.
      separation_mode: options?.separationMode ?? 'client',
      enable_voice_clone: options?.enableVoiceClone ?? false,
      mm_enhance: options?.mmEnhance ?? false,
      clone_smart_pick: options?.cloneSmartPick ?? false,
      custom_prompt: options?.customPrompt ?? '',
    }),
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Processing failed');
  }

  console.log(`[pipeline ${__stamp()}] 已连接，开始接收事件流（服务器可能先回放历史事件）`);

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error('ReadableStream not supported');
  }

  const decoder = new TextDecoder();
  let buffer = '';
  let received = 0;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          received += 1;
          onEvent(data);
          if (data.error) {
            throw new Error(data.error);
          }
        } catch (e) {
          if (e instanceof Error && e.message !== 'Unexpected end of JSON input') {
            throw e;
          }
        }
      }
    }
  } catch (err) {
    console.warn(`[pipeline ${__stamp()}] 事件流出错:`, err);
    throw err;
  } finally {
    console.log(
      `[pipeline ${__stamp()}] 事件流结束，用时 ${((Date.now() - t0) / 1000).toFixed(1)}s，` +
        `收到 ${received} 条事件`
    );
  }
}


/**
 * Reset a video's processing data (keeps original video file).
 */
export async function resetVideo(videoId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/reset`, {
    method: 'POST',
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Reset failed');
  }
}


/**
 * Get voice clone status for all speakers.
 */
export interface VoiceOption {
  id: string;
  label: string;
}

export interface VoiceList {
  /**
   * Voices that can speak this language. EMPTY when none can — see
   * `needsVoiceCloning`; an empty list is not "still loading".
   */
  voices: VoiceOption[];
  /** speaker_id -> voice_id, for the speakers that have been pinned. */
  assigned: Record<string, string>;
  /**
   * True when NO built-in voice speaks this language, so the only way to dub it
   * is voice cloning. The server decides this; the client never guesses, because
   * the answer is a property of the voice models.
   */
  needsVoiceCloning: boolean;
}

/**
 * The system voices available for a language, plus what each speaker is on.
 *
 * Filtered server-side: voice ids are model- AND language-specific, and a
 * mismatched pair is rejected by the TTS service rather than ignored, so the
 * rule lives in one place.
 */
export async function getVoices(videoId: string, language: string): Promise<VoiceList> {
  const params = new URLSearchParams({ language });
  const response = await fetch(`${API_BASE}/videos/${videoId}/voices?${params}`);
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Could not load voices');
  }
  const data = await response.json();
  return {
    voices: data.voices ?? [],
    assigned: data.assigned ?? {},
    needsVoiceCloning: Boolean(data.needs_voice_cloning),
  };
}

export interface AsrLanguages {
  /** Languages the transcription model can hear. */
  languages: string[];
  /** Chinese dialects it distinguishes — not languages in the same sense. */
  dialects: string[];
}

/**
 * What the ASR model can hear. Shown beside the upload on the landing page:
 * it constrains which videos can be used at all, before any setting matters.
 */
export async function getAsrLanguages(): Promise<AsrLanguages> {
  const response = await fetch(`${API_BASE}/asr/languages`);
  if (!response.ok) {
    throw new Error('Could not load the supported languages');
  }
  const data = await response.json();
  return { languages: data.languages ?? [], dialects: data.dialects ?? [] };
}

/**
 * Which built-in voices a language has, for a caller with no video yet.
 *
 * The landing page asks this to show what a language would sound like before
 * anything is uploaded — the answer depends only on the language, so it does not
 * need a job to exist.
 */
export async function getLanguageVoices(
  language: string,
): Promise<{ voices: VoiceOption[]; needsVoiceCloning: boolean }> {
  const params = new URLSearchParams({ language });
  const response = await fetch(`${API_BASE}/tts/voices?${params}`);
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Could not load voices');
  }
  const data = await response.json();
  return {
    voices: data.voices ?? [],
    needsVoiceCloning: Boolean(data.needs_voice_cloning),
  };
}

/**
 * Pin a speaker to a voice (used when voice cloning is off).
 *
 * Per SPEAKER, not per line: a voice is the speaker's, and pinning it per line
 * would let one speaker change voice mid-video.
 */
export async function setSpeakerVoice(
  videoId: string,
  speakerId: string,
  voice: string,
): Promise<void> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/speaker-voice`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ speaker_id: speakerId, voice }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Could not set the voice');
  }
}

export async function getVoiceCloneStatus(videoId: string): Promise<{ cloned_voices: Record<string, string> }> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/voice-clone/status`);

  if (!response.ok) {
    return { cloned_voices: {} };
  }

  return response.json();
}


/**
 * Generate a voice clone preview for a speaker (POST triggers generation).
 * Returns the audio URL for playback.
 */
export async function generateVoicePreview(videoId: string, speakerId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/voice-clone/${encodeURIComponent(speakerId)}/preview`, {
    method: 'POST',
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Voice preview generation failed');
  }

  // The POST returns the audio file directly — use the GET URL for playback
  return `${API_BASE}/videos/${videoId}/voice-clone/${encodeURIComponent(speakerId)}/preview`;
}


/* ------------------------------------------------------------------ *
 * Subtitles
 *
 * The style numbers mirror `subtitle_service.SubtitleStyle` field for field,
 * snake_case included: the same object is sent to the server, used to render
 * the in-app preview overlay, and (later) drawn by the browser burn-in. Keeping
 * one shape is what makes "what you see is what you export" true — a separate
 * CSS-only representation would drift.
 *
 * Sizes are PERCENTAGES OF THE VIDEO HEIGHT, never pixels, so one style is
 * correct at every resolution and in the preview element, whose CSS size has
 * nothing to do with the video's real size.
 * ------------------------------------------------------------------ */

/**
 * Fallback style, mirroring `config.py` defaults. Only used until the server
 * reports its own (`/subtitles/capabilities` carries `default_style`), so the
 * two cannot drift in a way that matters — but the overlay needs *something*
 * to render with on first paint.
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

/** Which text a track shows. */
export type SubtitleTrack = 'translated' | 'original' | 'bilingual';

/**
 * How subtitles reach the exported file.
 *  - "off":    no subtitle track
 *  - "soft":   MP4 with switchable, unstyled tracks (free, one remux)
 *  - "styled": MKV with a fully styled ASS track (still just a remux)
 *  - "burn":   rendered into the picture. The BROWSER does this; the server
 *              reports it unavailable because it lacks libass and a CJK font.
 */
export type SubtitleExportFormat = 'off' | 'soft' | 'styled' | 'burn';

export type SubtitleAlignment = 'bottom' | 'center' | 'top';
export type SubtitleBackground = 'none' | 'box';

export interface SubtitleStyle {
  track: SubtitleTrack;
  font_family: string;
  /** Percent of the video height. */
  font_size_percent: number;
  /** #RRGGBB. */
  primary_color: string;
  outline_color: string;
  outline_width: number;
  shadow: number;
  bold: boolean;
  background: SubtitleBackground;
  alignment: SubtitleAlignment;
  margin_v_percent: number;
  margin_h_percent: number;
  max_chars_per_line: number;
  max_lines: number;
  min_duration: number;
}

/** Partial style for updates: only the keys sent are merged server-side. */
export type SubtitleStylePatch = Partial<SubtitleStyle>;

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

export type SubtitleCapabilities = Partial<Record<SubtitleExportFormat, SubtitleCapability>>;

/** A display cue with the server's adapted timing. */
export interface SubtitleCue {
  start: number;
  end: number;
  text: string;
  /** The other track's text, for bilingual. */
  secondary: string;
  /** Pre-wrapped lines; the client may re-wrap while a slider moves. */
  lines: string[];
}

export interface SubtitleCueResponse {
  video_id: string;
  track: SubtitleTrack;
  count: number;
  style: SubtitleStyle;
  cues: SubtitleCue[];
  /**
   * A cue that is NOT in the video, for previewing a style on a stretch with no
   * subtitle. Wrapped server-side by the same code as `cues`, which is the only
   * reason it can be trusted: re-breaking the lines in JS would drift from the
   * export. Optional so an older backend still type-checks.
   */
  sample?: SubtitleCue;
}

export interface SubtitleCapabilitiesResponse {
  capabilities: SubtitleCapabilities;
  default_style: SubtitleStyle;
}

export interface SubtitleStyleResponse {
  video_id: string;
  style: SubtitleStyle;
  default_style: SubtitleStyle;
  is_custom: boolean;
}

/**
 * What this server can actually produce, and why not. The UI greys out an
 * option with the reason instead of offering something that fails mid-export.
 */
export async function getSubtitleCapabilities(): Promise<SubtitleCapabilitiesResponse> {
  const response = await fetch(`${API_BASE}/subtitles/capabilities`);
  if (!response.ok) {
    throw new Error(`Failed to load subtitle capabilities (${response.status})`);
  }
  return response.json();
}

/** The style in effect for a video, plus the server default for "reset". */
export async function getSubtitleStyle(videoId: string): Promise<SubtitleStyleResponse> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/subtitles/style`);
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Failed to load subtitle style');
  }
  return response.json();
}

/** Persist a video's subtitle style. Out-of-range values are clamped server-side. */
export async function saveSubtitleStyle(
  videoId: string,
  style: SubtitleStylePatch,
): Promise<SubtitleStyleResponse> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/subtitles/style`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(style),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Failed to save subtitle style');
  }
  return response.json();
}

/**
 * Display cues with the server's adapted timing.
 *
 * Timing comes from here rather than being recomputed locally because the
 * adaptation extends each cue to cover the dubbed audio that actually exists —
 * which needs audio durations. Recomputing it in the browser would drift from
 * what the export produces, and the preview's whole point is that it matches.
 */
export async function getSubtitleCues(
  videoId: string,
  track?: SubtitleTrack,
): Promise<SubtitleCueResponse> {
  const query = track ? `?track=${encodeURIComponent(track)}` : '';
  const response = await fetch(`${API_BASE}/videos/${videoId}/subtitles/cues${query}`);
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Failed to load subtitle cues');
  }
  return response.json();
}

/**
 * Cues rendered with a style the caller supplies, WITHOUT saving it.
 *
 * Used by the style editor to preview while a slider is being dragged. The
 * wrapping is done server-side on purpose: reimplementing it in JS would mean
 * the preview silently disagreeing with the export as soon as the two drifted.
 */
export async function previewSubtitleCues(
  videoId: string,
  options: { track?: SubtitleTrack; style?: SubtitleStylePatch } = {},
): Promise<SubtitleCueResponse> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/subtitles/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ track: options.track, style: options.style }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Failed to preview subtitle cues');
  }
  return response.json();
}

/**
 * Download URL for the standalone .srt.
 *
 * Returned as a URL rather than fetched because the browser should handle it as
 * a normal download (correct filename, no buffering in JS memory).
 */
export function getSrtDownloadUrl(videoId: string, track?: SubtitleTrack): string {
  const query = track ? `?track=${encodeURIComponent(track)}` : '';
  return `${API_BASE}/videos/${videoId}/subtitles.srt${query}`;
}

/** Name used for the downloaded .srt, mirroring the server's filename. */
export function srtFilename(videoId: string, track: SubtitleTrack): string {
  return `${track}_${videoId.slice(0, 8)}.srt`;
}
