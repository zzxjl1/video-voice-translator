/**
 * Shared browser dubbing runner.
 *
 * The same path is used by the init screen and the editor: assign voices,
 * synthesize each missing line in the tab, upload it to the normal TTS path,
 * then let the server reload its view.
 */
import * as api from '../net/client';
import type { Line, ProjectView } from '../net/types';
import { defaultVoiceForLanguage, synthesizeBrowserSpeech, voicesForLanguage } from './browserTts';

export interface BrowserDubbingProgress {
  index: number;
  total: number;
  line: Line;
  voice: string;
}

export async function fillBrowserDubbing(
  project: ProjectView,
  opts: {
    only?: Set<string>;
    onProgress?: (p: BrowserDubbingProgress) => void;
  } = {},
): Promise<number> {
  if (project.settings.dubbingMode !== 'browser') return 0;
  const voices = await voicesForLanguage(project.settings.targetLanguage);
  if (!voices.length) {
    throw new Error(`${project.settings.targetLanguage} 暂无本地音色，请改用 Remote 或 Off`);
  }

  const fallback = defaultVoiceForLanguage(project.settings.targetLanguage);
  const orderedKeys = [
    ...(fallback ? [fallback] : []),
    ...voices.map((v) => v.key).filter((key) => key !== fallback),
  ];
  const assigned = await api.command(project.id, 'assign_browser_voices', { voices: orderedKeys });
  if (!assigned.ok) throw new Error(assigned.error ?? '分配本地音色失败');

  const fresh = await api.getProject(project.id);
  const todo = fresh.segments.filter((l) =>
    l.kind === 'speech' && !l.muted && l.translation.trim()
    && l.ttsStatus !== 'fresh' && (!opts.only || opts.only.has(l.id)));
  if (!todo.length) return 0;

  const speakerVoice = new Map(fresh.speakers.map((sp) => [sp.id, sp.browserVoice]));
  const fallbackVoice = fresh.settings.browserVoice ?? fallback ?? voices[0].key;
  let done = 0;
  for (const line of todo) {
    const voice = line.browserVoice ?? speakerVoice.get(line.speakerId) ?? fallbackVoice;
    opts.onProgress?.({ index: done + 1, total: todo.length, line, voice });
    const blob = await synthesizeBrowserSpeech(line.translation, voice);
    await api.uploadBrowserTts(project.id, line.id, blob);
    done += 1;
  }
  return done;
}
