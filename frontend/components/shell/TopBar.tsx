/**
 * 编辑页顶栏。
 *
 * 旧版的“重新处理 / 从头开始”两个入口在这里没有了：那是旧管线（整条重跑）的概念。现在每个
 * 动作都是粒度很细的命令，“只补缺的”由服务端按指纹判断 —— 所以这里是：语言设置、撤销、
 * 待办提示（还差 N 句译文 / M 句配音，点一下补齐）、导出。
 */
import React from 'react';
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
  connection: 'connecting' | 'open' | 'reconnecting' | 'closed';
  exporting: boolean;
  toggleChat: () => void;
  chatOpen: boolean;
}

const TopBar: React.FC<Props> = ({
  filename, targetLanguage, onLanguageChange, needs, dubbing, jobs, undoSummary, onUndo, onFill, onCancelJobs,
  onOpenSettings, onExport, onHome, connection, exporting, toggleChat, chatOpen,
}) => {
  const running = jobs.length > 0;
  const pending = needs.translate + (dubbing ? needs.dub : 0);
  const job = jobs[0];

  return (
    <header className="h-16 shrink-0 bg-white/80 backdrop-blur-md border-b border-claude-border flex items-center justify-between px-5 lg:px-8 gap-4 z-20">
      <div className="flex items-center gap-4 min-w-0">
        <button onClick={onHome} className="flex items-center gap-2.5 group shrink-0" title="返回首页">
          <div className="w-8 h-8 rounded-lg bg-claude-accent flex items-center justify-center shadow-sm shadow-claude-accent/30 group-hover:scale-105 transition-transform">
            <svg className="w-4.5 h-4.5 text-white" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor" style={{ width: 18, height: 18 }}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 0 0 6-6v-1.5m-6 7.5a6 6 0 0 1-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 0 1-3-3V4.5a3 3 0 1 1 6 0v8.25a3 3 0 0 1-3 3Z" />
            </svg>
          </div>
        </button>
        <div className="min-w-0">
          <div className="text-[13px] font-serif font-bold text-claude-text truncate max-w-[26ch]" title={filename}>{filename}</div>
          <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-gray-400">
            <span className={`w-1.5 h-1.5 rounded-full ${connection === 'open' ? 'bg-emerald-400' : connection === 'closed' ? 'bg-gray-300' : 'bg-amber-400 animate-pulse'}`} />
            {connection === 'open' ? '已连接' : connection === 'reconnecting' ? '重连中' : connection === 'connecting' ? '连接中' : '离线'}
          </div>
        </div>
      </div>

      <div className="flex items-center gap-2.5">
        {running ? (
          <div className="hidden md:flex items-center gap-2.5 pl-3 pr-1.5 py-1.5 rounded-xl bg-claude-accent/5 border border-claude-accent/20">
            <span className="w-3 h-3 rounded-full border-2 border-claude-accent/30 border-t-claude-accent animate-spin" />
            <span className="text-[11px] font-bold text-claude-accent">
              {job.label}{job.total > 0 ? ` ${job.done}/${job.total}` : ''}
            </span>
            <button onClick={onCancelJobs} className="text-[10px] font-bold text-gray-400 hover:text-red-500 px-2 py-1 rounded-lg hover:bg-white transition-colors">
              取消
            </button>
          </div>
        ) : pending > 0 ? (
          <button
            onClick={onFill}
            className="hidden md:flex items-center gap-2 px-3 py-2 rounded-xl bg-amber-50 border border-amber-200 text-amber-700 hover:bg-amber-100 transition-colors text-[11px] font-bold"
            title="只补缺的和已过期的，已经好的不会重做"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
            {[needs.translate ? `${needs.translate} 句译文` : '', dubbing && needs.dub ? `${needs.dub} 句配音` : ''].filter(Boolean).join(' · ')} 待更新
            <span className="underline underline-offset-2">一键补齐</span>
          </button>
        ) : null}

        <button
          onClick={onUndo}
          disabled={!undoSummary}
          className="p-2 rounded-xl text-gray-400 hover:text-claude-accent hover:bg-claude-paper disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-gray-400 transition-colors"
          title={undoSummary ? `撤销：${undoSummary}` : '没有可撤销的改动'}
        >
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 10h10a6 6 0 010 12H9M3 10l5-5m-5 5l5 5" /></svg>
        </button>

        <select
          value={targetLanguage}
          onChange={(e) => onLanguageChange(e.target.value)}
          className="text-[12px] font-bold bg-white border border-claude-border rounded-xl px-3 py-2 outline-none focus:border-claude-accent/50 cursor-pointer"
          title="目标语言（更改会让译文和配音过期）"
        >
          {ALL_LANGUAGES.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
        </select>

        <button onClick={onOpenSettings} className="p-2 rounded-xl text-gray-400 hover:text-claude-accent hover:bg-claude-paper transition-colors" title="设置">
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M10.3 4.3a1.7 1.7 0 013.4 0 1.7 1.7 0 002.5 1l.1-.1a1.7 1.7 0 012.4 2.4l-.1.1a1.7 1.7 0 001 2.5 1.7 1.7 0 010 3.4 1.7 1.7 0 00-1 2.5l.1.1a1.7 1.7 0 01-2.4 2.4l-.1-.1a1.7 1.7 0 00-2.5 1 1.7 1.7 0 01-3.4 0 1.7 1.7 0 00-2.5-1l-.1.1a1.7 1.7 0 01-2.4-2.4l.1-.1a1.7 1.7 0 00-1-2.5 1.7 1.7 0 010-3.4 1.7 1.7 0 001-2.5l-.1-.1a1.7 1.7 0 012.4-2.4l.1.1a1.7 1.7 0 002.5-1z" /><circle cx="12" cy="12" r="3" strokeWidth={1.8} /></svg>
        </button>

        <button
          onClick={toggleChat}
          className={`p-2 rounded-xl transition-colors ${chatOpen ? 'text-claude-accent bg-claude-accent/10' : 'text-gray-400 hover:text-claude-accent hover:bg-claude-paper'}`}
          title={chatOpen ? '收起助手' : '展开助手'}
        >
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M8 10h.01M12 10h.01M16 10h.01M21 12c0 4.4-4 8-9 8a9.9 9.9 0 01-4-.8L3 20l1.3-3.9A7.6 7.6 0 013 12c0-4.4 4-8 9-8s9 3.6 9 8z" /></svg>
        </button>

        <button
          onClick={onExport}
          disabled={exporting}
          className="px-5 py-2.5 rounded-xl bg-claude-accent hover:bg-claude-accentHover text-white text-xs font-bold shadow-md shadow-claude-accent/20 transition-all active:scale-[0.98] disabled:opacity-50"
        >
          {exporting ? '导出中…' : '导出'}
        </button>
      </div>
    </header>
  );
};

export default TopBar;
