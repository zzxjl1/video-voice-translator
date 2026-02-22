import React, { useState, useEffect } from 'react';

interface HeaderProps {
  onOpenSettings: () => void;
  onReprocess: () => void;
  targetLanguage: string;
  onLanguageChange: (lang: string) => void;
  isProcessing: boolean;
  hasSegments: boolean;
}

const LANGUAGES = [
  'English',
  'Chinese',
  'Japanese',
  'Korean',
  'French',
  'German',
  'Spanish',
];

const Header: React.FC<HeaderProps> = ({
  onOpenSettings,
  onReprocess,
  targetLanguage,
  onLanguageChange,
  isProcessing,
  hasSegments
}) => {
  const [isScrolled, setIsScrolled] = useState(false);
  const [isVisible, setIsVisible] = useState(true);
  const [isHovered, setIsHovered] = useState(false);
  const [lastScrollY, setLastScrollY] = useState(0);

  useEffect(() => {
    const handleScroll = () => {
      const currentScrollY = window.scrollY;

      if (currentScrollY < 10) {
        setIsScrolled(false);
        setIsVisible(true);
      } else {
        setIsScrolled(true);
        if (currentScrollY > lastScrollY && currentScrollY > 50 && !isHovered) {
          setIsVisible(false);
        } else {
          setIsVisible(true);
        }
      }
      setLastScrollY(currentScrollY);
    };

    window.addEventListener('scroll', handleScroll, { passive: true });
    return () => window.removeEventListener('scroll', handleScroll);
  }, [lastScrollY, isHovered]);

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
            <div className="relative group/lang">
              <select
                value={targetLanguage}
                onChange={(e) => onLanguageChange(e.target.value)}
                disabled={isProcessing}
                className="bg-gray-50 border border-gray-200 rounded-xl px-4 py-2 text-sm font-bold text-gray-700 focus:outline-none focus:border-claude-accent transition appearance-none cursor-pointer pr-10 hover:bg-white w-40"
              >
                {LANGUAGES.map(l => <option key={l} value={l}>{l}</option>)}
              </select>
              <div className="pointer-events-none absolute inset-y-0 right-0 flex items-center px-3 text-claude-accent">
                <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" d="M19 9l-7 7-7-7"></path></svg>
              </div>
            </div>
          </div>

          {/* Reprocess Button */}
          <button
            onClick={onReprocess}
            disabled={isProcessing || !hasSegments}
            className="flex flex-col items-start gap-1 p-4 bg-claude-accent hover:bg-claude-accentHover shadow-xl shadow-claude-accent/20 hover:shadow-2xl hover:shadow-claude-accent/30 hover:-translate-y-0.5 transition-all duration-300 rounded-2xl w-52 group/btn text-white disabled:opacity-40 active:scale-[0.98] active:translate-y-0"
          >
            <div className="flex items-center gap-2">
              <div className="w-8 h-8 rounded-lg bg-white/20 flex items-center justify-center">
                <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
              </div>
              <span className="text-[10px] font-black uppercase tracking-[0.15em] text-white/70">Reprocess</span>
            </div>
            <span className="text-sm font-bold mt-1 ml-1 text-white">Translate & Synthesize</span>
          </button>
        </div>

        {/* Right Side: Settings & Affordance */}
        <div className={`flex items-center transition-all duration-500 ${expanded ? 'gap-0' : 'gap-4'}`}>
          {!expanded && (
            <div className="text-[10px] font-bold uppercase tracking-[0.2em] text-gray-400 group-indicator flex items-center gap-1.5 cursor-pointer hover:text-claude-accent transition-colors border px-2 py-0.5 rounded-full border-gray-200">
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
    </div>
  );
};

export default Header;
