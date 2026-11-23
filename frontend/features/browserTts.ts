/**
 * Browser Piper TTS. The model runs fully in the tab; generated WAVs are
 * uploaded to the normal tts/<segment>.mp3 path so mixing/export stay shared.
 */
import type { Voice } from '@diffusionstudio/vits-web';

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
    catalog = import('@diffusionstudio/vits-web').then((m) => m.voices()).catch((e) => {
      catalog = null;
      throw e;
    });
  }
  return catalog;
}

export async function voicesForLanguage(language: string): Promise<Voice[]> {
  const prefix = PREFIX[language];
  if (!prefix) return [];
  const voices = await allVoices();
  return voices.filter((v) => v.key.startsWith(prefix) && !UNUSABLE_VOICES.has(v.key));
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
  const m = await import('@diffusionstudio/vits-web');
  return m.predict(
    { text: cleanTtsText(text), voiceId: voiceId as never },
    onProgress ? (p) => onProgress(p.loaded, p.total) : undefined,
  );
}

export async function downloadBrowserVoice(
  voiceId: string,
  onProgress?: (loaded: number, total: number) => void,
): Promise<void> {
  const m = await import('@diffusionstudio/vits-web');
  await m.download(voiceId as never, onProgress ? (p) => onProgress(p.loaded, p.total) : undefined);
}
