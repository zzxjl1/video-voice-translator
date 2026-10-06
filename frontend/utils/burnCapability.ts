/**
 * Can THIS browser burn subtitles into a video?
 *
 * The server never can — its ffmpeg has no libass/freetype and it would have to
 * re-encode on a CPU-only host. So burn-in runs here, and the option has to be
 * greyed out with a concrete reason when the browser cannot do it, exactly like
 * the server-side separation and subtitle capabilities.
 *
 * The check is a real `isConfigSupported` probe rather than a feature sniff:
 * `VideoEncoder` existing says nothing about whether this machine has an H.264
 * encoder, and a user with the API but no encoder would otherwise get a failure
 * halfway through a long render.
 */

export interface BurnCapability {
  available: boolean;
  reason: string | null;
  /** Encoder strings accepted here, in preference order. */
  codecs: string[];
}

const CANDIDATE_CODECS = ['avc1.42E01E', 'avc1.4D401E', 'avc1.640028'];

/**
 * Whether the browser-side renderer exists yet.
 *
 * `VideoEncoder` being present is necessary but NOT sufficient — a capability
 * probe that only checks the API would offer "Burn in" and then fail at the
 * end of a long render. Until the WebCodecs pipeline ships, burn-in is
 * reported unavailable for this honest reason rather than being silently
 * downgraded to an embedded track server-side.
 */
const BURN_RENDERER_IMPLEMENTED = false;

let cached: BurnCapability | null = null;
let inFlight: Promise<BurnCapability> | null = null;

export async function probeBurnCapability(): Promise<BurnCapability> {
  if (cached) return cached;
  if (inFlight) return inFlight;

  if (!BURN_RENDERER_IMPLEMENTED) {
    cached = {
      available: false,
      reason: '浏览器端烧录功能尚未实现',
      codecs: [],
    };
    return cached;
  }

  inFlight = (async (): Promise<BurnCapability> => {
    const missing = (
      typeof VideoEncoder === 'undefined' ? 'VideoEncoder' :
      typeof VideoDecoder === 'undefined' ? 'VideoDecoder' :
      typeof AudioEncoder === 'undefined' ? 'AudioEncoder' :
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
    for (const codec of CANDIDATE_CODECS) {
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

/** Test seam: forget the memoised probe. */
export function resetBurnCapabilityCache(): void {
  cached = null;
}
