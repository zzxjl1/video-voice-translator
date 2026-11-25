/**
 * Node 里跑现有 MDX 模型（对比用）。
 *
 * 用 wasm EP，速度和浏览器 WebGPU 没可比性（Node 1.4x vs 浏览器 7x），只用来拿同一输入的 MDX 输出做量化/试听对比。
 *
 * 用法：
 *   ffmpeg -i <input> -f f32le -ac 2 -ar 44100 /tmp/sample.f32
 *   cd frontend && cp ../docs/spikes/roformer-separation/mdx_headless.mjs ./_mdx_run.mjs && node _mdx_run.mjs
 * 输出写到 /tmp/sep-out/mdx_vocals.wav、mdx_instrumental.wav。
 */
import fs from 'node:fs';
import * as ort from 'onnxruntime-web';
import { separateStems } from './features/separation/mdxSeparator.js';

const params = { nFft: 6144, dimF: 3072, segmentSize: 256, overlap: 0.25, compensate: 1.035,
  primaryStem: 'instrumental', zeroLowBins: 3, normalizationThreshold: 1.0 };
const session = await ort.InferenceSession.create(
  fs.readFileSync('../backend/models/uvr-mdx-net-inst-hq-3/UVR-MDX-NET-Inst_HQ_3.onnx'),
  { executionProviders: ['wasm'] });
const { makeRunModel } = await import('./features/separation/separatorClient.js');
const runModel = makeRunModel(session, params);

// ffmpeg 解成 f32le 交错立体声（别自己解 WAV 头——之前 dataStart 算错读到空）
const raw = fs.readFileSync('/tmp/sample.f32');
const n = raw.length / 8;
const chans = [new Float32Array(n), new Float32Array(n)];
const dv = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
for (let i = 0; i < n; i++) { chans[0][i] = dv.getFloat32(i * 8, true); chans[1][i] = dv.getFloat32(i * 8 + 4, true); }

const sr = 44100;
const t0 = Date.now();
const out = await separateStems(runModel, chans, params, { sampleRate: sr });
console.log(`MDX separated in ${(Date.now() - t0) / 1000}s → ${(n / sr / ((Date.now() - t0) / 1000)).toFixed(1)}x realtime (wasm)`);

const write = (name, chs) => {
  const L = chs[0].length;
  const b = Buffer.alloc(44 + L * 4);
  b.write('RIFF'); b.writeUInt32LE(36 + L * 4, 4); b.write('WAVE', 8); b.write('fmt ', 12);
  b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(2, 22); b.writeUInt32LE(sr, 24);
  b.writeUInt32LE(sr * 4, 28); b.writeUInt16LE(4, 32); b.writeUInt16LE(16, 34);
  b.write('data', 36); b.writeUInt32LE(L * 4, 40);
  for (let i = 0; i < L; i++) {
    b.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(chs[0][i] * 32768))), 44 + i * 4);
    b.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round((chs[1]?.[i] ?? chs[0][i]) * 32768))), 46 + i * 4);
  }
  fs.writeFileSync(name, b);
};
write('/tmp/sep-out/mdx_vocals.wav', out.vocals);
write('/tmp/sep-out/mdx_instrumental.wav', out.instrumental);
