/**
 * Shared browser dubbing runner.
 *
 * The same path is used by the init screen and the editor: assign voices,
 * synthesize each missing line in the tab, upload it to the normal TTS path,
 * then let the server reload its view.
 *
 * 一句失败不影响其它句：失败的收集起来返回，其余照常合成。整批因为一句话抛错而中止，会让
 * 初始化页卡死、整个工程进不了编辑器。
 */
import * as api from '../net/client';
import type { Line, ProjectView } from '../net/types';
import { cleanTtsText, defaultVoiceForLanguage, synthesizeBrowserSpeech, voicesForLanguage } from './browserTts';
import { log, shortId } from '../utils/log';

export interface BrowserDubbingProgress {
  index: number;
  total: number;
  line: Line;
  voice: string;
}

export interface BrowserDubbingResult {
  done: number;
  /** 没做成的句子：行号 + 原因。 */
  failed: { line: number; reason: string }[];
  cancelled: boolean;
}

export class DubbingCancelled extends Error {
  constructor() { super('cancelled'); }
}

export async function fillBrowserDubbing(
  project: ProjectView,
  opts: {
    only?: Set<string>;
    /** 即使配音是新鲜的也重做（行菜单里的「重新配音」）。 */
    force?: boolean;
    /** 点了取消：每句开始前检查，正在合成的这一句做完就停。 */
    signal?: AbortSignal;
    onProgress?: (p: BrowserDubbingProgress) => void;
  } = {},
): Promise<BrowserDubbingResult> {
  const result: BrowserDubbingResult = { done: 0, failed: [], cancelled: false };
  if (project.settings.dubbingMode !== 'browser') return result;
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
    && (opts.force || l.ttsStatus !== 'fresh') && (!opts.only || opts.only.has(l.id)));
  log.info('browser-dub', `todo ${todo.length} lines`);
  if (!todo.length) return result;

  const speakerVoice = new Map(fresh.speakers.map((sp) => [sp.id, sp.browserVoice]));
  const fallbackVoice = fresh.settings.browserVoice ?? fallback ?? voices[0].key;
  for (const line of todo) {
    if (opts.signal?.aborted) { result.cancelled = true; break; }
    const voice = line.browserVoice ?? speakerVoice.get(line.speakerId) ?? fallbackVoice;
    log.info('browser-dub', `line ${line.line}/${todo.length} voice=${voice}`);
    opts.onProgress?.({ index: result.done + result.failed.length + 1, total: todo.length, line, voice });
    try {
      // 整句只有 [laughing] 这类标签时清洗后是空串：Piper 对空文本的行为不确定，不去试。
      // 这一句没有可念的内容，算失败（保留原因），不拖累其它句。
      if (!cleanTtsText(line.translation)) throw new Error('译文只有语气标签，没有可念的内容');
      const blob = await synthesizeBrowserSpeech(line.translation, voice);
      // 带上合成时用的译文和音色：服务端核对和当前一致才收，期间被改过就拒绝（409）
      await api.uploadBrowserTts(project.id, line.id, blob, line.translation, voice);
      result.done += 1;
    } catch (e) {
      const reason = e instanceof Error ? e.message : String(e);
      log.warn('browser-dub', `line ${line.line} failed`, reason);
      result.failed.push({ line: line.line, reason });
    }
  }
  log.info('browser-dub', `done ${result.done}/${todo.length} failed=${result.failed.length} cancelled=${result.cancelled}`);
  return result;
}
