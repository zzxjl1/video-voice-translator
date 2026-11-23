/**
 * 后端 HTTP 客户端。每个函数对应一个端点，没有业务逻辑。
 *
 * 所有"改东西"的动作都走 `command()`：UI 的每个按钮和 AI 的每个 tool 在服务端是同一套命令。
 */
import type { AuthUser, ChatMessage, CommandResult, ProjectSummary, ProjectView, SubtitleCapabilities, SubtitleStyle, SubtitleCue, SubtitleTrack, VoiceOption } from './types';
import { log, safeUrl } from '../utils/log.ts';
import { notifyUnauthorized } from './session.ts';

export const API_BASE = '/api';

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase();
  const started = performance.now();
  log.debug('api', `→ ${method} ${safeUrl(path)}`);
  try {
    const response = await fetch(`${API_BASE}${path}`, init);
    const ms = Math.round(performance.now() - started);
    if (!response.ok) {
      const body = await response.json().catch(() => ({ detail: response.statusText }));
      const detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
      log.warn('api', `← ${response.status} ${method} ${safeUrl(path)} (${ms}ms)`, detail);
      // /auth/me 的 401 是"没登录"的正常答案，不是会话过期
      if (response.status === 401 && !path.startsWith('/auth/')) notifyUnauthorized();
      throw new ApiError(detail, response.status);
    }
    log.debug('api', `← ${response.status} ${method} ${safeUrl(path)} (${ms}ms)`);
    return response.json() as Promise<T>;
  } catch (e) {
    if (!(e instanceof ApiError)) {
      log.error('api', `× ${method} ${safeUrl(path)}`, e instanceof Error ? e.message : e);
    }
    throw e;
  }
}

const json = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

// ---- 认证 -------------------------------------------------------------------
export const me = () => request<{ user: AuthUser | null }>('/auth/me');
export const logout = () => request<{ ok: boolean }>('/auth/logout', { method: 'POST' });

// ---- 我的工程 ---------------------------------------------------------------
export const listProjects = () => request<{ projects: ProjectSummary[] }>('/projects');

// ---- 项目 -------------------------------------------------------------------

export interface UploadResult {
  id: string;
  filename: string;
  exists: boolean;
  stage: string;
  hasLines: boolean;
}

export function upload(file: File): Promise<UploadResult> {
  const form = new FormData();
  form.append('file', file);
  return request('/projects', { method: 'POST', body: form });
}

export const getProject = (id: string) => request<ProjectView>(`/projects/${id}`);
export const deleteProject = (id: string) => request<{ deleted: boolean }>(`/projects/${id}`, { method: 'DELETE' });

export interface SetupBody {
  target_language: string;
  accent?: string | null;
  separation_mode: 'client' | 'off';
  dubbing_mode: 'remote' | 'browser' | 'off';
  browser_voice?: string | null;
  enable_voice_clone: boolean;
  clone_smart_pick: boolean;
  mm_enhance: boolean;
  custom_prompt: string;
}

export const setup = (id: string, body: SetupBody) => request<{ job: string }>(`/projects/${id}/setup`, json(body));

// ---- 命令 -------------------------------------------------------------------

/** 执行一个命令。业务上做不了是 `{ok:false, error}`（HTTP 200）；只有传输/404 才抛。 */
export async function command<T = Record<string, unknown>>(
  id: string,
  name: string,
  args: Record<string, unknown> = {},
): Promise<CommandResult<T>> {
  return request<CommandResult<T>>(`/projects/${id}/commands/${name}`, json({ args }));
}

// ---- 聊天 -------------------------------------------------------------------

export interface ChatSummary {
  id: string;
  title: string;
  updated: number;
  count: number;
}

export const getChat = (id: string, conv?: string) =>
  request<{ messages: ChatMessage[]; running: boolean; conv: string }>(
    `/projects/${id}/chat${conv ? `?conv=${encodeURIComponent(conv)}` : ''}`,
  );

/** 这个工程的会话列表（新的在前）+ 当前会话 id。 */
export const listChats = (id: string) => request<{ current: string; conversations: ChatSummary[] }>(`/projects/${id}/chats`);

/** 开一个新对话，旧的留在历史里。 */
export const newChat = (id: string) => request<{ conv: string }>(`/projects/${id}/chats`, { method: 'POST' });

/** 删掉一个会话；返回删除后服务端当前会话。 */
export const deleteChat = (id: string, conv: string) =>
  request<{ conv: string }>(`/projects/${id}/chats/${encodeURIComponent(conv)}`, { method: 'DELETE' });

/** 切到某个历史会话，返回它的消息。 */
export const selectChat = (id: string, conv: string) =>
  request<{ conv: string; messages: ChatMessage[] }>(`/projects/${id}/chats/select`, json({ conv }));
export const sendChat = (id: string, text: string) => request<{ accepted: boolean }>(`/projects/${id}/chat`, json({ text }));
export const confirmChat = (id: string, cardId: string, approve: boolean) =>
  request<{ ok: boolean }>(`/projects/${id}/chat/confirm`, json({ id: cardId, approve }));
export const stopChat = (id: string) => request<{ stopping: boolean }>(`/projects/${id}/chat/stop`, { method: 'POST' });

// ---- 元信息 -----------------------------------------------------------------

export const getLanguageVoices = async (language: string) => {
  const data = await request<{ voices?: VoiceOption[]; needs_voice_cloning?: boolean }>(
    `/tts/voices?language=${encodeURIComponent(language)}`,
  );
  return { voices: data.voices ?? [], needsVoiceCloning: Boolean(data.needs_voice_cloning) };
};

export const getAsrLanguages = async () => {
  const data = await request<{ languages?: string[]; dialects?: string[] }>('/asr/languages');
  return { languages: data.languages ?? [], dialects: data.dialects ?? [] };
};

export const getSubtitleCapabilities = () =>
  request<{ capabilities: SubtitleCapabilities; default_style: SubtitleStyle }>('/subtitles/capabilities');

export interface SubtitleCueResponse {
  track: SubtitleTrack;
  count: number;
  style: SubtitleStyle;
  cues: SubtitleCue[];
}

export const previewSubtitleCues = (id: string, options: { track?: SubtitleTrack; style?: Partial<SubtitleStyle> } = {}) =>
  request<SubtitleCueResponse>(`/projects/${id}/subtitles/preview`, json({ track: options.track, style: options.style }));

// ---- URL（不发请求） --------------------------------------------------------

export const videoUrl = (id: string) => `${API_BASE}/projects/${id}/video`;
export const backgroundUrl = (id: string) => `${API_BASE}/projects/${id}/audio/background`;
export const getSrtDownloadUrl = (id: string, track = '') => `${API_BASE}/projects/${id}/subtitles.srt${track ? `?track=${track}` : ''}`;
export const getExportDownloadUrl = (id: string) => `${API_BASE}/projects/${id}/export/download`;
export const audioUrl = (id: string) => `${API_BASE}/projects/${id}/audio`;
export const stereoUrl = (id: string, token: string) =>
  `${API_BASE}/projects/${id}/audio/stereo?token=${encodeURIComponent(token)}`;

// ---- 浏览器端人声分离 --------------------------------------------------------

export interface SeparatorInfo {
  available: boolean;
  size_mb: number;
  params: Record<string, number | string>;
  fingerprint: string;
  download_url: string;
  size_bytes: number;
  backends: Record<string, { available: boolean; reason?: string | null }>;
  default_enabled: boolean;
}

export const getSeparatorInfo = () => request<SeparatorInfo>('/models/separator');
export const getSeparatorToken = (id: string) =>
  request<{ token: string; expires_in: number; model_download_url: string }>(`/projects/${id}/separator-token`, {
    method: 'POST',
  });

export async function uploadBrowserTts(id: string, segmentId: string, audio: Blob, text: string, voice: string) {
  const form = new FormData();
  form.append('audio', audio, `${segmentId}.wav`);
  // 合成时用的译文和音色：服务端核对和当前一致才收，期间被改过就 409
  form.append('text', text);
  form.append('voice', voice);
  return request<{ ok: boolean; duration: number }>(`/projects/${id}/browser-tts/${segmentId}`, {
    method: 'POST',
    body: form,
  });
}

export async function uploadStems(id: string, vocals: Blob, background: Blob, token: string) {
  const form = new FormData();
  form.append('vocals', vocals, 'vocals.wav');
  form.append('background', background, 'background.wav');
  return request<{ stems: { file: string; bytes: number }[] }>(`/projects/${id}/stems`, {
    method: 'POST',
    headers: { 'X-Separator-Token': token },
    body: form,
  });
}
