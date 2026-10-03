/**
 * Numerical validation of `utils/mdx/mdxDsp.js`.
 *
 * 1. FFT vs a naive DFT (arbitrary lengths, including 3*2^k).
 * 2. STFT vs golden vectors produced by torch (scripts/gen-dsp-golden.py).
 * 3. iSTFT vs the same golden vectors.
 *
 * Run:
 *   python3 scripts/gen-dsp-golden.py     # once, needs torch
 *   node scripts/test-dsp.mjs
 */
import fs from 'node:fs';
import { createFftPlan, fft, stft, istft, hannWindow, reflectPad } from '../utils/mdx/mdxDsp.js';

const GOLDEN = '/tmp/mdx_golden';
const CANDIDATES = [
  { tag: 'nfft6144', nFft: 6144, dimF: 3072 },
  { tag: 'nfft7680', nFft: 7680, dimF: 3072 },
];

let failures = 0;

function readF32(name) {
  const buf = fs.readFileSync(`${GOLDEN}/${name}`);
  const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
  return new Float32Array(ab);
}

function stats(a, b, len) {
  const n = len ?? Math.min(a.length, b.length);
  let maxAbs = 0;
  let sumSq = 0;
  let refSq = 0;
  for (let i = 0; i < n; i++) {
    const d = a[i] - b[i];
    if (Math.abs(d) > maxAbs) maxAbs = Math.abs(d);
    sumSq += d * d;
    refSq += b[i] * b[i];
  }
  const rms = Math.sqrt(sumSq / n);
  const ref = Math.sqrt(refSq / n);
  return { maxAbs, rms, ref, rel: ref > 0 ? rms / ref : (rms === 0 ? 0 : Infinity) };
}

function check(label, ok, detail) {
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? '  ' + detail : ''}`);
}

/* ---------------- 1. FFT vs naive DFT ---------------- */

function naiveDft(re, im) {
  const n = re.length;
  const outRe = new Float64Array(n);
  const outIm = new Float64Array(n);
  for (let k = 0; k < n; k++) {
    let sr = 0;
    let si = 0;
    for (let t = 0; t < n; t++) {
      const a = (-2 * Math.PI * k * t) / n;
      const c = Math.cos(a);
      const s = Math.sin(a);
      sr += re[t] * c - im[t] * s;
      si += re[t] * s + im[t] * c;
    }
    outRe[k] = sr;
    outIm[k] = si;
  }
  return [outRe, outIm];
}

console.log('== FFT vs naive DFT ==');
for (const n of [8, 12, 16, 20, 24, 30, 36, 48, 60, 96]) {
  const plan = createFftPlan(n);
  const re = new Float64Array(n);
  const im = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    re[i] = Math.sin(i * 0.7) + 0.3 * Math.cos(i * 2.1);
    im[i] = Math.cos(i * 0.3) - 0.2 * Math.sin(i * 1.7);
  }
  const aRe = Float64Array.from(re);
  const aIm = Float64Array.from(im);
  fft(aRe, aIm, plan, false);

  const [nRe, nIm] = naiveDft(re, im);
  const sRe = stats(aRe, nRe);
  const sIm = stats(aIm, nIm);
  check(
    `fft n=${n} (r=${plan.r}, m=${plan.m})`,
    sRe.rel < 1e-12 && sIm.rel < 1e-12,
    `reRel=${sRe.rel.toExponential(2)} imRel=${sIm.rel.toExponential(2)}`
  );

  // round trip
  fft(aRe, aIm, plan, true);
  const rt = stats(aRe, re);
  check(`ifft n=${n} round-trip`, rt.maxAbs < 1e-12, `maxAbs=${rt.maxAbs.toExponential(2)}`);
}

/* ---------------- window + padding ---------------- */

console.log('\n== window / padding ==');
{
  const w = hannWindow(6144, true);
  // torch.hann_window periodic: first sample 0, symmetric, peak at n/2
  let ok = Math.abs(w[0]) < 1e-12 && Math.abs(w[6144 / 2] - 1) < 1e-12;
  // periodic window is NOT symmetric at the very end (w[n-1] != w[1]) but w[k] == w[n-k]
  for (let k = 1; k < 6144 / 2; k++) if (Math.abs(w[k] - w[6144 - k]) > 1e-12) ok = false;
  check('hannWindow(6144, periodic)', ok);

  const p = reflectPad(Float64Array.from([1, 2, 3, 4]), 2);
  check(
    'reflectPad([1,2,3,4], 2) == [3,2,1,2,3,4,3,2]',
    Array.from(p).join() === [3, 2, 1, 2, 3, 4, 3, 2].join(),
    `got [${Array.from(p).join()}]`
  );
}

/* ---------------- 2 & 3. STFT / iSTFT vs torch golden ---------------- */

if (!fs.existsSync(`${GOLDEN}/input_left.f32`)) {
  console.log(`\n(golden vectors not found in ${GOLDEN} — run scripts/gen-dsp-golden.py first)`);
  process.exit(failures ? 1 : 0);
}

const left = readF32('input_left.f32');
const right = readF32('input_right.f32');

for (const { tag, nFft, dimF } of CANDIDATES) {
  console.log(`\n== ${tag} (nFft=${nFft}, dimF=${dimF}) ==`);
  const hopLength = nFft >> 2;
  const plan = createFftPlan(nFft);
  const window = hannWindow(nFft, true);

  const spec = stft([left, right], { nFft, hopLength, dimF, plan, window });
  const gold = readF32(`${tag}_spec_re_im.f32`);
  console.log(`  frames=${spec.frames} expected=${gold.length / (4 * dimF)}`);

  const s = stats(spec.data, gold);
  check(
    'STFT matches torch',
    s.rel < 5e-4,
    `relRms=${s.rel.toExponential(2)} maxAbs=${s.maxAbs.toExponential(2)} refRms=${s.ref.toFixed(3)}`
  );

  const rec = istft(spec, { nFft, hopLength, channels: 2, plan, window });
  const goldL = readF32(`${tag}_istft_L.f32`);
  const goldR = readF32(`${tag}_istft_R.f32`);
  console.log(`  istft len=${rec[0].length} expected=${goldL.length}`);

  const sl = stats(rec[0], goldL);
  const sr = stats(rec[1], goldR);
  check(
    'iSTFT matches torch (L)',
    sl.rel < 5e-4,
    `relRms=${sl.rel.toExponential(2)} maxAbs=${sl.maxAbs.toExponential(2)} refRms=${sl.ref.toFixed(3)}`
  );
  check(
    'iSTFT matches torch (R)',
    sr.rel < 5e-4,
    `relRms=${sr.rel.toExponential(2)} maxAbs=${sr.maxAbs.toExponential(2)} refRms=${sr.ref.toFixed(3)}`
  );
}

console.log(`\n${failures === 0 ? 'ALL PASS' : failures + ' FAILURE(S)'}`);
process.exit(failures ? 1 : 0);
