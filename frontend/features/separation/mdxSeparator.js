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
 * would kill a browser tab. So `separateStems` only ever sees one bounded
 * window, and `separateTrackStreaming()` slices a track into such windows with
 * a cross-fade, reading input and emitting output window by window — so the
 * peak is one window's worth no matter how long the track is.
 * (`separateTrack()` is the in-memory convenience form and holds the whole
 * result; it is for short audio and tests.)
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
 * Where the windows of a long track start.
 *
 * Windows advance by `win - fade` so neighbours overlap by exactly `fade`
 * samples (the cross-fade region). The list ends at the FIRST window that
 * reaches the end of the track: continuing to `start < total` would add a
 * trailing window lying entirely inside the previous one's last `fade` samples
 * — a whole extra model pass (tens of seconds of compute) for audio that is
 * already final.
 *
 * @param {number} total samples per channel
 * @param {number} win   window length in samples
 * @param {number} fade  overlap in samples (0 <= fade <= win / 4)
 * @returns {{starts: number[], step: number}}
 */
export function planWindows(total, win, fade) {
  const step = Math.max(1, win - fade);
  const starts = [];
  for (let s = 0; ; s += step) {
    starts.push(s);
    if (s + win >= total) break;
  }
  return { starts, step };
}

/**
 * Anything `separateTrackStreaming` can read audio from, one window at a time.
 * The whole point of the interface is that the caller never needs the entire
 * track as float arrays.
 *
 * @typedef {object} AudioSource
 * @property {number} channels   channel count (the model wants 2)
 * @property {number} length     samples per channel
 * @property {number} [sampleRate]
 * @property {(start: number, end: number) => Float32Array[] | Promise<Float32Array[]>} read
 *           channel arrays of exactly `end - start` samples; may be views
 */

/**
 * An already-decoded track as an AudioSource (zero-copy: `read` hands out views).
 * @param {Float32Array[]} channels
 * @param {number} [sampleRate]
 * @returns {AudioSource}
 */
export function sourceFromChannels(channels, sampleRate) {
  return {
    channels: channels.length,
    length: channels[0].length,
    sampleRate,
    read: (start, end) => channels.map((ch) => ch.subarray(start, end)),
  };
}

/**
 * Separate an arbitrarily long track window by window, cross-fading the
 * overlaps, and hand every finished stretch of samples to `onSamples` as soon
 * as it is final.
 *
 * Memory: one window of working buffers (~100 MB at the default 45 s) plus a
 * `fade`-sized carry, independent of the track length. Nothing here
 * accumulates the output — whatever consumes `onSamples` decides where the
 * samples go (e.g. straight into 16-bit PCM blobs).
 *
 * `onSamples` receives contiguous, in-order, non-overlapping ranges
 * (`start` is the sample index of the first sample) and may be async; the next
 * window starts only after it resolves. The arrays it receives are its own to
 * keep.
 *
 * @param {(spec: Float32Array, frames: number, dimF: number) => Promise<Float32Array>} runModel
 * @param {AudioSource} source
 * @param {SeparatorParams} params server-supplied
 * @param {{sampleRate?: number, windowSeconds?: number, fadeSeconds?: number,
 *          onSamples?: (out: {start: number, instrumental: Float32Array[], vocals: Float32Array[]}) => void | Promise<void>,
 *          onProgress?: (done: number, total: number, window: number, windows: number) => void,
 *          signal?: {aborted: boolean}}} [opts]
 */
export async function separateTrackStreaming(runModel, source, params, opts = {}) {
  validateSeparatorParams(params);
  const sampleRate = opts.sampleRate ?? source.sampleRate ?? 44100;
  const windowSeconds = opts.windowSeconds ?? 45;
  const fadeSeconds = opts.fadeSeconds ?? 0.75;

  const nCh = source.channels;
  const total = source.length;
  if (!(total > 0)) throw new Error('empty input');

  const win = Math.max(1, Math.round(windowSeconds * sampleRate));
  const fade = Math.min(Math.round(fadeSeconds * sampleRate), Math.floor(win / 4));
  const { starts } = planWindows(total, win, fade);

  /** Overlap carried from the previous window: weighted sums, not yet normalised. */
  let carry = null;
  let emitted = 0;

  for (let wi = 0; wi < starts.length; wi++) {
    if (opts.signal?.aborted) throw new Error('aborted');

    const start = starts[wi];
    const end = Math.min(total, start + win);
    const len = end - start;

    const slices = await source.read(start, end);
    if (slices.length !== nCh || slices[0].length !== len) {
      throw new Error(
        `audio source returned ${slices.length} channel(s) x ${slices[0]?.length} samples ` +
          `for [${start}, ${end}); expected ${nCh} x ${len}`
      );
    }

    const res = await separateStems(runModel, slices, params, {
      signal: opts.signal,
      onProgress: opts.onProgress
        ? (d, t) => opts.onProgress(d, t, wi + 1, starts.length)
        : undefined,
    });

    // Weighted accumulators for this window, seeded with the previous window's
    // overlap (it sits at positions [0, fadeIn) of this window).
    const fadeIn = carry ? carry.w.length : 0;
    const fadeOut = end < total ? fade : 0;
    const accI = [];
    const accV = [];
    const accW = new Float32Array(len);
    for (let c = 0; c < nCh; c++) {
      accI.push(new Float32Array(len));
      accV.push(new Float32Array(len));
    }
    if (carry) {
      for (let c = 0; c < nCh; c++) {
        accI[c].set(carry.inst[c]);
        accV[c].set(carry.voc[c]);
      }
      accW.set(carry.w);
    }

    for (let i = 0; i < len; i++) {
      let g = 1;
      if (fadeIn && i < fadeIn) g = 0.5 - 0.5 * Math.cos((Math.PI * i) / fadeIn);
      const tail = len - 1 - i;
      if (fadeOut && tail < fadeOut) {
        g = Math.min(g, 0.5 - 0.5 * Math.cos((Math.PI * tail) / fadeOut));
      }
      for (let c = 0; c < nCh; c++) {
        accI[c][i] += res.instrumental[c][i] * g;
        accV[c][i] += res.vocals[c][i] * g;
      }
      accW[i] += g;
    }

    // Everything except the last `fadeOut` samples is final: the next window
    // contributes there. Those are carried over still un-normalised.
    const outLen = len - fadeOut;
    if (start !== emitted) {
      throw new Error(`window plan is not contiguous: window ${wi} starts at ${start}, emitted ${emitted}`);
    }
    for (let i = 0; i < outLen; i++) {
      const w = accW[i];
      const inv = w > 1e-6 ? 1 / w : 0;
      for (let c = 0; c < nCh; c++) {
        accI[c][i] *= inv;
        accV[c][i] *= inv;
      }
    }

    carry = fadeOut
      ? {
          inst: accI.map((a) => a.slice(outLen)),
          voc: accV.map((a) => a.slice(outLen)),
          w: accW.slice(outLen),
        }
      : null;

    if (opts.onSamples) {
      await opts.onSamples({
        start: emitted,
        instrumental: accI.map((a) => a.subarray(0, outLen)),
        vocals: accV.map((a) => a.subarray(0, outLen)),
      });
    }
    emitted += outLen;
  }

  return { windows: starts.length, windowSeconds, fadeSeconds, sampleRate, samples: emitted };
}

/**
 * In-memory convenience wrapper around `separateTrackStreaming`: returns the
 * whole result as float arrays. Fine for short audio and for tests; for long
 * audio use the streaming form so the output never has to exist as floats.
 *
 * @param {(spec: Float32Array, frames: number, dimF: number) => Promise<Float32Array>} runModel
 * @param {Float32Array[]} channels
 * @param {SeparatorParams} params server-supplied
 * @param {{sampleRate?: number, windowSeconds?: number, fadeSeconds?: number,
 *          onProgress?: (done: number, total: number, window: number, windows: number) => void,
 *          signal?: {aborted: boolean}}} [opts]
 */
export async function separateTrack(runModel, channels, params, opts = {}) {
  const total = channels[0].length;
  const instrumental = channels.map(() => new Float32Array(total));
  const vocals = channels.map(() => new Float32Array(total));
  const stats = await separateTrackStreaming(
    runModel,
    sourceFromChannels(channels, opts.sampleRate),
    params,
    {
      ...opts,
      onSamples: ({ start, instrumental: i, vocals: v }) => {
        for (let c = 0; c < channels.length; c++) {
          instrumental[c].set(i[c], start);
          vocals[c].set(v[c], start);
        }
      },
    }
  );
  return { instrumental, vocals, stats };
}
