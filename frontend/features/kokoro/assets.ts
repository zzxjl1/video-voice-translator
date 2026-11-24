/**
 * Where the local-dubbing engine's files live.
 *
 * Everything — model, tokenizer, voice bins, espeak-ng — is committed to the repository
 * (backend/models/kokoro-82m/) and served by OUR backend (`GET /api/models/kokoro/*`).
 * Nothing is downloaded at deploy time and the browser contacts no third-party host: not
 * HuggingFace, not jsDelivr. The ONNX Runtime wasm that runs the model is bundled by Vite
 * (see kokoroWorker.ts), the same way the vocal-separation model's runtime is.
 */

/** transformers.js `localModelPath`: <root>/<model id>/<file>. */
export const MODELS_ROOT = '/api/models/';

/** Model id as transformers.js sees it; maps to backend/models/kokoro-82m via the /api/models/kokoro route. */
export const KOKORO_MODEL = 'kokoro';
export const KOKORO_BASE = `${MODELS_ROOT}${KOKORO_MODEL}`;

/**
 * The model file under `onnx/` (no extension): the official dynamic-int8 graph rewritten so that
 * ORT's WebGPU backend can run all of it (backend/scripts/kokoro_int8.py). The SAME file serves
 * WebGPU and WASM.
 *
 * CHANGE THIS NAME whenever the file's content changes (model_int8 -> model_int8_2, and the same
 * in backend/scripts/prepare_kokoro_models.py): transformers.js caches model files by URL and never
 * revalidates, so a replaced file under the same name would leave returning users on the old model.
 * scripts/test-kokoro.mjs checks the two names agree.
 */
export const KOKORO_MODEL_FILE = 'model_int8';

export const voiceUrl = (name: string) => `${KOKORO_BASE}/voices/${name}`;
export const espeakModuleUrl = () => `${self.location.origin}${KOKORO_BASE}/espeak/espeak-ng.js`;
