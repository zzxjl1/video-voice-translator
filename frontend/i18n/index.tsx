/**
 * UI localization for every target language the app offers.
 *
 * Chinese source strings are the keys. Each locale catalog maps Chinese -> that
 * language; `ZH` also maps hardcoded English chrome back to Chinese so the
 * Chinese UI does not leak English labels. Locale selection uses the same
 * browser-language detection as the target-language selector: first browser
 * preference, anything unsupported falls back to English.
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { detectBrowserLanguage } from '../utils/languages';
import en from './en.json';
import ja from './ja.json';
import ko from './ko.json';
import fr from './fr.json';
import de from './de.json';
import pt from './pt.json';
import it from './it.json';
import vi from './vi.json';
import id from './id.json';
import dynamic from './dynamic.json';
import backend from './backend.json';

export type Locale = 'en' | 'zh' | 'ja' | 'ko' | 'fr' | 'de' | 'pt' | 'it' | 'vi' | 'id';

interface LocaleMeta {
  code: Locale;
  target: string;       // value used by the backend / target-language selector
  native: string;       // shown in the language switcher
  lang: string;         // <html lang>
}

export const LOCALES: LocaleMeta[] = [
  { code: 'en', target: 'English', native: 'English', lang: 'en' },
  { code: 'zh', target: 'Chinese', native: '中文', lang: 'zh-CN' },
  { code: 'ja', target: 'Japanese', native: '日本語', lang: 'ja' },
  { code: 'ko', target: 'Korean', native: '한국어', lang: 'ko' },
  { code: 'fr', target: 'French', native: 'Français', lang: 'fr' },
  { code: 'de', target: 'German', native: 'Deutsch', lang: 'de' },
  { code: 'pt', target: 'Portuguese', native: 'Português', lang: 'pt' },
  { code: 'it', target: 'Italian', native: 'Italiano', lang: 'it' },
  { code: 'vi', target: 'Vietnamese', native: 'Tiếng Việt', lang: 'vi' },
  { code: 'id', target: 'Indonesian', native: 'Bahasa Indonesia', lang: 'id' },
];

const TARGET_TO_LOCALE: Record<string, Locale> = Object.fromEntries(
  LOCALES.map((l) => [l.target, l.code]),
);

const LOCALE_KEY = 'vvt.locale';

function readStoredLocale(): Locale | null {
  try {
    const value = localStorage.getItem(LOCALE_KEY);
    return LOCALES.some((l) => l.code === value) ? (value as Locale) : null;
  } catch {
    return null;
  }
}

function browserLocale(): Locale {
  if (typeof navigator === 'undefined') return 'en';
  // Same rule as the target-language selector: first browser preference that
  // matches a supported language; unsupported languages fall back to English.
  return TARGET_TO_LOCALE[detectBrowserLanguage()] ?? 'en';
}

export const locale: Locale = readStoredLocale() ?? browserLocale();
const localeMeta = LOCALES.find((l) => l.code === locale) ?? LOCALES[0];

if (typeof document !== 'undefined') {
  document.documentElement.lang = localeMeta.lang;
}

export type Vars = Record<string, string | number> | Array<string | number>;

function interpolate(text: string, vars?: Vars): string {
  if (!vars) return text;
  if (Array.isArray(vars)) {
    return text.replace(/\{(\d+)\}/g, (_, i: string) => String(vars[Number(i)] ?? `{${i}}`));
  }
  return text.replace(/\{(\w+)\}/g, (_, key: string) => String(vars[key] ?? `{${key}}`));
}

/** Translate one source string. Unknown entries fall back to the source. */
export function t(source: string, vars?: Vars): string {
  return interpolate(translateCore(source), vars);
}

const BACKEND = backend as Record<string, Record<string, string>>;

interface BackendPattern {
  re: RegExp;
  key: string;
}

const BACKEND_PATTERNS: BackendPattern[] = [
  {
    re: /没有足够的\s*([\d.]+)~([\d.]+)\s*秒中等长度句（只有\s*(\d+)\s*句）/,
    key: '没有足够的 {0}~{1} 秒中等长度句（只有 {2} 句）',
  },
  {
    re: /说话人\s*(.+?)\s*没有可用于克隆的素材/,
    key: '说话人 {0} 没有可用于克隆的素材',
  },
  {
    re: /预设\s*['"]?(.+?)['"]?\s*不是规则型预设/,
    key: '预设 {0} 不是规则型预设',
  },
];

/**
 * Backend validation / preset reasons are authored in Chinese. Translate the
 * known ones at the display boundary; unknown messages degrade to their source.
 */
export function tBackend(text: string): string {
  const table = BACKEND[locale];
  if (!table) return t(text);
  if (table[text]) return table[text];
  for (const { re, key } of BACKEND_PATTERNS) {
    const m = text.match(re);
    if (m) {
      const template = table[key];
      if (template) return interpolate(template, m.slice(1));
    }
  }
  return t(text);
}

/** Persist a manual choice and reload so constant labels are re-evaluated. */
export function setLocale(next: Locale): void {
  if (next === locale) return;
  try {
    localStorage.setItem(LOCALE_KEY, next);
  } catch {
    // Storage unavailable: still reload; the choice just will not survive.
  }
  window.location.reload();
}

/**
 * Language switcher: a globe icon that opens a custom dropdown. A native
 * <select> was hard to style and, in the editor, competed with the target
 * language selector.
 */
export const LanguageSwitch: React.FC<{ className?: string }> = ({ className = '' }) => {
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const current = LOCALES.find((l) => l.code === locale) ?? LOCALES[0];

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!boxRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  return (
    <div className={className}>
      <div ref={boxRef} data-i18n-skip className="relative">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-label="Language"
          title={`Language: ${current.native}`}
          className={`flex h-8 w-8 items-center justify-center rounded-full border border-[#e5e5e0] bg-white/90 text-gray-500 shadow-sm transition-colors hover:border-claude-accent/40 hover:text-claude-accent ${open ? 'border-claude-accent/50 text-claude-accent' : ''}`}
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
            <circle cx="12" cy="12" r="9" />
            <path d="M3 12h18M12 3c2.5 2.6 3.8 5.7 3.8 9S14.5 18.4 12 21c-2.5-2.6-3.8-5.7-3.8-9S9.5 5.6 12 3Z" />
          </svg>
        </button>

        {open && (
          <div
            role="listbox"
            aria-label="Language"
            className="absolute right-0 top-full z-[100] mt-2 max-h-80 w-44 overflow-y-auto rounded-2xl border border-[#e5e5e0] bg-white p-1.5 shadow-xl shadow-black/10"
          >
            {LOCALES.map((l) => (
              <button
                key={l.code}
                type="button"
                role="option"
                aria-selected={l.code === locale}
                onClick={() => { setOpen(false); setLocale(l.code); }}
                className={`flex w-full items-center justify-between rounded-xl px-3 py-2 text-left text-[12px] font-bold transition-colors ${
                  l.code === locale
                    ? 'bg-claude-accent/10 text-claude-accent'
                    : 'text-gray-600 hover:bg-claude-paper hover:text-claude-text'
                }`}
              >
                <span>{l.native}</span>
                {l.code === locale && (
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="m5 13 4 4L19 7" />
                  </svg>
                )}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Runtime text localization
// ---------------------------------------------------------------------------
//
// The app was written with Chinese UI copy inline. Rather than rewrite hundreds
// of JSX nodes (and risk touching user content), the active locale catalog is
// applied to rendered text nodes and a small set of display attributes. User
// content (transcript, translations, chat messages, filenames) is explicitly
// marked `data-i18n-skip` by its owner.
//
// This is deliberately a presentation layer: it never changes form values or
// API payloads, only what is painted.

const EN = en as Record<string, string>;

const CATALOGS: Record<Exclude<Locale, 'zh'>, Record<string, string>> = {
  en,
  ja: ja as Record<string, string>,
  ko: ko as Record<string, string>,
  fr: fr as Record<string, string>,
  de: de as Record<string, string>,
  pt: pt as Record<string, string>,
  it: it as Record<string, string>,
  vi: vi as Record<string, string>,
  id: id as Record<string, string>,
};

/**
 * A few labels were hardcoded in English and have no Chinese key in en.json.
 * They still need a target-language value in each catalog.
 */
const EXTRA: Record<Exclude<Locale, 'zh'>, Record<string, string>> = {
  en: {
    '时间轴': 'Timeline', '显示工具': 'Show tools', '补齐缺口': 'Fill gaps',
    '运行中': 'Running', 'AI驱动的视频智能翻译': 'AI-powered intelligent video translation',
  },
  ja: {
    '时间轴': 'タイムライン', '显示工具': 'ツールを表示', '补齐缺口': '不足を補う',
    '运行中': '実行中', 'AI驱动的视频智能翻译': 'AIによる動画スマート翻訳',
  },
  ko: {
    '时间轴': '타임라인', '显示工具': '도구 표시', '补齐缺口': '부족한 부분 채우기',
    '运行中': '실행 중', 'AI驱动的视频智能翻译': 'AI 기반 영상 지능형 번역',
  },
  fr: {
    '时间轴': 'Chronologie', '显示工具': 'Afficher les outils', '补齐缺口': 'Combler les lacunes',
    '运行中': 'En cours', 'AI驱动的视频智能翻译': 'Traduction vidéo intelligente par IA',
  },
  de: {
    '时间轴': 'Zeitachse', '显示工具': 'Werkzeuge anzeigen', '补齐缺口': 'Lücken füllen',
    '运行中': 'Läuft', 'AI驱动的视频智能翻译': 'KI-gestützte intelligente Videotranslation',
  },
  pt: {
    '时间轴': 'Linha do tempo', '显示工具': 'Mostrar ferramentas', '补齐缺口': 'Preencher lacunas',
    '运行中': 'Em execução', 'AI驱动的视频智能翻译': 'Tradução inteligente de vídeo com IA',
  },
  it: {
    '时间轴': 'Timeline', '显示工具': 'Mostra strumenti', '补齐缺口': 'Colma le lacune',
    '运行中': 'In esecuzione', 'AI驱动的视频智能翻译': 'Traduzione video intelligente con AI',
  },
  vi: {
    '时间轴': 'Dòng thời gian', '显示工具': 'Hiện công cụ', '补齐缺口': 'Bù phần thiếu',
    '运行中': 'Đang chạy', 'AI驱动的视频智能翻译': 'Dịch video thông minh bằng AI',
  },
  id: {
    '时间轴': 'Linimasa', '显示工具': 'Tampilkan alat', '补齐缺口': 'Isi kekosongan',
    '运行中': 'Berjalan', 'AI驱动的视频智能翻译': 'Terjemahan video cerdas dengan AI',
  },
};

function lookup(table: Record<string, string>, loc: Locale, key: string): string | undefined {
  return table[key] ?? (loc === 'zh' ? undefined : EXTRA[loc]?.[key]);
}

// The app also contains labels that were hardcoded in English (section
// headings, top-bar chrome). Build a reverse catalog from en.json and add the
// handful of source-English labels that have no Chinese source key.
const ZH: Record<string, string> = {};
for (const [zh, english] of Object.entries(EN)) {
  if (english && !(english in ZH)) ZH[english] = zh;
}
Object.assign(ZH, {
  'TARGET LANG:': '目标语言',
  'TARGET LANG': '目标语言',
  'Target Lang:': '目标语言',
  'Target Lang': '目标语言',
  'Target Language': '目标语言',
  'TARGET LANGUAGE': '目标语言',
  'SCRIPT & TRANSLATION': '脚本 & 译文',
  'Script & Translation': '脚本 & 译文',
  'ORIGINAL': '原文',
  'Original': '原文',
  'TRANSLATION': '译文',
  'Translation': '译文',
  'EDIT': '编辑',
  'Edit': '编辑',
  'TIMELINE': '时间轴',
  'Timeline': '时间轴',
  'Show Tools': '显示工具',
  'SHOW TOOLS': '显示工具',
  'Fill gaps': '补齐缺口',
  'FILL GAPS': '补齐缺口',
  'Running': '运行中',
  'Export': '导出',
  'Export video': '导出成片',
  'VOICES': '音色',
  'Voice': '音色',
  'Preview line': '试听句',
  'AI-Powered Translation': 'AI驱动的视频智能翻译',
  'AI-POWERED TRANSLATION': 'AI驱动的视频智能翻译',
  'RECENT PROJECTS': '最近工程',
  'This browser only': '仅本浏览器',
  'No projects yet': '还没有工程',
  'LANGUAGES YOU UNDERSTAND': '能听懂的语言',
  'HOW TO USE': '怎么用',
  "WHEN DONE, YOU'LL GET": '处理完你会得到',
  'Upload video file': '上传视频文件',
});

function catalog(loc: Locale): Record<string, string> {
  return loc === 'zh' ? ZH : (CATALOGS[loc] ?? EN);
}

const DYNAMIC = dynamic as Record<string, Record<string, string>>;

interface DynamicRule {
  re: RegExp;
  key: string;
  groups: number;
}

// The Chinese source -> English template keys here match dynamic.json. Each
// rule rewrites a count/unit phrase into the active language.
const DYNAMIC_RULES: DynamicRule[] = [
  { re: /取自第\s*([\d、,，\s]+)\s*句/g, key: 'From lines {0}', groups: 1 },
  { re: /删除没在用的\s*(\d+)\s*个音色/g, key: 'Delete {0} unused voices', groups: 1 },
  { re: /删掉没在用的\s*(\d+)\s*个音色/g, key: 'Delete {0} unused voices', groups: 1 },
  { re: /(\d+)\s*种\s*·\s*自动识别/g, key: '{0} languages · auto-detect', groups: 1 },
  { re: /变速\s*([\d.]+)x/g, key: 'Speed {0}x', groups: 1 },
  { re: /视频\s*([\d.]+)x/g, key: 'Video {0}x', groups: 1 },
  { re: /配音\s*([\d.]+)s\s*\/\s*时间槽\s*([\d.]+)s/g, key: 'Dubbing {0}s / slot {1}s', groups: 2 },
  { re: /(\d+)\s*句译文/g, key: '{0} translations', groups: 1 },
  { re: /(\d+)\s*句配音/g, key: '{0} dubbing lines', groups: 1 },
  { re: /·\s*偏长/g, key: '· Too long', groups: 0 },
  { re: /·\s*偏短/g, key: '· Too short', groups: 0 },
  { re: /补齐/g, key: 'Fill', groups: 0 },
  { re: /撤销：/g, key: 'Undo: ', groups: 0 },
  { re: /约\s*([\d.]+)\s*元/g, key: '~¥{0}', groups: 1 },
  { re: /(\d+(?:\.\d+)?)\s*秒/g, key: '{0}s', groups: 1 },
  { re: /(\d+)\s*分钟前/g, key: '{0} min ago', groups: 1 },
  { re: /(\d+)\s*小时前/g, key: '{0} h ago', groups: 1 },
  { re: /(\d+)\s*天前/g, key: '{0} d ago', groups: 1 },
  { re: /第\s*([\d、,，\s]+)\s*句/g, key: 'Lines {0}', groups: 1 },
  { re: /(\d+)\s*句/g, key: '{0} lines', groups: 1 },
];

function applyDynamic(text: string, loc: Locale): string {
  let out = text;
  for (const rule of DYNAMIC_RULES) {
    out = out.replace(rule.re, (...args: unknown[]) => {
      const values = args.slice(1, 1 + rule.groups) as Array<string | number>;
      const template = DYNAMIC[loc]?.[rule.key] ?? rule.key;
      return interpolate(template, values);
    });
  }
  // Line lists use the Chinese enumeration comma. Keep it for Japanese; other
  // languages use a normal comma.
  if (loc !== 'zh') out = out.replace(/[、，]/g, loc === 'ja' ? '、' : ', ');
  return out;
}

// React renders `{count} 句` as two text nodes. The number is live state, so we
// must not replace the parent's textContent (that would detach React's text
// node and freeze the count). Translate the unit fragment alone instead.
const FRAGMENT_KEYS: Record<string, string> = {
  '句': '{0} lines',
  '句译文': '{0} translations',
  '句配音': '{0} dubbing lines',
  '种 · 自动识别': '{0} languages · auto-detect',
  '分钟前': '{0} min ago',
  '小时前': '{0} h ago',
  '天前': '{0} d ago',
};

function translateFragment(body: string, loc: Locale): string | undefined {
  const key = FRAGMENT_KEYS[body];
  if (!key) return undefined;
  const template = DYNAMIC[loc]?.[key];
  if (!template) return undefined;
  return template.replace(/\{0\}/g, '').replace(/\s+/g, ' ').trim();
}

const ATTRS = ['title', 'placeholder', 'aria-label', 'alt'] as const;
const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'PRE', 'CODE']);

function hasChinese(value: string): boolean {
  return /[\u4e00-\u9fff]/.test(value);
}

function needsTranslation(value: string): boolean {
  if (locale === 'en') return hasChinese(value);
  if (locale === 'zh') return /[A-Za-z]/.test(value);
  return hasChinese(value) || /[A-Za-z]/.test(value);
}

const SPEAKER_WORDS: Record<Locale, string> = {
  en: 'Speaker', zh: '说话人', ja: 'スピーカー', ko: '스피커',
  fr: 'Locuteur', de: 'Sprecher', pt: 'Falante', it: 'Parlante',
  vi: 'Người nói', id: 'Pembicara',
};

function translateCore(core: string): string {
  // Some labels are prefixed with a decorative dash; translate the payload and
  // put the decoration back (the catalog keys do not include it).
  const prefix = core.match(/^[—–-]\s+/)?.[0] ?? '';
  const body = prefix ? core.slice(prefix.length) : core;

  if (locale === 'zh') {
    // Chinese is the source language; only hardcoded English chrome needs a
    // reverse lookup, plus the "Speaker N" labels.
    const out = body.replace(/\b(?:Speaker|SPEAKER)\s+(\d+)\b/g, '说话人 $1');
    return prefix + (ZH[out] ?? out);
  }

  const table = catalog(locale);
  const needsSpace = body === '把视频里的话';
  const withSpace = (text: string) => text + (needsSpace ? ' ' : '');

  if (hasChinese(body)) {
    const exact = lookup(table, locale, body);
    if (exact) return prefix + withSpace(exact);
    const fragmented = translateFragment(body, locale);
    if (fragmented) return prefix + withSpace(fragmented);
    const out = applyDynamic(body, locale);
    return prefix + withSpace(lookup(table, locale, out) ?? out);
  }

  // Hardcoded English label: map it to its Chinese source first, then into the
  // active language. Unknown English (brand names, user text) stays as-is.
  const speaker = body.match(/^(?:Speaker|SPEAKER)\s+(\d+)$/);
  if (speaker) return prefix + `${SPEAKER_WORDS[locale]} ${speaker[1]}`;
  const zh = ZH[body];
  const translated = zh ? lookup(table, locale, zh) : undefined;
  return prefix + withSpace(translated ?? body);
}

function skipped(el: Element | null): boolean {
  if (!el) return false;
  if (SKIP_TAGS.has(el.tagName)) return true;
  return !!el.closest('[data-i18n-skip], [contenteditable="true"], [contenteditable=""]');
}

function translateTextNode(node: Text): void {
  const raw = node.nodeValue;
  if (!raw || !needsTranslation(raw)) return;
  if (skipped(node.parentElement)) return;
  const lead = raw.match(/^\s*/)?.[0] ?? '';
  const trail = raw.match(/\s*$/)?.[0] ?? '';
  const core = raw.trim();
  if (!core) return;
  const translated = translateCore(core);
  if (translated !== core) node.nodeValue = lead + translated + trail;
}

function translateAttrs(el: Element): void {
  if (el.closest('[data-i18n-skip]')) return;
  for (const attr of ATTRS) {
    const value = el.getAttribute(attr);
    if (!value || !needsTranslation(value)) continue;
    const translated = translateCore(value.trim());
    if (translated !== value.trim()) el.setAttribute(attr, translated);
  }
}

function translateSubtree(root: Node): void {
  if (root.nodeType === Node.TEXT_NODE) {
    translateTextNode(root as Text);
    return;
  }
  if (!(root instanceof Element)) return;
  translateElement(root);
}

function translateElement(el: Element): void {
  translateAttrs(el);
  if (skipped(el)) return;
  el.childNodes.forEach((child) => {
    if (child.nodeType === Node.TEXT_NODE) translateTextNode(child as Text);
    else if (child instanceof Element) translateElement(child);
  });
}

/** Apply the active catalog to the mounted app and keep it applied during rerenders. */
export const I18nRoot: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  useLayoutEffect(() => {
    if (typeof document === 'undefined') return;
    let queued = false;
    const pending = new Set<Node>();

    const flush = () => {
      queued = false;
      const nodes = [...pending];
      pending.clear();
      for (const node of nodes) translateSubtree(node);
    };
    const queue = (node: Node) => {
      pending.add(node);
      if (!queued) {
        queued = true;
        requestAnimationFrame(flush);
      }
    };

    translateSubtree(document.body);

    const observer = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.type === 'childList') m.addedNodes.forEach(queue);
        else if (m.type === 'characterData') queue(m.target);
        else if (m.type === 'attributes' && m.target instanceof Element) queue(m.target);
      }
    });
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: [...ATTRS],
    });
    return () => {
      observer.disconnect();
    };
  }, []);

  return <>{children}</>;
};
