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

/**
 * The language matrix — the ONE place on the frontend that says which target
 * languages browser dubbing supports, which voice prefixes belong to each, and
 * which voice is the default. The backend keeps the same table in
 * `config.BROWSER_LANGUAGES` (it validates voice ids by prefix); a test
 * (frontend/scripts/test-fixes.mjs) compares the two, so changing one side and
 * forgetting the other fails CI instead of failing a user.
 *
 * Kokoro's voice ids start with `<language><gender>_`. German / Vietnamese /
 * Korean / Indonesian have no Kokoro voice; Japanese voices exist in the model
 * but the browser has no usable Japanese G2P — those languages are simply absent.
 */
export const LANGUAGES: Readonly<Record<string, { default: string; prefixes: readonly string[] }>> = {
  Chinese: { default: 'zf_xiaobei', prefixes: ['zf_', 'zm_'] },
  English: { default: 'af_heart', prefixes: ['af_', 'am_', 'bf_', 'bm_'] },
  French: { default: 'ff_siwis', prefixes: ['ff_'] },
  Italian: { default: 'if_sara', prefixes: ['if_', 'im_'] },
  Portuguese: { default: 'pf_dora', prefixes: ['pf_', 'pm_'] },
};

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
  // The model also has es / hi / ja voices; they are not app target languages (ja has no browser G2P), so
  // they are neither listed here nor shipped in backend/models/kokoro-82m/voices.
};

/**
 * Rank of a grade for sorting: A+ < A < A− < B+ … < F−; unrated (empty) last.
 * Lower = better. (The previous comparator was a descending string compare, which
 * put F and unrated voices FIRST and the A-grade default LAST — so speaker 2
 * onwards were assigned the worst voices in the catalog.)
 */
export function gradeRank(grade: string): number {
  const letter = 'ABCDEF'.indexOf((grade || '')[0]);
  if (letter === -1) return 100;
  const mod = grade.endsWith('+') ? 0 : /[−-]$/u.test(grade) ? 2 : 1;
  return letter * 3 + mod;
}

/** Voices per app target language, best grade first (stable within a grade). */
export function voicesFor(language: string): BrowserVoice[] {
  const lang = LANGUAGES[language];
  if (!lang) return [];
  return Object.values(CATALOG)
    .filter((v) => lang.prefixes.some((p) => v.key.startsWith(p)))
    .sort((a, b) => gradeRank(a.grade) - gradeRank(b.grade));
}

export function defaultVoiceFor(language: string): string | null {
  return LANGUAGES[language]?.default ?? null;
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
