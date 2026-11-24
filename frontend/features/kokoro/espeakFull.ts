/**
 * Full espeak-ng (all languages) in the browser, for Kokoro phonemization of
 * fr/it/pt (and es/hi if target languages ever expand).
 *
 * The `phonemizer` npm package that kokoro-js uses internally ships ONLY the
 * English espeak data, so it cannot phonemize anything else. This module talks
 * to @echogarden/espeak-ng-emscripten directly — same wrapper shape as
 * @pipedog/espeak-ng's EspeakNG class (BSD-2-Clause), trimmed to what Kokoro
 * needs: voice-by-language resolution, IPA output with stress, punctuation
 * kept in the phoneme string (the vocab has ,.:;?!… — they act as pauses),
 * and everything outside the Kokoro vocab stripped.
 *
 * The module is imported AT RUNTIME from our own server (see assets.ts), NOT
 * bundled: the emscripten build overwrites Module.locateFile with its own and
 * derives the 24 MB espeak-ng.data URL from its own import.meta.url, so a
 * bundled copy would look for the data in dist/assets/ where it does not exist.
 * Served from the same directory, both resolve. (espeak-ng is GPL-3.0; the
 * license text travels with the files as espeak/COPYING.)
 * English does NOT come through here (kokoro-js's built-in path handles en,
 * and zh uses zhG2P).
 */
import { KOKORO_VOCAB } from './catalog';
import { espeakModuleUrl } from './assets';

/* eslint-disable @typescript-eslint/no-explicit-any */
type EspeakModule = any;
type EspeakWorker = any;

let loading: Promise<EspeakModule> | null = null;

async function getEspeak(): Promise<EspeakModule> {
  if (!loading) {
    loading = (async () => {
      // Same-origin URL import: must stay runtime-resolved (see docstring).
      const mod = await import(/* @vite-ignore */ espeakModuleUrl());
      const factory = mod.default ?? mod;
      return await factory();
    })();
    loading.catch(() => { loading = null; });  // retry next call
  }
  return loading;
}

interface EspeakVoice { identifier: string; languages: Array<{ priority: number; language: string }> }

function listVoices(ng: EspeakModule, w: EspeakWorker): EspeakVoice[] {
  const heap8 = ng.HEAPU8;
  const decoder = new TextDecoder();
  const voices: EspeakVoice[] = [];
  for (let i = 0; ; i++) {
    const v = w.get_voices(i);
    if (!v || v.ptr === 0) break;
    // espeak_VOICE.languages: runs of <priority byte><language>\0, zero-terminated
    let p = ng.HEAPU32[(v.ptr + 4) >> 2];
    const languages: Array<{ priority: number; language: string }> = [];
    while (p && heap8[p] !== 0) {
      const priority = heap8[p];
      let end = p + 1;
      while (heap8[end] !== 0) end++;
      languages.push({ priority, language: decoder.decode(heap8.subarray(p + 1, end)) });
      p = end + 1;
    }
    voices.push({ identifier: String(v.get_identifier()).replaceAll('\\', '/'), languages });
  }
  return voices;
}

/** espeak language code -> voice identifier ("fr-fr" -> "roa/fr"), Python-phonemizer style. */
function voiceForLanguage(voices: EspeakVoice[], language: string): string | null {
  const code = language.toLowerCase();
  for (const v of voices) {
    if (v.identifier.startsWith('mb/')) continue;
    if (v.languages[0]?.language === code) return v.identifier;
  }
  return null;
}

function cString(ng: EspeakModule, ptr: number): string {
  const heap = ng.HEAPU8;
  let end = ptr;
  while (heap[end] !== 0) end++;
  return new TextDecoder().decode(heap.subarray(ptr, end));
}

/** Punctuation that survives into the phoneme string (as pause markers). */
const KEEPS = ',.:;?!…';

let worker: EspeakWorker | null = null;
let voices: EspeakVoice[] | null = null;
let currentVoice: string | null = null;

async function getWorker(): Promise<EspeakWorker> {
  if (worker) return worker;
  const ng = await getEspeak();
  worker = new ng.eSpeakNGWorker();
  voices = listVoices(ng, worker);
  return worker;
}

/** Phonemize `text` in `language` for Kokoro: IPA + stress, punctuation kept. */
export async function espeakPhonemize(text: string, language: string): Promise<string> {
  const w = await getWorker();
  const ng = await getEspeak();
  const identifier = voiceForLanguage(voices ?? [], language);
  if (!identifier) throw new Error(`espeak 数据里没有 ${language} 的音色`);
  if (currentVoice !== identifier) {
    if (w.set_voice(identifier) !== 0) throw new Error(`espeak 加载音色 ${identifier} 失败`);
    currentVoice = identifier;
  }

  // Split into word-runs and punctuation-runs; espeak drops punctuation, so
  // runs are phonemized separately and the marks are stitched back in.
  const parts = text.match(/[^.;:!?…,，。！？；：]+|[.;:!?…,，。！？；：]+/gu) ?? [];
  let out = '';
  for (const part of parts) {
    if (/^[.;:!?…,，。！？；：]+$/u.test(part)) {
      out += [...part].filter((c) => KEEPS.includes(c)).join('') + ' ';
      continue;
    }
    const r = w.text_to_phonemes(part, 1);
    let s: string;
    try {
      s = cString(ng, r.ptr);
    } finally {
      ng._free(r.ptr);
    }
    const phonemes = s.split(' | ').filter((c) => c.length > 0).join(' ').replaceAll('_', '');
    out += [...phonemes].filter((c) => KOKORO_VOCAB.has(c) || c === ' ').join('') + ' ';
  }
  return out.replace(/\s+/gu, ' ').trim();
}
