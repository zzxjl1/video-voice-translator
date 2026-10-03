/**
 * Browser-side vocal separation client.
 *
 * Pipeline: download the MDX-Net ONNX model (cached) -> build an
 * onnxruntime-web session (WebGPU, falling back to WASM) -> decode the audio
 * of the selected video file locally -> separate -> encode the two stems as
 * WAV -> upload them to the server.
 *
 * Running this in the browser is what makes separation possible at all on a
 * 2 GB / single-core / no-GPU server: the server never loads PyTorch, and no
 * per-minute API cost is incurred.
 *
 * Notes / caveats
 * ---------------
 * - The model must be `UVR-MDX-NET-Inst_HQ_3.onnx` (dim_f=3072, dim_t=256,
 *   n_fft=6144). Parameters come from `GET /api/models/separator`.
 * - Inference is the slow part: expect roughly 1.5x realtime per pass on
 *   single-threaded WASM, and several times faster on WebGPU. Progress is
 *   reported so the UI can show an ETA.
 * - Audio is resampled to 44.1 kHz because the model was trained at 44.1 kHz;
 *   feeding it 48 kHz would shift every frequency.
 * - Decoding uses `decodeAudioData`, so the container/codec must be one the
 *   browser can decode (MP4/AAC, WebM/Opus, etc.). If it fails we surface a
 *   clear error and the caller can fall back to running without separation.
 */
import { loadOrt } from './onnxRuntime.js';
import { separateTrack, validateSeparatorParams } from './mdxSeparator.js';

export const MODEL_SAMPLE_RATE = 44100;
const CACHE_NAME = 'vvt-separator-models-v1';

/* ------------------------------------------------------------------ *
 * Memory hygiene
 * ------------------------------------------------------------------ */

/**
 * Best-effort wipe of audio buffers once they are no longer needed.
 *
 * JavaScript gives no guarantee that this actually erases the bytes (the GC
 * may have copied them, and the engine may keep the backing store), so treat
 * this as hygiene rather than a security boundary. What it does guarantee is
 * that we stop holding tens of megabytes of the user's audio alive after the
 * work is done.
 *
 * @param {...any} values Float32Array / Uint8Array / ArrayBuffer instances
 */
export function zeroBuffers(...values) {
  for (const value of values) {
    if (!value) continue;
    if (Array.isArray(value)) {
      zeroBuffers(...value);
      continue;
    }
    try {
      if (ArrayBuffer.isView(value)) {
        value.fill(0);
      } else if (value instanceof ArrayBuffer) {
        new Uint8Array(value).fill(0);
      }
    } catch {
      /* detached or already released — nothing to do */
    }
  }
}

/* ------------------------------------------------------------------ *
 * Model download
 * ------------------------------------------------------------------ */

/**
 * Cache key deliberately excludes the token so repeated downloads with
 * different tokens share one cache entry.
 * @param {{download_url: string, fingerprint: string}} info
 */
function modelCacheKey(info) {
  return `${info.download_url}?v=${info.fingerprint}`;
}

/** Is the model already cached? Used to size up the first-run download. */
export async function isModelCached(info) {
  if (typeof caches === 'undefined') return false;
  try {
    const cache = await caches.open(CACHE_NAME);
    return Boolean(await cache.match(modelCacheKey(info)));
  } catch {
    return false;
  }
}

export async function clearModelCache() {
  if (typeof caches === 'undefined') return;
  await caches.delete(CACHE_NAME);
}

/**
 * Download the model weights, streaming progress and caching the result so
 * later videos do not re-download 64 MB.
 *
 * @param {{download_url: string, fingerprint: string, size_bytes: number}} info
 * @param {(loaded: number, total: number, fromCache: boolean) => void} [onProgress]
 * @param {string} [token] short-lived token issued by the server
 * @returns {Promise<ArrayBuffer>}
 */
export async function fetchModelBuffer(info, onProgress, token) {
  const cacheKey = modelCacheKey(info);

  if (typeof caches !== 'undefined') {
    try {
      const cache = await caches.open(CACHE_NAME);
      const hit = await cache.match(cacheKey);
      if (hit) {
        const buf = await hit.arrayBuffer();
        onProgress?.(buf.byteLength, buf.byteLength, true);
        return buf;
      }
    } catch (e) {
      console.warn('[separator] cache lookup failed, downloading', e);
    }
  }

  const url = token ? `${cacheKey}&token=${encodeURIComponent(token)}` : cacheKey;
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(
      response.status === 401 || response.status === 403
        ? 'Model download was rejected (token missing, expired, or not valid for this video).'
        : `Model download failed: HTTP ${response.status}`
    );
  }

  const total = Number(response.headers.get('content-length')) || info.size_bytes || 0;
  let buffer;

  if (response.body && typeof response.body.getReader === 'function') {
    const reader = response.body.getReader();
    const chunks = [];
    let loaded = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      loaded += value.length;
      onProgress?.(loaded, total, false);
    }
    buffer = await new Blob(chunks).arrayBuffer();
  } else {
    buffer = await response.arrayBuffer();
    onProgress?.(buffer.byteLength, total, false);
  }

  if (typeof caches !== 'undefined') {
    try {
      const cache = await caches.open(CACHE_NAME);
      // Stored under the token-free key so the next run (with a fresh token)
      // hits the cache instead of downloading again.
      await cache.put(
        cacheKey,
        new Response(buffer.slice(0), { headers: { 'Content-Type': 'application/octet-stream' } })
      );
    } catch (e) {
      console.warn('[separator] failed to cache model', e);
    }
  }

  return buffer;
}

/* ------------------------------------------------------------------ *
 * ONNX Runtime session
 * ------------------------------------------------------------------ */

/**
 * Create an inference session, preferring WebGPU and falling back to WASM.
 * @param {ArrayBuffer} modelBuffer
 * @param {(backend: 'webgpu'|'wasm', error?: Error) => void} [onBackend]
 */
export async function createSeparatorSession(modelBuffer, onBackend) {
  const ort = await loadOrt();

  // Threading requires SharedArrayBuffer, which requires COOP/COEP headers.
  // Without them ORT silently runs single-threaded, which is fine (we do not
  // require those headers so the rest of the app is unaffected).
  const threads =
    typeof SharedArrayBuffer !== 'undefined'
      ? Math.max(1, Math.min(4, (globalThis.navigator?.hardwareConcurrency || 2) - 1))
      : 1;
  ort.env.wasm.numThreads = threads;
  ort.env.logLevel = 'error';

  if (globalThis.navigator?.gpu) {
    try {
      const session = await ort.InferenceSession.create(modelBuffer, {
        executionProviders: ['webgpu'],
        graphOptimizationLevel: 'all',
      });
      onBackend?.('webgpu');
      return session;
    } catch (e) {
      console.warn('[separator] WebGPU session failed, falling back to WASM', e);
      onBackend?.('wasm', e);
    }
  } else {
    onBackend?.('wasm');
  }

  return ort.InferenceSession.create(modelBuffer, {
    executionProviders: ['wasm'],
    graphOptimizationLevel: 'all',
  });
}

/**
 * Wrap a session as the `runModel` callback expected by `separateTrack`.
 * @param {import('onnxruntime-web').InferenceSession} session
 * @param {{dimF: number, segmentSize: number}} params
 */
export function makeRunModel(session, params) {
  const shape = [1, 4, params.dimF, params.segmentSize];
  return async (specData, frames) => {
    const ort = await loadOrt();
    const input = new ort.Tensor('float32', specData, [shape[0], shape[1], shape[2], frames]);
    const output = await session.run({ input });
    const data = output.output.data;
    // ORT may reuse the output buffer between calls, so copy.
    return new Float32Array(data);
  };
}

/* ------------------------------------------------------------------ *
 * Audio decode / encode
 * ------------------------------------------------------------------ */

/**
 * Decode a video/audio file to stereo float channels at `targetRate`.
 * @param {File|Blob} file
 * @param {number} [targetRate]
 * @returns {Promise<{channels: Float32Array[], sampleRate: number, duration: number}>}
 */
export async function decodeAudio(file, targetRate = MODEL_SAMPLE_RATE) {
  const arrayBuffer = await file.arrayBuffer();
  const Ctx = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!Ctx) throw new Error('Web Audio API is not available in this browser');

  const decodeCtx = new Ctx();
  let decoded;
  try {
    decoded = await decodeCtx.decodeAudioData(arrayBuffer.slice(0));
  } catch (e) {
    throw new Error(
      'Could not decode this file in the browser (unsupported container or codec). ' +
        'Disable browser separation to process it on the server instead.'
    );
  } finally {
    decodeCtx.close?.();
  }

  let buffer = decoded;
  if (Math.abs(decoded.sampleRate - targetRate) > 1) {
    const OfflineCtx = globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext;
    if (OfflineCtx) {
      const target = new OfflineCtx(decoded.numberOfChannels, Math.ceil(decoded.duration * targetRate), targetRate);
      const source = target.createBufferSource();
      source.buffer = decoded;
      source.connect(target.destination);
      source.start(0);
      buffer = await target.startRendering();
    } else {
      console.warn('[separator] no OfflineAudioContext; using the original sample rate');
    }
  }

  const nCh = buffer.numberOfChannels;
  const channels = [];
  for (let c = 0; c < 2; c++) {
    // mono sources are duplicated so the model always sees a stereo pair
    const src = buffer.getChannelData(Math.min(c, nCh - 1));
    channels.push(Float32Array.from(src));
  }

  return { channels, sampleRate: buffer.sampleRate, duration: buffer.duration };
}

function floatToInt16(sample) {
  const clamped = sample < -1 ? -1 : sample > 1 ? 1 : sample;
  return clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
}

/**
 * Encode channels as 16-bit PCM WAV.
 * @param {Float32Array[]} channels
 * @param {number} sampleRate
 * @param {{mono?: boolean}} [opts]
 * @returns {Blob}
 */
export function encodeWav(channels, sampleRate, opts = {}) {
  const mono = Boolean(opts.mono);
  const frames = channels[0].length;
  const outChannels = mono ? 1 : channels.length;
  const dataBytes = frames * outChannels * 2;

  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);
  const writeAscii = (offset, text) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };

  writeAscii(0, 'RIFF');
  view.setUint32(4, 36 + dataBytes, true);
  writeAscii(8, 'WAVE');
  writeAscii(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, outChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * outChannels * 2, true);
  view.setUint16(32, outChannels * 2, true);
  view.setUint16(34, 16, true);
  writeAscii(36, 'data');
  view.setUint32(40, dataBytes, true);

  let offset = 44;
  if (mono) {
    const n = channels.length;
    for (let i = 0; i < frames; i++) {
      let sum = 0;
      for (let c = 0; c < n; c++) sum += channels[c][i];
      view.setInt16(offset, floatToInt16(sum / n), true);
      offset += 2;
    }
  } else {
    for (let i = 0; i < frames; i++) {
      for (let c = 0; c < channels.length; c++) {
        view.setInt16(offset, floatToInt16(channels[c][i]), true);
        offset += 2;
      }
    }
  }

  return new Blob([buffer], { type: 'audio/wav' });
}

/* ------------------------------------------------------------------ *
 * Full flow
 * ------------------------------------------------------------------ */

/**
 * Download + separate + encode + upload, with progress reporting.
 *
 * The browser is used purely as a compute node: it holds no pipeline state,
 * finishes by pushing the stems straight back to the server, and then wipes
 * every audio buffer it touched.
 *
 * @param {object} args
 * @param {{channels: Float32Array[], sampleRate: number, duration: number}} args.audio
 *        as returned by `decodeAudio()`
 * @param {{params: object, size_mb: number, fingerprint: string, download_url: string,
 *          size_bytes: number}} args.info response of GET /api/models/separator
 * @param {string} [args.token]      short-lived token issued by the server
 * @param {(msg: string) => void} [args.onLog]
 * @param {(stage: string, payload: any) => void} [args.onProgress]
 * @param {(vocalsBlob: Blob, backgroundBlob: Blob) => Promise<any>} args.upload
 * @param {{aborted: boolean}} [args.signal]
 */
export async function separateAndUpload({
  audio,
  info,
  token,
  onLog,
  onProgress,
  upload,
  signal,
}) {
  const log = (m) => onLog?.(m);

  // Params come from the server only — see validateSeparatorParams().
  const params = validateSeparatorParams(info.params);

  let modelBuffer = null;
  let session = null;

  try {
    log(`Fetching separation model (${info.size_mb} MB)...`);
    modelBuffer = await fetchModelBuffer(
      info,
      (loaded, total, fromCache) => {
        if (fromCache) {
          log('Separation model loaded from browser cache.');
        } else if (total > 0) {
          onProgress?.('model', { loaded, total, percent: (loaded / total) * 100 });
        }
      },
      token
    );
    if (signal?.aborted) throw new Error('aborted');

    log('Creating inference session...');
    session = await createSeparatorSession(modelBuffer, (backend, err) => {
      if (backend === 'webgpu') log('Using WebGPU backend.');
      else log(`Using WASM backend${err ? ` (WebGPU unavailable: ${err.message})` : ''}.`);
    });
    if (signal?.aborted) throw new Error('aborted');

    const runModel = makeRunModel(session, params);

    const started = Date.now();
    const result = await separateTrack(runModel, audio.channels, params, {
      sampleRate: audio.sampleRate,
      onProgress: (done, total, window, windows) => {
        onProgress?.('separate', { done, total, window, windows });
      },
      signal,
    });
    const elapsed = (Date.now() - started) / 1000;

    const minutes = audio.channels[0].length / audio.sampleRate / 60;
    log(
      `Separation finished in ${elapsed.toFixed(1)}s for ${minutes.toFixed(1)} min of audio ` +
        `(${((minutes * 60) / elapsed).toFixed(1)}x realtime).`
    );

    // Background keeps stereo; vocals only feed ASR and cloning, so mono halves
    // the upload size at no practical cost.
    const vocalsBlob = encodeWav([result.vocals[0]], audio.sampleRate, { mono: true });
    const backgroundBlob = encodeWav(result.instrumental, audio.sampleRate, { mono: false });

    log(
      `Uploading stems (vocals ${(vocalsBlob.size / 1048576).toFixed(1)} MB, ` +
        `background ${(backgroundBlob.size / 1048576).toFixed(1)} MB)...`
    );
    const uploadResult = await upload(vocalsBlob, backgroundBlob);
    log('Stems delivered to the server.');

    // ---- Release everything as soon as the server has the result ----
    zeroBuffers(result.instrumental, result.vocals, audio.channels);
    modelBuffer = null;

    return { uploadResult, elapsed, stats: result.stats };
  } finally {
    // Runs on success, failure and abort alike: never leave a GPU session or
    // the user's audio resident in the tab.
    try {
      await session?.release?.();
    } catch {
      /* ignore */
    }
    zeroBuffers(audio.channels);
    if (modelBuffer) {
      zeroBuffers(modelBuffer);
      modelBuffer = null;
    }
  }
}
