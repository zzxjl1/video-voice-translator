/**
 * MDX-Net stem separation orchestration, in the browser.
 *
 * Mirrors `audio_separator/separator/architectures/mdx_separator.py`
 * (`demix` + `run_model`) so the results match the reference implementation:
 *
 *   1. peak-normalise the mixture
 *   2. pad with silence: `mixture = [0]*trim + mix + [0]*pad`
 *   3. slide a `chunk_size = hop * (segment_size - 1)` window with `step`
 *      frames, STFT each chunk (256 spectrogram frames)
 *   4. zero the first 3 frequency bins, run the model
 *   5. inverse STFT per chunk, apply a symmetric Hann window, overlap-add and
 *      normalise by the accumulated window (the "divider")
 *   6. trim, restore the original length, scale back by the peak
 *   7. the model output is the primary stem; the other stem is derived in the
 *      time domain as `mix - primary * compensate`
 *
 * Memory
 * ------
 * Processing a whole track at once needs ~1.4 GB of buffers for a 5 minute
 * stereo file (mixture + result + divider + both stems, in float64), which
 * would kill a browser tab. So the accumulating buffers here are float32 and
 * `separateTrack()` slices the audio into windows with a cross-fade, which
 * caps peak usage at roughly one window's worth.
 *
 * The ONNX call is injected as `runModel` so this module has no dependency on
 * onnxruntime and can be unit-tested in Node.
 */
import { hannWindow, istft, planChunks, stft } from './mdxDsp.js';

/**
 * DSP parameters are deliberately NOT defined here.
 *
 * They are owned by the server (`GET /api/models/separator`, produced from
 * `config.SEPARATOR_PARAMS`) and validated by `validateSeparatorParams()`
 * below. Keeping them out of the bundle means:
 *   - shipping no readable tuning constants to the client,
 *   - being able to retune (e.g. `overlap` for speed/quality) without
 *     rebuilding or redeploying the frontend, and
 *   - a single source of truth, so the client can never silently disagree
 *     with the model the server hands out.
 *
 * @typedef {object} SeparatorParams
 * @property {number} nFft        STFT size (pinned by the model graph)
 * @property {number} dimF        retained frequency bins
 * @property {number} segmentSize spectrogram frames per chunk (model input)
 * @property {number} [overlap]   chunk overlap, 0..0.9
 * @property {number} [compensate] secondary-stem scaling
 * @property {string} [primaryStem] 'instrumental' | 'vocals'
 * @property {number} [zeroLowBins] lowest bins zeroed before inference
 * @property {number} [normalizationThreshold] peak normalisation target
 */

/**
 * Reject incomplete or nonsensical parameters instead of silently falling back
 * to a hardcoded constant — a wrong `nFft` would not crash, it would just
 * produce subtly broken audio.
 *
 * @param {any} params
 * @returns {SeparatorParams}
 */
export function validateSeparatorParams(params) {
  const required = ['nFft', 'dimF', 'segmentSize'];
  if (!params || typeof params !== 'object') {
    throw new Error('Separator parameters are missing (expected them from /api/models/separator)');
  }
  for (const key of required) {
    const value = params[key];
    if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
      throw new Error(`Separator parameter "${key}" is missing or invalid: ${JSON.stringify(value)}`);
    }
  }
  const { nFft, dimF, segmentSize, overlap } = params;
  if (dimF > nFft / 2 + 1) {
    throw new Error(`Separator parameter dimF (${dimF}) exceeds nFft/2+1 (${nFft / 2 + 1})`);
  }
  if (segmentSize < 2) {
    throw new Error(`Separator parameter segmentSize must be >= 2, got ${segmentSize}`);
  }
  if (overlap !== undefined && (typeof overlap !== 'number' || overlap < 0 || overlap >= 1)) {
    throw new Error(`Separator parameter overlap must be in [0, 1), got ${overlap}`);
  }
  return params;
}

/**
 * Zero the first `count` frequency bins of a UVR-layout spectrogram.
 * @param {Float32Array} data layout (C*2, dimF, frames)
 */
function zeroLowFrequencyBins(data, dimF, frames, count) {
  for (let idx = 0; idx < 4; idx++) {
    const base = idx * dimF * frames;
    for (let k = 0; k < count; k++) {
      const off = base + k * frames;
      data.fill(0, off, off + frames);
    }
  }
}

/**
 * Separate a single, bounded block of audio (see `separateTrack` for long
 * input). All channels must have the same length.
 *
 * @param {(spec: Float32Array, frames: number, dimF: number) => Promise<Float32Array>} runModel
 * @param {ArrayLike<number>[]} inputChannels
 * @param {SeparatorParams} params server-supplied, see validateSeparatorParams
 * @param {{onProgress?: (done: number, total: number) => void, signal?: {aborted: boolean}}} [opts]
 * @returns {Promise<{instrumental: Float32Array[], vocals: Float32Array[], stats: object}>}
 */
export async function separateStems(runModel, inputChannels, params, opts = {}) {
  validateSeparatorParams(params);
  const { onProgress, signal } = opts;
  const nFft = params.nFft;
  const dimF = params.dimF;
  const segmentSize = params.segmentSize;
  const overlap = params.overlap ?? 0.25;
  const compensate = params.compensate ?? 1.0;

  const nCh = inputChannels.length;
  const totalSamples = inputChannels[0].length;
  if (totalSamples === 0) throw new Error('empty input');

  const plan = planChunks(totalSamples, { nFft, segmentSize, overlap });
  const { hop, trim, chunkSize, step } = plan;

  const window = hannWindow(nFft, true);
  const olaWindow = hannWindow(chunkSize, false); // numpy.hanning is symmetric

  // Peak normalisation, folded directly into the padded mixture so no extra
  // full-length copy is needed.
  let peak = 0;
  for (const ch of inputChannels) {
    for (let i = 0; i < ch.length; i++) {
      const v = Math.abs(ch[i]);
      if (v > peak) peak = v;
    }
  }
  const scale = peak > 0 ? (params.normalizationThreshold ?? 1.0) / peak : 0;

  // Build the padded mixture in one pass: scale + place at offset `trim`.
  // Doing it here avoids an extra full-length copy of the signal.
  const mixtureLength = plan.mixtureLength;
  const mixture = [];
  for (let c = 0; c < nCh; c++) {
    const buf = new Float32Array(mixtureLength);
    const src = inputChannels[c];
    for (let i = 0; i < totalSamples; i++) buf[trim + i] = src[i] * scale;
    mixture.push(buf);
  }

  const result = [];
  const divider = [];
  for (let c = 0; c < nCh; c++) {
    result.push(new Float32Array(mixtureLength));
    divider.push(new Float32Array(mixtureLength));
  }

  const chunkChannels = [];
  for (let c = 0; c < nCh; c++) chunkChannels.push(new Float64Array(chunkSize));

  const starts = [];
  for (let s = 0; s < mixtureLength; s += step) starts.push(s);

  let done = 0;
  for (const start of starts) {
    if (signal?.aborted) throw new Error('aborted');

    const available = Math.min(chunkSize, mixtureLength - start);
    for (let c = 0; c < nCh; c++) {
      const src = mixture[c];
      const dst = chunkChannels[c];
      dst.fill(0);
      for (let i = 0; i < available; i++) dst[i] = src[start + i];
    }

    const spec = stft(chunkChannels, { nFft, hopLength: hop, dimF, window });
    if (params.zeroLowBins) {
      zeroLowFrequencyBins(spec.data, dimF, spec.frames, params.zeroLowBins);
    }

    const predicted = await runModel(spec.data, spec.frames, dimF);

    const rec = istft(
      { data: predicted, frames: spec.frames, dimF },
      { nFft, hopLength: hop, channels: nCh, window }
    );

    for (let c = 0; c < nCh; c++) {
      const recC = rec[c];
      const resC = result[c];
      const divC = divider[c];
      const limit = Math.min(available, recC.length);
      for (let i = 0; i < limit; i++) {
        const w = olaWindow[i];
        resC[start + i] += recC[i] * w;
        divC[start + i] += w;
      }
    }

    done++;
    if (onProgress) onProgress(done, starts.length);
  }

  const instrumental = [];
  const vocals = [];
  for (let c = 0; c < nCh; c++) {
    const resC = result[c];
    const divC = divider[c];
    const outI = new Float32Array(totalSamples);
    const outV = new Float32Array(totalSamples);
    for (let i = 0; i < totalSamples; i++) {
      const d = divC[trim + i];
      const iVal = d > 1e-8 ? (resC[trim + i] / d) * peak : 0;
      outI[i] = iVal;
      // the secondary stem is derived in the time domain, as audio-separator does
      outV[i] = inputChannels[c][i] - iVal * compensate;
    }
    instrumental.push(outI);
    vocals.push(outV);
  }

  const swap = params.primaryStem === 'vocals';
  return {
    instrumental: swap ? vocals : instrumental,
    vocals: swap ? instrumental : vocals,
    stats: {
      chunks: starts.length,
      step,
      chunkSize,
      hop,
      trim,
      mixtureLength,
      peak,
      params: { nFft, dimF, segmentSize, overlap, compensate },
    },
  };
}

/**
 * Separate an arbitrarily long track by processing it in windows and
 * cross-fading the results, which keeps peak memory proportional to one
 * window instead of the whole file.
 *
 * @param {(spec: Float32Array, frames: number, dimF: number) => Promise<Float32Array>} runModel
 * @param {Float32Array[]} channels
 * @param {SeparatorParams} params server-supplied
 * @param {{sampleRate?: number, windowSeconds?: number, fadeSeconds?: number,
 *          onProgress?: (done: number, total: number, window: number, windows: number) => void,
 *          signal?: {aborted: boolean}}} [opts]
 */
export async function separateTrack(runModel, channels, params, opts = {}) {
  validateSeparatorParams(params);
  const sampleRate = opts.sampleRate ?? 44100;
  const windowSeconds = opts.windowSeconds ?? 45;
  const fadeSeconds = opts.fadeSeconds ?? 0.75;

  const nCh = channels.length;
  const total = channels[0].length;
  const win = Math.max(1, Math.round(windowSeconds * sampleRate));
  const fade = Math.min(Math.round(fadeSeconds * sampleRate), Math.floor(win / 4));
  const step = Math.max(1, win - fade);

  const outInstrumental = channels.map(() => new Float32Array(total));
  const outVocals = channels.map(() => new Float32Array(total));
  const weight = new Float32Array(total);

  const windows = [];
  for (let s = 0; s < total; s += step) windows.push(s);

  for (let wi = 0; wi < windows.length; wi++) {
    const start = windows[wi];
    if (start >= total) break;
    const end = Math.min(total, start + win);
    const len = end - start;

    const slices = channels.map((ch) => ch.subarray(start, end));
    const res = await separateStems(runModel, slices, params, {
      signal: opts.signal,
      onProgress: opts.onProgress
        ? (d, t) => opts.onProgress(d, t, wi + 1, windows.length)
        : undefined,
    });

    const fadeIn = start > 0 ? fade : 0;
    const fadeOut = end < total ? fade : 0;

    for (let c = 0; c < nCh; c++) {
      const iSrc = res.instrumental[c];
      const vSrc = res.vocals[c];
      const iDst = outInstrumental[c];
      const vDst = outVocals[c];
      for (let i = 0; i < len; i++) {
        let g = 1;
        if (fadeIn && i < fadeIn) g = 0.5 - 0.5 * Math.cos((Math.PI * i) / fadeIn);
        const tail = len - 1 - i;
        if (fadeOut && tail < fadeOut) {
          const gOut = 0.5 - 0.5 * Math.cos((Math.PI * tail) / fadeOut);
          g = Math.min(g, gOut);
        }
        const pos = start + i;
        iDst[pos] += iSrc[i] * g;
        vDst[pos] += vSrc[i] * g;
        if (c === 0) weight[pos] += g;
      }
    }
  }

  for (let c = 0; c < nCh; c++) {
    const iDst = outInstrumental[c];
    const vDst = outVocals[c];
    for (let i = 0; i < total; i++) {
      const w = weight[i];
      const inv = w > 1e-6 ? 1 / w : 0;
      iDst[i] *= inv;
      vDst[i] *= inv;
    }
  }

  return {
    instrumental: outInstrumental,
    vocals: outVocals,
    stats: { windows: windows.length, windowSeconds, fadeSeconds, sampleRate },
  };
}
