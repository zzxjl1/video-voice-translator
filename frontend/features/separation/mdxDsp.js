/**
 * DSP core for running UVR MDX-Net vocal separation in the browser.
 *
 * This is a faithful port of what `audio-separator` does for MDX models
 * (`architectures/mdx_separator.py` + `uvr_lib_v5/stft.py`), so the numbers
 * match the reference implementation:
 *
 *   - Hann window, `periodic = true`
 *   - `torch.stft(..., center=True, return_complex=False)` -> reflect padding
 *   - channel layout `(B, C*2, dimF, T)` with order `[ch0.re, ch0.im, ch1.re, ch1.im]`
 *   - frequency dim truncated to `dimF` (drops the Nyquist bin, 3073 -> 3072)
 *   - inverse pads the frequency dim back with zeros and uses `torch.istft`
 *     window-normalisation (divide by the summed squared window)
 *
 * `n_fft` for UVR-MDX-NET-Inst_HQ_3 is 6144, which is NOT a power of two
 * (6144 = 3 * 2^11). A plain radix-2 FFT cannot handle it, so a mixed-radix
 * Cooley-Tukey transform is used: the input is decomposed as `n = r * m`
 * with `m` a power of two (radix-2 stages) and a small `r`-point DFT combine.
 *
 * Written as plain ESM JavaScript (no TypeScript syntax) on purpose: the same
 * file is imported by the app AND executed directly by Node in
 * `scripts/test-dsp.mjs` and `scripts/test-mdx-model.mjs`, so the shipped code
 * is the code that gets verified.
 */

/* ------------------------------------------------------------------ *
 * Window
 * ------------------------------------------------------------------ */

/**
 * Hann window.
 * `periodic = true` matches `torch.hann_window(..., periodic=True)`:
 *   w[i] = 0.5 - 0.5 * cos(2 * pi * i / n)
 * @param {number} n
 * @param {boolean} [periodic]
 * @returns {Float64Array}
 */
export function hannWindow(n, periodic = true) {
  const w = new Float64Array(n);
  const denom = periodic ? n : n - 1;
  for (let i = 0; i < n; i++) {
    w[i] = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / denom);
  }
  return w;
}

/* ------------------------------------------------------------------ *
 * FFT: mixed radix (r small, m power of two), complex, in place
 * ------------------------------------------------------------------ */

/**
 * Build a reusable plan for a complex FFT of length `n`.
 * Requires `n = r * 2^k` (any `n` of the form used by UVR models).
 * @param {number} n
 */
export function createFftPlan(n) {
  let m = 1;
  let r = n;
  while (r % 2 === 0) {
    r /= 2;
    m *= 2;
  }
  // n = r * m, m is a power of two, r is odd (r === 1 -> pure radix-2)

  const log2m = Math.round(Math.log2(m));
  if (2 ** log2m !== m) throw new Error(`m is not a power of two for n=${n}`);

  // radix-2 twiddles: exp(-2*pi*i*j/m) stored as cos/sin of +angle
  const halfM = m >> 1;
  const cosTab = new Float64Array(halfM);
  const sinTab = new Float64Array(halfM);
  for (let j = 0; j < halfM; j++) {
    const a = (2 * Math.PI * j) / m;
    cosTab[j] = Math.cos(a);
    sinTab[j] = Math.sin(a);
  }

  // bit-reversal permutation for the m-point transform
  const bitrev = new Int32Array(m);
  for (let i = 0; i < m; i++) {
    let x = i;
    let y = 0;
    for (let b = 0; b < log2m; b++) {
      y = (y << 1) | (x & 1);
      x >>= 1;
    }
    bitrev[i] = y;
  }

  // outer twiddles: exp(-2*pi*i*n0*k0/n), indexed n0 * m + k0
  const outerCos = new Float64Array(n);
  const outerSin = new Float64Array(n);
  for (let n0 = 0; n0 < r; n0++) {
    for (let k0 = 0; k0 < m; k0++) {
      const a = (2 * Math.PI * n0 * k0) / n;
      outerCos[n0 * m + k0] = Math.cos(a);
      outerSin[n0 * m + k0] = Math.sin(a);
    }
  }

  // small r-point DFT matrix: exp(-2*pi*i*n0*k1/r), indexed n0 * r + k1
  const dftCos = new Float64Array(r * r);
  const dftSin = new Float64Array(r * r);
  for (let n0 = 0; n0 < r; n0++) {
    for (let k1 = 0; k1 < r; k1++) {
      const a = (2 * Math.PI * n0 * k1) / r;
      dftCos[n0 * r + k1] = Math.cos(a);
      dftSin[n0 * r + k1] = Math.sin(a);
    }
  }

  return {
    n,
    r,
    m,
    log2m,
    cosTab,
    sinTab,
    bitrev,
    outerCos,
    outerSin,
    dftCos,
    dftSin,
    // scratch buffers, reused across calls (single threaded use)
    _bufRe: new Float64Array(m),
    _bufIm: new Float64Array(m),
    _tmpRe: new Float64Array(n),
    _tmpIm: new Float64Array(n),
  };
}

/**
 * In-place radix-2 complex FFT of length `m` (power of two).
 * @param {Float64Array} re
 * @param {Float64Array} im
 * @param {object} plan
 * @param {number} sign -1 forward, +1 inverse (twiddle conjugation only)
 */
function radix2(re, im, plan, sign) {
  const { m, bitrev, cosTab, sinTab } = plan;

  for (let i = 0; i < m; i++) {
    const j = bitrev[i];
    if (j > i) {
      let t = re[i];
      re[i] = re[j];
      re[j] = t;
      t = im[i];
      im[i] = im[j];
      im[j] = t;
    }
  }

  for (let size = 2; size <= m; size <<= 1) {
    const half = size >> 1;
    const step = m / size;
    for (let start = 0; start < m; start += size) {
      for (let k = 0; k < half; k++) {
        const tw = k * step;
        const wr = cosTab[tw];
        const wi = sign * sinTab[tw];

        const a = start + k;
        const b = a + half;
        const xr = re[b];
        const xi = im[b];
        const tr = wr * xr - wi * xi;
        const ti = wr * xi + wi * xr;

        re[b] = re[a] - tr;
        im[b] = im[a] - ti;
        re[a] += tr;
        im[a] += ti;
      }
    }
  }
}

/**
 * In-place complex FFT of length `plan.n`.
 * Forward includes no scaling; the inverse is scaled by 1/n
 * (the standard normalisation, matching `torch.istft`).
 * @param {Float64Array} re
 * @param {Float64Array} im
 * @param {object} plan
 * @param {boolean} [inverse]
 */
export function fft(re, im, plan, inverse = false) {
  const { n, r, m, outerCos, outerSin, dftCos, dftSin } = plan;
  const sign = inverse ? 1 : -1;

  if (r === 1) {
    radix2(re, im, plan, sign);
  } else {
    const bufRe = plan._bufRe;
    const bufIm = plan._bufIm;
    const tmpRe = plan._tmpRe;
    const tmpIm = plan._tmpIm;

    // step 1: r decimated m-point transforms + outer twiddles
    for (let n0 = 0; n0 < r; n0++) {
      for (let n1 = 0; n1 < m; n1++) {
        const idx = n1 * r + n0;
        bufRe[n1] = re[idx];
        bufIm[n1] = im[idx];
      }
      radix2(bufRe, bufIm, plan, sign);

      const base = n0 * m;
      for (let k0 = 0; k0 < m; k0++) {
        const cr = outerCos[base + k0];
        const ci = sign * outerSin[base + k0];
        const xr = bufRe[k0];
        const xi = bufIm[k0];
        tmpRe[base + k0] = cr * xr - ci * xi;
        tmpIm[base + k0] = cr * xi + ci * xr;
      }
    }

    // step 2: r-point DFT combine, output index k1 * m + k0
    for (let k0 = 0; k0 < m; k0++) {
      for (let k1 = 0; k1 < r; k1++) {
        let sr = 0;
        let si = 0;
        for (let n0 = 0; n0 < r; n0++) {
          const cr = dftCos[n0 * r + k1];
          const ci = sign * dftSin[n0 * r + k1];
          const xr = tmpRe[n0 * m + k0];
          const xi = tmpIm[n0 * m + k0];
          sr += cr * xr - ci * xi;
          si += cr * xi + ci * xr;
        }
        re[k1 * m + k0] = sr;
        im[k1 * m + k0] = si;
      }
    }
  }

  if (inverse) {
    const inv = 1 / n;
    for (let i = 0; i < n; i++) {
      re[i] *= inv;
      im[i] *= inv;
    }
  }
}

/* ------------------------------------------------------------------ *
 * Padding
 * ------------------------------------------------------------------ */

/**
 * Reflect padding, matching `torch.nn.functional.pad(mode="reflect")`
 * and therefore `torch.stft(center=True)`.
 * @param {Float32Array|Float64Array} x
 * @param {number} pad
 * @returns {Float64Array}
 */
export function reflectPad(x, pad) {
  const n = x.length;
  if (pad === 0) return Float64Array.from(x);
  if (pad >= n) {
    throw new Error(`reflect padding ${pad} requires at least ${pad + 1} samples, got ${n}`);
  }
  const out = new Float64Array(n + 2 * pad);
  for (let j = 0; j < pad; j++) out[j] = x[pad - j];
  for (let i = 0; i < n; i++) out[pad + i] = x[i];
  for (let j = 0; j < pad; j++) out[pad + n + j] = x[n - 2 - j];
  return out;
}

/* ------------------------------------------------------------------ *
 * STFT / iSTFT (stereo, UVR layout)
 * ------------------------------------------------------------------ */

/**
 * @typedef {object} StftResult
 * @property {Float32Array} data  flat (C*2, dimF, frames)
 * @property {number} frames
 * @property {number} dimF
 */

/**
 * Multi-channel STFT in the exact layout UVR MDX models expect.
 * @param {ArrayLike<number>[]} channels stereo (or more) time-domain channels
 * @param {{nFft: number, hopLength: number, dimF: number, window?: Float64Array, plan?: object}} opts
 * @returns {StftResult}
 */
export function stft(channels, opts) {
  const { nFft, hopLength, dimF } = opts;
  const window = opts.window || hannWindow(nFft, true);
  const plan = opts.plan || createFftPlan(nFft);
  const half = nFft >> 1;
  const nCh = channels.length;
  const T = channels[0].length;
  const frames = 1 + Math.floor(T / hopLength);

  const data = new Float32Array(nCh * 2 * dimF * frames);
  const fRe = new Float64Array(nFft);
  const fIm = new Float64Array(nFft);

  for (let c = 0; c < nCh; c++) {
    const padded = reflectPad(channels[c], half);
    const reBase = 2 * c * dimF * frames;
    const imBase = (2 * c + 1) * dimF * frames;

    for (let f = 0; f < frames; f++) {
      const off = f * hopLength;
      for (let i = 0; i < nFft; i++) {
        fRe[i] = padded[off + i] * window[i];
        fIm[i] = 0;
      }
      fft(fRe, fIm, plan, false);
      for (let k = 0; k < dimF; k++) {
        data[reBase + k * frames + f] = fRe[k];
        data[imBase + k * frames + f] = fIm[k];
      }
    }
  }

  return { data, frames, dimF };
}

/**
 * Inverse STFT: pads the frequency dim back to `nFft/2 + 1`, runs the inverse
 * transform, applies the window twice (analysis + synthesis) and normalises by
 * the summed squared window, then trims `nFft/2` from both ends
 * (all matching `torch.istft`).
 *
 * @param {StftResult} spec
 * @param {{nFft: number, hopLength: number, channels: number, window?: Float64Array, plan?: object}} opts
 * @returns {Float64Array[]} time-domain channels, length (frames-1)*hopLength
 */
export function istft(spec, opts) {
  const { nFft, hopLength } = opts;
  const nCh = opts.channels;
  const dimF = spec.dimF;
  const frames = spec.frames;
  const window = opts.window || hannWindow(nFft, true);
  const plan = opts.plan || createFftPlan(nFft);

  const half = nFft >> 1;
  const nBins = half + 1;
  const outLen = (frames - 1) * hopLength;
  const bufLen = outLen + nFft;

  const result = [];
  const fRe = new Float64Array(nFft);
  const fIm = new Float64Array(nFft);

  for (let c = 0; c < nCh; c++) {
    const out = new Float64Array(bufLen);
    const env = new Float64Array(bufLen);
    const reBase = 2 * c * dimF * frames;
    const imBase = (2 * c + 1) * dimF * frames;

    for (let f = 0; f < frames; f++) {
      fRe.fill(0);
      fIm.fill(0);
      for (let k = 0; k < dimF && k < nBins; k++) {
        fRe[k] = spec.data[reBase + k * frames + f];
        fIm[k] = spec.data[imBase + k * frames + f];
      }
      // bins dimF..nBins-1 stay zero (the Nyquist bin dropped by the model)

      // The stored spectrum only holds the non-negative frequencies
      // (0 .. nFft/2). A real-valued signal needs the full Hermitian spectrum,
      // otherwise the inverse transform yields an analytic signal whose real
      // part is exactly half the original. `torch.fft.irfft` applies this
      // mirroring internally; do the same explicitly.
      for (let k = 1; k < nBins - 1; k++) {
        fRe[nFft - k] = fRe[k];
        fIm[nFft - k] = -fIm[k];
      }

      fft(fRe, fIm, plan, true);

      const off = f * hopLength;
      for (let i = 0; i < nFft; i++) {
        const w = window[i];
        out[off + i] += fRe[i] * w;
        env[off + i] += w * w;
      }
    }

    const trimmed = new Float64Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const e = env[half + i];
      trimmed[i] = e > 1e-8 ? out[half + i] / e : 0;
    }
    result.push(trimmed);
  }

  return result;
}

/* ------------------------------------------------------------------ *
 * Chunk plan (mirrors mdx_separator.demix)
 * ------------------------------------------------------------------ */

/**
 * Compute the padding / hopping plan used to feed a full track through a
 * model whose input covers `segmentSize` spectrogram frames.
 *
 * @param {number} totalSamples
 * @param {{nFft: number, segmentSize?: number, overlap?: number}} opts
 */
export function planChunks(totalSamples, opts) {
  const { nFft } = opts;
  // No defaults: these come from the server (config.SEPARATOR_PARAMS), so the
  // bundle carries no tuning constants of its own.
  const { segmentSize, overlap } = opts;
  if (!Number.isFinite(segmentSize) || !Number.isFinite(overlap)) {
    throw new Error('planChunks requires explicit segmentSize and overlap');
  }

  const hop = nFft >> 2; // audio-separator uses n_fft // 4
  const trim = nFft >> 1;
  const chunkSize = hop * (segmentSize - 1);
  const genSize = chunkSize - 2 * trim;
  const pad = genSize + trim - (totalSamples % genSize);
  const step = Math.round((1 - overlap) * chunkSize);

  return {
    hop,
    trim,
    chunkSize,
    genSize,
    pad,
    step,
    segmentSize,
    mixtureLength: trim + totalSamples + pad,
    framesPerChunk: segmentSize,
  };
}

/* ------------------------------------------------------------------ *
 * Notes
 * ------------------------------------------------------------------ *
 * The padded mixture (`[0]*trim + signal + [0]*pad`) is built inline in
 * mdxSeparator.js so the signal can be peak-scaled in the same pass, avoiding
 * an extra full-length buffer.
 */
