/**
 * Minimal UI localization.
 *
 * The Chinese source string is the key: `t('上传视频文件')` returns the English
 * copy when the UI locale is English, and the source itself for Chinese. This
 * keeps call sites readable and means a missing English entry degrades to
 * Chinese instead of showing a raw key.
 *
 * Locale selection is intentionally simple: an explicit user choice stored in
 * localStorage wins; otherwise the first browser preference decides
 * Chinese vs. English; everything else falls back to English. Switching locale
 * reloads the page so module-level strings (option labels, constants) are
 * re-evaluated too.
 */
import React, { useLayoutEffect } from 'react';
import en from './en.json';

export type Locale = 'zh' | 'en';

const LOCALE_KEY = 'vvt.locale';

function browserLocale(): Locale {
  if (typeof navigator === 'undefined') return 'en';
  // The UI has two languages. Use the browser's primary preference; anything
  // that is not Chinese falls back to English (the requested default).
  const first = navigator.languages?.[0] ?? navigator.language ?? '';
  const primary = String(first).toLowerCase().split('-')[0];
  return primary === 'zh' ? 'zh' : 'en';
}

function readStoredLocale(): Locale | null {
  try {
    const value = localStorage.getItem(LOCALE_KEY);
    return value === 'zh' || value === 'en' ? value : null;
  } catch {
    return null;
  }
}

export const locale: Locale = readStoredLocale() ?? browserLocale();

if (typeof document !== 'undefined') {
  document.documentElement.lang = locale === 'zh' ? 'zh-CN' : 'en';
}

export type Vars = Record<string, string | number> | Array<string | number>;

function interpolate(text: string, vars?: Vars): string {
  if (!vars) return text;
  if (Array.isArray(vars)) {
    return text.replace(/\{(\d+)\}/g, (_, i: string) => String(vars[Number(i)] ?? `{${i}}`));
  }
  return text.replace(/\{(\w+)\}/g, (_, key: string) => String(vars[key] ?? `{${key}}`));
}

/** Translate one source string. Unknown English entries fall back to Chinese. */
export function t(source: string, vars?: Vars): string {
  const text = locale === 'en'
    ? ((en as Record<string, string>)[source] ?? source)
    : source;
  return interpolate(text, vars);
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

/** Compact two-segment language switch for the top of the app. */
export const LanguageSwitch: React.FC<{ className?: string }> = ({ className = '' }) => (
  <div
    data-i18n-skip
    className={`inline-flex items-center rounded-full border border-[#e5e5e0] bg-white/90 p-0.5 text-[10px] font-bold shadow-sm ${className}`}
  >
    <button
      type="button"
      onClick={() => setLocale('zh')}
      aria-pressed={locale === 'zh'}
      className={`px-2 py-1 rounded-full transition-colors ${locale === 'zh' ? 'bg-claude-accent text-white' : 'text-gray-500 hover:text-claude-accent'}`}
    >
      中文
    </button>
    <button
      type="button"
      onClick={() => setLocale('en')}
      aria-pressed={locale === 'en'}
      className={`px-2 py-1 rounded-full transition-colors ${locale === 'en' ? 'bg-claude-accent text-white' : 'text-gray-500 hover:text-claude-accent'}`}
    >
      EN
    </button>
  </div>
);

// ---------------------------------------------------------------------------
// Runtime text localization
// ---------------------------------------------------------------------------
//
// The app was written with Chinese UI copy inline. Rather than rewrite hundreds
// of JSX nodes (and risk touching user content), the English catalog is applied
// to rendered text nodes and a small set of display attributes. Chinese is the
// source language and stays untouched. User content (transcript, translations,
// chat messages, filenames) is explicitly marked `data-i18n-skip` by its owner.
//
// This is deliberately a presentation layer: it never changes form values or
// API payloads, only what is painted.

const EN = en as Record<string, string>;

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
  return loc === 'en' ? EN : ZH;
}

const PATTERNS: Array<[RegExp, string]> = [
  [/取自第\s*([\d、,，\s]+)\s*句/g, 'From lines $1'],
  [/第\s*([\d、,，\s]+)\s*句/g, 'Lines $1'],
  [/删除没在用的\s*(\d+)\s*个音色/g, 'Delete $1 unused voices'],
  [/删掉没在用的\s*(\d+)\s*个音色/g, 'Delete $1 unused voices'],
  [/(\d+)\s*种\s*·\s*自动识别/g, '$1 languages · auto-detect'],
  [/变速\s*([\d.]+)x/g, 'Speed $1x'],
  [/视频\s*([\d.]+)x/g, 'Video $1x'],
  [/(\d+)\s*句译文/g, '$1 translations'],
  [/(\d+)\s*句配音/g, '$1 dubbing lines'],
  [/补齐/g, 'Fill'],
  [/撤销：/g, 'Undo: '],
  [/配音\s*([\d.]+)s\s*\/\s*时间槽\s*([\d.]+)s/g, 'Dubbing $1s / slot $2s'],
  [/·\s*偏长/g, '· Too long'],
  [/·\s*偏短/g, '· Too short'],
  [/(\d+)\s*句/g, '$1 lines'],
  [/(\d+)\s*秒/g, '$1s'],
  [/(\d+)\s*分钟前/g, '$1 min ago'],
  [/(\d+)\s*小时前/g, '$1 h ago'],
  [/(\d+)\s*天前/g, '$1 d ago'],
  [/约\s*([\d.]+)\s*元/g, '~¥$1'],
];

const ATTRS = ['title', 'placeholder', 'aria-label', 'alt'] as const;
const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'PRE', 'CODE']);

function hasChinese(value: string): boolean {
  return /[\u4e00-\u9fff]/.test(value);
}

function needsTranslation(value: string): boolean {
  return locale === 'en' ? hasChinese(value) : /[A-Za-z]/.test(value);
}

const ZH_PATTERNS: Array<[RegExp, string]> = [
  [/\bSpeaker\s+(\d+)\b/g, '说话人 $1'],
  [/\bSPEAKER\s+(\d+)\b/g, '说话人 $1'],
];

function translateCore(core: string): string {
  // Some labels are prefixed with a decorative dash; translate the payload and
  // put the decoration back (the catalog keys do not include it).
  const prefix = core.match(/^[—–-]\s+/)?.[0] ?? '';
  const body = prefix ? core.slice(prefix.length) : core;
  const exact = catalog(locale)[body];
  if (exact) return prefix + exact;
  let out = body;
  if (locale === 'en') {
    for (const [re, replacement] of PATTERNS) out = out.replace(re, replacement);
  } else {
    for (const [re, replacement] of ZH_PATTERNS) out = out.replace(re, replacement);
  }
  return prefix + out;
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
    const parent = (root as Text).parentElement;
    // If the parent only holds text, translate the whole string: React often
    // splits `{count} 句` into two text nodes, and neither half has a digit.
    if (parent && !parent.querySelector('*') && !skipped(parent)) {
      translateElementText(parent);
    } else {
      translateTextNode(root as Text);
    }
    return;
  }
  if (!(root instanceof Element)) return;
  translateElement(root);
}

function translateElementText(el: Element): void {
  if (skipped(el) || el.querySelector('*')) return;
  const raw = el.textContent ?? '';
  if (!needsTranslation(raw)) return;
  const lead = raw.match(/^\s*/)?.[0] ?? '';
  const trail = raw.match(/\s*$/)?.[0] ?? '';
  const core = raw.trim();
  const translated = translateCore(core);
  if (translated !== core) el.textContent = lead + translated + trail;
}

function translateElement(el: Element): void {
  translateAttrs(el);
  if (skipped(el)) return;
  if (!el.querySelector('*')) {
    translateElementText(el);
    return;
  }
  el.childNodes.forEach((child) => {
    if (child.nodeType === Node.TEXT_NODE) translateTextNode(child as Text);
    else if (child instanceof Element) translateElement(child);
  });
}

/** Apply the English catalog to the mounted app and keep it applied during rerenders. */
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
