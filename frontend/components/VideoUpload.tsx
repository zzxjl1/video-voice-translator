
import React, { useEffect, useRef, useState } from 'react';
import SeparationModeSelector from './SeparationModeSelector';
import { getAsrLanguages, getLanguageVoices } from '../services/apiService';
import type { SeparationMode, SeparationBackends, VoiceOption } from '../services/apiService';
import {
  CHINESE_ACCENTS,
  COMMON_LANGUAGES,
  MORE_LANGUAGES,
  accentLabel,
  hasAccents,
} from '../utils/languages';
import { formatRelativeTime, type RecentProject } from '../utils/recentProjects';

/**
 * Dot colour for a project's server status. Absent (server unreachable at
 * validation time) stays neutral rather than pretending to know.
 */
function statusDotClass(status?: string): string {
  if (status === 'completed') return 'bg-emerald-500';
  if (status === 'error') return 'bg-red-400';
  if (!status) return 'bg-gray-300';
  // processing / uploaded / anything else still in flight
  return 'bg-amber-400';
}

interface VideoUploadProps {
  /** The user picked a file. Nothing is uploaded until they press start. */
  onFilePicked: (file: File) => void;
  /** Name of the picked-but-not-started file; absent means nothing picked yet. */
  pendingFileName?: string;
  /** Begin uploading and processing the picked file. */
  onStart: () => void;
  isLoading: boolean;
  targetLanguage: string;
  onLanguageChange: (lang: string) => void;
  /** Chinese dialect for the dub; '' is Mandarin. Ignored for other languages. */
  targetAccent: string;
  onAccentChange: (accent: string) => void;
  enableVoiceClone: boolean;
  onVoiceCloneChange: (v: boolean) => void;
  /** 语气模仿：omni 听原声给译文注入情感/拟声标签。 */
  mmEnhance: boolean;
  onMmEnhanceChange: (v: boolean) => void;
  /** 智能选材：omni 通过 tool call 挑克隆参考段（依赖克隆开关）。 */
  cloneSmartPick: boolean;
  onCloneSmartPickChange: (v: boolean) => void;
  /** 注入 DS 翻译的自定义要求。默认空 = 标准行为。 */
  customPrompt: string;
  onCustomPromptChange: (v: string) => void;
  separationMode: SeparationMode;
  onSeparationModeChange: (mode: SeparationMode) => void;
  separationBackends: SeparationBackends;
  bgmSeparationLocked?: boolean;
  /** Projects this browser opened before, validated against the server. */
  recentProjects: RecentProject[];
  /** Open a previous project — the app recovers it from the URL. */
  onOpenProject: (videoId: string) => void;
}

const InfoTooltip: React.FC<{ text: string }> = ({ text }) => {
  const [show, setShow] = useState(false);
  return (
    <span className="relative inline-flex items-center ml-1.5">
      <span
        className="w-4 h-4 rounded-full border border-gray-300 text-gray-400 text-[10px] font-bold flex items-center justify-center cursor-help hover:border-claude-accent hover:text-claude-accent transition-colors"
        onMouseEnter={() => setShow(true)}
        onMouseLeave={() => setShow(false)}
      >
        i
      </span>
      {show && (
        <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 z-50 pointer-events-none">
          <div className="bg-gray-800 text-white text-[11px] leading-relaxed rounded-lg px-3 py-2 shadow-lg whitespace-normal w-52 text-center">
            {text}
            <div className="absolute left-1/2 -translate-x-1/2 top-full w-0 h-0 border-l-[5px] border-l-transparent border-r-[5px] border-r-transparent border-t-[5px] border-t-gray-800"></div>
          </div>
        </div>
      )}
    </span>
  );
};

/*
 * The three capabilities shown before any file is chosen.
 *
 * One row, three answers — each to a question the product name leaves open.
 * Kept as data so the row stays a single `map` and adding a fourth means
 * confronting the grid, not editing markup.
 */
const FEATURES: { label: string; sub: string; icon: React.ReactNode }[] = [
  {
    label: 'AI 重新配音',
    sub: '译文用新的声音说出来',
    icon: (
      <svg className="h-4 w-4 text-claude-accent" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 0 0 6-6v-1.5m-6 7.5a6 6 0 0 1-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 0 1-3-3V4.5a3 3 0 1 1 6 0v8.25a3 3 0 0 1-3 3Z" />
      </svg>
    ),
  },
  {
    label: '保留背景音乐',
    sub: '只替换人声，BGM 不动',
    icon: (
      <svg className="h-4 w-4 text-claude-accent" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" d="m9 9 10.5-3m0 6.553v3.75a2.25 2.25 0 0 1-1.632 2.163l-1.32.377a1.803 1.803 0 1 1-.99-3.467l2.31-.66a2.25 2.25 0 0 0 1.632-2.163Zm0 0V2.25L9 5.25v10.303m0 0v3.75a2.25 2.25 0 0 1-1.632 2.163l-1.32.377a1.803 1.803 0 0 1-.99-3.467l2.31-.66A2.25 2.25 0 0 0 9 15.553Z" />
      </svg>
    ),
  },
  {
    label: '双语字幕',
    sub: '可内嵌、烧录或导出',
    icon: (
      <svg className="h-4 w-4 text-claude-accent" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 6.75A2.25 2.25 0 0 1 4.5 4.5h15a2.25 2.25 0 0 1 2.25 2.25v10.5a2.25 2.25 0 0 1-2.25 2.25h-15a2.25 2.25 0 0 1-2.25-2.25V6.75Zm3.75 5.25h4.5m-4.5 3.75h4.5m4.5-3.75h3m-3 3.75h3" />
      </svg>
    ),
  },
];

const VideoUpload: React.FC<VideoUploadProps> = ({ onFilePicked, pendingFileName, onStart, isLoading, targetLanguage, onLanguageChange, targetAccent, onAccentChange, enableVoiceClone, onVoiceCloneChange, mmEnhance, onMmEnhanceChange, cloneSmartPick, onCloneSmartPickChange, customPrompt, onCustomPromptChange, separationMode, onSeparationModeChange, separationBackends, bgmSeparationLocked, recentProjects, onOpenProject }) => {
  /**
   * Whether the second column exists yet.
   *
   * It is not merely hidden: nothing to configure means nothing to show, and
   * revealing it only after a file is chosen is what turns the page from one
   * column into two. Configuration is meaningless without a video — the voice
   * preview, for one, has no language to preview against until the user has
   * arrived with something to dub.
   */
  const hasFile = Boolean(pendingFileName);
  /**
   * The less-common half of the list stays collapsed.
   *
   * Six is what fits on two rows of the grid and covers almost every job; the
   * other four are reachable but do not push the common ones down.
   */
  const [showMoreLanguages, setShowMoreLanguages] = useState(false);
  /**
   * 高级项（自定义翻译要求）默认折叠。低频选项不能常驻占高度，也不能和
   * 上面几个开关争夺注意力 —— 一行小入口即可；有内容时用「已设置」角标
   * 露出状态，避免折叠成隐形状态（恢复回来的工程可能带着要求）。
   */
  const [showAdvanced, setShowAdvanced] = useState(false);
  /** Which language's accent menu is open, if any. */
  const [accentMenuFor, setAccentMenuFor] = useState<string | null>(null);
  const visibleLanguages = showMoreLanguages
    ? [...COMMON_LANGUAGES, ...MORE_LANGUAGES]
    : [
        ...COMMON_LANGUAGES,
        /*
         * The current choice is always on screen, even when it is one of the
         * less-common four.
         *
         * This matters because the initial language comes from the BROWSER now:
         * a Portuguese or Indonesian browser starts on a language that lives
         * behind the "更多语言" toggle, and a selector with nothing highlighted
         * reads as broken rather than as "your choice is one click away".
         *
         * Appended rather than prepended so the common six keep their order and
         * nothing moves; the cost is a third row, and only in this case.
         */
        ...MORE_LANGUAGES.filter(lang => lang.value === targetLanguage),
      ];

  /**
   * The built-in voices the chosen language will actually use.
   *
   * Asked of the server rather than derived here: which voices exist is a
   * property of the voice models, and a second copy of that knowledge in the
   * frontend is how the language list once came to offer a language no voice
   * could speak.
   *
   * It is a PREVIEW — the real assignment happens per speaker once a job runs —
   * so a failure here hides the list rather than blocking the upload.
   */
  const [languageVoices, setLanguageVoices] = useState<VoiceOption[]>([]);
  const [languageNeedsCloning, setLanguageNeedsCloning] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getLanguageVoices(targetLanguage)
      .then(result => {
        if (cancelled) return;
        setLanguageVoices(result.voices);
        setLanguageNeedsCloning(result.needsVoiceCloning);
      })
      .catch(() => {
        if (!cancelled) setLanguageVoices([]);
      });
    return () => {
      cancelled = true;
    };
  }, [targetLanguage]);

  /** What the ASR model can hear — shown beside the upload. */
  const [asrLanguages, setAsrLanguages] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    getAsrLanguages()
      .then(result => {
        if (cancelled) return;
        setAsrLanguages(result.languages);
      })
      .catch(() => {
        // A preview; losing it must not block uploading.
        if (!cancelled) {
          setAsrLanguages([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    if (event.target.files && event.target.files[0]) {
      // Reports the choice upward; the upload itself waits for `onStart`.
      onFilePicked(event.target.files[0]);
      // Reset so picking the SAME file again still fires a change event.
      event.target.value = '';
    }
  };

  const handleClick = () => {
    inputRef.current?.click();
  };

  // Generate repeated watermark items
  const watermarkRows = Array.from({ length: 12 }, (_, i) => i);

  return (
    <div className="relative flex flex-col items-center justify-center min-h-screen overflow-hidden bg-claude-bg">
      {/* Animated watermark background */}
      <div className="absolute inset-0 overflow-hidden pointer-events-none select-none" aria-hidden="true">
        <div className="absolute inset-0" style={{ transform: 'rotate(-18deg)', transformOrigin: 'center center' }}>
          {watermarkRows.map((row) => (
            <div
              key={row}
              className="whitespace-nowrap flex items-center gap-0"
              style={{
                animation: `marquee-${row % 2 === 0 ? 'left' : 'right'} ${50 + row * 3}s linear infinite`,
                marginTop: row === 0 ? '-80px' : '0',
                height: '100px',
              }}
            >
              {Array.from({ length: 20 }, (_, i) => (
                <span
                  key={i}
                  className="inline-flex items-center gap-4 mx-8 text-claude-accent/[0.04] font-serif font-bold select-none"
                  style={{ fontSize: '42px', letterSpacing: '0.02em' }}
                >
                  <svg className="w-8 h-8 flex-shrink-0 opacity-60" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 0 0 6-6v-1.5a6 6 0 0 0-6-6 6 6 0 0 0-6 6v1.5a6 6 0 0 0 6 6Z" />
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 12.75a3 3 0 0 0 3-3v-1.5a3 3 0 0 0-3-3 3 3 0 0 0-3 3v1.5a3 3 0 0 0 3 3Z" />
                  </svg>
                  Video Voice Translator
                </span>
              ))}
            </div>
          ))}
        </div>
      </div>

      {/* CSS animation */}
      <style>{`
        @keyframes marquee-left {
          0% { transform: translateX(0); }
          100% { transform: translateX(-50%); }
        }
        @keyframes marquee-right {
          0% { transform: translateX(-50%); }
          100% { transform: translateX(0); }
        }
        /*
         * The one→two split: the columns grow, nothing slides sideways.
         *
         * (No backticks anywhere in this block: it is a JS template literal, so
         * a stray backtick ends the string and the file stops parsing. That is
         * not hypothetical — it is what this comment did on the first attempt.)
         *
         * "grid-cols-1 → lg:grid-cols-2" cannot be transitioned: it is a
         * discrete change in track COUNT, so it lands within a single frame.
         * Measured on this page at the moment a file was picked:
         *
         *     left card   464px → 220px   in 11ms   (−53%)
         *     container   512px → 512px   (had not started moving yet)
         *
         * The left card was slapped to half width and only then grew back to
         * 476px over 500ms. That jolt is what this replaces.
         *
         * Two tracks are therefore kept at ALL times, so the track count never
         * changes and the widths can interpolate. The closed state is "1fr 0fr"
         * — a real but zero-width second track — and column-gap travels 0 →
         * 24px with it. The honest movement is then a 12px squeeze (512 → 500)
         * instead of a 244px jump.
         *
         * Scoped to lg, because below it the two columns stack and there are no
         * columns to animate.
         */
        .split-grid {
          grid-template-columns: 1fr;
        }
        @media (min-width: 1024px) {
          .split-grid {
            grid-template-columns: 1fr 0fr;
            column-gap: 0px;
          }
          .split-grid[data-open='true'] {
            grid-template-columns: 1fr 1fr;
            column-gap: 24px;
          }
        }
        /*
         * The right column fades in, a beat late.
         *
         * The delay is load-bearing rather than decorative. Without it the card
         * is visible while its column is still a few pixels wide, and its own
         * layout — a four-across language grid, a voice list — reflows on every
         * frame of the way out. Letting the space open first and the content
         * arrive into it means the card is essentially its final shape by the
         * time it is readable.
         */
        @keyframes split-in-right {
          from { opacity: 0; }
          to { opacity: 1; }
        }
        /*
         * The start button: an entrance ring, then a breath.
         *
         * The ring is a one-shot pseudo-element that starts oversized and
         * collapses onto the button; the breath is a slow scale-plus-halo that
         * runs forever after. Both are delayed so the column has finished
         * arriving first — the button calls to you only once it is there.
         *
         * The halo lives on ::before rather than the button's own box-shadow,
         * which would fight the hover-shadow classes every frame. The scale
         * lives on the button itself; its active:scale is only meaningful while
         * enabled, and these classes are only applied while enabled, so they do
         * not contend.
         */
        @keyframes cta-ring-in {
          0% { opacity: 0; transform: scale(1.6); }
          30% { opacity: 0.85; }
          100% { opacity: 0; transform: scale(1); }
        }
        @keyframes cta-breathe {
          0%, 100% { transform: scale(1); }
          50% { transform: scale(1.035); }
        }
        @keyframes cta-halo {
          0%, 100% { box-shadow: 0 0 0 0 rgba(218, 119, 86, 0); }
          50% { box-shadow: 0 0 0 7px rgba(218, 119, 86, 0.22); }
        }
        .start-cta { position: relative; }
        .start-cta::after {
          content: '';
          position: absolute;
          inset: 0;
          border-radius: inherit;
          border: 2px solid rgba(218, 119, 86, 0.8);
          opacity: 0;
          pointer-events: none;
        }
        .start-cta::before {
          content: '';
          position: absolute;
          inset: 0;
          border-radius: inherit;
          pointer-events: none;
        }
        .start-cta-live::after {
          animation: cta-ring-in 620ms cubic-bezier(0.22, 1, 0.36, 1) 450ms both;
        }
        .start-cta-live {
          animation: cta-breathe 2.4s ease-in-out 1.15s infinite;
        }
        .start-cta-live::before {
          animation: cta-halo 2.4s ease-in-out 1.15s infinite;
        }
        @media (prefers-reduced-motion: reduce) {
          .start-cta-live,
          .start-cta-live::after,
          .start-cta-live::before {
            animation: none;
          }
        }
      `}</style>

      {/* Logo — a row of its own, with the space under it stated here.
          It used to sit flush against the cards: the margin lived on a wrapper
          that the two-column split removed, so the gap silently disappeared. */}
      <div className="relative z-10 flex w-full items-center justify-center gap-4 px-6 mb-10">
        <div className="w-14 h-14 bg-claude-accent text-white rounded-2xl flex items-center justify-center shadow-lg shadow-claude-accent/20">
          <svg className="w-8 h-8" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={2.5} stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 0 0 6-6v-1.5a6 6 0 0 0-6-6 6 6 0 0 0-6 6v1.5a6 6 0 0 0 6 6Z" />
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 12.75a3 3 0 0 0 3-3v-1.5a3 3 0 0 0-3-3 3 3 0 0 0-3 3v1.5a3 3 0 0 0 3 3Z" />
          </svg>
        </div>
        <div>
          <h1 className="text-2xl font-serif font-bold text-gray-900 tracking-tight">Video Voice Translator</h1>
          <p className="text-[10px] uppercase tracking-[0.2em] font-bold text-gray-400 mt-0.5">AI-Powered Translation</p>
        </div>
      </div>

      {/* Main content.
          One column until a file is chosen: there is nothing to configure yet,
          and an empty settings panel would be a promise the page cannot keep.
          Picking a file opens the second column by GROWING it — the track count
          is the same before and after, so the widths can be interpolated and
          the left card is squeezed rather than snapped. See `.split-grid`.
          The easing is stated rather than left at `ease-out` so the container
          and the columns move on one clock.
          Left: the file, and what the ASR model can hear — that constrains the
          upload, so it belongs with it.
          Right: the target language, the settings, and the button that starts
          the job. */}
      <div
        data-open={hasFile ? 'true' : 'false'}
        className={`split-grid relative z-10 grid w-full gap-6 px-6 transition-all duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] ${
          hasFile ? 'max-w-5xl lg:items-start' : 'max-w-lg'
        }`}
      >
        {/* `min-w-0` so the cell can actually shrink to the `0fr` track: a grid
            item's default `min-width: auto` would refuse to go below its
            content's intrinsic width and the second column would never close. */}
        <div className="flex min-w-0 flex-col gap-5">
          {/* Upload */}
          <div className="w-full bg-white/80 backdrop-blur-xl rounded-2xl border border-gray-200/80 shadow-[0_8px_30px_rgba(0,0,0,0.06)] p-8 flex flex-col items-center text-center">
          <div className="w-16 h-16 bg-claude-paper rounded-full flex items-center justify-center mb-5 text-claude-accent">
            <svg className="w-8 h-8" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="m15.75 10.5 4.72-4.72a.75.75 0 0 1 1.28.53v11.38a.75.75 0 0 1-1.28.53l-4.72-4.72M4.5 18.75h9a2.25 2.25 0 0 0 2.25-2.25v-9A2.25 2.25 0 0 0 13.5 5.25h-9A2.25 2.25 0 0 0 2.25 7.5v9A2.25 2.25 0 0 0 4.5 18.75Z" />
            </svg>
          </div>

          {/*
           * What this product IS, before asking anything of the visitor.
           *
           * A first-time visitor decides within seconds whether a tool applies
           * to their job. Before this block the only hint was the product name
           * — "Video Voice Translator" could mean subtitles, could mean dubbing
           * — and the one sentence that actually answered it sat in the second
           * column, which is not rendered until a file has already been chosen.
           * Nobody uploads first and understands after.
           */}
          {pendingFileName ? (
            <p className="text-sm text-gray-500 mb-4 leading-relaxed max-w-sm break-all">
              已选择 <span className="font-bold text-claude-accent">{pendingFileName}</span>
            </p>
          ) : (
            <p className="text-[15px] leading-relaxed text-gray-700 mb-4 max-w-sm">
              把视频里的话<strong className="font-bold text-claude-text">翻译成另一种语言</strong>，再用 AI 重新配音。
            </p>
          )}

          {/* The three capabilities a novice is deciding between. Each earns its
              place by answering a question the name does not: does my background
              music survive? do I get subtitles? what exactly comes out? */}
          <div className="mb-6 grid w-full grid-cols-3 gap-2">
            {FEATURES.map(feature => (
              <div
                key={feature.label}
                className="flex flex-col items-center gap-1 rounded-xl bg-gray-50/80 px-1 py-2.5"
              >
                {feature.icon}
                <span className="text-[11px] font-bold text-gray-700">{feature.label}</span>
                <span className="text-center text-[10px] leading-tight text-gray-400">{feature.sub}</span>
              </div>
            ))}
          </div>

          <input
            type="file"
            ref={inputRef}
            onChange={handleFileChange}
            className="hidden"
            accept="video/*"
            disabled={isLoading}
          />
          <button
            onClick={handleClick}
            disabled={isLoading}
            className={`w-full px-8 py-4 font-semibold rounded-xl transition-all duration-300 text-base ${
              pendingFileName
                ? 'bg-white text-[#da7756] border border-claude-border hover:bg-claude-paper active:scale-[0.98]'
                : 'bg-claude-accent text-white hover:bg-claude-accentHover shadow-lg shadow-claude-accent/20 hover:shadow-xl hover:shadow-claude-accent/30 hover:-translate-y-0.5 active:translate-y-0 active:scale-[0.98]'
            } disabled:bg-gray-300 disabled:text-white disabled:cursor-not-allowed`}
          >
            {pendingFileName ? '重新选择视频' : '上传视频文件'}
          </button>
          </div>

          {/* Recent projects — the memory that makes a 32-character URL
              unnecessary. localStorage only remembers WHICH projects this
              browser touched; everything shown was validated against the
              server moments ago, and entries it no longer has are simply not
              here. Hidden entirely when the list is empty: an empty "recent"
              section is noise on a first visit. */}
          {recentProjects.length > 0 && (
            <div className="w-full bg-white/80 backdrop-blur-xl rounded-2xl border border-gray-200/80 shadow-[0_8px_30px_rgba(0,0,0,0.06)] p-6">
              <div className="flex items-baseline justify-between mb-2">
                <span className="text-[11px] font-bold uppercase tracking-wider text-gray-500">
                  最近工程
                </span>
                <span className="text-[10px] text-gray-400">
                  {recentProjects.length} 个 · 仅本浏览器
                </span>
              </div>
              <div className="flex flex-col gap-0.5">
                {recentProjects.map(project => (
                  <button
                    key={project.videoId}
                    onClick={() => onOpenProject(project.videoId)}
                    className="flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-left hover:bg-gray-50 transition-colors cursor-pointer"
                  >
                    <span
                      className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusDotClass(project.status)}`}
                    />
                    <span className="flex-1 min-w-0 truncate text-xs text-gray-700">
                      {project.filename || project.videoId.slice(0, 8)}
                    </span>
                    <span className="shrink-0 text-[10px] text-gray-400">
                      {formatRelativeTime(project.updatedAt)}
                    </span>
                  </button>
                ))}
              </div>
              <p className="mt-2 text-[10px] text-gray-400">
                点开即回到当时的进度和设置。
              </p>
            </div>
          )}

          {/* ASR languages — with the upload, not with the settings: they are a
              constraint on what can be uploaded, not a preference.

              Titled and explained in the visitor's words, not the model's: the
              old heading "能识别的语言 · ASR" made a novice decode an acronym
              before learning anything. The Chinese-dialect roll call that used
              to sit under the chips was a spec detail, not a decision — the
              model hears them whether or not they are listed, nobody picks one
              here, and it cost a whole block of vertical space. */}
          <div className="w-full bg-white/80 backdrop-blur-xl rounded-2xl border border-gray-200/80 shadow-[0_8px_30px_rgba(0,0,0,0.06)] p-6">
            <div className="flex items-baseline justify-between mb-1">
              <span className="text-[11px] font-bold uppercase tracking-wider text-gray-500">
                能听懂的语言
              </span>
              <span className="text-[10px] text-gray-400">
                {asrLanguages.length} 种 · 自动识别
              </span>
            </div>
            <p className="mb-2.5 text-[10px] leading-relaxed text-gray-400">
              视频里说的话会自动转写成文字，不需要你选语言。
            </p>
            <div className="flex flex-wrap gap-1">
              {asrLanguages.map(lang => (
                <span
                  key={lang}
                  className="px-1.5 py-0.5 rounded-md bg-gray-50 border border-gray-100 text-[10px] text-gray-600"
                >
                  {lang}
                </span>
              ))}
            </div>
          </div>

        </div>

        {hasFile && (
        /*
         * ONE card with three zones, not three cards.
         *
         * Three stacked cards cost two extra paddings and two 20px gaps — about
         * 140px — and this column measured 780px against the left column's 517.
         * That difference is what pushed the page into scrolling on a 768px
         * laptop (108px over), and merging is the cheapest way to get it back
         * without hiding anything: every control stays on screen, and the last
         * zone is separated by a rule instead of by whitespace.
         *
         * The zones are also the order the decisions get made in: what comes out
         * (language), how the job runs (settings), then start it.
         *
         * `overflow-hidden` is required, not cosmetic — the footer's tinted
         * background has to be clipped by the card's rounded corners.
         */
        <div className="min-w-0">
        <div className="w-full bg-white/80 backdrop-blur-xl rounded-2xl border border-gray-200/80 shadow-[0_8px_30px_rgba(0,0,0,0.06)] overflow-hidden lg:sticky lg:top-6 animate-[split-in-right_250ms_ease-out_150ms_both]">
          {/* Zone 1 — the target language, and the voice pool it selects. */}
          <div className="p-6 pb-4">
          <label className="block text-xs font-bold uppercase tracking-[0.15em] text-gray-500 mb-3 text-center">
            Translate video to
          </label>
          <div className="grid grid-cols-4 gap-2 mb-1">
            {visibleLanguages.map(lang => {
              const selected = targetLanguage === lang.value;
              const withAccent = hasAccents(lang.value);
              return (
                <div key={lang.value} className="relative">
                  <button
                    onClick={() => {
                      onLanguageChange(lang.value);
                      // Selecting a language closes any accent menu that belonged
                      // to another one.
                      setAccentMenuFor(null);
                    }}
                    className={`
                      w-full py-2.5 px-3 rounded-xl text-sm font-semibold transition-all duration-200
                      ${withAccent ? 'pr-7' : ''}
                      ${selected
                        ? 'bg-claude-accent text-white shadow-md shadow-claude-accent/25 scale-[1.02]'
                        : 'bg-gray-50 text-gray-600 hover:bg-gray-100 hover:text-gray-800 border border-gray-100'
                      }
                    `}
                  >
                    {lang.label}
                    {withAccent && selected && targetAccent ? (
                      <span className="block text-[9px] font-medium opacity-80 leading-tight">
                        {accentLabel(targetAccent)}
                      </span>
                    ) : null}
                  </button>

                  {/*
                    A sibling, not a child. Interactive content cannot nest inside
                    a <button> — the parser drops it out and its clicks become
                    unpredictable — so the chevron is a real button positioned
                    over the cell's right edge instead.
                  */}
                  {withAccent && (
                    <button
                      type="button"
                      aria-label="选择中文口音"
                      aria-expanded={accentMenuFor === lang.value}
                      onClick={() =>
                        setAccentMenuFor(open => (open === lang.value ? null : lang.value))
                      }
                      className={`
                        absolute right-1 top-1/2 -translate-y-1/2 w-5 h-5 rounded-md
                        flex items-center justify-center transition-colors cursor-pointer
                        ${selected ? 'text-white/80 hover:bg-white/20' : 'text-gray-400 hover:bg-gray-200'}
                      `}
                    >
                      <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" d="M19 9l-7 7-7-7" />
                      </svg>
                    </button>
                  )}

                  {withAccent && accentMenuFor === lang.value && (
                    <>
                      {/* Click-away layer: no document listeners to add and
                          remove, and it cannot miss a click that lands on
                          nothing. */}
                      <span
                        className="fixed inset-0 z-10"
                        onClick={() => setAccentMenuFor(null)}
                      />
                      <div className="absolute left-0 right-0 top-full mt-1 z-20 bg-white rounded-xl border border-gray-200 shadow-xl py-1 max-h-56 overflow-y-auto">
                        {CHINESE_ACCENTS.map(accent => (
                          <button
                            key={accent.value}
                            type="button"
                            onClick={() => {
                              onAccentChange(accent.value);
                              onLanguageChange('Chinese');
                              setAccentMenuFor(null);
                            }}
                            className={`
                              w-full text-left px-3 py-1.5 text-xs transition-colors cursor-pointer
                              ${targetAccent === accent.value && selected
                                ? 'bg-claude-accent/10 text-claude-accent font-bold'
                                : 'text-gray-600 hover:bg-gray-50'
                              }
                            `}
                          >
                            {accent.label}
                          </button>
                        ))}
                      </div>
                    </>
                  )}
                </div>
              );
            })}
          </div>

          <button
            type="button"
            onClick={() => setShowMoreLanguages(v => !v)}
            className="mt-1 mx-auto flex items-center gap-1 text-[11px] font-medium text-gray-400 hover:text-claude-accent transition-colors cursor-pointer"
          >
            {showMoreLanguages ? '收起' : '更多语言'}
            <svg
              className={`h-3 w-3 transition-transform ${showMoreLanguages ? 'rotate-180' : ''}`}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M19 9l-7 7-7-7" />
            </svg>
          </button>

        </div>

          {/* Zone 2 — how the job runs. Separation and cloning are the two
              decisions that shape the dub, and the voice display sits under the
              clone switch it is a consequence of. */}
          <div className="px-6 pb-5">
          <h2 className="text-sm font-serif font-bold text-gray-900 mb-4">处理设置</h2>
          {/* Processing Options. The old `mb-6` is gone: the footer's own rule
              and padding now do that job, and keeping both would waste the
              height this merge exists to save. */}
          <div className="w-full space-y-3 text-left">
            <div className="py-2.5 px-3 bg-gray-50 rounded-xl">
              <div className="flex items-center mb-2">
                <span className={`text-sm font-medium ${bgmSeparationLocked ? 'text-gray-400' : 'text-gray-700'}`}>BGM Separation</span>
                <InfoTooltip text="Splits vocals from background music for cleaner transcription and voice cloning. Pick which backend does the work, or turn it off." />
              </div>
              <SeparationModeSelector
                value={separationMode}
                onChange={onSeparationModeChange}
                backends={separationBackends}
                locked={bgmSeparationLocked}
              />
            </div>
            <div className="flex items-center justify-between py-1.5 px-3 bg-gray-50 rounded-xl">
              <div className="flex items-center">
                <span className="text-sm font-medium text-gray-700">Voice Cloning</span>
                <InfoTooltip text="Clone the original speaker's voice for synthesis. The translated audio will sound like the original speaker. Requires more processing time." />
              </div>
              <button
                onClick={() => onVoiceCloneChange(!enableVoiceClone)}
                className={`relative w-10 h-[22px] rounded-full transition-colors duration-200 ${enableVoiceClone ? 'bg-claude-accent' : 'bg-gray-300'}`}
              >
                <span className={`absolute top-[2px] left-[2px] w-[18px] h-[18px] bg-white rounded-full shadow transition-transform duration-200 ${enableVoiceClone ? 'translate-x-[18px]' : ''}`} />
              </button>
            </div>

            {/* 多模态增强识别：omni 听原声 —— 纠正 ASR 错漏 + 标注语气标签，
                翻译基于纠错后的文本。与克隆正交。 */}
            <div className="flex items-center justify-between py-1.5 px-3 bg-gray-50 rounded-xl">
              <div className="flex items-center">
                <span className="text-sm font-medium text-gray-700">多模态增强识别</span>
                <InfoTooltip text="AI 听一遍原视频：纠正语音识别的错字漏字，并标注每句的语气（兴奋、耳语、大笑…），翻译基于纠错后的文本进行。由 qwen-omni 完成，会增加一些处理时间。" />
              </div>
              <button
                onClick={() => onMmEnhanceChange(!mmEnhance)}
                className={`relative w-10 h-[22px] rounded-full transition-colors duration-200 ${mmEnhance ? 'bg-claude-accent' : 'bg-gray-300'}`}
              >
                <span className={`absolute top-[2px] left-[2px] w-[18px] h-[18px] bg-white rounded-full shadow transition-transform duration-200 ${mmEnhance ? 'translate-x-[18px]' : ''}`} />
              </button>
            </div>

            {/* 智能选材：依赖克隆开关 —— 不克隆就没有「选材」这件事。 */}
            {enableVoiceClone && (
              <div className="flex items-center justify-between py-1.5 px-3 bg-gray-50 rounded-xl">
                <div className="flex items-center">
                  <span className="text-sm font-medium text-gray-700">智能选材</span>
                  <InfoTooltip text="AI 试听候选片段，挑出无重叠、情绪中性的部分做克隆参考素材；每轮选择都会校验，不过关自动重选，失败则退回规则选材。" />
                </div>
                <button
                  onClick={() => onCloneSmartPickChange(!cloneSmartPick)}
                  className={`relative w-10 h-[22px] rounded-full transition-colors duration-200 ${cloneSmartPick ? 'bg-claude-accent' : 'bg-gray-300'}`}
                >
                  <span className={`absolute top-[2px] left-[2px] w-[18px] h-[18px] bg-white rounded-full shadow transition-transform duration-200 ${cloneSmartPick ? 'translate-x-[18px]' : ''}`} />
                </button>
              </div>
            )}

            {/* What the switch above actually decides.
                Off: the built-in voices this language draws from, so the result
                is visible instead of implied.
                On: no list at all — cloning REPLACES them, so there is nothing
                to choose and showing a list would suggest otherwise. */}
            {enableVoiceClone ? (
              <div className="py-2.5 px-3 bg-purple-50 border border-purple-100 rounded-xl">
                <p className="text-[11px] leading-relaxed text-purple-700">
                  会先克隆每位说话人自己的声音，再用它来配。
                  <span className="font-semibold">预制音色不再参与</span>
                  ——所以这里没有音色可选。
                </p>
              </div>
            ) : languageNeedsCloning ? (
              <div className="py-2.5 px-3 bg-amber-50 border border-amber-200 rounded-xl">
                <p className="text-[11px] leading-relaxed text-amber-700">
                  没有内置音色能说 {targetLanguage}，请开启声音复刻。
                </p>
              </div>
            ) : languageVoices.length > 0 ? (
              <div className="py-2.5 px-3 bg-gray-50 rounded-xl">
                <div className="flex items-baseline justify-between mb-1.5">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-gray-500">
                    预制音色 · {targetLanguage}
                  </span>
                  <span className="text-[10px] text-gray-400">{languageVoices.length} 个</span>
                </div>
                <div className="flex flex-wrap gap-1">
                  {languageVoices.slice(0, 12).map(voice => (
                    <span
                      key={voice.id}
                      className="px-1.5 py-0.5 rounded-md bg-white border border-gray-200 text-[10px] text-gray-600"
                    >
                      {voice.label}
                    </span>
                  ))}
                  {languageVoices.length > 12 && (
                    <span className="px-1 py-0.5 text-[10px] text-gray-400">
                      +{languageVoices.length - 12}
                    </span>
                  )}
                </div>
                <p className="mt-1.5 text-[10px] leading-snug text-gray-400">
                  多说话人视频会自动为每位说话人分配不同的音色。生成后可在每一行的菜单里改。
                </p>
              </div>
            ) : null}

            {/* 高级：自定义翻译要求。低频高级项的形态是折叠的一行 ——
                它改的是 DS 翻译的默认行为，绝大多数人不需要碰；展开后
                注入的是自然语言要求，所以不用开关、不用枚举，一个文本框
                就是全部。空串在后端完全不注入，默认行为零改动。
                无边框、py-1.5：这一行是页面上最低优先级的可点物，视觉上
                必须比开关行轻，且 768 首屏的高度经不起一个带边框的盒子。 */}
            <div>
              <button
                onClick={() => setShowAdvanced(v => !v)}
                className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left cursor-pointer"
              >
                <svg
                  className={`h-3 w-3 text-gray-400 transition-transform duration-200 ${showAdvanced ? 'rotate-90' : ''}`}
                  fill="none" viewBox="0 0 24 24" strokeWidth={2.5} stroke="currentColor"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" />
                </svg>
                <span className="text-[11px] font-bold uppercase tracking-wider text-gray-500">
                  高级 · 自定义翻译要求
                </span>
                {customPrompt.trim() && (
                  <span className="px-1.5 py-px rounded-full bg-claude-accent/10 text-[10px] font-bold text-claude-accent">
                    已设置
                  </span>
                )}
              </button>
              {showAdvanced && (
                <div className="px-3 pb-3">
                  <textarea
                    value={customPrompt}
                    onChange={e => onCustomPromptChange(e.target.value.slice(0, 500))}
                    rows={3}
                    placeholder={'例：专有名词保留英文原文；语气正式，面向企业客户；数字一律写成阿拉伯数字…'}
                    className="w-full resize-none rounded-lg border border-gray-200 bg-white px-2.5 py-2 text-xs leading-relaxed text-gray-700 placeholder:text-gray-300 focus:outline-none focus:border-claude-accent/50"
                  />
                  <div className="mt-1 flex items-baseline justify-between text-[10px] text-gray-400">
                    <span>注入翻译模型，可覆盖默认行为；留空 = 标准翻译。</span>
                    <span className="shrink-0 tabular-nums">{customPrompt.length}/500</span>
                  </div>
                </div>
              )}
            </div>
          </div>
          </div>

          {/* Zone 3 — the outcome, stated next to the button that causes it.
              A rule and a tint rather than a card: separating it this way costs
              no height, which is the point of this layout. The sentence lives
              here and not in the upload card because it describes what the two
              zones above it will DO — by the time it is read, the language and
              the clone switch are already decided. */}
          <div className="border-t border-gray-200/70 bg-gray-50/60 px-6 py-4 flex items-center gap-3">
            <p className="flex-1 min-w-0 text-[11px] leading-snug text-gray-500">
              We'll transcribe, translate to <span className="font-bold text-claude-accent">{targetLanguage}</span>, and re-voice your video with AI.
            </p>
            <button
              onClick={onStart}
              disabled={isLoading}
              className={`shrink-0 px-5 py-3 bg-claude-accent text-white text-sm font-semibold rounded-xl hover:bg-claude-accentHover disabled:bg-gray-300 disabled:cursor-not-allowed transition-all duration-300 shadow-lg shadow-claude-accent/20 hover:shadow-xl hover:shadow-claude-accent/30 active:scale-[0.98] ${isLoading ? '' : 'start-cta start-cta-live'}`}
            >
              {isLoading ? (
                <span className="flex items-center gap-2">
                  <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg>
                  处理中…
                </span>
              ) : '开始处理'}
            </button>
          </div>
        </div>
        </div>
        )}
      </div>
    </div>
  );
};

export default VideoUpload;
