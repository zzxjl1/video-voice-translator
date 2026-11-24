/**
 * Mandarin G2P for Kokoro: pinyin -> IPA with tone arrows, ported from misaki
 * (hexgrad/misaki, Apache-2.0) — the exact phoneme format Kokoro's zh voices
 * were trained on. Tables transcribed from misaki/transcription.py
 * (stefantaubert/pinyin-to-ipa, MIT); tone contours -> arrows (→ ↗ ↓ ↘,
 * neutral = none) per misaki/zh.py retone().
 *
 *   大家 -> ta↘ʨja→   (syllables inside a word unspaced, words space-separated)
 *
 * The Kokoro vocab has NO tone digits, so espeak's "tɑ5" style output can never
 * express tones — this port is why browser Kokoro can do Mandarin at all.
 * Verified 7/9 project lines identical to real Python misaki; the 2 diffs are
 * 一/不 sandhi where the port uses natural speech (pypinyin's dict-driven
 * sandhi is inconsistent anyway).
 *
 * On top of the misaki tables this file does the text front-end misaki leaves
 * to its callers: numbers/percent/money/time/year reading, and handing Latin
 * words (brand names, acronyms) to an English phonemizer instead of letting raw
 * letters into the phoneme string.
 */
import { pinyin } from 'pinyin-pro';

const INITIAL: Record<string, string> = {
  b: 'p', c: 'ʦʰ', ch: 'ꭧʰ', d: 't', f: 'f', g: 'k', j: 'ʨ', k: 'kʰ', l: 'l',
  m: 'm', n: 'n', p: 'pʰ', q: 'ʨʰ', r: 'ɻ', s: 's', sh: 'ʂ', t: 'tʰ', x: 'ɕ',
  z: 'ʦ', zh: 'ꭧ',
  // h: [("x",), ("h",)] — variant[0] only, like legacy_call
  h: 'x',
};
const INITIALS = Object.keys(INITIAL).sort((a, b) => b.length - a.length);

const TONE_CONTOUR: Record<number, string> = { 1: '˥', 2: '˧˥', 3: '˧˩˧', 4: '˥˩', 5: '' };

const SYLLABIC: Record<string, string> = { hm: 'hm0', hng: 'hŋ0', m: 'm0', n: 'n0', ng: 'ŋ0' };
const INTERJECTION: Record<string, string> = { io: 'jɔ0', ê: 'ɛ0', er: 'ɚ0', o: 'ɔ0' };

const FINAL: Record<string, string> = {
  a: 'a0', ai: 'ai̯0', an: 'a0n', ang: 'a0ŋ', ao: 'au̯0', e: 'ɤ0', ei: 'ei̯0',
  en: 'ə0n', eng: 'ə0ŋ', i: 'i0', ia: 'ja0', ian: 'jɛ0n', iang: 'ja0ŋ',
  iao: 'jau̯0', ie: 'je0', in: 'i0n', iou: 'jou̯0', ing: 'i0ŋ', iong: 'jʊ0ŋ',
  ong: 'ʊ0ŋ', ou: 'ou̯0', u: 'u0', uei: 'wei̯0', ua: 'wa0', uai: 'wai̯0',
  uan: 'wa0n', uen: 'wə0n', uang: 'wa0ŋ', ueng: 'wə0ŋ', uo: 'wo0', o: 'wo0',
  'ü': 'y0', 'üe': 'ɥe0', 'üan': 'ɥɛ0n', 'ün': 'y0n',
};
const FINAL_AFTER_RETROFLEX: Record<string, string> = { i: 'ɻ̩0' };  // after zh ch sh r
const FINAL_AFTER_DENTAL: Record<string, string> = { i: 'z̩0' };     // after z c s

function retone(p: string): string {
  p = p.replace('˧˩˧', '↓').replace('˧˥', '↗').replace('˥˩', '↘').replace('˥', '→');
  p = p.replaceAll('ɻ̩', 'ɨ').replaceAll('ʐ̩', 'ɨ').replaceAll('z̩', 'ɨ');
  return p.replaceAll('\u0329', '').replaceAll('\u032F', '');  // ̩ ̯ leftovers
}

function syllableIpa(base: string, tone: number): string | null {
  const contour = TONE_CONTOUR[tone] ?? '';
  const apply = (s: string) => retone(s.replaceAll('0', contour));

  if (INTERJECTION[base]) return apply(INTERJECTION[base]);
  if (SYLLABIC[base]) return apply(SYLLABIC[base]);

  let initial = '';
  let rest = base;
  for (const ini of INITIALS) {
    if (rest.startsWith(ini)) { initial = ini; rest = rest.slice(ini.length); break; }
  }

  let finalMap = FINAL;
  if ((initial === 'zh' || initial === 'ch' || initial === 'sh' || initial === 'r') && rest === 'i') {
    finalMap = FINAL_AFTER_RETROFLEX;
  } else if ((initial === 'z' || initial === 'c' || initial === 's') && rest === 'i') {
    finalMap = FINAL_AFTER_DENTAL;
  }
  const final = finalMap[rest];
  if (final === undefined) return null;
  return apply((initial ? INITIAL[initial] : '') + final);
}

/** y/w/ü normalization -> pypinyin strict initial+final view. */
function normalize(base: string): string {
  const R: Array<[RegExp, string]> = [
    [/^yu(?=[aeiun])/u, 'ü'], [/^yu$/u, 'ü'],
    [/^yi/u, 'i'], [/^ya/u, 'ia'], [/^ye/u, 'ie'], [/^yao/u, 'iao'],
    [/^you/u, 'iou'], [/^yan/u, 'ian'], [/^yin/u, 'in'], [/^yang/u, 'iang'],
    [/^ying/u, 'ing'], [/^yong/u, 'iong'], [/^yo$/u, 'io'],
    [/^wu$/u, 'u'], [/^wa/u, 'ua'], [/^wo/u, 'uo'], [/^wai/u, 'uai'],
    [/^wei/u, 'uei'], [/^wan/u, 'uan'], [/^wen/u, 'uen'], [/^wang/u, 'uang'],
    [/^weng/u, 'ueng'], [/^wong/u, 'ong'],
  ];
  for (const [re, to] of R) {
    if (re.test(base)) { base = base.replace(re, to); break; }
  }
  if (/^[jqx]/u.test(base)) base = base.replace(/^([jqx])u/, '$1ü');
  // pypinyin strict finals: ui->uei, iu->iou, un->uen (jqx already became ü)
  base = base.replace(/ui$/u, 'uei').replace(/iu$/u, 'iou');
  if (/un$/u.test(base) && !base.includes('ü')) base = base.replace(/un$/u, 'uen');
  return base;
}

/** A Latin word, possibly with digits, apostrophes and inner dots/hyphens ("thirdweb.com", "e-mail", "Web3"). */
const LATIN = "[A-Za-z][A-Za-z0-9'’]*(?:[.\\-][A-Za-z0-9'’]+)*";
const SPLIT_RE = new RegExp(`([\\u4E00-\\u9FFF]+|${LATIN})`, 'u');
const IS_LATIN_RE = new RegExp(`^${LATIN}$`, 'u');
const HANZI_RE = /[\u4E00-\u9FFF]/u;
const LATIN_SPLIT = new RegExp(`(${LATIN})`, 'u');

// ---------------------------------------------------------------------------
// Text normalization: what a Chinese TTS front-end has to do BEFORE pinyin.
//
// The old port converted every digit to its hanzi ("100" -> 一零零, "35.5%" ->
// 三五.五%) and let Latin letters straight through into the phoneme string —
// the Kokoro vocabulary has no capital T, so "Thirdweb" came out as garbage.
// Both are normal in translated tech text, so both are handled here.
// ---------------------------------------------------------------------------

const DIGIT_ZH = '零一二三四五六七八九';
const UNITS = ['', '十', '百', '千'];

/** 0..9999 -> hanzi, zeros collapsed ("1002" -> 一千零二). Leading "一十" is fixed by the caller. */
function group4(n: number): string {
  const digits = String(n).padStart(4, '0');
  let out = '';
  let pendingZero = false;
  for (let i = 0; i < 4; i++) {
    const d = Number(digits[i]);
    if (d === 0) { if (out) pendingZero = true; continue; }
    if (pendingZero) { out += '零'; pendingZero = false; }
    out += DIGIT_ZH[d] + UNITS[3 - i];
  }
  return out;
}

/** Cardinal number (0 <= n < 1e12) the way it is spoken: 200 -> 两百, 12000 -> 一万两千, 10 -> 十. */
export function cardinalZh(n: number): string {
  if (n === 0) return '零';
  const yi = Math.floor(n / 1e8);
  const wan = Math.floor(n / 1e4) % 1e4;
  const ge = n % 1e4;
  let out = '';
  if (yi) out += group4(yi) + '亿';
  if (wan) { if (out && wan < 1000) out += '零'; out += group4(wan) + '万'; }
  if (ge) { if (out && ge < 1000) out += '零'; out += group4(ge); }
  if (out.startsWith('一十')) out = out.slice(1);                  // 一十二 -> 十二
  return out.replace(/二(?=[百千万亿])/gu, '两');                   // 二百 -> 两百, 一万二千 -> 一万两千
}

const digitsZh = (s: string) => [...s].map((d) => DIGIT_ZH[Number(d)] ?? d).join('');

/** An integer string: spoken as a cardinal, or digit by digit when that is how people say it. */
function integerZh(s: string): string {
  const spellOut = (s.length > 1 && s.startsWith('0'))               // 007, 0123
    || s.length > 12                                                  // beyond 万亿
    || (s.length === 11 && /^1[3-9]/.test(s));                        // phone numbers
  return spellOut ? digitsZh(s) : cardinalZh(Number(s));
}

function decimalZh(s: string): string {
  const [int, frac] = s.split('.');
  return integerZh(int) + '点' + digitsZh(frac);
}

const numberZh = (s: string) => (s.includes('.') ? decimalZh(s) : integerZh(s));
const MONEY: Record<string, string> = { $: '美元', '＄': '美元', '£': '英镑', '€': '欧元', '¥': '元', '￥': '元' };
// A lone 2 before these reads 两 (两个, 两年), not 二个.
const TWO_BEFORE = '个只条位名件天年次种家本台块倍人元';

function normalizeSegment(segment: string): string {
  return segment
    .replace(/(?<=\d),(?=\d{3}(?!\d))/g, '')                          // 1,000 -> 1000
    .replace(/(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)/g, (_, h: string, m: string) =>
      `${integerZh(String(Number(h)))}点${Number(m) === 0 ? '' : (Number(m) < 10 ? '零' : '') + integerZh(String(Number(m))) + '分'}`)
    .replace(/([$＄£€¥￥])\s?(\d+(?:\.\d+)?)/gu, (_, sym: string, n: string) => numberZh(n) + MONEY[sym])
    .replace(/(\d+(?:\.\d+)?)\s?[%％]/gu, (_, n: string) => '百分之' + numberZh(n))
    .replace(/(?<!\d)(1[89]\d\d|20\d\d)\s?[-–~～]\s?(1[89]\d\d|20\d\d)(?!\d)/gu,
      (_, a: string, b: string) => `${digitsZh(a)}到${digitsZh(b)}`)       // 2020-2024 年份区间逐位读
    .replace(/(?<!\d)(\d{4})(?=\s?年)/gu, (_, y: string) => digitsZh(y))
    .replace(/(?<=\d)\s?[-–~～]\s?(?=\d)/gu, '到')                     // 3-5
    .replace(/\d+\.\d+/g, (n) => decimalZh(n))
    .replace(/\d+/g, (n, offset: number, whole: string) =>
      n === '2' && TWO_BEFORE.includes(whole[offset + 1] ?? '') ? '两' : integerZh(n));
}

/**
 * Numbers, percentages, money, times and years -> hanzi.
 *
 * Latin words are cut out first and left untouched: the digits inside "Web3",
 * "v2.0" or "H264" belong to the word (the English phonemizer reads them), and
 * "v2.0" must not turn into "v二点零".
 */
export function normalizeZh(input: string): string {
  const widened = input.replace(/[０-９Ａ-Ｚａ-ｚ]/gu, (c) => String.fromCharCode(c.charCodeAt(0) - 0xFEE0));
  return widened.split(LATIN_SPLIT).map((part, i) => (i % 2 === 1 ? part : normalizeSegment(part))).join('');
}

// ---------------------------------------------------------------------------
// Tokenizing a normalized string: hanzi runs / Latin runs / everything else
// ---------------------------------------------------------------------------


const PUNCT_MAP: Array<[string, string]> = [
  ['、', ', '], ['，', ', '], ['。', '. '], ['．', '. '], ['！', '! '], ['：', ': '],
  ['；', '; '], ['？', '? '], ['«', ' “'], ['»', '” '], ['《', ' “'], ['》', '” '],
  ['「', ' “'], ['」', '” '], ['【', ' “'], ['】', '” '], ['（', ' ('], ['）', ') '],
  ['……', '…'],
];

function prepare(input: string): string {
  let text = normalizeZh(input);
  for (const [from, to] of PUNCT_MAP) text = text.replaceAll(from, to);
  return text.trim();
}

/** The distinct Latin runs `zhG2P` will ask the English phonemizer about. */
export function latinRunsZh(input: string): string[] {
  const out = new Set<string>();
  for (const part of prepare(input).split(SPLIT_RE)) if (part && IS_LATIN_RE.test(part)) out.add(part);
  return [...out];
}

export interface ZhG2PResult {
  phonemes: string;
  /** Pieces that could not be phonemized (unmapped pinyin, or Latin with no phonemizer given). */
  skipped: Array<{ char: string; pinyin: string }>;
}

const segmenter = new Intl.Segmenter('zh', { granularity: 'word' });

/**
 * Chinese text -> Kokoro phonemes (misaki zh format).
 *
 * @param latin  phonemes for a Latin run (see `latinRunsZh`). Without it Latin
 *               words are skipped rather than passed through as raw letters.
 */
export function zhG2P(input: string, latin?: (run: string) => string | undefined): ZhG2PResult {
  const text = prepare(input);
  const skipped: Array<{ char: string; pinyin: string }> = [];
  let phonemes = '';

  for (const part of text.split(SPLIT_RE)) {
    if (!part) continue;

    if (IS_LATIN_RE.test(part)) {
      const ipa = latin?.(part);
      if (ipa) phonemes += ` ${ipa} `;
      else skipped.push({ char: part, pinyin: '' });
      continue;
    }
    if (!HANZI_RE.test(part)) { phonemes += part; continue; }        // punctuation, spaces

    phonemes += ' ';
    // The whole run in ONE call so 了/地/还 are disambiguated by sentence context.
    const items = pinyin(part, { type: 'all', toneType: 'num' });
    // Word boundaries by POSITION. (Keying a map by the character itself broke
    // whenever one character sat in two adjacent words: 学生生活 -> 学 | 生生活.)
    const wordOf: number[] = [];
    let wid = 0;
    for (const w of segmenter.segment(part)) {
      for (let k = 0; k < w.segment.length; k++) wordOf[w.index + k] = wid;
      wid++;
    }
    let last = -1;
    items.forEach((it, idx) => {
      const m = /^([^0-5]*)([0-5])?$/u.exec(it.pinyin);
      if (!m || !m[1]) return;
      const w = wordOf[idx];
      if (last !== -1 && w !== last) phonemes += ' ';
      last = w;
      const base = normalize(m[1]);
      const tone = m[2] ? (m[2] === '0' ? 5 : Number(m[2])) : 5;
      const ipa = syllableIpa(base, tone);
      if (ipa === null) { skipped.push({ char: it.origin, pinyin: it.pinyin }); return; }
      phonemes += ipa;
    });
    phonemes += ' ';
  }
  return { phonemes: phonemes.replace(/\s+/gu, ' ').trim(), skipped };
}

/** zhG2P with an async Latin phonemizer (the worker's English espeak is async). */
export async function zhG2PWith(
  input: string,
  latinAsync: (run: string) => Promise<string>,
): Promise<ZhG2PResult> {
  const known = new Map<string, string>();
  for (const run of latinRunsZh(input)) known.set(run, await latinAsync(run));
  return zhG2P(input, (run) => known.get(run));
}
