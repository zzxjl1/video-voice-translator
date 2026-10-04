/**
 * API Service - calls the FastAPI backend
 */

const API_BASE = '/api';

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
export async function translateScript(
  videoId: string,
  segments: { id: string; text: string; speaker_id: string; start_time: number; end_time?: number }[],
  targetLanguage: string = 'English',
): Promise<TranslateResult[]> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/translate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      target_language: targetLanguage,
      segments,
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
  options?: { targetDuration?: number; targetLanguage?: string },
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
    }),
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'TTS failed');
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
}

/**
 * Mux the dubbed audio back into the video and return the download URL.
 * Cheap on the server: the video stream is copied, not re-encoded.
 */
export async function exportVideo(videoId: string): Promise<ExportResult> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/export`, {
    method: 'POST',
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
export async function processVideo(
  videoId: string,
  targetLanguage: string,
  onEvent: (event: any) => void,
  options?: {
    separationMode?: SeparationMode;
    enableBgmSeparation?: boolean;
    enableVoiceClone?: boolean;
    exportVideo?: boolean;
  },
): Promise<void> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/process`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      target_language: targetLanguage,
      // Which backend splits the vocals: "client" (browser, default), "api"
      // (302.AI) or "off". The server validates this against the backends it
      // can actually run and degrades to "off" if the choice is unusable.
      separation_mode: options?.separationMode ?? 'client',
      enable_bgm_separation: options?.enableBgmSeparation ?? false,
      enable_voice_clone: options?.enableVoiceClone ?? false,
      export_video: options?.exportVideo ?? true,
    }),
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Processing failed');
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error('ReadableStream not supported');
  }

  const decoder = new TextDecoder();
  let buffer = '';

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
