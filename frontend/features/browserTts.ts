/**
 * Browser Kokoro TTS. The model runs in a dedicated worker (WebGPU fp16 when
 * available, else WASM q8); generated WAVs are uploaded to the normal
 * tts/<segment>.mp3 path so mixing/export stay shared.
 *
 * Phonemization: zh via our misaki-format G2P (zhG2P.ts), en via kokoro-js's
 * built-in espeak, fr/it/pt via the full espeak-ng WASM build. Voice ids are
 * Kokoro's ("zf_xiaobei"); the app's browser dubbing languages are exactly
 * Kokoro's matrix (Chinese/English/French/Italian/Portuguese) — de/vi/ja/ko/id
 * have no usable browser voices and stay greyed out, like ja/ko/id under Piper.
 * Research notes live in docs/dubbing-modes.md (language matrix, G2P, engines).
 */
import { kokoro } from './kokoro/client';
import { defaultVoiceFor, voicesFor, voiceMeta, type BrowserVoice } from './kokoro/catalog';
import { log } from '../utils/log';

export type Voice = BrowserVoice;

export function voicesForLanguage(language: string): Promise<Voice[]> {
  return Promise.resolve(voicesFor(language));
}

export function browserVoiceLabel(voice: Pick<Voice, 'key' | 'name' | 'grade'>): string {
  const meta = voiceMeta(voice.key);
  const grade = voice.grade || meta?.grade || '';
  return grade ? `${voice.name} · ${grade}` : voice.name;
}

export function defaultVoiceForLanguage(language: string): string | null {
  return defaultVoiceFor(language);
}

export function cleanTtsText(text: string): string {
  return (text || '')
    .replace(/\[[^\]]*\]|【[^】]*】/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Load the engine ahead of the first line so model download progress can be
 * shown as its own step instead of looking like a stalled first sentence.
 */
export function ensureBrowserEngine(
  onProgress?: (loaded: number, total: number) => void,
  onDevice?: (d: 'webgpu' | 'wasm') => void,
): Promise<void> {
  return kokoro.warmup({ onProgress, onDevice });
}

export async function synthesizeBrowserSpeech(
  text: string,
  voiceId: string,
  onProgress?: (loaded: number, total: number) => void,
): Promise<Blob> {
  const started = performance.now();
  const clean = cleanTtsText(text);
  try {
    const blob = await kokoro.synthesize(clean, voiceId, { onProgress });
    log.info('tts', `done ${voiceId} (${Math.round(performance.now() - started)}ms, ${blob.size}b)`);
    return blob;
  } catch (e) {
    log.error('tts', `synthesize failed ${voiceId}`, e);
    throw e;
  }
}
