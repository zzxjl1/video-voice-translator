/**
 * Kokoro TTS worker: the whole engine (model + G2P + espeak) runs here so the
 * UI thread never blocks during synthesis.
 *
 * - Model via transformers.js, loaded from OUR server (see assets.ts): ONE file,
 *   `model_int8` (92 MB), on WebGPU (~11x realtime) when the GPU is usable, else on WASM
 *   (~0.8x single-threaded). The browser never contacts HuggingFace or a CDN.
 * - ONNX Runtime's wasm/mjs are bundled by Vite and handed to ORT explicitly.
 *   Without that, transformers.js points ORT at jsDelivr (a hidden runtime
 *   dependency that fails behind firewalls).
 * - Voice bins (0.5 MB each) come from our server too; kokoro-js hardcodes the
 *   HuggingFace URL, so its fetch is redirected below.
 * - Phonemization: en via kokoro-js's built-in espeak (en-us, with number
 *   normalization); zh via zhG2P (misaki-format tones; Latin words go through
 *   the English phonemizer); fr/it/pt via the full espeak-ng WASM build.
 *   The official kokoro-js registry is English-only; other languages go
 *   through generate_from_ids with our own phonemes.
 *
 * Requests are processed ONE AT A TIME (queue below): the engine is a single
 * ORT session, so two overlapping requests must not load the model twice or
 * run the session concurrently.
 *
 * Protocol (postMessage):
 *   in:  { id, kind: 'synth' | 'warmup', text?, voice? }
 *   out: { id, type: 'progress' | 'device' | 'ok' | 'error', ... }  — every
 *        message carries the id of the request it belongs to.
 */
/// <reference lib="webworker" />
import type { KokoroTTS } from 'kokoro-js';
import { env } from '@huggingface/transformers';
import { phonemize as englishPhonemize } from 'phonemizer';
import ortMjs from 'kokoro-ort:ort-wasm-simd-threaded.jsep.mjs?url';
import ortWasm from 'kokoro-ort:ort-wasm-simd-threaded.jsep.wasm?url';
import { MODELS_ROOT, voiceUrl } from './assets';
import { KOKORO_VOCAB } from './catalog';
import { loadKokoro } from './engine';
import { espeakPhonemize } from './espeakFull';
import { bisect, splitSentences } from './text';
import { zhG2PWith } from './zhG2P';

// ---- no third-party hosts at runtime ---------------------------------------
env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = MODELS_ROOT;
// transformers.js defaults ORT's wasm location to jsDelivr unless one is set.
(env.backends.onnx as { wasm: { wasmPaths: unknown } }).wasm.wasmPaths = { mjs: ortMjs, wasm: ortWasm };

// kokoro-js fetches voice bins from HuggingFace at runtime; serve them ourselves.
const rawFetch = self.fetch.bind(self);
self.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : (input as Request)?.url ?? String(input);
  if (/\/voices\/[a-z]{2}_[\w]+\.bin(\?.*)?$/i.test(url)) {
    return rawFetch(voiceUrl((url.split('?')[0]).split('/').pop() as string), { ...init, credentials: 'same-origin' });
  }
  return rawFetch(input as RequestInfo, init);
};

const post = (msg: unknown, transfer?: Transferable[]) =>
  (self as unknown as Worker).postMessage(msg, transfer ?? []);

/** The id of the request being processed: progress / device messages are reported against it. */
let current = 0;

interface Engine {
  tts: KokoroTTS;
  device: 'webgpu' | 'wasm';
}

let engine: Engine | null = null;
// `new Worker(url, { name: 'kokoro-wasm' })` forces the WASM engine: for diagnosing WebGPU-only problems
// (compare outputs of the two engines) without a code change.
let preferWasm = self.name === 'kokoro-wasm';

/** The model session failed to run (as opposed to: bad text, missing file). Only these justify a WASM retry. */
class EngineError extends Error {}

/**
 * One model file for both devices. WebGPU when the browser has a usable adapter, else WASM.
 *
 * It is the official dynamic-int8 graph with its integer operators rewritten (see
 * backend/scripts/kokoro_int8.py). ORT's WebGPU backend has no MatMulInteger / ConvInteger, so the stock
 * int8 file ran at CPU speed there; and its DequantizeLinear is wrong for some variants, so the rewrite
 * dequantizes with Cast/Sub/Mul instead. The rewritten file runs ~11x realtime on WebGPU (about 20%
 * behind fp16) at 92 MB instead of 163 MB.
 * Do NOT swap in `model_q8f16`: on ort-web WebGPU its audio comes out ~8x too long and wrong.
 */
async function pickDevice(): Promise<'webgpu' | 'wasm'> {
  if (!preferWasm) {
    try {
      const gpu = (globalThis as { navigator?: { gpu?: { requestAdapter: () => Promise<unknown> } } }).navigator?.gpu;
      if (gpu && await gpu.requestAdapter()) return 'webgpu';
    } catch { /* fall through to wasm */ }
  }
  return 'wasm';
}

/** A failure to GET one of our files (missing, not logged in, offline) — retrying on another device cannot fix it. */
const isAssetError = (e: unknown) =>
  /Could not locate|Unauthorized|\b40[134]\b|Failed to fetch|NetworkError|Load failed/i.test(String((e as Error)?.message ?? e));

function explainAssetError(e: unknown): Error {
  const msg = String((e as Error)?.message ?? e);
  if (/Unauthorized|\b401\b/i.test(msg)) return new Error('未登录或登录已过期，无法加载本地配音资源');
  if (/Could not locate|\b404\b/i.test(msg)) {
    return new Error(`本地配音资源缺失（${msg}）。这些文件应随代码库提交（backend/models/kokoro-82m），请检查部署是否完整`);
  }
  return e instanceof Error ? e : new Error(msg);
}

async function load(): Promise<Engine> {
  const device = await pickDevice();
  const progress = (p: { status?: string; file?: string; loaded?: number; total?: number }) => {
    if (p.status === 'progress' && p.file?.endsWith('.onnx') && p.loaded && p.total) {
      post({ id: current, type: 'progress', phase: 'model', loaded: p.loaded, total: p.total });
    }
  };
  try {
    return { tts: await loadKokoro(device, progress), device };
  } catch (e) {
    if (device === 'webgpu' && !isAssetError(e)) {
      // WebGPU adapter answered but the session could not be created (driver quirks etc.) — WASM always works.
      post({ id: current, type: 'device', device: 'wasm-fallback', message: String((e as Error)?.message ?? e) });
      preferWasm = true;
      try {
        return { tts: await loadKokoro('wasm', progress), device: 'wasm' };
      } catch (e2) {
        throw explainAssetError(e2);
      }
    }
    throw explainAssetError(e);
  }
}

async function getEngine(): Promise<Engine> {
  if (!engine) engine = await load();
  return engine;
}

// ---- phonemization -----------------------------------------------------------

function voiceLanguage(voice: string): 'zh' | 'en' | 'fr' | 'it' | 'pt' | 'ja' | '' {
  if (/^(zf|zm)_/.test(voice)) return 'zh';
  if (/^(af|am|bf|bm)_/.test(voice)) return 'en';
  if (/^ff_/.test(voice)) return 'fr';
  if (/^(if|im)_/.test(voice)) return 'it';
  if (/^(pf|pm)_/.test(voice)) return 'pt';
  if (/^(jf|jm)_/.test(voice)) return 'ja';
  return '';
}

const ESPEAK_CODE: Record<string, string> = { fr: 'fr-fr', it: 'it', pt: 'pt-br' };

/** Keep only characters the Kokoro tokenizer knows; anything else would become an unknown token. */
const keepVocab = (phonemes: string) => [...phonemes].filter((c) => KOKORO_VOCAB.has(c)).join('');

/**
 * An English word inside Chinese text (brand names, acronyms) -> Kokoro phonemes.
 * Same espeak en-us + the same IPA fix-ups kokoro-js applies to English input.
 */
async function englishWord(word: string): Promise<string> {
  const ipa = (await englishPhonemize(word, 'en-us')).join(' ');
  return keepVocab(ipa.replace(/ʲ/g, 'j').replace(/r/g, 'ɹ').replace(/x/g, 'k').replace(/ɬ/g, 'l'));
}

async function phonemize(text: string, voice: string): Promise<string> {
  const lang = voiceLanguage(voice);
  if (lang === 'zh') return keepVocab((await zhG2PWith(text, englishWord)).phonemes);
  if (ESPEAK_CODE[lang]) return espeakPhonemize(text, ESPEAK_CODE[lang]);
  throw new Error(`音色 ${voice} 不支持浏览器合成（ja 需要 OpenJTalk 级 G2P）`);
}

// ---- synthesis -----------------------------------------------------------------

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

/** Run the model; any failure here is the engine's, and the only kind that justifies a WASM retry. */
async function runModel<T>(fn: () => Promise<T>): Promise<T> {
  try { return await fn(); } catch (e) { throw new EngineError(String((e as Error)?.message ?? e)); }
}

const countTokens = (tts: KokoroTTS, phonemes: string): number =>
  ((tts.tokenizer(phonemes, { truncation: false }).input_ids as { dims: number[] }).dims.at(-1)) ?? 0;

interface Piece { phonemes: string; tokens: number }

/**
 * Phonemize one sentence. A sentence whose phonemes would not fit the model's
 * 511-token window is split in two and retried — the tokenizer's own
 * `truncation` would otherwise silently cut the end of the sentence off.
 */
async function toPieces(tts: KokoroTTS, sentence: string, voice: string): Promise<Piece[]> {
  const phonemes = (await phonemize(sentence, voice)).trim();
  if (!phonemes) return [];                                   // nothing speakable (only symbols)
  const tokens = countTokens(tts, phonemes);
  if (tokens > MAX_IDS - 2) {
    if (sentence.length < 8) throw new Error('这一句无法切分到模型能处理的长度');
    const [a, b] = bisect(sentence);
    return [...await toPieces(tts, a, voice), ...await toPieces(tts, b, voice)];
  }
  return [{ phonemes, tokens }];
}

async function synthesize(text: string, voice: string): Promise<Float32Array> {
  const { tts } = await getEngine();
  const lang = voiceLanguage(voice);
  const sentences = splitSentences(text, lang);
  const chunks: Float32Array[] = [];

  if (lang === 'en') {
    // kokoro-js phonemizes (and normalizes numbers) internally; one call per sentence.
    for (const s of sentences) {
      const audio = await runModel(() => tts.generate(s, { voice: voice as never, speed: 1 }));
      chunks.push(audio.audio as Float32Array, new Float32Array(GAP));
    }
  } else {
    const pieces: Piece[] = [];
    for (const s of sentences) pieces.push(...await toPieces(tts, s, voice));
    // Greedy pack: short sentences bundle together (official: bundling sounds better).
    const packs: Piece[][] = [];
    for (const p of pieces) {
      const last = packs.at(-1);
      const size = (last ?? []).reduce((n, x) => n + x.tokens, 0);
      if (last && size + p.tokens <= PACK_TOKENS) last.push(p); else packs.push([p]);
    }
    for (const pack of packs) {
      const joined = pack.map((p) => p.phonemes).join(' ');
      const { input_ids } = tts.tokenizer(joined, { truncation: true, max_length: MAX_IDS });
      const audio = await runModel(() => tts.generate_from_ids(input_ids as never, { voice: voice as never, speed: 1 }));
      chunks.push(audio.audio as Float32Array, new Float32Array(GAP));
    }
  }
  if (!chunks.length) throw new Error('没有可念的内容');
  chunks.pop();                                               // drop trailing gap
  const out = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
  let at = 0;
  for (const c of chunks) { out.set(c, at); at += c.length; }
  return out;
}

/** Synthesize; if the WebGPU session itself fails mid-run, rebuild on WASM once and retry. */
async function synthesizeWithFallback(text: string, voice: string): Promise<Float32Array> {
  try {
    return await synthesize(text, voice);
  } catch (e) {
    // Only a model/session failure justifies a WASM rebuild (another ~90 MB and ~15x slower for the rest of
    // the session). Bad text, an unsupported voice or an unreachable file would just fail again.
    if (!(e instanceof EngineError) || engine?.device !== 'webgpu' || preferWasm) throw e;
    post({ id: current, type: 'device', device: 'wasm-fallback', message: e.message });
    preferWasm = true;
    engine = null;
    return await synthesize(text, voice);
  }
}

// ---- request queue ----------------------------------------------------------------

async function handle(msg: { id: number; kind: 'synth' | 'warmup'; text?: string; voice?: string }): Promise<void> {
  const { id, kind, text, voice } = msg;
  current = id;
  try {
    const t0 = performance.now();
    const eng = await getEngine();
    post({ id, type: 'device', device: eng.device });
    if (kind === 'warmup') {
      post({ id, type: 'ok', wav: new ArrayBuffer(0), ms: Math.round(performance.now() - t0) });
      return;
    }
    const wav = wavEncode(await synthesizeWithFallback(text ?? '', voice ?? ''));
    post({ id, type: 'ok', wav, ms: Math.round(performance.now() - t0) }, [wav]);
  } catch (e) {
    post({ id, type: 'error', message: String((e as Error)?.message ?? e) });
  }
}

let queue: Promise<void> = Promise.resolve();
self.onmessage = (ev: MessageEvent) => {
  queue = queue.then(() => handle(ev.data));
};
