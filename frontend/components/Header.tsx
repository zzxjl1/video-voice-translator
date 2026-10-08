import React, { useState, useEffect, useRef } from 'react';
import {
  CHINESE_ACCENTS,
  COMMON_LANGUAGES,
  MORE_LANGUAGES,
  accentLabel,
  hasAccents,
} from '../utils/languages';

interface HeaderProps {
  onOpenSettings: () => void;
  /**
   * 重译 + 重做全部音频（**不**重跑语音识别）。换语言 / 改翻译要求 / 重做配音用它：
   * 不必重听音频，省时间也省钱。
   */
  onReprocess: () => void;
  /**
   * 从头开始：清空已有进度，重跑识别 / 增强 / 翻译 / 配音。
   * 与 `onReprocess` 是两个作用域，所以摆在同一个菜单里而不是塞进一个按钮。
   */
  onRestartFromScratch: () => void;
  targetLanguage: string;
  onLanguageChange: (lang: string) => void;
  /** Chinese dialect for the dub; '' is Mandarin. Ignored for other languages. */
  targetAccent: string;
  onAccentChange: (accent: string) => void;
  isProcessing: boolean;
  hasSegments: boolean;
  /** Opens the export dialog. Same action in every state — see the button. */
  onExport: () => void;
  isExporting: boolean;
  exportError?: string;
}

const Header: React.FC<HeaderProps> = ({
  onOpenSettings,
  onReprocess,
  onRestartFromScratch,
  targetLanguage,
  onLanguageChange,
  targetAccent,
  onAccentChange,
  isProcessing,
  hasSegments,
  onExport,
  isExporting,
  exportError,
}) => {
  const [isVisible, setIsVisible] = useState(true);
  const [isHovered, setIsHovered] = useState(false);
  /** Reprocess 的下拉菜单。两件事（重译+重配音 / 从头开始）作用域不同，见 props。 */
  const [menuOpen, setMenuOpen] = useState(false);
  /**
   * 菜单的视口坐标。
   *
   * 菜单**不能**留在 `<header>` 里：那个元素有 `overflow-hidden`（收起动画要裁住
   * 自己的内容），而菜单必须伸到按钮下方 —— 于是整块被剪掉，表现就是"点开箭头
   * 什么也看不到"。header 上还有 `translate-y` 变换，所以直接改成 `position: fixed`
   * 也不行（变换祖先会成为 fixed 的参照物，照样被裁）。
   *
   * 所以菜单渲染在 `<header>` **之外**（外层那个 fixed 包装里，无裁剪、无变换），
   * 位置在这里量出来。坐标是视口坐标，配 `position: fixed` 用。
   */
  const [menuPos, setMenuPos] = useState<{ top: number; left: number } | null>(null);
  const triggerRef = useRef<HTMLDivElement>(null);
  const menuPanelRef = useRef<HTMLDivElement>(null);

  const openMenu = () => {
    const r = triggerRef.current?.getBoundingClientRect();
    if (r) setMenuPos({ top: r.bottom + 8, left: r.left });
    setMenuOpen(true);
  };

  useEffect(() => {
    if (!menuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      const t = e.target as Node;
      // 菜单已经不在触发器里了，两处都要查，否则点菜单项会被当成"点外面"。
      if (triggerRef.current?.contains(t) || menuPanelRef.current?.contains(t)) return;
      setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false);
    };
    // 头部收起/隐藏时锚点没了，位置也就不再有意义。
    const onResize = () => setMenuOpen(false);
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', onResize);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', onResize);
    };
  }, [menuOpen]);

  // 头部整体隐藏（向下滚动时 -translate-y-full）→ 菜单跟着收掉。
  useEffect(() => {
    if (!isVisible) setMenuOpen(false);
  }, [isVisible]);
  /*
   * 上次滚动位置与悬停态放 ref：它们只是判断依据，放进 state 会让 effect 依赖
   * 它们 → 每滚动一像素就解绑/重绑监听并重渲染一次（P2 #28）。现在监听只绑一次，
   * 只有 isVisible 真正变化时才重渲染。
   */
  const lastScrollYRef = useRef(0);
  const isHoveredRef = useRef(false);

  useEffect(() => {
    isHoveredRef.current = isHovered;
  }, [isHovered]);

  useEffect(() => {
    const handleScroll = () => {
      const currentScrollY = window.scrollY;

      if (currentScrollY < 10) {
        setIsVisible(true);
      } else {
        if (
          currentScrollY > lastScrollYRef.current &&
          currentScrollY > 50 &&
          !isHoveredRef.current
        ) {
          setIsVisible(false);
        } else {
          setIsVisible(true);
        }
      }
      lastScrollYRef.current = currentScrollY;
    };

    window.addEventListener('scroll', handleScroll, { passive: true });
    return () => window.removeEventListener('scroll', handleScroll);
  }, []);

  const expanded = isHovered;

  return (
    <div className="fixed top-0 left-0 right-0 z-50 flex justify-center pointer-events-auto">
      <header
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
        className={`
          flex items-center transition-all duration-700 ease-[cubic-bezier(0.175,0.885,0.32,1.275)]
          bg-white/95 backdrop-blur-2xl border-b
          w-full overflow-hidden
          ${expanded
            ? 'px-8 py-6 gap-10 shadow-[0_12px_40px_rgba(0,0,0,0.08)] justify-center border-gray-200'
            : 'px-6 py-1.5 gap-3 shadow-sm border-gray-200/50 justify-between'
          }
          ${isVisible ? 'translate-y-0 opacity-100' : '-translate-y-full opacity-0 pointer-events-none'}
        `}
      >
        {/* Left Side: Logo & Status */}
        <div className={`flex items-center transition-all duration-500 ${expanded ? 'gap-5' : 'gap-3'}`}>
          <div
            className={`
              flex items-center justify-center bg-claude-accent text-white shadow-lg shadow-claude-accent/20 transition-all duration-500
              ${expanded ? 'w-12 h-12 rounded-2xl' : 'w-5 h-5 rounded-md shadow-none'}
            `}
          >
            <svg className={expanded ? "w-7 h-7" : "w-3 h-3"} xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={expanded ? 2.5 : 3.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 0 0 6-6v-1.5a6 6 0 0 0-6-6 6 6 0 0 0-6 6v1.5a6 6 0 0 0 6 6Z" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 12.75a3 3 0 0 0 3-3v-1.5a3 3 0 0 0-3-3 3 3 0 0 0-3 3v1.5a3 3 0 0 0 3 3Z" />
            </svg>
          </div>

          <div className="flex flex-col">
            <div className="flex items-center gap-3">
              <h1
                className={`
                  font-serif font-bold text-gray-900 tracking-tight transition-all duration-500 whitespace-nowrap
                  ${expanded ? 'text-xl' : 'text-[11px] uppercase tracking-[0.15em] text-gray-600 font-sans'}
                `}
              >
                Video Voice Translator
              </h1>
              {!expanded && (
                <>
                  <div className="w-[1px] h-3 bg-gray-300"></div>
                  <span className="text-[10px] font-black uppercase tracking-widest text-claude-accent">
                    TARGET LANGUAGE: {targetLanguage}
                    {hasAccents(targetLanguage) ? ` · ${accentLabel(targetAccent)}` : ''}
                  </span>
                </>
              )}
            </div>
            {expanded && (
              <span className="text-[10px] uppercase tracking-widest font-bold text-gray-400 mt-0.5">Workspace Alpha</span>
            )}
          </div>
        </div>

        {/* Center: Action Controls Section (Visible only when expanded) */}
        <div className={`items-center gap-6 transition-all duration-500 ${expanded ? 'flex opacity-100 scale-100' : 'hidden'}`}>
          <div className="h-10 w-[1px] bg-gray-200"></div>

          <div className="flex flex-col gap-2">
            <span className="text-[9px] font-bold uppercase tracking-widest text-gray-400 ml-1">Target Language</span>
            <div className="flex items-center gap-2">
              <div className="relative group/lang">
                <select
                  value={targetLanguage}
                  onChange={(e) => onLanguageChange(e.target.value)}
                  disabled={isProcessing}
                  className="bg-gray-50 border border-gray-200 rounded-xl px-4 py-2 text-sm font-bold text-gray-700 focus:outline-none focus:border-claude-accent transition appearance-none cursor-pointer pr-10 hover:bg-white w-36"
                >
                  {COMMON_LANGUAGES.map(l => (
                    <option key={l.value} value={l.value}>{l.label}</option>
                  ))}
                  {/* The rest of the languages both models handle. Grouped rather
                      than listed flat so the common case still reads as 7 items
                      instead of 11 — a native <optgroup> because it needs no
                      open/close state of its own. */}
                  <optgroup label="更多">
                    {MORE_LANGUAGES.map(l => (
                      <option key={l.value} value={l.value}>{l.label}</option>
                    ))}
                  </optgroup>
                </select>
                <div className="pointer-events-none absolute inset-y-0 right-0 flex items-center px-3 text-claude-accent">
                  <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" d="M19 9l-7 7-7-7"></path></svg>
                </div>
              </div>

              {/* Accent only exists for Chinese — the other languages have no
                  documented dialect control, so showing the control would imply
                  a choice that does nothing. */}
              {hasAccents(targetLanguage) && (
                <div className="relative group/accent">
                  <select
                    value={targetAccent}
                    onChange={(e) => onAccentChange(e.target.value)}
                    disabled={isProcessing}
                    title="中文口音"
                    className="bg-gray-50 border border-gray-200 rounded-xl pl-3 py-2 text-sm font-bold text-gray-700 focus:outline-none focus:border-claude-accent transition appearance-none cursor-pointer pr-8 hover:bg-white w-24"
                  >
                    {CHINESE_ACCENTS.map(a => (
                      <option key={a.value} value={a.value}>{a.label}</option>
                    ))}
                  </select>
                  <div className="pointer-events-none absolute inset-y-0 right-0 flex items-center px-2 text-claude-accent">
                    <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" d="M19 9l-7 7-7-7"></path></svg>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Reprocess —— **分体按钮**。
              左（主区）：点它就是那件事本身 —— 重译 + 重做全部音频，**不跑语音
              识别**（换语言 / 改翻译要求 / 重做配音都不必重听音频）。
              右（竖线隔开的小块）：另一个作用域 —— "从头开始（含语音识别）"。
              两块分开是因为它们不是"同一个动作的两个选项"，而是两种代价不同的
              操作：主区免费得快，右侧那条要重听音频、重新花钱。 */}
          <div
            className="relative flex items-stretch w-60 rounded-2xl overflow-hidden shadow-xl shadow-claude-accent/20 hover:shadow-2xl hover:shadow-claude-accent/30 hover:-translate-y-0.5 transition-all duration-300"
            ref={triggerRef}
          >
            <button
              onClick={onReprocess}
              disabled={isProcessing || !hasSegments}
              title="重新翻译并重做全部音频（不重跑语音识别）"
              className="flex-1 flex flex-col items-start gap-1 p-4 text-left bg-claude-accent hover:bg-claude-accentHover text-white transition-colors duration-300 disabled:opacity-40 disabled:cursor-not-allowed active:scale-[0.99]"
            >
              <div className="flex items-center gap-2">
                <div className="w-8 h-8 rounded-lg bg-white/20 flex items-center justify-center">
                  <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                </div>
                <span className="text-[10px] font-black uppercase tracking-[0.15em] text-white/70">Reprocess</span>
              </div>
              <span className="text-sm font-bold mt-1 ml-1 text-white whitespace-nowrap">Translate &amp; Synthesize</span>
            </button>

            {/* 竖线隔开的一小块：只负责开菜单。它在没有分段时仍然可用 ——
                菜单里的「从头开始」正是"识别什么都没出来、想再跑一遍"的出路。 */}
            <button
              type="button"
              onClick={() => (menuOpen ? setMenuOpen(false) : openMenu())}
              disabled={isProcessing}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-label="更多重新处理方式"
              title="更多重新处理方式"
              className="w-9 shrink-0 flex items-center justify-center bg-claude-accent hover:bg-claude-accentHover border-l border-white/25 text-white transition-colors duration-300 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <svg
                className={`w-3.5 h-3.5 transition-transform duration-200 ${menuOpen ? 'rotate-180' : ''}`}
                fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
              </svg>
            </button>

          </div>

          {/* Export Button.
              One button, one label, one meaning: open the export dialog. It used
              to flip to "Download Video" once a file existed, which made the
              export settings unreachable exactly for someone re-rendering with
              different subtitles. There is no "already exported" state to
              reflect — the file is produced on demand. */}
          <div className="flex flex-col gap-1">
            <button
              onClick={onExport}
              disabled={isExporting || isProcessing || !hasSegments}
              className="flex flex-col items-start gap-1 p-4 bg-white hover:bg-gray-50 border border-gray-200 shadow-xl shadow-black/5 hover:-translate-y-0.5 transition-all duration-300 rounded-2xl w-52 text-gray-700 disabled:opacity-40 active:scale-[0.98] active:translate-y-0"
            >
              <div className="flex items-center gap-2">
                <div className="w-8 h-8 rounded-lg bg-gray-100 flex items-center justify-center text-gray-500">
                  {isExporting ? (
                    <svg className="animate-spin w-5 h-5" viewBox="0 0 24 24" fill="none">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
                    </svg>
                  ) : (
                    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 8h10M7 12h4m1 8l3-3h6a2 2 0 002-2V7a2 2 0 00-2-2H6a2 2 0 00-2 2v8a2 2 0 002 2h3l3 3z" />
                    </svg>
                  )}
                </div>
                <span className="text-[10px] font-black uppercase tracking-[0.15em] text-gray-400">
                  {isExporting ? 'Exporting' : 'Export'}
                </span>
              </div>
              <span className="text-sm font-bold mt-1 ml-1">Export</span>
            </button>

            {exportError && (
              <span className="text-[10px] text-red-500 font-semibold max-w-52 px-1" title={exportError}>
                Export failed: {exportError.slice(0, 60)}
              </span>
            )}
          </div>
        </div>

        {/* Right Side: Settings & Affordance */}
        <div className={`flex items-center transition-all duration-500 ${expanded ? 'gap-0' : 'gap-4'}`}>
          {!expanded && (
            <div className="text-[10px] font-bold uppercase tracking-[0.2em] text-gray-400 flex items-center gap-1.5 cursor-pointer hover:text-claude-accent transition-colors border px-2 py-0.5 rounded-full border-gray-200">
              <span className="mt-0.5">Show Tools</span>
              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
              </svg>
            </div>
          )}

          <button
            onClick={onOpenSettings}
            className={`
               flex items-center justify-center transition-all duration-300 hover:bg-gray-100 rounded-full
               ${expanded ? 'w-12 h-12 bg-gray-50 text-gray-500 ml-2' : 'w-6 h-6 text-gray-400'}
            `}
            title="App Settings"
          >
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor" className={expanded ? "w-6 h-6" : "w-4 h-4"}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.324.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.24-.438.613-.431.992a6.759 6.759 0 010 .255c-.007.378.138.75.43.99l1.005.828c.424.35.534.954.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.57 6.57 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.28c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.02-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.24.437-.613.43-.992a6.932 6.932 0 010-.255c.007-.378-.138-.75-.43-.99l-1.004-.828a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.087.22-.128.332-.183.582-.495.644-.869l.214-1.281z" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
          </button>
        </div>
      </header>
      {/*
        下拉菜单渲染在 <header> 之外：header 有 overflow-hidden（收起动画需要），
        菜单又必须伸到按钮下方，留在里面会被整块剪掉。位置由按钮 rect 量出，
        用 position: fixed；外层这个包装是 fixed 且无 transform，所以坐标就是视口坐标。
      */}
      {menuOpen && menuPos && (
        <div
          ref={menuPanelRef}
          role="menu"
          style={{ top: menuPos.top, left: menuPos.left }}
          className="fixed w-72 z-[60] bg-white border border-gray-200 rounded-xl shadow-2xl shadow-gray-900/10 overflow-hidden"
        >
          <button
            role="menuitem"
            onClick={() => { setMenuOpen(false); onReprocess(); }}
            disabled={isProcessing || !hasSegments}
            className="w-full text-left px-4 py-3 hover:bg-gray-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <span className="block text-xs font-bold text-gray-800">重新翻译并重做配音</span>
            <span className="mt-0.5 block text-[10px] leading-snug text-gray-400">
              不重跑语音识别。换语言、改翻译要求、重做音频用它 —— 更快也更省。
            </span>
          </button>
          <div className="h-px bg-gray-100" />
          <button
            role="menuitem"
            onClick={() => { setMenuOpen(false); onRestartFromScratch(); }}
            disabled={isProcessing}
            className="w-full text-left px-4 py-3 hover:bg-gray-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <span className="block text-xs font-bold text-gray-800">从头开始（含语音识别）</span>
            <span className="mt-0.5 block text-[10px] leading-snug text-gray-400">
              清空已有进度，重跑识别 / 增强 / 翻译 / 配音。会重新产生 API 费用。
            </span>
          </button>
        </div>
      )}
    </div>
  );
};

export default Header;
