/**
 * 聊天面板：和 AI 一起微调。
 *
 * 一条消息流里有四种东西：
 *   用户消息   右对齐的暖色气泡
 *   AI 消息    左对齐；上面可以带"这一轮做了什么"的步骤；行号 #12 是可点击的标签
 *   确认卡片   花钱 / 推翻已有工作的操作，AI 请求后先在这里等你点
 *   系统通知   你在界面上做的操作（灰色小字）—— 让你看到 AI 会看到什么
 *
 * 视觉沿用现有风格：暖色 `claude-*` 色板、`rounded-2xl` 卡片、
 * `text-[10px] font-bold uppercase tracking-widest` 的小标签、内联 Heroicons。
 */
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ChatMessage } from '../../net/types';
import { pendingConfirms, stepLabel, type ChatState, type ToolStep } from '../../store/chat';
import { ConfirmCard } from './ConfirmCard';
import { RichText } from './RichText';

interface Props {
  chat: ChatState;
  /** 总行数：行号标签据此判断是否有效。 */
  lineCount: number;
  disabled?: boolean;
  disabledReason?: string;
  onSend: (text: string) => void;
  onStop: () => void;
  onConfirm: (cardId: string, approve: boolean) => void;
  onFocusLine: (line: number) => void;
  /** 输入框里的预填内容（比如从脚本里"引用到聊天"）。 */
  draft?: string;
  onDraftConsumed?: () => void;
  suggestions?: string[];
}

const ChatPanel: React.FC<Props> = ({
  chat, lineCount, disabled, disabledReason, onSend, onStop, onConfirm, onFocusLine,
  draft, onDraftConsumed, suggestions = [],
}) => {
  const [text, setText] = useState('');
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const stickRef = useRef(true);                 // 用户没往上翻时，新内容自动滚到底

  useEffect(() => {
    if (draft) {
      setText((prev) => (prev ? `${prev} ${draft}` : draft));
      onDraftConsumed?.();
      inputRef.current?.focus();
    }
  }, [draft, onDraftConsumed]);

  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  useLayoutEffect(() => {
    if (stickRef.current) bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [chat.messages.length, chat.streaming?.text, chat.streaming?.steps.length]);

  const waiting = pendingConfirms(chat).length > 0;
  const submit = () => {
    const value = text.trim();
    if (!value || disabled) return;
    stickRef.current = true;
    onSend(value);
    setText('');
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter 发送，Shift+Enter 换行；输入法组词（IME）期间的 Enter 是"选词"，不能发送
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const empty = chat.messages.length === 0 && !chat.streaming;

  return (
    <div className="flex flex-col h-full bg-[#fbfbf9] border border-[#e5e5e0] rounded-3xl overflow-hidden shadow-sm">
      <div className="px-6 py-5 border-b border-[#e5e5e0] flex items-center justify-between bg-white/60 backdrop-blur-md">
        <h3 className="text-xs font-serif font-bold text-gray-700 flex items-center gap-2 tracking-wide">
          <span className={`w-1.5 h-1.5 rounded-full ${chat.busy ? 'bg-claude-accent animate-pulse' : 'bg-claude-accent'}`} />
          ASSISTANT
        </h3>
        {chat.busy && (
          <span className="text-[10px] uppercase font-bold tracking-tighter text-claude-accent/80">
            {waiting ? '等你确认' : '处理中'}
          </span>
        )}
      </div>

      <div ref={scrollerRef} onScroll={onScroll} className="flex-grow overflow-y-auto px-5 py-5 space-y-4">
        {empty && (
          <div className="h-full flex flex-col items-center justify-center text-center gap-3 opacity-60 px-6">
            <div className="w-12 h-12 rounded-full border border-dashed border-gray-300 flex items-center justify-center">
              <svg className="w-6 h-6 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1} d="M8 10h.01M12 10h.01M16 10h.01M21 12c0 4.418-4.03 8-9 8a9.86 9.86 0 01-4-.8L3 20l1.3-3.9A7.6 7.6 0 013 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
              </svg>
            </div>
            <p className="text-xs text-gray-400 leading-relaxed">说说你想怎么改。比如「第 3 句翻译太生硬」</p>
          </div>
        )}

        {chat.messages.map((m) => (
          <Message key={m.id} message={m} lineCount={lineCount} onConfirm={onConfirm} onFocusLine={onFocusLine} />
        ))}

        {chat.streaming && (
          <div className="flex flex-col gap-2 items-start">
            {chat.streaming.steps.length > 0 && <Steps steps={chat.streaming.steps} />}
            {chat.streaming.text ? (
              <Bubble role="assistant"><RichText text={chat.streaming.text} lineCount={lineCount} onFocusLine={onFocusLine} /></Bubble>
            ) : chat.streaming.steps.length === 0 ? (
              <Bubble role="assistant"><Dots /></Bubble>
            ) : null}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {suggestions.length > 0 && chat.messages.length <= 1 && !chat.busy && (
        <div className="px-5 pb-2 flex flex-wrap gap-2">
          {suggestions.map((s) => (
            <button
              key={s}
              onClick={() => onSend(s)}
              disabled={disabled}
              className="text-[11px] px-3 py-1.5 rounded-full border border-claude-border bg-white text-gray-600 hover:text-claude-accent hover:border-claude-accent/40 transition-colors disabled:opacity-40"
            >
              {s}
            </button>
          ))}
        </div>
      )}

      <div className="p-4 border-t border-[#e5e5e0] bg-white/60">
        {disabled && disabledReason && <p className="text-[10px] text-gray-400 mb-2 px-1">{disabledReason}</p>}
        <div className="flex items-end gap-2 bg-white border border-claude-border rounded-2xl px-3 py-2 focus-within:border-claude-accent/50 focus-within:ring-2 focus-within:ring-claude-accent/10 transition-all">
          <textarea
            ref={inputRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            rows={1}
            disabled={disabled}
            placeholder={waiting ? '先处理上面的确认，或直接说别的…' : '告诉我哪里要改…'}
            className="flex-grow resize-none bg-transparent outline-none text-sm leading-relaxed max-h-32 py-1.5 placeholder:text-gray-300 disabled:opacity-50"
            style={{ fieldSizing: 'content' } as React.CSSProperties}
          />
          {chat.busy ? (
            <button
              onClick={onStop}
              className="shrink-0 w-9 h-9 rounded-xl border border-claude-border text-gray-500 hover:text-red-500 hover:border-red-200 flex items-center justify-center transition-colors"
              title="停止"
            >
              <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="2" /></svg>
            </button>
          ) : (
            <button
              onClick={submit}
              disabled={!text.trim() || disabled}
              className="shrink-0 w-9 h-9 rounded-xl bg-claude-accent text-white hover:bg-claude-accentHover disabled:opacity-30 disabled:cursor-not-allowed flex items-center justify-center transition-colors shadow-sm shadow-claude-accent/20"
              title="发送 (Enter)"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.2} d="M5 12h14M13 6l6 6-6 6" /></svg>
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

// ----------------------------------------------------------------------------

const Message: React.FC<{
  message: ChatMessage;
  lineCount: number;
  onConfirm: (id: string, approve: boolean) => void;
  onFocusLine: (line: number) => void;
}> = React.memo(({ message: m, lineCount, onConfirm, onFocusLine }) => {
  if (m.role === 'user') {
    return (
      <div className="flex justify-end">
        <Bubble role="user"><span className="whitespace-pre-wrap">{m.text}</span></Bubble>
      </div>
    );
  }
  if (m.role === 'confirm') return <ConfirmCard message={m} onResolve={onConfirm} />;
  if (m.role === 'error') {
    return <div className="text-[11px] text-red-500 bg-red-50 border border-red-100 rounded-xl px-3 py-2">{m.text}</div>;
  }
  const actions = m.actions ?? [];
  return (
    <div className="flex flex-col gap-2 items-start">
      {actions.length > 0 && <DoneSteps actions={actions} />}
      {m.text && (
        <Bubble role="assistant" system={m.system}>
          <RichText text={m.text} lineCount={lineCount} onFocusLine={onFocusLine} />
        </Bubble>
      )}
    </div>
  );
});

const Bubble: React.FC<{ role: 'user' | 'assistant'; system?: boolean; children: React.ReactNode }> = ({ role, system, children }) => (
  <div
    className={`max-w-[92%] px-4 py-3 text-[13px] leading-relaxed rounded-2xl ${
      role === 'user'
        ? 'bg-claude-accent text-white rounded-br-md shadow-sm shadow-claude-accent/20'
        : system
          ? 'bg-claude-paper/70 border border-claude-border/70 text-claude-text rounded-bl-md'
          : 'bg-white border border-claude-border text-claude-text rounded-bl-md shadow-sm'
    }`}
  >
    {children}
  </div>
);

const Dots: React.FC = () => (
  <div className="flex gap-1 py-1">
    <span className="w-1.5 h-1.5 bg-gray-300 rounded-full animate-bounce [animation-delay:-0.3s]" />
    <span className="w-1.5 h-1.5 bg-gray-300 rounded-full animate-bounce [animation-delay:-0.15s]" />
    <span className="w-1.5 h-1.5 bg-gray-300 rounded-full animate-bounce" />
  </div>
);

const Steps: React.FC<{ steps: ToolStep[] }> = ({ steps }) => (
  <div className="flex flex-col gap-1 pl-1">
    {steps.map((s) => (
      <div key={s.id} className="flex items-center gap-2 text-[11px] text-gray-500">
        {s.status === 'running' ? (
          <span className="w-3 h-3 rounded-full border-2 border-claude-accent/30 border-t-claude-accent animate-spin" />
        ) : s.status === 'ok' ? (
          <svg className="w-3 h-3 text-emerald-500" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>
        ) : (
          <svg className="w-3 h-3 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M6 18L18 6M6 6l12 12" /></svg>
        )}
        <span className={s.status === 'failed' ? 'text-red-500' : ''}>{stepLabel(s)}</span>
        {s.status === 'failed' && s.error && <span className="text-red-400 truncate max-w-[200px]" title={s.error}>— {s.error}</span>}
      </div>
    ))}
  </div>
);

const DoneSteps: React.FC<{ actions: NonNullable<ChatMessage['actions']> }> = ({ actions }) => {
  const [open, setOpen] = useState(false);
  const labels = useMemo(() => actions.map((a) => a.label ?? a.name), [actions]);
  return (
    <button
      onClick={() => setOpen((v) => !v)}
      className="text-left pl-1 text-[10px] font-bold uppercase tracking-widest text-gray-400 hover:text-claude-accent transition-colors"
    >
      <span className="inline-flex items-center gap-1.5">
        <svg className={`w-2.5 h-2.5 transition-transform ${open ? 'rotate-90' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M9 5l7 7-7 7" /></svg>
        做了 {actions.length} 件事
      </span>
      {open && (
        <ul className="mt-1.5 space-y-0.5 normal-case tracking-normal font-normal text-[11px] text-gray-500">
          {labels.map((l, i) => <li key={i}>· {l}</li>)}
        </ul>
      )}
    </button>
  );
};

export default ChatPanel;
