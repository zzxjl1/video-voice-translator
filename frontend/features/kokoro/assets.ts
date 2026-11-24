/**
 * Where the local-dubbing engine's files live.
 *
 * Everything — model, tokenizer, 54 voice bins, espeak-ng — is served by OUR
 * backend (`GET /api/models/kokoro/*`, files placed there by
 * backend/scripts/fetch_kokoro_models.py). At runtime the browser contacts no
 * third-party host: not HuggingFace, not jsDelivr. The ONNX Runtime wasm that
 * runs the model is bundled by Vite (see kokoroWorker.ts), the same way the
 * vocal-separation model's runtime is.
 */
export const KOKORO_BASE = '/api/models/kokoro';

/** Model id as laid out under KOKORO_BASE (mirrors the HuggingFace repo path). */
export const KOKORO_MODEL = 'onnx-community/Kokoro-82M-v1.0-ONNX';

export const voiceUrl = (name: string) => `${KOKORO_BASE}/voices/${name}`;
export const espeakModuleUrl = () => `${self.location.origin}${KOKORO_BASE}/espeak/espeak-ng.js`;
