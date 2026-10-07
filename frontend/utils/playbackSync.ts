/**
 * Playback-speed maths, shared by the main player and the segment panel so the
 * two can never disagree about how fast a line is being played.
 *
 * Why these bounds are tight
 * --------------------------
 * The server now aims every line at its own time slot — `tts_service`
 * synthesizes at a predicted `speech_rate`, measures the result with ffprobe
 * and re-synthesizes when it misses by more than `TTS_FIT_TOLERANCE` (20%).
 *
 * That means the browser only has to absorb a small residual, so these numbers
 * are a *quality guard* rather than the mechanism that makes timing work.
 * Beyond roughly 25% a pitch-preserved time-stretch stops being transparent
 * (phase-vocoder smearing), and a slightly-off line sounds better than a
 * mangled one — so the old 0.75–1.5 range, which let the player stretch audio
 * to the point of being audibly processed, is deliberately narrowed.
 *
 * Keeping the bounds in one place matters: the panel displays the stretch it
 * thinks is applied, so a mismatch with the player would show the user numbers
 * that are not real.
 */

const AUDIO_RATE_MIN = 0.8;
const AUDIO_RATE_MAX = 1.25;

/** Fallback bounds for the video, only reached if the audio could not cope. */
const VIDEO_RATE_MIN = 0.8;
const VIDEO_RATE_MAX = 1.25;

export interface SyncRates {
  audioRate: number;
  videoRate: number;
}

function clampAudioRate(rate: number): number {
  return Math.min(Math.max(rate, AUDIO_RATE_MIN), AUDIO_RATE_MAX);
}

function clampVideoRate(rate: number): number {
  return Math.min(Math.max(rate, VIDEO_RATE_MIN), VIDEO_RATE_MAX);
}

/**
 * How fast to play a line's audio (and, if it still cannot fit, the video).
 *
 * `audioRate` above 1 means the clip is too long for its slot and is sped up.
 * With the server-side fitting in place `videoRate` should always come out as
 * exactly 1 — it only moves if a line lands outside the audio bounds, which the
 * server is supposed to prevent.
 */
export function computeSyncRates(
  actualDuration?: number,
  targetDuration?: number,
): SyncRates {
  if (!actualDuration || !targetDuration || targetDuration <= 0) {
    return { audioRate: 1, videoRate: 1 };
  }

  const idealFactor = actualDuration / targetDuration;
  const audioRate = clampAudioRate(idealFactor);

  // Whatever the audio could not absorb would have to come out of the video.
  const remainingFactor = audioRate / idealFactor;
  const videoRate = clampVideoRate(remainingFactor);

  return { audioRate, videoRate };
}

/**
 * True when a line is so far from its slot that even the combined audio+video
 * bounds cannot make it fit — it will end up cut short or leave a gap.
 *
 * Derived from the bounds rather than hardcoded, so changing them cannot leave
 * the "Duration Mismatch" badge pointing at the wrong threshold.
 */
export function isOutsideFitRange(
  actualDuration?: number,
  targetDuration?: number,
): boolean {
  if (!actualDuration || !targetDuration || targetDuration <= 0) return false;
  const factor = actualDuration / targetDuration;
  const maxFactor = AUDIO_RATE_MAX / VIDEO_RATE_MIN;
  const minFactor = AUDIO_RATE_MIN / VIDEO_RATE_MAX;
  return factor > maxFactor || factor < minFactor;
}

/** How many seconds a line has to be trimmed/padded to fit, signed. */
export function fitErrorSeconds(
  actualDuration?: number,
  targetDuration?: number,
): number {
  if (!actualDuration || !targetDuration) return 0;
  return actualDuration - targetDuration;
}

/**
 * Turn on pitch preservation explicitly.
 *
 * Browsers default `preservesPitch` to true, so this is usually a no-op — but
 * the whole approach depends on the stretch not shifting pitch, and older
 * WebKit only honoured the prefixed spelling. Relying on a default for
 * something this load-bearing is not worth it.
 */
export function setPreservesPitch(audio: HTMLAudioElement, enabled = true): void {
  audio.preservesPitch = enabled;
  // Prefixed spellings for older engines.
  const legacy = audio as HTMLAudioElement & {
    webkitPreservesPitch?: boolean;
    mozPreservesPitch?: boolean;
  };
  if ('webkitPreservesPitch' in legacy) legacy.webkitPreservesPitch = enabled;
  if ('mozPreservesPitch' in legacy) legacy.mozPreservesPitch = enabled;
}
