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

const DIGIT_ZH: Record<string, string> = { 0: '零', 1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六', 7: '七', 8: '八', 9: '九' };
const PUNCT_MAP: Array<[string, string]> = [
  ['、', ', '], ['，', ', '], ['。', '. '], ['．', '. '], ['！', '! '], ['：', ': '],
  ['；', '; '], ['？', '? '], ['«', ' “'], ['»', '” '], ['《', ' “'], ['》', '” '],
  ['「', ' “'], ['」', '” '], ['【', ' “'], ['】', '” '], ['（', ' ('], ['）', ') '],
];

const segmenter = new Intl.Segmenter('zh', { granularity: 'word' });

export interface ZhG2PResult {
  phonemes: string;
  /** Chars whose pinyin didn't map to the IPA tables (should be rare). */
  skipped: Array<{ char: string; pinyin: string }>;
}

/** Chinese text -> Kokoro phonemes (misaki zh format). */
export function zhG2P(input: string): ZhG2PResult {
  let text = input.replace(/\d/gu, (d) => DIGIT_ZH[d]);
  for (const [from, to] of PUNCT_MAP) text = text.replaceAll(from, to);
  text = text.replaceAll('……', '…').trim();
  const skipped: Array<{ char: string; pinyin: string }> = [];
  let phonemes = '';
  let inZhRun = false;

  // Contiguous hanzi runs get ONE pinyin-pro call each (sentence context for
  // 了/地/还 polyphones); Intl.Segmenter only decides word spacing.
  for (const part of text.split(/([\u4E00-\u9FFF]+)/u)) {
    if (!part) continue;
    if (!/[\u4E00-\u9FFF]/u.test(part)) {
      phonemes += part;
      inZhRun = false;
      continue;
    }
    if (phonemes && !inZhRun && !/\s$/u.test(phonemes)) phonemes += ' ';
    inZhRun = true;

    // Whole run in ONE call: 了/地/还 disambiguation uses sentence context.
    // 一/不 sandhi stays on (natural speech; misaki's dict was inconsistent
    // there anyway — pypinyin's dictionary-driven sandhi is inconsistent).
    const items = pinyin(part, { type: 'all', toneType: 'num' });
    const wordIdOf = new Map<string, number>();
    let wid = 0;
    for (const w of segmenter.segment(part)) {
      for (const ch of w.segment) wordIdOf.set(ch, wid);
      wid++;
    }
    let lastWord = -1;
    for (const it of items) {
      const m = /^([^0-5]*)([0-5])?$/u.exec(it.pinyin);
      if (!m || !m[1]) continue;
      const w = wordIdOf.get(it.origin);
      if (w !== undefined && w !== lastWord && lastWord !== -1) phonemes += ' ';
      lastWord = w ?? -2;
      const base = normalize(m[1]);
      const tone = m[2] ? (m[2] === '0' ? 5 : Number(m[2])) : 5;
      const ipa = syllableIpa(base, tone);
      if (ipa === null) { skipped.push({ char: it.origin, pinyin: it.pinyin }); continue; }
      phonemes += ipa;
    }
    phonemes += ' ';
  }
  return { phonemes: phonemes.replace(/\s+/gu, ' ').trim(), skipped };
}
