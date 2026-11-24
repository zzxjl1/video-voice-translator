/**
 * Text handling for Kokoro that has nothing to do with the model: sentence
 * splitting and long-sentence bisection. Pure functions, no browser APIs, so
 * they run (and are tested) directly in Node.
 *
 * Why this exists instead of `text.split(/[.!?]/)`: a period is not always a
 * sentence end. Cutting at every one turned "thirdweb.com" into "thirdweb." +
 * "com", "3.5" into "3." + "5", "Dr. Smith" into "Dr." + "Smith", and
 * "Wait..." into four fragments — each synthesized separately with a 0.15 s gap
 * pasted between them, i.e. audible stutter in the middle of a word.
 */

/** Characters that can end a sentence. */
const TERMINATORS = new Set('.!?…。！？；;'.split(''));
/** Closing quotes / brackets that belong to the sentence they close. */
const CLOSERS = new Set('"\'”’）)】」』》〉]'.split(''));

/**
 * Abbreviations whose trailing period does NOT end a sentence ("Dr. Smith").
 * One merged list for en/fr/it/pt: they share most of these, and a wrong guess
 * costs a missed pause, not a broken word.
 */
const ABBREVIATIONS = new Set([
  'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'sra', 'srta', 'jr', 'sgt', 'col', 'gen', 'rep', 'sen', 'gov',
  'lt', 'maj', 'capt', 'st', 'mt', 'co', 'inc', 'ltd', 'corp', 'dept', 'vs', 'no', 'nº', 'fig', 'approx',
  'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'sept', 'oct', 'nov', 'dec',
  'mon', 'tue', 'tues', 'wed', 'thu', 'thur', 'thurs', 'fri', 'sat', 'sun',
  'm', 'mme', 'mlle', 'pr', 'cf', 'sig', 'sigra', 'dott', 'ing', 'avv', 'dra', 'exmo', 'ilmo',
]);

const isUpper = (c: string | undefined) => !!c && /\p{Lu}/u.test(c);
const isLower = (c: string | undefined) => !!c && /\p{Ll}/u.test(c);
const hasContent = (s: string) => /[\p{L}\p{N}]/u.test(s);

/** Longest piece (in characters) one synthesis call should get, per language. */
export function maxSentenceChars(lang: string): number {
  // A Chinese character becomes ~3-4 phoneme tokens, Latin text ~1 token/char,
  // and the model takes at most 511 tokens.
  return lang === 'zh' ? 70 : 250;
}

function isBoundary(src: string, at: number, run: string, after: number, lang: string): boolean {
  const next = src[after];
  if (next === undefined) return true;
  const strong = /[!?！？。；;]/.test(run);

  if (strong) {
    // Full-width terminators always end a sentence. ASCII "!?;" glued to a
    // following letter or digit ("Yahoo!com", "a?b") does not.
    if (/[！？。；]/.test(run)) return true;
    return !(after === at + run.length && /[\p{L}\p{N}]/u.test(next));
  }

  // Remaining runs are made of "." and/or "…".
  const rest = src.slice(after);
  const nextVisible = rest.match(/\S/u)?.[0];
  const spaced = /\s/u.test(next);

  if (run.includes('…') || run.length >= 3) {
    if (lang === 'zh') return true;
    // "Wait... what" continues; "Wait... What happened" starts a new sentence.
    return spaced && (isUpper(nextVisible) || /\p{Lo}/u.test(nextVisible ?? ''));
  }

  // A single period.
  if (!spaced) return false;                       // 3.5, thirdweb.com, v2.0, a.m.x
  const word = src.slice(0, at).match(/[\p{L}\p{N}.]+$/u)?.[0] ?? '';
  const lower = word.toLowerCase();
  if (lower === 'e.g' || lower === 'i.e') return false;   // always followed by the example / restatement
  if (word.includes('.')) {                        // "U.S", "a.m": ends a sentence only before a capital
    return isUpper(nextVisible);
  }
  if (lower === 'etc') return isUpper(nextVisible);
  if (ABBREVIATIONS.has(lower)) return false;      // Dr. Smith
  if (/^\p{Lu}$/u.test(word)) return false;        // initials: J. Smith
  if (isLower(nextVisible)) return false;          // "approx. five", "etc. and" — mid-sentence
  return true;
}

/** Split `text` at the nearest clause/space boundary around its middle. */
export function bisect(text: string): [string, string] {
  const mid = Math.floor(text.length / 2);
  const find = (re: RegExp): number => {
    let best = -1;
    for (let i = 0; i < text.length; i++) {
      if (re.test(text[i]) && (best === -1 || Math.abs(i - mid) < Math.abs(best - mid))) best = i;
    }
    return best;
  };
  let at = find(/[,，、:：—–]/u);
  if (at !== -1 && Math.abs(at - mid) > text.length * 0.35) at = -1;   // too lopsided: look for a space
  if (at === -1) at = find(/\s/u);
  if (at === -1 || Math.abs(at - mid) > text.length * 0.35) return [text.slice(0, mid), text.slice(mid)];
  return [text.slice(0, at + 1), text.slice(at + 1)];
}

function chunkLong(sentence: string, max: number): string[] {
  const s = sentence.trim();
  if (s.length <= max) return [s];
  const [a, b] = bisect(s);
  return [...chunkLong(a, max), ...chunkLong(b, max)].filter((x) => x.trim());
}

/**
 * Split text into sentences for synthesis. Terminators stay attached to their
 * sentence; sentences longer than `maxSentenceChars` are cut at the clause
 * boundary nearest the middle (a hard cut only when there is none).
 */
export function splitSentences(text: string, lang = 'en'): string[] {
  const src = text.replace(/\r\n?/g, '\n');
  const raw: string[] = [];
  let start = 0;
  let i = 0;
  const push = (end: number) => {
    const piece = src.slice(start, end).trim();
    start = end;
    if (!piece) return;
    // A piece with nothing to say ("." / "”" left over after an ellipsis) belongs to the previous one.
    if (!hasContent(piece)) { if (raw.length) raw[raw.length - 1] += piece; return; }
    raw.push(piece);
  };

  while (i < src.length) {
    const ch = src[i];
    if (ch === '\n') { push(i); start = i + 1; i += 1; continue; }
    if (!TERMINATORS.has(ch)) { i += 1; continue; }
    let j = i;
    while (j < src.length && TERMINATORS.has(src[j])) j += 1;
    const run = src.slice(i, j);
    let k = j;
    while (k < src.length && CLOSERS.has(src[k])) k += 1;
    if (isBoundary(src, i, run, k, lang)) { push(k); i = k; } else { i = j; }
  }
  push(src.length);

  const max = maxSentenceChars(lang);
  const out = raw.flatMap((s) => chunkLong(s, max));
  return out.length ? out : [text.trim()].filter(Boolean);
}
