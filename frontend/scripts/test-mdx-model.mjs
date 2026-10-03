/**
 * End-to-end check of the browser MDX-Net pipeline against the real ONNX model,
 * executed in Node through onnxruntime-web (same runtime the app uses).
 *
 * Why this exists
 * ---------------
 * `UVR-MDX-NET-Inst_HQ_3.onnx` pins `dim_f = 3072` and `dim_t = 256`, but the
 * STFT size is not recorded in the graph. Two values are plausible from
 * audio-separator's model data (n_fft = 6144 or 7680). This script settles it
 * empirically:
 *
 *   Feed an *instrumental-only* signal. A correct instrumental model must
 *   output approximately the input (correlation ~1, energy ratio ~1). A wrong
 *   STFT convention produces garbage.
 *
 * It also confirms the primary stem really is the instrumental (if the model
 * were a vocals model, music input would come back near-silent).
 *
 * Run from `frontend/`:
 *   node scripts/test-mdx-model.mjs            # both candidates
 *   node scripts/test-mdx-model.mjs 6144       # just one
 */
import fs from 'node:fs';
import path from 'node:path';
import * as ort from 'onnxruntime-web';
import { MDX_INST_HQ_3_PARAMS, separateStems } from '../utils/mdx/mdxSeparator.js';

ort.env.wasm.numThreads = 1;
ort.env.wasm.wasmPaths = path.join(process.cwd(), 'node_modules/onnxruntime-web/dist/');

const MODEL = path.join(process.cwd(), '../backend/models/UVR-MDX-NET-Inst_HQ_3.onnx');
const SR = 44100;
const TOTAL = 240000; // ~5.4 s -> keeps mixtureLength at exactly one chunk size

function makeMusic() {
  const l = new Float64Array(TOTAL);
  const r = new Float64Array(TOTAL);
  for (let i = 0; i < TOTAL; i++) {
    const t = i / SR;
    const env = 0.6 + 0.4 * Math.sin(2 * Math.PI * 0.7 * t); // slow swell
    // bass + chord + closed hi-hat-ish noise
    const bass = 0.30 * Math.sin(2 * Math.PI * 82.4 * t);
    const chord =
      0.20 * Math.sin(2 * Math.PI * 261.6 * t) +
      0.16 * Math.sin(2 * Math.PI * 329.6 * t) +
      0.14 * Math.sin(2 * Math.PI * 392.0 * t);
    const hat = (i % 2205 < 200 ? 1 : 0) * 0.05 * Math.sin(i * 12.9898);
    l[i] = env * (bass + chord + hat);
    r[i] = env * (bass * 0.9 + chord * 0.95 + hat * 0.9);
  }
  return [l, r];
}

function rms(a) {
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * a[i];
  return Math.sqrt(s / a.length);
}

function correlation(a, b) {
  let num = 0;
  let da = 0;
  let db = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    num += a[i] * b[i];
    da += a[i] * a[i];
    db += b[i] * b[i];
  }
  return num / Math.sqrt(da * db);
}

function makeRunner(session) {
  let modelCalls = 0;
  let modelMs = 0;
  const runner = async (specData, frames, dimF) => {
    const t0 = Date.now();
    const input = new ort.Tensor('float32', specData, [1, 4, dimF, frames]);
    const out = await session.run({ input });
    modelMs += Date.now() - t0;
    modelCalls++;
    return Float32Array.from(out.output.data);
  };
  runner.stats = () => ({ modelCalls, modelMs });
  return runner;
}

const candidates = process.argv.slice(2).map(Number).filter((n) => !Number.isNaN(n));
const nFftList = candidates.length ? candidates : [6144, 7680];

if (!fs.existsSync(MODEL)) {
  console.error(`Model not found: ${MODEL}`);
  process.exit(1);
}

console.log(`Loading ${path.basename(MODEL)} (${(fs.statSync(MODEL).size / 1048576).toFixed(1)} MB)...`);
const session = await ort.InferenceSession.create(MODEL, { executionProviders: ['wasm'] });
console.log('session ready\n');

const [musicL, musicR] = makeMusic();
const musicRms = rms(musicL);

const results = [];
for (const nFft of nFftList) {
  const params = { ...MDX_INST_HQ_3_PARAMS, nFft };
  const runner = makeRunner(session);

  const t0 = Date.now();
  const out = await separateStems(runner, [musicL, musicR], params, {
    onProgress: (d, t) => process.stdout.write(`\r  [n_fft=${nFft}] chunk ${d}/${t}   `),
  });
  const elapsed = Date.now() - t0;
  process.stdout.write('\r');

  const inst = out.instrumental[0];
  const voc = out.vocals[0];
  const c = correlation(inst, musicL);
  const ratio = rms(inst) / musicRms;
  const vocRatio = rms(voc) / musicRms;

  results.push({ nFft, corr: c, ratio, vocRatio, elapsed, stats: runner.stats() });

  console.log(`n_fft = ${nFft}  dim_f = ${out.stats.params.dimF}  chunks = ${out.stats.chunks}`);
  console.log(`  instrumental vs input : corr = ${c.toFixed(5)}   energy ratio = ${ratio.toFixed(4)}`);
  console.log(`  vocals (derived)      : energy ratio = ${vocRatio.toFixed(4)}`);
  console.log(
    `  timing                : ${(elapsed / 1000).toFixed(1)}s total, ` +
      `${runner.stats().modelCalls} model calls, ${(runner.stats().modelMs / 1000).toFixed(1)}s in the model`
  );
  console.log('');
}

if (results.length > 1) {
  const best = results.reduce((a, b) => (b.corr > a.corr ? b : a));
  console.log('=== VERDICT ===');
  for (const r of results) {
    console.log(`  n_fft=${r.nFft}: corr=${r.corr.toFixed(5)} ratio=${r.ratio.toFixed(4)}${r === best ? '   <-- best' : ''}`);
  }
  console.log(`\nCorrect n_fft for UVR-MDX-NET-Inst_HQ_3 = ${best.nFft}`);
}

const ok = results.every((r) => r.corr > 0.5);
process.exit(ok ? 0 : 1);
