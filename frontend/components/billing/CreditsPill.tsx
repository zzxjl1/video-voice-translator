/**
 * 首页右上角的积分药丸 + 玻璃浮层（用户选定的方案：「② 全宽主按钮 + 文字次级」）。
 *
 * 只负责展示和两个入口；数据由调用方取（首页和编辑器共用同一个 /billing/summary）。
 * 浮层里的口径：积分是用户侧唯一单位，页面上不出现成本。
 */
import React, { useEffect, useRef, useState } from 'react';
import { t } from '@/i18n';

export interface CreditsSnapshot {
  balance: number;
  monthPoints: number;
  monthCalls: number;
}

const Coin = ({ className = 'h-3 w-3' }: { className?: string }) => (
  <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
    <circle cx="12" cy="12" r="9" />
    <path strokeLinecap="round" d="M9.5 8.8h4.2a2.1 2.1 0 010 4.2H9.5m0 0h4.6a2.2 2.2 0 010 4.4H9.5M12 6.2v11.6" />
  </svg>
);

/** 余额显示：保留到 2 位小数并去掉尾随 0（837.87 / 428.5 / 500）——一位小数会把 0.03 抹掉。 */
const fmt = (n: number) => n.toFixed(2).replace(/\.?0+$/, '');

interface Props {
  /** null = 还没取到（显示占位，不闪烁 0）。 */
  credits: CreditsSnapshot | null;
  onOpenDetail: () => void;
  onRecharge: () => void;
  className?: string;
}

const CreditsPill: React.FC<Props> = ({ credits, onOpenDetail, onRecharge, className = '' }) => {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  if (!credits) return null;

  // 条表示"本月消耗占（本月消耗 + 当前余额）的比例"——即消耗速度，不编造月额度。
  const total = credits.monthPoints + Math.max(credits.balance, 0);
  const pct = total > 0 ? Math.min(100, (credits.monthPoints / total) * 100) : 0;

  return (
    <div ref={rootRef} className={`relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="dialog"
        aria-expanded={open}
        title={t('积分余额')}
        className={`flex h-8 items-center gap-1 rounded-full border bg-white/90 px-2.5 text-[11px] font-bold text-claude-text shadow-sm transition-colors hover:border-claude-accent/40 hover:text-claude-accent ${
          open ? 'border-claude-accent/50 ring-2 ring-claude-accent/10' : 'border-[#e5e5e0]'
        }`}
      >
        <Coin className="h-3 w-3 text-claude-accent" />
        <span data-i18n-skip className="tabular-nums">{fmt(credits.balance)}</span>
      </button>

      {open && (
        <div
          role="dialog"
          className="absolute right-0 top-full z-[120] mt-2 w-[244px] rounded-2xl border border-white/70 bg-white/80 p-3.5 shadow-[0_12px_32px_rgba(45,45,45,0.10)] backdrop-blur-md"
        >
          <div className="flex items-center gap-1 text-[9px] font-bold uppercase tracking-[0.12em] text-gray-400">
            <Coin className="h-2.5 w-2.5 text-claude-accent" /> {t('积分')}
          </div>
          <div className="mt-0.5 flex items-baseline gap-1">
            <span data-i18n-skip className="font-serif text-[30px] font-bold leading-none text-claude-text">{fmt(credits.balance)}</span>
            <span className="text-[10px] text-gray-400">{t('可用')}</span>
          </div>

          <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-claude-paper">
            <div className="h-full rounded-full bg-claude-accent transition-[width]" style={{ width: `${pct}%` }} />
          </div>
          <div className="mt-1.5 flex justify-between text-[10px] text-gray-400">
            <span>
              {t('本月已用')} <span data-i18n-skip className="font-bold text-gray-600">{fmt(credits.monthPoints)}</span>
            </span>
            <span>{credits.monthCalls}×</span>
          </div>

          {/* 方案 ②：全宽主按钮 + 文字次级 */}
          <button
            type="button"
            onClick={() => { setOpen(false); onRecharge(); }}
            className="mt-3 w-full rounded-xl bg-claude-accent py-2 text-[11px] font-bold text-white shadow-sm shadow-claude-accent/25 transition-opacity hover:opacity-90"
          >
            {t('充值')}
          </button>
          <button
            type="button"
            onClick={() => { setOpen(false); onOpenDetail(); }}
            className="mt-1 flex w-full items-center justify-center gap-0.5 py-1 text-[10px] font-bold text-gray-500 transition-colors hover:text-claude-charcoal"
          >
            {t('费用明细')}
            <svg className="h-2.5 w-2.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={3}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
            </svg>
          </button>
        </div>
      )}
    </div>
  );
};

export default CreditsPill;
