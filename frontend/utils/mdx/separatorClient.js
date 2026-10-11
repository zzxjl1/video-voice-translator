/**
 * Browser-side vocal separation client.
 *
 * Pipeline: download the MDX-Net ONNX model (cached) -> build an
 * onnxruntime-web session (WebGPU, falling back to WASM) -> open the audio
 * (decoded locally when short, read from the server's WAV when long) ->
 * separate it window by window, appending each finished window to the output
 * WAV blobs -> upload them to the server.
 *
 * Memory
 * ------
 * Peak memory does not grow with the length of the video. The only things
 * resident are the model, one window of working buffers, and the output as
 * browser-managed Blobs (16-bit PCM, ~16 MB per minute for both stems — about
 * 950 MB per hour of audio, which Chromium keeps on disk rather than in RAM).
 * The one exception is a SHORT local file (<= LOCAL_DECODE_MAX_SECONDS), which
 * is decoded whole because `decodeAudioData` offers nothing else; the cap is
 * what keeps that bounded.
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
 * - Local decoding uses `decodeAudioData`, so the container/codec must be one
 *   the browser can decode (MP4/AAC, WebM/Opus, etc.). If it fails,
 *   `openAudioSource` falls back to the server's WAV, which ffmpeg produced
 *   from any format.
 */
import { loadOrt } from './onnxRuntime.js';
import { separateTrackStreaming, sourceFromChannels, validateSeparatorParams } from './mdxSeparator.js';

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
    try {
      // ORT may reuse the output buffer between calls, so copy.
      return new Float32Array(output.output.data);
    } finally {
      // A 12 MB spectrogram per chunk, thousands of chunks per hour of audio:
      // backends that keep tensor storage outside the JS heap (WASM heap, GPU
      // buffers) only give it back on dispose, not when the wrapper is GC'd.
      output.output.dispose?.();
      input.dispose?.();
    }
  };
}

/* ------------------------------------------------------------------ *
* Audio sources
*
* Everything below hands `separateTrackStreaming` an AudioSource: audio it can
* read one window at a time. What differs is how much of the track has to be
* resident for that to work.
*
*   decoded in memory   whole track as floats, 353 KB per second of audio
*                       (~420 MB for 20 min). Only for short inputs.
*   WAV blob            nothing resident: each window is sliced out of the
*                       blob and converted on demand. Any length.
* ------------------------------------------------------------------ */

/**
 * Longest input decoded locally, as one piece, from the user's own file.
 *
 * `decodeAudioData` has no streaming mode: it holds the compressed file AND the
 * whole decoded track at once. 20 minutes is ~420 MB decoded at 44.1 kHz stereo
 * — comfortably inside a tab's budget. Longer inputs go through the server's
 * WAV instead (see `openAudioSource`), which costs a download but whose memory
 * does not grow with the length.
 */
export const LOCAL_DECODE_MAX_SECONDS = 20 * 60;

/**
 * Decode a video/audio file to stereo float channels at `targetRate`.
 *
 * Holds the entire decoded track: use `openAudioSource` unless the input is
 * known to be short.
 *
 * @param {File|Blob} file
 * @param {number} [targetRate]
 * @returns {Promise<{channels: Float32Array[], sampleRate: number, duration: number}>}
 */
export async function decodeAudio(file, targetRate = MODEL_SAMPLE_RATE) {
  const OfflineCtx = globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext;
  const Ctx = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!OfflineCtx && !Ctx) throw new Error('Web Audio API is not available in this browser');

  // decodeAudioData detaches this buffer, so there is no reason to copy it first.
  const arrayBuffer = await file.arrayBuffer();

  let decoded;
  try {
    if (OfflineCtx) {
      // decodeAudioData resamples to the CONTEXT's rate. An offline context at
      // the target rate therefore decodes and resamples in one step — no
      // native-rate intermediate (a 48 kHz track would otherwise exist twice) and
      // no second render pass. The 1-frame length is irrelevant: it never renders.
      decoded = await new OfflineCtx(2, 1, targetRate).decodeAudioData(arrayBuffer);
    } else {
      const ctx = new Ctx({ sampleRate: targetRate });
      try {
        decoded = await ctx.decodeAudioData(arrayBuffer);
      } finally {
        ctx.close?.();
      }
    }
  } catch (e) {
    throw new Error(
      'Could not decode this file in the browser (unsupported container or codec). ' +
        'Disable browser separation to process it on the server instead.'
    );
  }

  // Only reached on a browser that ignored the context's rate.
  let buffer = decoded;
  if (Math.abs(decoded.sampleRate - targetRate) > 1 && OfflineCtx) {
    const target = new OfflineCtx(decoded.numberOfChannels, Math.ceil(decoded.duration * targetRate), targetRate);
    const source = target.createBufferSource();
    source.buffer = decoded;
    source.connect(target.destination);
    source.start(0);
    buffer = await target.startRendering();
  }

  const nCh = buffer.numberOfChannels;
  const channels = [];
  for (let c = 0; c < 2; c++) {
    // Views onto the AudioBuffer's own storage, not copies. Mono sources are
    // duplicated (the same view twice) so the model always sees a stereo pair.
    channels.push(buffer.getChannelData(Math.min(c, nCh - 1)));
  }

  return { channels, sampleRate: buffer.sampleRate, duration: buffer.duration };
}

/** @param {Float32Array[]} channels @param {number} sampleRate */
function decodedToSource(channels, sampleRate) {
  return {
    ...sourceFromChannels(channels, sampleRate),
    sampleRate, // known here, unlike for a bare in-memory source
    duration: channels[0].length / sampleRate,
    release: () => zeroBuffers(channels),
  };
}

const WAV_FORMAT_PCM = 1;
const WAV_FORMAT_EXTENSIBLE = 0xfffe;

/**
 * Locate the PCM payload of a WAV blob without reading it.
 *
 * Walks the chunk list, so a LIST/INFO chunk ahead of `data` (ffmpeg writes
 * one) is fine. A `data` size of 0 or 0xFFFFFFFF means "to the end of the
 * file", which is what streaming and RF64 writers put there.
 *
 * Only 16-bit PCM, mono or stereo — what the server's extraction produces.
 * Anything else is rejected loudly rather than read as noise.
 *
 * @param {Blob} blob
 * @returns {Promise<{sampleRate: number, channels: number, blockAlign: number,
 *                    dataOffset: number, dataBytes: number}>}
 */
export async function parseWavHeader(blob) {
  const view = async (from, to) => new DataView(await blob.slice(from, to).arrayBuffer());
  const tag = (v, o) =>
    String.fromCharCode(v.getUint8(o), v.getUint8(o + 1), v.getUint8(o + 2), v.getUint8(o + 3));

  if (blob.size < 12) throw new Error('not a WAV file (too short)');
  const riff = await view(0, 12);
  const magic = tag(riff, 0);
  if ((magic !== 'RIFF' && magic !== 'RF64') || tag(riff, 8) !== 'WAVE') {
    throw new Error('not a WAV file (missing RIFF/WAVE header)');
  }

  let fmt = null;
  let offset = 12;
  while (offset + 8 <= blob.size) {
    const head = await view(offset, offset + 8);
    const id = tag(head, 0);
    const size = head.getUint32(4, true);
    const body = offset + 8;

    if (id === 'fmt ') {
      const f = await view(body, body + Math.min(size, 16));
      if (f.byteLength < 16) throw new Error('WAV fmt chunk is truncated');
      fmt = {
        format: f.getUint16(0, true),
        channels: f.getUint16(2, true),
        sampleRate: f.getUint32(4, true),
        blockAlign: f.getUint16(12, true),
        bits: f.getUint16(14, true),
      };
    } else if (id === 'data') {
      if (!fmt) throw new Error('WAV data chunk precedes its fmt chunk');
      if (fmt.format !== WAV_FORMAT_PCM && fmt.format !== WAV_FORMAT_EXTENSIBLE) {
        throw new Error(`unsupported WAV encoding (format tag ${fmt.format}); expected 16-bit PCM`);
      }
      if (fmt.bits !== 16) throw new Error(`unsupported WAV bit depth (${fmt.bits}); expected 16-bit PCM`);
      if (fmt.channels !== 1 && fmt.channels !== 2) {
        throw new Error(`unsupported WAV channel count (${fmt.channels}); expected mono or stereo`);
      }
      if (fmt.blockAlign !== fmt.channels * 2) throw new Error('WAV block alignment is inconsistent');

      const available = blob.size - body;
      const declared = size === 0 || size === 0xffffffff ? available : size;
      return {
        sampleRate: fmt.sampleRate,
        channels: fmt.channels,
        blockAlign: fmt.blockAlign,
        dataOffset: body,
        dataBytes: Math.min(declared, available),
      };
    }
    offset = body + size + (size & 1); // chunks are word-aligned
  }
  throw new Error('WAV has no data chunk');
}

/**
 * A 16-bit PCM WAV blob as an AudioSource. Nothing is decoded up front: each
 * `read` slices one window out of the blob and converts only that. The model
 * always gets two channels; a mono file is duplicated.
 *
 * @param {Blob} blob
 */
export async function sourceFromWav(blob) {
  const h = await parseWavHeader(blob);
  const frames = Math.floor(h.dataBytes / h.blockAlign);
  if (frames === 0) throw new Error('WAV contains no audio');

  return {
    channels: 2,
    length: frames,
    sampleRate: h.sampleRate,
    duration: frames / h.sampleRate,
    async read(start, end) {
      const n = end - start;
      const from = h.dataOffset + start * h.blockAlign;
      const raw = await blob.slice(from, from + n * h.blockAlign).arrayBuffer();
      if (raw.byteLength !== n * h.blockAlign) {
        throw new Error('WAV ended before the audio did (truncated download?)');
      }
      // Little-endian hosts only, which is every platform a browser runs on.
      const pcm = new Int16Array(raw);
      const left = new Float32Array(n);
      if (h.channels === 1) {
        for (let i = 0; i < n; i++) left[i] = pcm[i] / 32768;
        return [left, left];
      }
      const right = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        left[i] = pcm[2 * i] / 32768;
        right[i] = pcm[2 * i + 1] / 32768;
      }
      return [left, right];
    },
    release() {},
  };
}

/**
 * Duration of a media file in seconds from its metadata, or null when the
 * browser cannot tell. Costs no decoding and next to no memory, which is why
 * it can be used to decide HOW to decode.
 *
 * @param {Blob} blob
 * @param {number} [timeoutMs]
 * @returns {Promise<number | null>}
 */
export function probeMediaDuration(blob, timeoutMs = 8000) {
  return new Promise((resolve) => {
    if (typeof document === 'undefined') return resolve(null);
    const el = document.createElement('video');
    el.preload = 'metadata';
    el.muted = true;
    const url = URL.createObjectURL(blob);
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      el.removeAttribute('src');
      el.load();
      URL.revokeObjectURL(url);
      resolve(value);
    };
    const timer = setTimeout(() => finish(null), timeoutMs);
    // WebM written by a recorder often has no duration: it reads as Infinity,
    // which is "unknown", not "very long".
    el.onloadedmetadata = () =>
      finish(Number.isFinite(el.duration) && el.duration > 0 ? el.duration : null);
    el.onerror = () => finish(null);
    el.src = url;
  });
}

/**
 * Pick the audio source whose memory cost is safe for this input.
 *
 *   - short local file      decode it here: no transfer at all;
 *   - long / unknown length, or no local file (page reloaded), or the browser
 *     cannot decode the container
 *                           use the server's stereo 44.1 kHz WAV, read window
 *                           by window. The server already has the upload, so
 *                           the price is one download (~10.6 MB per minute).
 *
 * "Unknown length" takes the safe branch on purpose: guessing short and being
 * wrong is an out-of-memory tab, guessing long and being wrong is a download.
 *
 * @param {object} args
 * @param {Blob} [args.file]                       the user's file, if still in memory
 * @param {() => Promise<Blob>} [args.fetchServerSource] the server's WAV
 * @param {(msg: string) => void} [args.onLog]
 * @param {number} [args.maxLocalSeconds]
 */
export async function openAudioSource({
  file,
  fetchServerSource,
  onLog,
  maxLocalSeconds = LOCAL_DECODE_MAX_SECONDS,
}) {
  const log = (m) => onLog?.(m);

  if (file) {
    const duration = await probeMediaDuration(file);
    if (duration !== null && duration <= maxLocalSeconds) {
      log(`Decoding audio locally (${duration.toFixed(0)} s)...`);
      try {
        const decoded = await decodeAudio(file);
        return decodedToSource(decoded.channels, decoded.sampleRate);
      } catch (e) {
        if (!fetchServerSource) throw e;
        log(`Local decode failed (${e.message}) — using the server's audio instead.`);
      }
    } else if (duration === null) {
      log('Could not read the duration from the file — using the server\'s audio to keep memory bounded.');
    } else {
      log(
        `Long input (${(duration / 60).toFixed(0)} min) — streaming the server's audio ` +
          'window by window to keep memory bounded.'
      );
    }
  }

  if (!fetchServerSource) throw new Error('no audio source is available for separation');
  const wav = await fetchServerSource();
  return sourceFromWav(wav);
}

/* ------------------------------------------------------------------ *
* WAV encoding
* ------------------------------------------------------------------ */

function floatToInt16(sample) {
  const clamped = sample < -1 ? -1 : sample > 1 ? 1 : sample;
  return clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
}

/**
 * The 44-byte header of a 16-bit PCM WAV with `frames` frames.
 * @returns {Uint8Array}
 */
function wavHeader(frames, channels, sampleRate) {
  const dataBytes = frames * channels * 2;
  // RIFF sizes are 32-bit. ~6.7 hours of 44.1 kHz stereo; failing here beats
  // writing a header that silently wraps and a file every reader truncates.
  if (dataBytes + 36 > 0xffffffff) {
    throw new Error('audio is too long to store as a WAV file (4 GB limit)');
  }
  const bytes = new Uint8Array(44);
  const view = new DataView(bytes.buffer);
  const writeAscii = (offset, text) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };
  writeAscii(0, 'RIFF');
  view.setUint32(4, 36 + dataBytes, true);
  writeAscii(8, 'WAVE');
  writeAscii(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, WAV_FORMAT_PCM, true);
  view.setUint16(22, channels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * channels * 2, true);
  view.setUint16(32, channels * 2, true);
  view.setUint16(34, 16, true);
  writeAscii(36, 'data');
  view.setUint32(40, dataBytes, true);
  return bytes;
}

/**
 * Interleave float channels into little-endian 16-bit PCM.
 * @param {Float32Array[]} channels
 */
function floatsToPcm16(channels) {
  const n = channels[0].length;
  const nCh = channels.length;
  const pcm = new Int16Array(n * nCh);
  if (nCh === 1) {
    const ch = channels[0];
    for (let i = 0; i < n; i++) pcm[i] = floatToInt16(ch[i]);
  } else {
    for (let i = 0; i < n; i++) {
      for (let c = 0; c < nCh; c++) pcm[i * nCh + c] = floatToInt16(channels[c][i]);
    }
  }
  return pcm;
}

/**
 * Encode channels as 16-bit PCM WAV, all at once. Fine for short audio; the
 * separator's output goes through `createPcmBlobWriter` instead, so it never
 * has to exist as one contiguous buffer.
 *
 * @param {Float32Array[]} channels
 * @param {number} sampleRate
 * @param {{mono?: boolean}} [opts] mix down to one channel
 * @returns {Blob}
 */
export function encodeWav(channels, sampleRate, opts = {}) {
  const source = opts.mono && channels.length > 1 ? [mixToMono(channels)] : channels;
  return new Blob([wavHeader(source[0].length, source.length, sampleRate), floatsToPcm16(source)], {
    type: 'audio/wav',
  });
}

function mixToMono(channels) {
  const n = channels[0].length;
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let sum = 0;
    for (let c = 0; c < channels.length; c++) sum += channels[c][i];
    out[i] = sum / channels.length;
  }
  return out;
}

/**
 * Build a WAV out of audio that arrives in pieces, without ever holding it
 * as one array.
 *
 * Each `push` converts its piece to 16-bit PCM (half the size of the floats),
 * wraps it in a Blob and drops the typed array. A Blob is browser-managed
 * storage: Chromium spills large ones to disk, so the JS heap carries only the
 * piece being converted. `finish` stitches the parts behind a header — no copy
 * — and the result uploads straight from that storage.
 *
 * @param {number} sampleRate
 * @param {number} channels number of channels every `push` will carry
 */
export function createPcmBlobWriter(sampleRate, channels) {
  const parts = [];
  let frames = 0;
  return {
    /** @param {Float32Array[]} piece one array per channel, equal lengths */
    push(piece) {
      if (piece.length !== channels) {
        throw new Error(`expected ${channels} channel(s), got ${piece.length}`);
      }
      if (piece[0].length === 0) return;
      parts.push(new Blob([floatsToPcm16(piece)]));
      frames += piece[0].length;
    },
    get frames() {
      return frames;
    },
    /** @returns {Blob} */
    finish() {
      if (frames === 0) throw new Error('no audio was written');
      return new Blob([wavHeader(frames, channels, sampleRate), ...parts], { type: 'audio/wav' });
    },
  };
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
 * @param {import('./mdxSeparator.js').AudioSource & {sampleRate: number, release?: () => void}} args.audio
 *        as returned by `openAudioSource()`; released here when done
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

    // The session owns its own copy of the weights; holding the download as well
    // would pin another 64 MB for the whole run.
    modelBuffer = null;

    const runModel = makeRunModel(session, params);

    // Background keeps stereo; vocals only feed ASR and cloning, so mono halves
    // the upload size at no practical cost.
    const vocalsWriter = createPcmBlobWriter(audio.sampleRate, 1);
    const backgroundWriter = createPcmBlobWriter(audio.sampleRate, 2);

    const started = Date.now();
    // Streamed end to end: each window is read, separated, and appended to the
    // output Blobs as 16-bit PCM before the next one starts. Peak memory is one
    // window, not the track.
    const stats = await separateTrackStreaming(runModel, audio, params, {
      sampleRate: audio.sampleRate,
      onProgress: (done, total, window, windows) => {
        onProgress?.('separate', { done, total, window, windows });
      },
      onSamples: ({ instrumental, vocals }) => {
        vocalsWriter.push([vocals[0]]);
        backgroundWriter.push(instrumental);
      },
      signal,
    });
    const elapsed = (Date.now() - started) / 1000;

    const minutes = audio.length / audio.sampleRate / 60;
    log(
      `Separation finished in ${elapsed.toFixed(1)}s for ${minutes.toFixed(1)} min of audio ` +
        `(${((minutes * 60) / elapsed).toFixed(1)}x realtime).`
    );

    // Inference is over: free the session (and its WASM/GPU memory) before the
    // upload rather than after it.
    try {
      await session.release?.();
    } catch {
      /* ignore */
    }
    session = null;
    audio.release?.();

    const vocalsBlob = vocalsWriter.finish();
    const backgroundBlob = backgroundWriter.finish();

    log(
      `Uploading stems (vocals ${(vocalsBlob.size / 1048576).toFixed(1)} MB, ` +
        `background ${(backgroundBlob.size / 1048576).toFixed(1)} MB)...`
    );
    const uploadResult = await upload(vocalsBlob, backgroundBlob);
    log('Stems delivered to the server.');

    return { uploadResult, elapsed, stats };
  } finally {
    // Runs on success, failure and abort alike: never leave a GPU session or
    // the user's audio resident in the tab.
    try {
      await session?.release?.();
    } catch {
      /* ignore */
    }
    audio.release?.();
    if (modelBuffer) {
      zeroBuffers(modelBuffer);
      modelBuffer = null;
    }
  }
}
