import type { SubtitleCue, SubtitleStyle } from '../services/apiService';
import { cueAtTime, drawCueOnCanvas } from './subtitleStyle';

/**
 * Burn subtitles into a video, in this browser.
 *
 * Why here and not on the server: rasterising glyphs needs libass plus an
 * installed CJK font, and it forces a full video re-encode. This deployment is a
 * CPU-only host with neither, which is also why `capabilities()["burn_server"]`
 * reports unavailable. The user's machine has fonts and spare cycles, so the
 * work goes there — the same bargain as in-browser vocal separation.
 *
 * Why mediabunny
 * --------------
 * WebCodecs hands back bare `EncodedVideoChunk`s with no container, so a
 * hand-rolled version needs a demuxer AND a muxer AND its own decode/encode
 * choreography. Mediabunny wraps all of it and, crucially, exposes a per-frame
 * `process` hook: give it a function that receives a `VideoSample` and returns a
 * canvas, and the whole burn becomes that function. It also decides the
 * container and codecs for us, so the result is H.264 + AAC in MP4 — the same
 * thing the server's own export produces.
 *
 * Not MediaRecorder: that records in REAL TIME (a minute of wall clock per
 * minute of video), which is a far worse thing to ask of the user than a few
 * seconds of CPU.
 */

const BURN_MIME_TYPE = 'video/mp4';

export interface BurnRequest {
  /** The dubbed export WITHOUT subtitles — the canvas paints them in. */
  videoUrl: string;
  cues: SubtitleCue[];
  style: SubtitleStyle;
  /** 0..1 as the conversion advances. */
  onProgress?: (fraction: number) => void;
  signal?: AbortSignal;
}

/**
 * Reasons a track can be dropped, in words.
 *
 * Mediabunny reports these as enum codes; a user staring at a failed export
 * needs the sentence, not the code.
 */
const DISCARD_REASON: Record<string, string> = {
  discarded_by_user: '与设置冲突',
  max_track_count_reached: '轨道数量超出容器上限',
  max_track_count_of_type_reached: '同类轨道超出容器上限',
  unknown_source_codec: '无法识别源视频的编码',
  undecodable_source_codec: '这个浏览器无法解码源视频',
  no_encodable_target_codec: '这个浏览器没有可用的编码器',
};

export async function burnSubtitles({
  videoUrl,
  cues,
  style,
  onProgress,
  signal,
}: BurnRequest): Promise<Blob> {
  /*
   * Imported lazily, and deliberately so. The library measures ~85 KB gzipped
   * after tree-shaking and minification — about as much as the rest of the app
   * combined — and loading it for everyone would make every visitor pay for a
   * feature most of them never touch. Vite turns this into a separate chunk
   * fetched the first time someone picks "烧录".
   */
  const { BufferTarget, Conversion, Input, MP4, Mp4OutputFormat, Output, UrlSource } =
    await import('mediabunny');

  /*
   * `[MP4]`, not `ALL_FORMATS`.
   *
   * The source is always the server's own export, which is an MP4. Passing
   * `ALL_FORMATS` tells the bundler to keep a demuxer for every container the
   * library knows (Matroska, MPEG-TS, Ogg, HLS, …) — measured at 595 KB
   * minified / 150 KB gzipped for this lazy chunk, versus a fraction of that
   * for MP4 alone. Listing the one format actually possible is the difference
   * between a cheap optional feature and a heavy one.
   */
  const input = new Input({ formats: [MP4], source: new UrlSource(videoUrl) });
  const output = new Output({ format: new Mp4OutputFormat(), target: new BufferTarget() });

  /*
   * One canvas, reused for every frame — the pattern Mediabunny's own docs use,
   * because the sample is consumed before `process` is called again. Rebuilt
   * only if the frame size changes, which a resize mid-file would cause.
   */
  let canvas: HTMLCanvasElement | null = null;
  let ctx: CanvasRenderingContext2D | null = null;

  const conversion = await Conversion.init({
    input,
    output,
    video: {
      codec: 'avc',
      // The picture MUST be re-encoded: painting into it is the entire point.
      forceTranscode: true,
      process: sample => {
        const width = sample.displayWidth;
        const height = sample.displayHeight;
        if (!canvas || canvas.width !== width || canvas.height !== height) {
          canvas = document.createElement('canvas');
          canvas.width = width;
          canvas.height = height;
          ctx = canvas.getContext('2d');
        }
        if (!canvas || !ctx) return null;

        sample.draw(ctx, 0, 0, width, height);

        /*
         * `sample.timestamp` is in SECONDS (confirmed in the type definition:
         * "The presentation timestamp of the frame in seconds"), which is the
         * same unit `cueAtTime` and the whole cue pipeline already use.
         *
         * `drawCueOnCanvas` is the renderer the on-screen overlay's numbers came
         * from, so the burned frame matches the preview the user approved.
         */
        const cue = cueAtTime(cues, sample.timestamp);
        if (cue && cue.lines.length) {
          drawCueOnCanvas(ctx, cue.lines, style, width, height);
        }
        return canvas;
      },
    },
    /*
     * Audio is NOT forced through the encoder. It is already AAC inside the
     * source MP4, so copying it is both faster and lossless; `codec` only states
     * what to use if the copy turns out to be impossible for some input.
     */
    audio: { codec: 'aac' },
  });

  if (!conversion.isValid) {
    const reasons = Array.from(
      new Set(conversion.discardedTracks.map(track => DISCARD_REASON[track.reason] ?? track.reason)),
    );
    throw new Error(
      reasons.length
        ? `浏览器无法完成这次烧录：${reasons.join('、')}`
        : '浏览器无法完成这次烧录',
    );
  }

  const abort = () => {
    void conversion.cancel();
  };
  signal?.addEventListener('abort', abort, { once: true });
  conversion.onProgress = fraction => onProgress?.(fraction);

  try {
    await conversion.execute();
  } finally {
    signal?.removeEventListener('abort', abort);
  }

  if (signal?.aborted) {
    throw new DOMException('aborted', 'AbortError');
  }

  const buffer = output.target.buffer;
  if (!buffer) {
    throw new Error('烧录没有产出任何数据');
  }
  return new Blob([buffer], { type: BURN_MIME_TYPE });
}
