/**
 * Tests for the streaming separation path (no model, no browser needed).
 *
 * The ONNX model is replaced by a stand-in: an identity (or a pure gain) on the
 * spectrogram. For such a model the "instrumental" output must equal
 * `gain * input`, which pins down everything this path is responsible for —
 * window planning, the cross-fade carry between windows, the hand-off to the
 * sink, and the WAV readers/writers around it — without a 64 MB model.
 *
 * Run:  node scripts/test-streaming.mjs
 */
import {
  planWindows,
  separateTrack,
  separateTrackStreaming,
  sourceFromChannels,
} from '../utils/mdx/mdxSeparator.js';
import {
  createPcmBlobWriter,
  encodeWav,
  openAudioSource,
  parseWavHeader,
  probeMediaDuration,
  sourceFromWav,
} from '../utils/mdx/separatorClient.js';

let failures = 0;
function check(label, ok, detail) {
  if (!ok) failures++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? '  ' + detail : ''}`);
}

async function rejects(fn) {
  try {
    await fn();
    return null;
  } catch (e) {
    return e instanceof Error ? e.message : String(e);
  }
}

/* Small STFT geometry so a few windows run in milliseconds. */
const PARAMS = { nFft: 512, dimF: 256, segmentSize: 16, overlap: 0.25 };
const RATE = 8000;
const OPTS = { sampleRate: RATE, windowSeconds: 1, fadeSeconds: 0.1 }; // win 8000, fade 800

const gainModel = (g) => async (spec) => {
  const out = new Float32Array(spec.length);
  for (let i = 0; i < spec.length; i++) out[i] = spec[i] * g;
  return out;
};

function makeSignal(n, seed = 1) {
  let s = seed;
  const rnd = () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296) - 0.5;
  const left = new Float32Array(n);
  const right = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    left[i] = 0.4 * Math.sin((2 * Math.PI * 440 * i) / RATE) + 0.1 * rnd();
    right[i] = 0.3 * Math.sin((2 * Math.PI * 660 * i) / RATE) + 0.1 * rnd();
  }
  return [left, right];
}

function maxErr(a, b, scale = 1) {
  let m = 0;
  for (let i = 0; i < a.length; i++) m = Math.max(m, Math.abs(a[i] - b[i] * scale));
  return m;
}

/** Run the streaming separator and gather what the sink saw. */
async function collect(model, channels, extra = {}) {
  const n = channels[0].length;
  const inst = channels.map(() => new Float32Array(n));
  const voc = channels.map(() => new Float32Array(n));
  const chunks = [];
  const reads = [];
  const base = sourceFromChannels(channels, RATE);
  const source = {
    ...base,
    read: (s, e) => {
      reads.push([s, e]);
      return base.read(s, e);
    },
  };
  const stats = await separateTrackStreaming(model, source, PARAMS, {
    ...OPTS,
    ...extra,
    onSamples: ({ start, instrumental, vocals }) => {
      chunks.push([start, instrumental[0].length]);
      for (let c = 0; c < channels.length; c++) {
        inst[c].set(instrumental[c], start);
        voc[c].set(vocals[c], start);
      }
    },
  });
  return { inst, voc, chunks, reads, stats };
}

/* ---------------- 1. window planning ---------------- */

{
  const win = 8000;
  const fade = 800;
  const step = win - fade;

  const covers = (total) => {
    const { starts } = planWindows(total, win, fade);
    const lastEnd = Math.min(total, starts[starts.length - 1] + win);
    return lastEnd === total;
  };
  const totals = [1, 100, 7999, 8000, 8001, 8000 + 500, 8000 + 799, 8000 + 800, 8000 + 801, 3 * step, 3 * step + 1, 25000, 100000];
  check('planWindows: every plan reaches the end of the track', totals.every(covers));

  // No window may be redundant: a window that starts inside the previous one's
  // cross-fade tail is a whole model pass for audio that is already final.
  const redundantFree = totals.every((t) => {
    const { starts } = planWindows(t, win, fade);
    return starts.every((s, i) => i === 0 || starts[i - 1] + win < t);
  });
  check('planWindows: no redundant trailing window', redundantFree);

  check('planWindows: shorter than one window -> exactly one', planWindows(5000, win, fade).starts.length === 1);
  check('planWindows: exactly one window long -> one', planWindows(win, win, fade).starts.length === 1);
  check('planWindows: one sample past a window -> two', planWindows(win + 1, win, fade).starts.length === 2);
}

/* ---------------- 2. streaming == gain * input ---------------- */

for (const total of [3000, 8000, 8000 + 500, 8000 + 800, 8000 + 801, 20000, 31337]) {
  const channels = makeSignal(total, total);
  const { inst, voc, chunks, reads, stats } = await collect(gainModel(0.5), channels);

  const eI = Math.max(maxErr(inst[0], channels[0], 0.5), maxErr(inst[1], channels[1], 0.5));
  const eV = Math.max(maxErr(voc[0], channels[0], 0.5), maxErr(voc[1], channels[1], 0.5));
  // vocals = mix - 0.5*mix = 0.5*mix, instrumental = 0.5*mix
  check(`total=${total}: instrumental = 0.5 * input`, eI < 2e-3, `max err ${eI.toExponential(2)}`);
  check(`total=${total}: vocals = input - instrumental`, eV < 2e-3, `max err ${eV.toExponential(2)}`);

  let next = 0;
  const contiguous = chunks.every(([s, n]) => s === next && n > 0 && (next += n, true));
  check(`total=${total}: sink got contiguous, in-order ranges covering the track`, contiguous && next === total, `${chunks.length} chunk(s)`);
  check(`total=${total}: stats.samples matches`, stats.samples === total && stats.windows === chunks.length);
  check(
    `total=${total}: every read and every emit is at most one window`,
    reads.every(([s, e]) => e - s <= 8000) && chunks.every(([, n]) => n <= 8000)
  );
}

{
  // Pure sines: nothing lives in the one bin the model drops (Nyquist), so the
  // only error left is what the window seams add. Noise would put an inherent
  // ~1e-3 floor under this and hide a seam artefact of the same size.
  const n = 40000; // five windows
  const channels = [new Float32Array(n), new Float32Array(n)];
  for (let i = 0; i < n; i++) {
    channels[0][i] = 0.5 * Math.sin((2 * Math.PI * 440 * i) / RATE);
    channels[1][i] = 0.4 * Math.sin((2 * Math.PI * 1234 * i) / RATE + 1);
  }
  const { inst, voc, chunks } = await collect(gainModel(1), channels);

  // Away from the two ends of the track (where the STFT's reflect padding leaves
  // an error of ~2e-4 that is inherent to the algorithm and not a seam matter).
  const edge = 2000;
  const interior = (a, b, f = 1) => {
    let m = 0;
    for (let i = edge; i < n - edge; i++) m = Math.max(m, Math.abs(a[i] - b[i] * f));
    return m;
  };
  const eI = Math.max(interior(inst[0], channels[0]), interior(inst[1], channels[1]));
  check('identity model reproduces a clean signal across all window seams', eI < 2e-4, `max err ${eI.toExponential(2)}, ${chunks.length} windows`);

  // Each window after the first begins inside the previous one's cross-fade.
  // The reconstruction has a small error floor of its own; a bad seam would
  // show up as error well ABOVE that floor, so compare the two.
  const seams = chunks.slice(1).map(([s]) => s);
  const nearSeam = (i) => seams.some((s) => Math.abs(i - s) < 100);
  let seamErr = 0;
  let floorErr = 0;
  for (let i = edge; i < n - edge; i++) {
    const e = Math.max(Math.abs(inst[0][i] - channels[0][i]), Math.abs(inst[1][i] - channels[1][i]));
    if (nearSeam(i)) seamErr = Math.max(seamErr, e);
    else floorErr = Math.max(floorErr, e);
  }
  check(
    '...and the seams are no worse than the rest of the signal',
    seams.length === 5 && seamErr <= floorErr * 1.25,
    `seams ${seamErr.toExponential(2)} vs elsewhere ${floorErr.toExponential(2)}`
  );

  const silent = new Float32Array(n);
  const eV = Math.max(interior(voc[0], silent), interior(voc[1], silent));
  check('the secondary stem of an identity model is silence', eV < 2e-4, `max ${eV.toExponential(2)}`);
}

/* ---------------- 3. in-memory wrapper agrees with the stream ---------------- */

{
  const channels = makeSignal(19000, 3);
  const streamed = await collect(gainModel(0.7), channels);
  const whole = await separateTrack(gainModel(0.7), channels, PARAMS, OPTS);
  const e = Math.max(maxErr(streamed.inst[0], whole.instrumental[0]), maxErr(streamed.voc[1], whole.vocals[1]));
  check('separateTrack (in-memory) matches the streaming result', e === 0, `max diff ${e}`);
}

/* ---------------- 4. guard rails ---------------- */

{
  const channels = makeSignal(30000, 9);
  const controller = { aborted: false };
  let windowsSeen = 0;
  const msg = await rejects(() =>
    separateTrackStreaming(gainModel(1), sourceFromChannels(channels, RATE), PARAMS, {
      ...OPTS,
      signal: controller,
      onSamples: () => {
        windowsSeen++;
        controller.aborted = true;
      },
    })
  );
  check('abort stops before the next window', msg === 'aborted' && windowsSeen === 1, `${windowsSeen} window(s) ran`);

  const bad = {
    channels: 2,
    length: 9000,
    sampleRate: RATE,
    read: (s, e) => [new Float32Array(e - s - 1), new Float32Array(e - s - 1)],
  };
  const m2 = await rejects(() => separateTrackStreaming(gainModel(1), bad, PARAMS, OPTS));
  check('a source returning the wrong length is rejected', /expected 2 x/.test(m2 ?? ''), m2 ?? '');

  const m3 = await rejects(() =>
    separateTrackStreaming(gainModel(1), { channels: 2, length: 0, read: () => [] }, PARAMS, OPTS)
  );
  check('empty input is rejected', m3 === 'empty input', m3 ?? '');
}

/* ---------------- 5. WAV read / write ---------------- */

// 16-bit round trip: the encoder truncates and scales by 32767 on the way in,
// the reader divides by 32768 on the way out — up to ~1.4 LSB apart.
const TWO_LSB = 2 / 32768;

{
  const channels = makeSignal(5000, 11);
  const wav = encodeWav(channels, RATE);
  const header = await parseWavHeader(wav);
  check(
    'parseWavHeader reads a plain stereo WAV',
    header.channels === 2 && header.sampleRate === RATE && header.dataOffset === 44 && header.dataBytes === 5000 * 4,
    JSON.stringify(header)
  );

  const src = await sourceFromWav(wav);
  check('sourceFromWav: length and duration', src.length === 5000 && src.channels === 2 && Math.abs(src.duration - 5000 / RATE) < 1e-9);

  const [l, r] = await src.read(1234, 4321);
  const expectL = channels[0].subarray(1234, 4321);
  const expectR = channels[1].subarray(1234, 4321);
  check(
    'sourceFromWav: a window read back equals the original to 16-bit precision',
    l.length === 3087 && Math.max(maxErr(l, expectL), maxErr(r, expectR)) < TWO_LSB
  );
}

{
  // A LIST chunk of ODD size between fmt and data (ffmpeg writes LIST/INFO; odd
  // sizes are padded to a word boundary and a reader that ignores it is off by one).
  const channels = makeSignal(2000, 5);
  const plain = new Uint8Array(await encodeWav(channels, RATE).arrayBuffer());
  const listBody = new TextEncoder().encode('INFOabc'); // 7 bytes -> 1 pad byte
  const list = new Uint8Array(8 + listBody.length + 1);
  list.set(new TextEncoder().encode('LIST'), 0);
  new DataView(list.buffer).setUint32(4, listBody.length, true);
  list.set(listBody, 8);
  const withList = new Uint8Array(plain.length + list.length);
  withList.set(plain.subarray(0, 36), 0);
  withList.set(list, 36);
  withList.set(plain.subarray(36), 36 + list.length);
  new DataView(withList.buffer).setUint32(4, withList.length - 8, true);

  const src = await sourceFromWav(new Blob([withList]));
  const [l] = await src.read(0, 2000);
  check('WAV with an odd-sized LIST chunk before data is read correctly', src.length === 2000 && maxErr(l, channels[0]) < TWO_LSB);

  // data size 0xFFFFFFFF = "to the end of the file" (streaming / RF64 writers)
  const open = new Uint8Array(plain);
  new DataView(open.buffer).setUint32(40, 0xffffffff, true);
  const srcOpen = await sourceFromWav(new Blob([open]));
  check('data chunk of unknown size is read to the end of the file', srcOpen.length === 2000);

  // header says more than the file has (truncated download)
  const cut = plain.subarray(0, 44 + 1000 * 4);
  const srcCut = await sourceFromWav(new Blob([cut]));
  check('a truncated file is measured by what is actually there', srcCut.length === 1000);
}

{
  const mono = encodeWav([makeSignal(800, 2)[0]], RATE);
  const src = await sourceFromWav(mono);
  const [l, r] = await src.read(0, 800);
  check('mono WAV is presented to the model as a stereo pair', src.channels === 2 && l === r && l.length === 800);
}

{
  const good = new Uint8Array(await encodeWav(makeSignal(100, 4), RATE).arrayBuffer());
  const patch = (fn) => {
    const copy = new Uint8Array(good);
    fn(new DataView(copy.buffer));
    return new Blob([copy]);
  };
  check('rejects 8-bit WAV', /bit depth/.test((await rejects(() => parseWavHeader(patch((v) => v.setUint16(34, 8, true))))) ?? ''));
  check('rejects float WAV', /format tag 3/.test((await rejects(() => parseWavHeader(patch((v) => v.setUint16(20, 3, true))))) ?? ''));
  check('rejects 6-channel WAV', /channel count/.test((await rejects(() => parseWavHeader(patch((v) => v.setUint16(22, 6, true))))) ?? ''));
  check('rejects a non-WAV file', /not a WAV/.test((await rejects(() => parseWavHeader(new Blob([new Uint8Array(64)])))) ?? ''));
  check('rejects a file that is too short', /too short/.test((await rejects(() => parseWavHeader(new Blob([new Uint8Array(5)])))) ?? ''));

  // read past the end of a file whose header over-claims -> clear error, not silence
  const src = await sourceFromWav(new Blob([good]));
  const m = await rejects(() => src.read(0, 500));
  check('reading beyond the data reports a truncated WAV', /ended before/.test(m ?? ''), m ?? '');
}

/* ---------------- 6. blob writer ---------------- */

{
  const channels = makeSignal(10000, 13);
  const writer = createPcmBlobWriter(RATE, 2);
  for (const [a, b] of [[0, 3000], [3000, 3001], [3001, 3001], [3001, 10000]]) {
    writer.push([channels[0].subarray(a, b), channels[1].subarray(a, b)]);
  }
  const streamed = new Uint8Array(await writer.finish().arrayBuffer());
  const direct = new Uint8Array(await encodeWav(channels, RATE).arrayBuffer());
  check(
    'createPcmBlobWriter output is byte-identical to encoding it in one go',
    writer.frames === 10000 && streamed.length === direct.length && streamed.every((v, i) => v === direct[i])
  );

  const wrong = (() => {
    try {
      createPcmBlobWriter(RATE, 2).push([channels[0]]);
      return null;
    } catch (e) {
      return e.message;
    }
  })();
  check('writer rejects a piece with the wrong channel count', /expected 2/.test(wrong ?? ''), wrong ?? '');
  check('writer refuses to finish with no audio', /no audio/.test((await rejects(async () => createPcmBlobWriter(RATE, 1).finish())) ?? ''));

  const mono = encodeWav(channels, RATE, { mono: true });
  const h = await parseWavHeader(mono);
  check('encodeWav mono mix-down yields one channel', h.channels === 1 && h.dataBytes === 10000 * 2);
}

/* ---------------- 7. choosing a source ---------------- */

{
  check('probeMediaDuration is null where there is no media element', (await probeMediaDuration(new Blob([new Uint8Array(10)]))) === null);

  const wav = encodeWav(makeSignal(3000, 17), RATE);
  const logs = [];
  let fetched = 0;
  const viaServer = await openAudioSource({
    file: new Blob([new Uint8Array(10)]), // duration unknown -> must take the bounded route
    fetchServerSource: async () => (fetched++, wav),
    onLog: (m) => logs.push(m),
  });
  check(
    'unknown duration takes the server route (never guesses "short")',
    fetched === 1 && viaServer.length === 3000 && logs.some((m) => /Could not read the duration/.test(m))
  );

  const noFile = await openAudioSource({ fetchServerSource: async () => wav });
  check('no local file (page reloaded) -> server route', noFile.length === 3000);

  const none = await rejects(() => openAudioSource({ file: new Blob([new Uint8Array(10)]) }));
  check('no usable source at all is a clear error', /no audio source/.test(none ?? ''), none ?? '');
}

console.log(failures === 0 ? '\n合计 全部通过' : `\n合计 ${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
