/**
 * Kokoro voice catalog (static, no network).
 *
 * All 54 Kokoro-82M v1.0 voices with metadata from the official VOICES.md
 * sheet (hexgrad/Kokoro-82M). The engine (kokoroWorker) accepts any of these
 * ids even though the official kokoro-js registry only registers the English
 * ones — the worker phonemizes other languages itself and calls
 * generate_from_ids (see docs/dubbing-modes.md).
 *
 * Grade = official estimate of training-data quality×quantity (A best, F worst).
 * The Chinese voices are all D — that is the model's weakest language.
 */
export interface BrowserVoice {
  /** Kokoro voice id, e.g. "zf_xiaobei" — stored in project.json. */
  key: string;
  name: string;
  gender: '女' | '男';
  grade: string;
}

/** Target languages the browser engine can actually speak (Kokoro's matrix). */
export const KOKORO_LANGUAGES = new Set(['Chinese', 'English', 'French', 'Italian', 'Portuguese']);

const CATALOG: Record<string, BrowserVoice> = {
  // en-us
  af_heart: { key: 'af_heart', name: 'Heart', gender: '女', grade: 'A' },
  af_alloy: { key: 'af_alloy', name: 'Alloy', gender: '女', grade: 'C' },
  af_aoede: { key: 'af_aoede', name: 'Aoede', gender: '女', grade: 'C+' },
  af_bella: { key: 'af_bella', name: 'Bella', gender: '女', grade: 'A−' },
  af_jessica: { key: 'af_jessica', name: 'Jessica', gender: '女', grade: 'D' },
  af_kore: { key: 'af_kore', name: 'Kore', gender: '女', grade: 'C+' },
  af_nicole: { key: 'af_nicole', name: 'Nicole', gender: '女', grade: 'B−' },
  af_nova: { key: 'af_nova', name: 'Nova', gender: '女', grade: 'C' },
  af_river: { key: 'af_river', name: 'River', gender: '女', grade: 'D' },
  af_sarah: { key: 'af_sarah', name: 'Sarah', gender: '女', grade: 'C+' },
  af_sky: { key: 'af_sky', name: 'Sky', gender: '女', grade: 'C−' },
  am_adam: { key: 'am_adam', name: 'Adam', gender: '男', grade: 'F+' },
  am_echo: { key: 'am_echo', name: 'Echo', gender: '男', grade: 'D' },
  am_eric: { key: 'am_eric', name: 'Eric', gender: '男', grade: 'D' },
  am_fenrir: { key: 'am_fenrir', name: 'Fenrir', gender: '男', grade: 'C+' },
  am_liam: { key: 'am_liam', name: 'Liam', gender: '男', grade: 'D' },
  am_michael: { key: 'am_michael', name: 'Michael', gender: '男', grade: 'C+' },
  am_onyx: { key: 'am_onyx', name: 'Onyx', gender: '男', grade: 'D' },
  am_puck: { key: 'am_puck', name: 'Puck', gender: '男', grade: 'C+' },
  am_santa: { key: 'am_santa', name: 'Santa', gender: '男', grade: 'D−' },
  // en-gb
  bf_emma: { key: 'bf_emma', name: 'Emma', gender: '女', grade: 'B−' },
  bf_isabella: { key: 'bf_isabella', name: 'Isabella', gender: '女', grade: 'C' },
  bf_alice: { key: 'bf_alice', name: 'Alice', gender: '女', grade: 'D' },
  bf_lily: { key: 'bf_lily', name: 'Lily', gender: '女', grade: 'D' },
  bm_george: { key: 'bm_george', name: 'George', gender: '男', grade: 'C' },
  bm_lewis: { key: 'bm_lewis', name: 'Lewis', gender: '男', grade: 'D+' },
  bm_daniel: { key: 'bm_daniel', name: 'Daniel', gender: '男', grade: 'D' },
  bm_fable: { key: 'bm_fable', name: 'Fable', gender: '男', grade: 'C' },
  // fr
  ff_siwis: { key: 'ff_siwis', name: 'Siwis', gender: '女', grade: 'B−' },
  // it
  if_sara: { key: 'if_sara', name: 'Sara', gender: '女', grade: 'C' },
  im_nicola: { key: 'im_nicola', name: 'Nicola', gender: '男', grade: 'C' },
  // pt-br
  pf_dora: { key: 'pf_dora', name: 'Dora', gender: '女', grade: '' },
  pm_alex: { key: 'pm_alex', name: 'Alex', gender: '男', grade: '' },
  pm_santa: { key: 'pm_santa', name: 'Santa', gender: '男', grade: '' },
  // zh — official grades are all D (thin training data)
  zf_xiaobei: { key: 'zf_xiaobei', name: '晓贝', gender: '女', grade: 'D' },
  zf_xiaoni: { key: 'zf_xiaoni', name: '晓妮', gender: '女', grade: 'D' },
  zf_xiaoxiao: { key: 'zf_xiaoxiao', name: '晓晓', gender: '女', grade: 'D' },
  zf_xiaoyi: { key: 'zf_xiaoyi', name: '晓伊', gender: '女', grade: 'D' },
  zm_yunjian: { key: 'zm_yunjian', name: '云健', gender: '男', grade: 'D' },
  zm_yunxi: { key: 'zm_yunxi', name: '云希', gender: '男', grade: 'D' },
  zm_yunxia: { key: 'zm_yunxia', name: '云夏', gender: '男', grade: 'D' },
  zm_yunyang: { key: 'zm_yunyang', name: '云扬', gender: '男', grade: 'D' },
  // es / hi exist in the model but are not target languages of the app
  ef_dora: { key: 'ef_dora', name: 'Dora', gender: '女', grade: '' },
  em_alex: { key: 'em_alex', name: 'Alex', gender: '男', grade: '' },
  em_santa: { key: 'em_santa', name: 'Santa', gender: '男', grade: '' },
  hf_alpha: { key: 'hf_alpha', name: 'Alpha', gender: '女', grade: 'C' },
  hf_beta: { key: 'hf_beta', name: 'Beta', gender: '女', grade: 'C' },
  hm_omega: { key: 'hm_omega', name: 'Omega', gender: '男', grade: 'C' },
  hm_psi: { key: 'hm_psi', name: 'Psi', gender: '男', grade: 'C' },
  // ja voices exist but the browser has no Japanese G2P — excluded
};

/** Voices per app target language, best grade first. */
export function voicesFor(language: string): BrowserVoice[] {
  const prefix = { Chinese: ['zf_', 'zm_'], English: ['af_', 'am_', 'bf_', 'bm_'], French: ['ff_'], Italian: ['if_', 'im_'], Portuguese: ['pf_', 'pm_'] }[language];
  if (!prefix) return [];
  const list = Object.values(CATALOG).filter((v) => prefix.some((p) => v.key.startsWith(p)));
  return list.sort((a, b) => (b.grade || 'Z').localeCompare(a.grade || 'Z'));
}

export function defaultVoiceFor(language: string): string | null {
  return { Chinese: 'zf_xiaobei', English: 'af_heart', French: 'ff_siwis', Italian: 'if_sara', Portuguese: 'pf_dora' }[language] ?? null;
}

export function voiceMeta(key: string): BrowserVoice | undefined {
  return CATALOG[key];
}

/**
 * The 115 phoneme characters the Kokoro tokenizer knows (tokenizer.json vocab).
 * Anything else in an espeak phoneme string would become unknown tokens, so
 * the worker strips non-vocab chars before synthesis.
 */
export const KOKORO_VOCAB: ReadonlySet<string> = new Set(
  ` !"$(),.:;?AIOQSTWYabcdefhijklmnopqrstuvwxyzæçðøŋœɐɑɒɔɕɖəɚɛɜɟɡɣɤɥɨɪɯɰɲɳɴɸɹɻɽɾʁʂʃʈʊʋʌʎʒʔʝʣʤʥʦʧʨʰʲˈˌː̃βθχᵊᵝᵻ—“”…→↓↗↘ꭧ`,
);
