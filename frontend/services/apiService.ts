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
 * Synthesize speech via OpenAI TTS on the backend.
 */
export async function synthesizeSpeech(
  videoId: string,
  segmentId: string,
  text: string,
  voice?: string,
): Promise<TTSResult> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/tts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ segment_id: segmentId, text, voice }),
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'TTS failed');
  }

  return response.json();
}

export interface SeparationResult {
  video_id: string;
  vocals: string;
  background: string;
  background_url: string;
}

/**
 * Separate audio into vocals and background with SSE progress.
 */
export async function separateAudio(
  videoId: string,
  onProgress?: (progress: number) => void,
): Promise<SeparationResult> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/separate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(err.detail || 'Vocal separation failed');
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error('ReadableStream not supported');
  }

  const decoder = new TextDecoder();
  let result: SeparationResult | null = null;
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
        if (data.error) {
          throw new Error(data.error);
        }
        if (data.progress !== undefined && onProgress) {
          onProgress(data.progress);
        }
        if (data.done) {
          result = {
            video_id: data.video_id,
            vocals: data.vocals,
            background: data.background,
            background_url: data.background_url,
          };
        }
      } catch (e) {
        if (e instanceof Error && e.message !== 'Unexpected end of JSON input') {
          throw e;
        }
      }
    }
  }

  if (!result) {
    throw new Error('Separation completed but no result received');
  }

  return result;
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
 * Parameters of the vocal-separation model that runs in the browser.
 * The DSP constants live on the server so the client never hardcodes them.
 */
export interface SeparatorInfo {
  available: boolean;
  mode: string;
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
  options?: { enableBgmSeparation?: boolean; enableVoiceClone?: boolean; exportVideo?: boolean },
): Promise<void> {
  const response = await fetch(`${API_BASE}/videos/${videoId}/process`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      target_language: targetLanguage,
      // Server default is off: separation needs PyTorch, which is not viable
      // on a small CPU-only host.
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
