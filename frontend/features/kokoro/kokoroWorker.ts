/**
 * Kokoro TTS worker: the whole engine (model + G2P + espeak) runs here so the
 * UI thread never blocks during synthesis.
 *
 * - Model via transformers.js: WebGPU fp16 when the GPU is usable (163 MB,
 *   ~10x realtime), else WASM q8 (92 MB, ~0.7x). Falls back to WASM on any
 *   WebGPU failure. Model files come from HF (or hf-mirror.com if HF is
 *   unreachable) and are browser-cached.
 * - Voice bins (0.5 MB each) are fetched from jsDelivr — the official
 *   kokoro-js fetches them from HF, which is unreliable here; every kokoro-js
 *   voice id exists in the npm package on jsDelivr, English ones included.
 * - Phonemization: en via kokoro-js's built-in espeak (en-us), zh via zhG2P
 *   (misaki-format tones), fr/it/pt/es/hi via the full espeak-ng WASM build.
 *   The official kokoro-js registry is English-only; other languages go
 *   through generate_from_ids with our own phonemes.
 *
 * Protocol (postMessage):
 *   in:  { id, kind: 'synth' | 'warmup', text?, voice? }
 *   out: { id, type: 'progress' | 'device' | 'ok' | 'error', ... }
 */
/// <reference lib="webworker" />
import { KokoroTTS } from 'kokoro-js';
import { env } from '@huggingface/transformers';
import { zhG2P } from './zhG2P';
import { espeakPhonemize } from './espeakFull';

const MODEL = 'onnx-community/Kokoro-82M-v1.0-ONNX';
const VOICE_CDN = 'https://cdn.jsdelivr.net/npm/kokoro-js@1.2.1/voices';

// kokoro-js fetches voice bins from HF at runtime; redirect to jsDelivr.
const rawFetch = self.fetch.bind(self);
self.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : (input as Request)?.url ?? String(input);
  if (/\/voices\/[a-z]{2}_[\w]+\.bin(\?.*)?$/i.test(url)) {
    return rawFetch(`${VOICE_CDN}/${url.split('/').pop()}`, init);
  }
  return rawFetch(input as RequestInfo, init);
};

const post = (msg: unknown, transfer?: Transferable[]) =>
  (self as unknown as Worker).postMessage(msg, transfer ?? []);

interface Engine {
  tts: KokoroTTS;
  device: 'webgpu' | 'wasm';
  dtype: string;
}

let engine: Engine | null = null;
let preferWasm = false;

type EngineChoice = { device: 'webgpu' | 'wasm'; dtype: 'fp32' | 'q8'; model_file_name?: string };

/**
 * WebGPU → `model_q8f16.onnx`（86MB）：int8 权重 + fp16 计算，权重解量化后
 * MatMul 走 GPU，实测与 fp16 同速（~10.8x 实时）且是发布里最小的文件。
 * 不能用 dtype 'q8'：纯 int8 计算的 MatMulInteger 在 ort-web 的 WebGPU EP 没有
 * 实现，会回落 CPU（速度和 WASM 一样，0.7x）。q8f16 也不在 transformers.js 的
 * dtype 映射表里，所以用 dtype:'fp32' + model_file_name 直接指到该文件。
 * 其余 → WASM + q8（92MB，~0.7x）。
 */
async function pickDevice(): Promise<EngineChoice> {
  if (!preferWasm) {
    try {
      const gpu = (globalThis as { navigator?: { gpu?: { requestAdapter: () => Promise<unknown> } } }).navigator?.gpu;
      if (gpu && await gpu.requestAdapter()) {
        return { device: 'webgpu', dtype: 'fp32', model_file_name: 'model_q8f16' };
      }
    } catch { /* fall through to wasm */ }
  }
  return { device: 'wasm', dtype: 'q8' };
}

async function pickRemoteHost(): Promise<string> {
  try {
    await rawFetch(`https://huggingface.co/api/models/${MODEL}`, { method: 'HEAD', signal: AbortSignal.timeout(4000) });
    return 'https://huggingface.co';
  } catch {
    return 'https://hf-mirror.com';
  }
}

async function load(onProgress: (loaded: number, total: number) => void): Promise<Engine> {
  env.remoteHost = await pickRemoteHost();
  const choice = await pickDevice();
  const progress = (p: { status?: string; file?: string; loaded?: number; total?: number }) => {
    if (p.status === 'progress' && p.file?.endsWith('.onnx') && p.loaded && p.total) onProgress(p.loaded, p.total);
  };
  try {
    const tts = await KokoroTTS.from_pretrained(MODEL, { ...choice, progress_callback: progress });
    return { tts, device: choice.device, dtype: choice.model_file_name ?? choice.dtype };
  } catch (e) {
    if (choice.device === 'webgpu') {
      // WebGPU adapter answered but the model failed (driver quirks etc.) — WASM always works.
      post({ type: 'device', device: 'wasm-fallback', message: String((e as Error)?.message ?? e) });
      const tts = await KokoroTTS.from_pretrained(MODEL, { device: 'wasm', dtype: 'q8', progress_callback: progress });
      preferWasm = true;
      return { tts, device: 'wasm', dtype: 'q8' };
    }
    throw e;
  }
}

async function getEngine(): Promise<Engine> {
  if (!engine) engine = await load((loaded, total) => post({ type: 'progress', phase: 'model', loaded, total }));
  return engine;
}

function voiceLanguage(voice: string): 'zh' | 'en' | 'fr' | 'it' | 'pt' | 'es' | 'hi' | 'ja' | '' {
  if (/^(zf|zm)_/.test(voice)) return 'zh';
  if (/^(af|am|bf|bm)_/.test(voice)) return 'en';
  if (/^ff_/.test(voice)) return 'fr';
  if (/^(if|im)_/.test(voice)) return 'it';
  if (/^(pf|pm)_/.test(voice)) return 'pt';
  if (/^(ef|em)_/.test(voice)) return 'es';
  if (/^(hf|hm)_/.test(voice)) return 'hi';
  if (/^(jf|jm)_/.test(voice)) return 'ja';
  return '';
}

const ESPEAK_CODE: Record<string, string> = { fr: 'fr-fr', it: 'it', pt: 'pt-br', es: 'es', hi: 'hi' };

async function phonemize(text: string, voice: string): Promise<string | null> {
  const lang = voiceLanguage(voice);
  if (lang === 'zh') return zhG2P(text).phonemes;
  if (lang === 'en') return null;  // kokoro-js built-in path
  if (ESPEAK_CODE[lang]) return espeakPhonemize(text, ESPEAK_CODE[lang]);
  throw new Error(`音色 ${voice} 不支持浏览器合成（ja 需要 OpenJTalk 级 G2P）`);
}

/** Sentence splitter; punctuation stays attached to its sentence. */
function splitSentences(text: string, lang: string): string[] {
  const finals = lang === 'zh' ? '。！？；…' : '.!?;';
  const out: string[] = [];
  let buf = '';
  for (const ch of text) {
    buf += ch;
    if (finals.includes(ch) || ch === '\n') {
      if (buf.trim()) out.push(buf.trim());
      buf = '';
    }
  }
  if (buf.trim()) out.push(buf.trim());
  return out.length ? out : [text];
}

function wavEncode(audio: Float32Array, sampleRate = 24000): ArrayBuffer {
  const pcm = new Int16Array(audio.length);
  for (let i = 0; i < audio.length; i++) {
    pcm[i] = Math.max(-32768, Math.min(32767, Math.round(Math.max(-1, Math.min(1, audio[i])) * 32767)));
  }
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const v = new DataView(buf);
  const str = (o: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); str(8, 'WAVE');
  str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, sampleRate, true); v.setUint32(28, sampleRate * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, 'data'); v.setUint32(40, pcm.length * 2, true);
  new Int16Array(buf, 44).set(pcm);
  return buf;
}

/** Official guidance: best around 100-200 tokens, max 511. Pack under that. */
const PACK_TOKENS = 450;
const MAX_IDS = 511;
const GAP = Math.round(24000 * 0.15);

async function synthIds(tts: KokoroTTS, ids: unknown, voice: string): Promise<Float32Array> {
  const audio = await tts.generate_from_ids(ids as never, { voice: voice as never, speed: 1 });
  return audio.audio as Float32Array;
}

async function synthesize(text: string, voice: string): Promise<Float32Array> {
  const { tts } = await getEngine();
  const lang = voiceLanguage(voice);
  const sentences = splitSentences(text, lang);
  interface Seg { phonemes: string | null; tokens: number }
  const segs: Seg[] = [];
  for (const s of sentences) {
    const phonemes = await phonemize(s, voice);
    if (phonemes === null) {
      // English: kokoro-js's own espeak path (with number normalization); it
      // phonemizes + tokenizes internally, so estimate ~1 token per 2.5 chars.
      segs.push({ phonemes: null, tokens: Math.ceil(s.length / 2.5) });
      continue;
    }
    const { input_ids } = tts.tokenizer(phonemes, { truncation: true, max_length: MAX_IDS });
    segs.push({ phonemes, tokens: (input_ids as { dims: number[] }).dims.at(-1) ?? 0 });
  }
  // Greedy pack: short sentences bundle together (official: bundling sounds better).
  const packs: Seg[][] = [];
  for (const seg of segs) {
    const last = packs.at(-1);
    const size = (last ?? []).reduce((n, x) => n + x.tokens, 0);
    if (last && size + seg.tokens <= PACK_TOKENS) last.push(seg);
    else packs.push([seg]);
  }
  const chunks: Float32Array[] = [];
  for (const pack of packs) {
    const enPack = pack.some((s) => s.phonemes === null);
    if (enPack) {
      // English: one generate() per sentence (its tokenizer truncates at the model max).
      for (const seg of pack) {
        const s = sentences[segs.indexOf(seg)] ?? '';
        const audio = await tts.generate(s, { voice: voice as never, speed: 1 });
        chunks.push(audio.audio as Float32Array, new Float32Array(GAP));
      }
      continue;
    }
    const phonemes = pack.map((s) => s.phonemes).join(' ');
    const { input_ids } = tts.tokenizer(phonemes, { truncation: true, max_length: MAX_IDS });
    chunks.push(await synthIds(tts, input_ids, voice), new Float32Array(GAP));
  }
  chunks.pop();  // drop trailing gap
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Float32Array(total);
  let at = 0;
  for (const c of chunks) { out.set(c, at); at += c.length; }
  return out;
}

self.onmessage = async (ev: MessageEvent) => {
  const { id, kind, text, voice } = ev.data as { id: number; kind: 'synth' | 'warmup'; text?: string; voice?: string };
  try {
    const t0 = performance.now();
    const eng = await getEngine();
    post({ id, type: 'device', device: eng.device, dtype: eng.dtype });
    if (kind === 'warmup') { post({ id, type: 'ok', wav: new ArrayBuffer(0), ms: Math.round(performance.now() - t0) }); return; }
    const audio = await synthesize(text ?? '', voice ?? '');
    const wav = wavEncode(audio);
    post({ id, type: 'ok', wav, ms: Math.round(performance.now() - t0) }, [wav]);
  } catch (e) {
    const message = String((e as Error)?.message ?? e);
    // A WebGPU engine that loaded but cannot generate: rebuild on WASM once.
    if (engine?.device === 'webgpu' && !preferWasm) {
      preferWasm = true;
      engine = null;
      try {
        await getEngine();
        if (kind === 'synth') {
          const audio = await synthesize(text ?? '', voice ?? '');
          const wav = wavEncode(audio);
          post({ id, type: 'ok', wav, ms: 0 }, [wav]);
          return;
        }
        post({ id, type: 'ok', wav: new ArrayBuffer(0), ms: 0 });
        return;
      } catch { /* report the original error below */ }
    }
    post({ id, type: 'error', message });
  }
};
