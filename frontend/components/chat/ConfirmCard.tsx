/**
 * 确认卡片：AI 想做一件会花钱 / 会推翻已有工作的事，先在这里等你点。
 *
 * 取代旧版的 11 处 `window.confirm`/`alert`：那些会阻塞线程，AI 没法驱动；这里是一条
 * 普通的聊天消息，状态（待确认 / 已确认 / 已拒绝）随事件流同步，刷新页面还在。
 */
import React from 'react';
import type { ChatMessage } from '../../net/types';
import { COMMAND_LABEL } from '../../store/chat';

const LEVEL: Record<string, { tag: string; tone: string; hint: string }> = {
  cost: { tag: '会花钱 / 花时间', tone: 'text-amber-700 bg-amber-50 border-amber-200', hint: '会调用外部服务，产生费用。' },
  destroy: { tag: '会推翻已有内容', tone: 'text-red-600 bg-red-50 border-red-200', hint: '已有的译文或配音会因此作废。' },
};

function summarizeArgs(args: Record<string, unknown> | undefined): string[] {
  if (!args) return [];
  return Object.entries(args).map(([k, v]) => {
    const value = Array.isArray(v) ? `第 ${v.join('、')} 句` : typeof v === 'string' ? `「${v}」` : String(v);
    const name = ({ lines: '范围', line: '范围', target_language: '目标语言', length: '长度', instruction: '要求', speaker: '说话人',
      accent: '方言', enable_dubbing: '配音', force: '强制重做' } as Record<string, string>)[k] ?? k;
    return `${name}：${value}`;
  });
}

export const ConfirmCard: React.FC<{ message: ChatMessage; onResolve: (id: string, approve: boolean) => void }> = ({ message, onResolve }) => {
  const level = LEVEL[message.level ?? 'cost'];
  const resolved = message.status === 'approved' || message.status === 'rejected';
  const label = COMMAND_LABEL[message.command ?? ''] ?? message.command;
  const details = summarizeArgs(message.args);

  return (
    <div className={`rounded-2xl border bg-white shadow-sm overflow-hidden ${resolved ? 'opacity-60' : 'border-claude-accent/30'}`}>
      <div className="px-4 py-3 flex items-start gap-3">
        <div className="shrink-0 mt-0.5 w-7 h-7 rounded-full bg-claude-accent/10 text-claude-accent flex items-center justify-center">
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" /></svg>
        </div>
        <div className="min-w-0 flex-grow">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[13px] font-bold">{label}</span>
            <span className={`text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded-md border ${level.tone}`}>{level.tag}</span>
          </div>
          <p className="text-[11px] text-gray-500 mt-1">{message.description?.split('。')[0]}。{level.hint}</p>
          {details.length > 0 && (
            <ul className="mt-2 text-[11px] text-gray-600 space-y-0.5">
              {details.map((d) => <li key={d}>· {d}</li>)}
            </ul>
          )}
        </div>
      </div>
      {resolved ? (
        <div className="px-4 py-2 bg-claude-paper/50 border-t border-claude-border/60 text-[10px] font-bold uppercase tracking-widest text-gray-400">
          {message.status === 'approved' ? '已确认' : '已拒绝'}
        </div>
      ) : (
        <div className="px-4 py-2.5 bg-claude-paper/40 border-t border-claude-border/60 flex justify-end gap-2">
          <button
            onClick={() => onResolve(message.id, false)}
            className="px-3.5 py-1.5 rounded-xl text-[11px] font-bold text-gray-500 hover:bg-white border border-transparent hover:border-claude-border transition-colors"
          >
            取消
          </button>
          <button
            onClick={() => onResolve(message.id, true)}
            className="px-3.5 py-1.5 rounded-xl text-[11px] font-bold bg-claude-accent text-white hover:bg-claude-accentHover shadow-sm shadow-claude-accent/20 transition-colors"
          >
            确认执行
          </button>
        </div>
      )}
    </div>
  );
};
