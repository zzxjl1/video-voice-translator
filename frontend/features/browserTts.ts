/**
 * Browser Piper TTS. The model runs fully in the tab; generated WAVs are
 * uploaded to the normal tts/<segment>.mp3 path so mixing/export stay shared.
 */
import type { Voice } from '@diffusionstudio/vits-web';
import { log } from '../utils/log';

const PREFIX: Record<string, string> = {
  Chinese: 'zh_CN',
  English: 'en_',
  French: 'fr_FR',
  German: 'de_DE',
  Italian: 'it_IT',
  Portuguese: 'pt_',
  Vietnamese: 'vi_VN',
  Japanese: 'ja_',
  Korean: 'ko_',
  Indonesian: 'id_',
};

export const BROWSER_DEFAULT_VOICE: Record<string, string> = {
  Chinese: 'zh_CN-huayan-medium',
  English: 'en_US-hfc_female-medium',
  French: 'fr_FR-siwis-medium',
  German: 'de_DE-thorsten-medium',
  Italian: 'it_IT-riccardo-x_low',
  Portuguese: 'pt_BR-faber-medium',
  Vietnamese: 'vi_VN-vais1000-medium',
};

let catalog: Promise<Voice[]> | null = null;

// zh_CN-huayan-x_low has a broken phoneme table in the pinned browser
// runtime (Gather idx out of bounds); only medium is usable.
const UNUSABLE_VOICES = new Set(['zh_CN-huayan-x_low']);

export async function allVoices(): Promise<Voice[]> {
  if (!catalog) {
    log.info('tts', 'loading voice catalog');
    catalog = import('@diffusionstudio/vits-web').then((m) => m.voices()).then((v) => {
      log.info('tts', `voice catalog loaded: ${v.length}`);
      return v;
    }).catch((e) => {
      catalog = null;
      log.error('tts', 'voice catalog failed', e);
      throw e;
    });
  }
  return catalog;
}

export async function voicesForLanguage(language: string): Promise<Voice[]> {
  const prefix = PREFIX[language];
  if (!prefix) return [];
  const voices = await allVoices();
  const available = voices.filter((v) => v.key.startsWith(prefix) && !UNUSABLE_VOICES.has(v.key));
  log.info('tts', `voices for ${language}: ${available.length}`, available.map((v) => v.key));
  return available;
}

export function browserVoiceLabel(voice: Pick<Voice, 'key' | 'name' | 'quality'>): string {
  const quality = voice.quality === 'x_low' ? 'x-low' : voice.quality;
  return `${voice.name} · ${quality}`;
}

export function defaultVoiceForLanguage(language: string): string | null {
  return BROWSER_DEFAULT_VOICE[language] ?? null;
}

export function cleanTtsText(text: string): string {
  return (text || '')
    .replace(/\[[^\]]*\]|【[^】]*】/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

export async function synthesizeBrowserSpeech(
  text: string,
  voiceId: string,
  onProgress?: (loaded: number, total: number) => void,
): Promise<Blob> {
  const started = performance.now();
  log.info('tts', `synthesize ${voiceId} (${text.length} chars)`);
  try {
    const m = await import('@diffusionstudio/vits-web');
    const blob = await m.predict(
      { text: cleanTtsText(text), voiceId: voiceId as never },
      onProgress ? (p) => onProgress(p.loaded, p.total) : undefined,
    );
    log.info('tts', `done ${voiceId} (${Math.round(performance.now() - started)}ms, ${blob.size}b)`);
    return blob;
  } catch (e) {
    log.error('tts', `synthesize failed ${voiceId}`, e);
    throw e;
  }
}

export async function downloadBrowserVoice(
  voiceId: string,
  onProgress?: (loaded: number, total: number) => void,
): Promise<void> {
  log.info('tts', `download voice ${voiceId}`);
  const m = await import('@diffusionstudio/vits-web');
  await m.download(voiceId as never, onProgress ? (p) => onProgress(p.loaded, p.total) : undefined);
  log.info('tts', `voice ready ${voiceId}`);
}
