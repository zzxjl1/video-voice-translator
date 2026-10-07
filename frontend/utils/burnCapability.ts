/**
 * Can THIS browser burn subtitles into a video?
 *
 * The server never can — its ffmpeg has no libass/freetype and it would have to
 * re-encode on a CPU-only host. So burn-in runs here, and the option has to be
 * greyed out with a concrete reason when the browser cannot do it, exactly like
 * the server-side separation and subtitle capabilities.
 *
 * The renderer runs on WebCodecs (wrapped by mediabunny), which encodes as fast
 * as this machine allows rather than in real time. That is the whole reason for
 * choosing it: a recorder-based path would take one minute of wall clock per
 * minute of video, and the wait would be the user's, not the server's.
 *
 * The check is a real `isConfigSupported` probe rather than a feature sniff:
 * the API existing says nothing about whether this machine has the encoders,
 * and a user with the API but no encoder would otherwise get a failure halfway
 * through a long export.
 *
 * BOTH tracks are probed. The original version only checked video, which means
 * a browser with an H.264 encoder and no AAC encoder would have been reported
 * usable and then failed at the end of the render — precisely what this probe
 * exists to prevent.
 */

export interface BurnCapability {
  available: boolean;
  reason: string | null;
  /** Encoder strings accepted here, in preference order. */
  codecs: string[];
}

const CANDIDATE_VIDEO_CODECS = ['avc1.42E01E', 'avc1.4D401E', 'avc1.640028'];
/**
 * AAC-LC. Required rather than preferred: an MP4 with Opus audio is not what
 * the server's own export produces, and offering two different containers from
 * one button is worse than saying the browser cannot do it.
 */
const REQUIRED_AUDIO_CODEC = 'mp4a.40.2';

let cached: BurnCapability | null = null;
let inFlight: Promise<BurnCapability> | null = null;

export async function probeBurnCapability(): Promise<BurnCapability> {
  if (cached) return cached;
  if (inFlight) return inFlight;

  inFlight = (async (): Promise<BurnCapability> => {
    const missing = (
      typeof VideoEncoder === 'undefined' ? 'VideoEncoder' :
      typeof VideoDecoder === 'undefined' ? 'VideoDecoder' :
      typeof AudioEncoder === 'undefined' ? 'AudioEncoder' :
      typeof AudioDecoder === 'undefined' ? 'AudioDecoder' :
      typeof AudioContext === 'undefined' ? 'AudioContext' :
      null
    );
    if (missing) {
      const result: BurnCapability = {
        available: false,
        reason: `this browser has no ${missing} API (WebCodecs is available in Chrome 94+, Safari 16.4+ and Firefox 130+)`,
        codecs: [],
      };
      cached = result;
      return result;
    }

    const supported: string[] = [];
    for (const codec of CANDIDATE_VIDEO_CODECS) {
      try {
        const result = await VideoEncoder.isConfigSupported({
          codec,
          width: 1920,
          height: 1080,
          bitrate: 8_000_000,
          framerate: 30,
        });
        if (result.supported) supported.push(codec);
      } catch {
        // A codec string the browser does not parse is simply not a candidate.
      }
    }

    if (supported.length === 0) {
      const result: BurnCapability = {
        available: false,
        reason: 'this browser has WebCodecs but no H.264 encoder, so nothing could be rendered',
        codecs: [],
      };
      cached = result;
      return result;
    }

    let audioOk = false;
    try {
      const probed = await AudioEncoder.isConfigSupported({
        codec: REQUIRED_AUDIO_CODEC,
        sampleRate: 48_000,
        numberOfChannels: 2,
      });
      audioOk = probed.supported === true;
    } catch {
      audioOk = false;
    }

    if (!audioOk) {
      // Deliberately fatal rather than falling back to Opus/WebM: the server's
      // export is H.264+AAC in MP4, and a burn-in that quietly returned a
      // different container would be a surprise at the end of a long render.
      const result: BurnCapability = {
        available: false,
        reason: 'this browser has no AAC encoder, so the burned file could not match the .mp4 the server produces',
        codecs: [],
      };
      cached = result;
      return result;
    }

    const result: BurnCapability = { available: true, reason: null, codecs: supported };
    cached = result;
    return result;
  })();

  try {
    return await inFlight;
  } finally {
    inFlight = null;
  }
}

