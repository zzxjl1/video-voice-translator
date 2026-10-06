/**
 * The one list of target languages.
 *
 * It used to be written out verbatim in both `Header.tsx` and `VideoUpload.tsx`,
 * which meant every change had to be made twice and the two copies could
 * silently drift.
 *
 * What is here, and why, is not a style choice — it is the INTERSECTION of the
 * two models in the pipeline:
 *
 *   ASR (`qwen-audio-3.1-asr-flash-filetrans`) hears 30 languages
 *   TTS (`qwen-audio-3.1-tts-flash`)             speaks 10, via its 4
 *                                                multilingual system voices
 *
 * Anything outside that intersection can be transcribed but not dubbed, so
 * offering it would produce a video with no audio track worth hearing. The
 * other 20 languages the ASR model understands are reachable by picking a
 * SOURCE video in them — the target list is about output only.
 */

export interface LanguageOption {
  /** Exactly what the backend stores in `target_language`. */
  value: string;
  /** Shown in the selector. */
  label: string;
}

/**
 * A target language, which additionally knows how a browser asks for it.
 *
 * `tags` exists so that "default to the browser's language" and this list cannot
 * drift apart. They did: the guess was a hand-written table of five prefixes
 * over in `App.tsx`, while this list grew to ten — so a browser set to
 * Portuguese or Italian fell through to English, even though both are offered
 * as targets. Carrying the tags ON the options turns an omission into a type
 * error instead of a silent fallback.
 */
export interface TargetLanguage extends LanguageOption {
  /**
   * BCP-47 PRIMARY subtags that select this language ("pt-BR" → "pt").
   *
   * An array because a language can be written more than one way; the first
   * match in the browser's own preference order wins.
   */
  tags: string[];
}

/**
 * The six that cover almost every job, in rough order of how often they are
 * the target.
 *
 * Deliberately ordered by how often they are the target rather than
 * alphabetically: English and Chinese are where most dubs land, and burying
 * them below French would cost a scroll on the common case.
 *
 * (This said "seven" for a while after Spanish was removed. The list is the
 * authority; the number in the prose was not.)
 */
export const COMMON_LANGUAGES: TargetLanguage[] = [
  { value: 'English', label: 'English', tags: ['en'] },
  { value: 'Chinese', label: 'Chinese', tags: ['zh'] },
  { value: 'Japanese', label: 'Japanese', tags: ['ja'] },
  { value: 'Korean', label: 'Korean', tags: ['ko'] },
  { value: 'French', label: 'French', tags: ['fr'] },
  { value: 'German', label: 'German', tags: ['de'] },
];

/**
 * Spanish used to be here, with a note apologising for it.
 *
 * It is the ONE language in this list that no built-in voice speaks: the four
 * multilingual system voices are documented for the eight languages below, and
 * Spanish is not among them. Keeping it meant every Spanish job produced audio
 * that mispronounces — the docs call it 「可能发音错误或语音不自然」, a quality
 * problem rather than an error — and nothing surfaced it.
 *
 * Both lists below are a subset of `config.SYSTEM_VOICE_LANGUAGES` on the
 * backend, which is the authority. Nothing in this file may name a language
 * outside that set: the UI only appears before a video exists, so it cannot ask
 * the server, and a list that disagrees with the voices is what caused this.
 *
 * Spanish remains reachable through voice CLONING instead, which is documented
 * for it and 15 other foreign languages.
 */

/**
 * The rest of the intersection — both models handle these, they are simply
 * asked for less often. Kept behind a toggle so the common case stays a short
 * list rather than a 11-item menu.
 */
export const MORE_LANGUAGES: TargetLanguage[] = [
  { value: 'Portuguese', label: 'Portuguese', tags: ['pt'] },
  { value: 'Italian', label: 'Italian', tags: ['it'] },
  { value: 'Vietnamese', label: 'Vietnamese', tags: ['vi'] },
  { value: 'Indonesian', label: 'Indonesian', tags: ['id'] },
];

export const ALL_LANGUAGES: TargetLanguage[] = [...COMMON_LANGUAGES, ...MORE_LANGUAGES];

/** Where the selector lands when nothing about the browser is recognised. */
export const DEFAULT_TARGET_LANGUAGE = 'English';

/**
 * The target language to start on, taken from the browser's own preference.
 *
 * Reads `navigator.languages` — the ORDERED list — rather than the singular
 * `navigator.language`. They differ: a browser set to [fr-FR, en-US] means the
 * user reads French first, and only looking at the first entry would have been
 * enough here, but reading the singular property alone ignores the list
 * entirely and there is no reason to.
 *
 * Matching is on the primary subtag, so every regional variant of a language
 * lands on one option: "pt-BR" and "pt-PT" both give Portuguese, "zh-Hans-CN"
 * gives Chinese.
 *
 * Falls back to English, which is also what a language outside the list gets.
 * There is no honest way to choose between ten options from a tag none of them
 * claims — English is the least surprising guess, not a good one.
 */
export function detectBrowserLanguage(): string {
  if (typeof navigator === 'undefined') return DEFAULT_TARGET_LANGUAGE;

  const preferred =
    navigator.languages && navigator.languages.length
      ? navigator.languages
      : [navigator.language];

  for (const tag of preferred) {
    const primary = String(tag || '').toLowerCase().split('-')[0];
    if (!primary) continue;
    const match = ALL_LANGUAGES.find(l => l.tags.includes(primary));
    if (match) return match.value;
  }

  return DEFAULT_TARGET_LANGUAGE;
}

/**
 * Dialects the four multilingual SYSTEM voices are documented to speak.
 *
 * Deliberately not the 23-dialect list from the voice-cloning page. That list
 * needs cloning enabled to mean anything, whereas these eight are what
 * `longanhuan_v3.1` / `longanlingxin_v3.1` / `longanfengyue_v3.1` /
 * `xunanchuan_v3.1` support on their own — and those four are exactly what
 * `config.voices_for_language("Chinese")` selects.
 *
 * Offering the 23 would mean offering 15 dialects that silently fall back to
 * Mandarin for everyone who has not turned cloning on.
 */
export const CHINESE_ACCENTS: LanguageOption[] = [
  { value: '', label: '普通话' },
  { value: '广东话', label: '广东话' },
  { value: '上海话', label: '上海话' },
  { value: '东北话', label: '东北话' },
  { value: '重庆话', label: '重庆话' },
  { value: '陕西话', label: '陕西话' },
  { value: '云南话', label: '云南话' },
  { value: '宁波话', label: '宁波话' },
  { value: '甘肃话', label: '甘肃话' },
];

/** Mandarin. The empty value means "send no instruction at all". */
export const DEFAULT_CHINESE_ACCENT = '';

/** Whether this language takes an accent. Only Chinese has documented ones. */
export function hasAccents(value: string): boolean {
  return value === 'Chinese';
}

/** The accent label for display, never the raw empty string. */
export function accentLabel(accent: string): string {
  return CHINESE_ACCENTS.find(a => a.value === accent)?.label ?? '普通话';
}

/** The language label for display. */
export function languageLabel(value: string): string {
  return ALL_LANGUAGES.find(l => l.value === value)?.label ?? value;
}
