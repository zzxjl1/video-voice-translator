/**
 * 后端 HTTP 客户端。每个函数对应一个端点，没有业务逻辑。
 *
 * 所有"改东西"的动作都走 `command()`：UI 的每个按钮和 AI 的每个 tool 在服务端是同一套命令。
 */
import type { ChatMessage, CommandResult, ProjectView, SubtitleCapabilities, SubtitleStyle, SubtitleCue, SubtitleTrack, VoiceOption } from './types';

export const API_BASE = '/api';

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(
      typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail),
      response.status,
    );
  }
  return response.json() as Promise<T>;
}

const json = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

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
  enable_dubbing: boolean;
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
