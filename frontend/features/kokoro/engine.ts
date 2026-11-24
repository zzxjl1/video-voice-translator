/**
 * Build the Kokoro engine from the files we ship. Shared by the worker and by the Node test
 * (scripts/test-kokoro.mjs), so the test exercises the exact assembly production uses.
 *
 * Why not `KokoroTTS.from_pretrained`: it only forwards `dtype` / `device`, NOT `model_file_name`.
 * Asking it for a custom file silently loads the default `model.onnx` (a file we do not ship)
 * instead. The KokoroTTS constructor is public, so assemble the two parts from_pretrained does —
 * model + tokenizer — with the option it drops.
 */
import { AutoTokenizer, StyleTextToSpeech2Model } from '@huggingface/transformers';
import { KokoroTTS } from 'kokoro-js';
import { KOKORO_MODEL, KOKORO_MODEL_FILE } from './assets';

export type EngineBackend = 'webgpu' | 'wasm' | 'cpu';
export type ProgressFn = (p: { status?: string; file?: string; loaded?: number; total?: number }) => void;

export async function loadKokoro(device: EngineBackend, progress?: ProgressFn): Promise<KokoroTTS> {
  const [model, tokenizer] = await Promise.all([
    // dtype 'fp32' = no filename suffix, so `model_file_name` is the file exactly as shipped.
    StyleTextToSpeech2Model.from_pretrained(KOKORO_MODEL, {
      device, dtype: 'fp32', model_file_name: KOKORO_MODEL_FILE, progress_callback: progress,
    } as never),
    AutoTokenizer.from_pretrained(KOKORO_MODEL, { progress_callback: progress } as never),
  ]);
  return new KokoroTTS(model, tokenizer);
}
