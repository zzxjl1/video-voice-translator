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
import { log, shortId } from '../utils/log';

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
  log.info('browser-dub', `start ${shortId(project.id)} lang=${project.settings.targetLanguage} accent=${project.settings.accent ?? '-'}`);
  const voices = await voicesForLanguage(project.settings.targetLanguage);
  if (!voices.length) {
    log.error('browser-dub', 'no local voices');
    throw new Error(`${project.settings.targetLanguage} 暂无本地音色，请改用 Remote 或 Off`);
  }

  const fallback = defaultVoiceForLanguage(project.settings.targetLanguage);
  const orderedKeys = [
    ...(fallback ? [fallback] : []),
    ...voices.map((v) => v.key).filter((key) => key !== fallback),
  ];
  const assigned = await api.command(project.id, 'assign_browser_voices', { voices: orderedKeys });
  if (!assigned.ok) {
    log.error('browser-dub', 'assign voices failed', assigned.error);
    throw new Error(assigned.error ?? '分配本地音色失败');
  }
  log.info('browser-dub', 'voices assigned', assigned.result);

  const fresh = await api.getProject(project.id);
  const todo = fresh.segments.filter((l) =>
    l.kind === 'speech' && !l.muted && l.translation.trim()
    && l.ttsStatus !== 'fresh' && (!opts.only || opts.only.has(l.id)));
  log.info('browser-dub', `todo ${todo.length} lines`);
  if (!todo.length) return 0;

  const speakerVoice = new Map(fresh.speakers.map((sp) => [sp.id, sp.browserVoice]));
  const fallbackVoice = fresh.settings.browserVoice ?? fallback ?? voices[0].key;
  let done = 0;
  for (const line of todo) {
    const voice = line.browserVoice ?? speakerVoice.get(line.speakerId) ?? fallbackVoice;
    log.info('browser-dub', `line ${line.line}/${todo.length} voice=${voice}`);
    opts.onProgress?.({ index: done + 1, total: todo.length, line, voice });
    const blob = await synthesizeBrowserSpeech(line.translation, voice);
    await api.uploadBrowserTts(project.id, line.id, blob);
    done += 1;
  }
  log.info('browser-dub', `done ${done}/${todo.length}`);
  return done;
}
