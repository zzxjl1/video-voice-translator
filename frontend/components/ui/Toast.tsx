import React, { useEffect, useRef } from 'react';
import { t } from '../../i18n';

interface Props {
  message: string | null;
  onClose: () => void;
  tone?: 'info' | 'error';
  /** Auto-dismiss; pass 0 to keep until closed. */
  durationMs?: number;
}

/** Global top-right transient notice. Replaces the old notice bars. */
const Toast: React.FC<Props> = ({ message, onClose, tone = 'info', durationMs = 5000 }) => {
  // 调用方常传内联箭头函数（每次渲染都是新的）。onClose 放进依赖会让父组件每次重渲染——
  // 比如任务进度的事件——都把计时器重置，通知就永远不会自己消失。用 ref 拿最新的，依赖只留内容。
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!message || !durationMs) return;
    const timer = setTimeout(() => closeRef.current(), durationMs);
    return () => clearTimeout(timer);
  }, [message, durationMs]);

  if (!message) return null;
  const color = tone === 'error'
    ? 'border-red-200 text-red-700'
    : 'border-claude-border text-gray-700';

  return (
    <div data-i18n-skip className="fixed top-5 right-5 z-[100] w-[min(420px,calc(100vw-2.5rem))]">
      <div className={`flex items-start gap-3 rounded-2xl border bg-white/95 px-4 py-3 shadow-xl backdrop-blur ${color}`}>
        <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${tone === 'error' ? 'bg-red-500' : 'bg-claude-accent'}`} />
        <p className="min-w-0 flex-1 text-[12px] leading-relaxed break-words">{message}</p>
        <button
          type="button"
          onClick={onClose}
          aria-label={t('关闭通知')}
          className="shrink-0 rounded-md px-1.5 py-0.5 text-[11px] font-bold text-gray-400 hover:bg-gray-100 hover:text-gray-700"
        >
          ×
        </button>
      </div>
    </div>
  );
};

export default Toast;
