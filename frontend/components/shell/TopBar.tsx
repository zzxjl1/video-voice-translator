/**
 * 编辑页顶栏 —— 沿用重构前那套浮动 Header 的视觉与动效：
 *
 *   收起态：一条细栏（小 logo + 小字标题 + 目标语言 + 「Show Tools」药丸 + 图标）
 *   悬停：整条展开成 py-6 的工具栏（大 logo、衬线大标题、语言选择、大动作卡片），
 *        缓动用 cubic-bezier(0.175,0.885,0.32,1.275)、700ms —— 就是原来那条"弹出来"的手感。
 *
 * 与旧 Header 的差别只在内容：旧的「Reprocess / 从头开始」是整条管线重跑的概念，
 * 现在换成「补齐缺口 / 运行中（可取消）」；另外多了撤销和 AI 助手开关。
 *
 * 顶栏是 fixed 的（展开时要浮在画面上方），所以编辑页的 main 要留出顶部空间。
 */
import React, { useState } from 'react';
import { ALL_LANGUAGES } from '../../utils/languages';
import type { JobInfo, Needs } from '../../net/types';

interface Props {
  filename: string;
  targetLanguage: string;
  onLanguageChange: (language: string) => void;
  needs: Needs;
  dubbing: boolean;
  jobs: JobInfo[];
  undoSummary: string | null;
  onUndo: () => void;
  onFill: () => void;
  onCancelJobs: () => void;
  onOpenSettings: () => void;
  onExport: () => void;
  onHome: () => void;
  exporting: boolean;
  toggleChat: () => void;
  chatOpen: boolean;
}

const EASE = 'ease-[cubic-bezier(0.175,0.885,0.32,1.275)]';

const GEAR = 'M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.324.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.24-.438.613-.431.992a6.759 6.759 0 010 .255c-.007.378.138.75.43.99l1.005.828c.424.35.534.954.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.57 6.57 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.28c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.02-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.24.437-.613.43-.992a6.932 6.932 0 010-.255c.007-.378-.138-.75-.43-.99l-1.004-.828a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.087.22-.128.332-.183.582-.495.644-.869l.214-1.281z';

const TopBar: React.FC<Props> = ({
  filename, targetLanguage, onLanguageChange, needs, dubbing, jobs, undoSummary, onUndo, onFill,
  onCancelJobs, onOpenSettings, onExport, onHome, exporting, toggleChat, chatOpen,
}) => {
  const [expanded, setExpanded] = useState(false);
  const running = jobs.length > 0;
  const job = jobs[0];
  const pending = needs.translate + (dubbing ? needs.dub : 0);
  const pendingText = [
    needs.translate ? `${needs.translate} 句译文` : '',
    dubbing && needs.dub ? `${needs.dub} 句配音` : '',
  ].filter(Boolean).join(' · ');

  return (
    <div className="fixed top-0 inset-x-0 z-50 flex justify-center">
      <header
        onMouseEnter={() => setExpanded(true)}
        onMouseLeave={() => setExpanded(false)}
        className={`
          flex items-center transition-all duration-700 ${EASE}
          bg-white/95 backdrop-blur-2xl border-b w-full overflow-hidden
          ${expanded
            ? 'px-8 py-6 gap-8 shadow-[0_12px_40px_rgba(0,0,0,0.08)] justify-center border-gray-200'
            : 'px-6 py-1.5 gap-3 shadow-sm border-gray-200/50 justify-between'}
        `}
      >
        {/* 左：logo + 标题（尺寸随展开/收起变形，就是原来那条动效） */}
        <div className={`flex items-center transition-all duration-500 ${expanded ? 'gap-5' : 'gap-3'} min-w-0`}>
          <button
            onClick={onHome}
            title="返回首页"
            className={`
              flex items-center justify-center bg-claude-accent text-white shadow-lg shadow-claude-accent/20
              transition-all duration-500 hover:scale-105 active:scale-95 shrink-0
              ${expanded ? 'w-12 h-12 rounded-2xl' : 'w-5 h-5 rounded-md shadow-none'}
            `}
          >
            <svg className={expanded ? 'w-7 h-7' : 'w-3 h-3'} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={expanded ? 2.5 : 3.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 0 0 6-6v-1.5a6 6 0 0 0-6-6 6 6 0 0 0-6 6v1.5a6 6 0 0 0 6 6Z" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 12.75a3 3 0 0 0 3-3v-1.5a3 3 0 0 0-3-3 3 3 0 0 0-3 3v1.5a3 3 0 0 0 3 3Z" />
            </svg>
          </button>

          <div className="flex flex-col min-w-0">
            <div className="flex items-center gap-3 min-w-0">
              <h1
                className={`
                  font-serif font-bold text-gray-900 tracking-tight transition-all duration-500 truncate
                  ${expanded ? 'text-xl' : 'text-[11px] uppercase tracking-[0.15em] text-gray-600 font-sans max-w-[18ch]'}
                `}
              >
                {expanded ? 'Video Voice Translator' : (filename || 'Video Voice Translator')}
              </h1>
              {!expanded && (
                <>
                  <div className="w-px h-3 bg-gray-300 shrink-0" />
                  <span className="text-[10px] font-black uppercase tracking-widest text-gray-400 whitespace-nowrap">
                    Target Lang:
                  </span>
                  <span className="text-[10px] font-black uppercase tracking-widest text-claude-accent whitespace-nowrap">
                    {targetLanguage}
                  </span>
                </>
              )}
            </div>
            {expanded && (
              <span className="text-[10px] uppercase tracking-widest font-bold text-gray-400 mt-0.5 truncate max-w-[32ch]">
                {filename}
              </span>
            )}
          </div>
        </div>

        {/* 中：展开时的工具区 —— 语言 + 大动作卡片（旧版的手感） */}
        <div className={`items-center gap-6 transition-all duration-500 ${expanded ? 'flex opacity-100 scale-100' : 'hidden opacity-0 scale-95'}`}>
          <div className="h-10 w-px bg-gray-200" />

          <div className="flex flex-col gap-2">
            <span className="text-[9px] font-bold uppercase tracking-widest text-gray-400 ml-1">Target Language</span>
            <div className="relative group/lang">
              <select
                value={targetLanguage}
                onChange={(e) => onLanguageChange(e.target.value)}
                className="bg-gray-50 border border-gray-200 rounded-xl px-4 py-2 text-sm font-bold text-gray-700 focus:outline-none focus:border-claude-accent transition appearance-none cursor-pointer pr-10 hover:bg-white w-36"
              >
                {ALL_LANGUAGES.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
              </select>
              <div className="pointer-events-none absolute inset-y-0 right-0 flex items-center px-3 text-claude-accent">
                <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M19 9l-7 7-7-7" /></svg>
              </div>
            </div>
          </div>

          {/* 主卡片：运行中 → 进度 + 取消；有缺口 → 补齐；都没有时不占位 */}
          {running ? (
            <div className="flex items-stretch w-60 rounded-2xl overflow-hidden shadow-xl shadow-claude-accent/20 bg-claude-accent text-white">
              <div className="flex-1 flex flex-col items-start gap-1 p-4">
                <div className="flex items-center gap-2">
                  <div className="w-8 h-8 rounded-lg bg-white/20 flex items-center justify-center">
                    <span className="w-4 h-4 rounded-full border-2 border-white/30 border-t-white animate-spin" />
                  </div>
                  <span className="text-[10px] font-black uppercase tracking-[0.15em] text-white/70">Running</span>
                </div>
                <span className="text-sm font-bold mt-1 ml-1 whitespace-nowrap">
                  {job.label}{job.total > 0 ? ` ${job.done}/${job.total}` : ''}
                </span>
              </div>
              <button
                onClick={onCancelJobs}
                title="取消"
                className="w-9 shrink-0 flex items-center justify-center bg-claude-accent hover:bg-claude-accentHover border-l border-white/25 transition-colors"
              >
                <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="2" /></svg>
              </button>
            </div>
          ) : pending > 0 ? (
            <button
              onClick={onFill}
              title="只补缺的和已过期的，已经好的不会重做"
              className="flex items-stretch w-60 rounded-2xl overflow-hidden shadow-xl shadow-claude-accent/20 hover:shadow-2xl hover:shadow-claude-accent/30 hover:-translate-y-0.5 transition-all duration-300 text-left bg-claude-accent hover:bg-claude-accentHover text-white active:scale-[0.99]"
            >
              <div className="flex flex-col items-start gap-1 p-4">
                <div className="flex items-center gap-2">
                  <div className="w-8 h-8 rounded-lg bg-white/20 flex items-center justify-center">
                    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                    </svg>
                  </div>
                  <span className="text-[10px] font-black uppercase tracking-[0.15em] text-white/70">Fill gaps</span>
                </div>
                <span className="text-sm font-bold mt-1 ml-1 whitespace-nowrap">补齐 {pendingText}</span>
              </div>
            </button>
          ) : null}

          {/* 导出（旧版那张白卡片） */}
          <button
            onClick={onExport}
            disabled={exporting}
            className="flex flex-col items-start gap-1 p-4 bg-white hover:bg-gray-50 border border-gray-200 shadow-xl shadow-black/5 hover:-translate-y-0.5 transition-all duration-300 rounded-2xl w-52 text-gray-700 disabled:opacity-40 active:scale-[0.98] active:translate-y-0"
          >
            <div className="flex items-center gap-2">
              <div className="w-8 h-8 rounded-lg bg-gray-100 flex items-center justify-center text-gray-500">
                {exporting ? (
                  <svg className="animate-spin w-5 h-5" viewBox="0 0 24 24" fill="none">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                ) : (
                  <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 8h10M7 12h4m1 8l3-3h6a2 2 0 002-2V7a2 2 0 00-2-2H6a2 2 0 00-2 2v8a2 2 0 002 2h3l3 3z" />
                  </svg>
                )}
              </div>
              <span className="text-[10px] font-black uppercase tracking-[0.15em] text-gray-400">
                {exporting ? 'Exporting' : 'Export'}
              </span>
            </div>
            <span className="text-sm font-bold mt-1 ml-1">导出成片</span>
          </button>
        </div>

        {/* 右：撤销 / 助手 / 设置；收起态还带「Show Tools」药丸 */}
        <div className={`flex items-center transition-all duration-500 ${expanded ? 'gap-1' : 'gap-4'}`}>
          {!expanded && (
            <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-gray-400 flex items-center gap-1.5 cursor-pointer hover:text-claude-accent transition-colors border px-2 py-0.5 rounded-full border-gray-200 whitespace-nowrap">
              Show Tools
              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
              </svg>
            </span>
          )}

          <button
            onClick={onUndo}
            disabled={!undoSummary}
            title={undoSummary ? `撤销：${undoSummary}` : '没有可撤销的改动'}
            className={`
              flex items-center justify-center transition-all duration-300 hover:bg-gray-100 rounded-full text-gray-400
              disabled:opacity-25 disabled:hover:bg-transparent
              ${expanded ? 'w-12 h-12 bg-gray-50 text-gray-500' : 'w-6 h-6'}
            `}
          >
            <svg className={expanded ? 'w-6 h-6' : 'w-4 h-4'} fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 10h10a6 6 0 010 12H9M3 10l5-5m-5 5l5 5" />
            </svg>
          </button>

          <button
            onClick={toggleChat}
            title={chatOpen ? '收起助手' : '展开助手'}
            className={`
              flex items-center justify-center transition-all duration-300 rounded-full
              ${expanded
                ? `w-12 h-12 ${chatOpen ? 'bg-claude-accent/10 text-claude-accent' : 'bg-gray-50 text-gray-500 hover:bg-gray-100'}`
                : `w-6 h-6 ${chatOpen ? 'text-claude-accent' : 'text-gray-400 hover:text-claude-accent'}`}
            `}
          >
            <svg className={expanded ? 'w-6 h-6' : 'w-4 h-4'} fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M21 12c0 4.4-4 8-9 8a9.9 9.9 0 01-4-.8L3 20l1.3-3.9A7.6 7.6 0 013 12c0-4.4 4-8 9-8s9 3.6 9 8z" />
            </svg>
          </button>

          <button
            onClick={onOpenSettings}
            title="项目设置"
            className={`
              flex items-center justify-center transition-all duration-300 hover:bg-gray-100 rounded-full
              ${expanded ? 'w-12 h-12 bg-gray-50 text-gray-500 ml-1' : 'w-6 h-6 text-gray-400'}
            `}
          >
            <svg className={expanded ? 'w-6 h-6' : 'w-4 h-4'} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d={GEAR} />
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
          </button>
        </div>
      </header>
    </div>
  );
};

export default TopBar;
