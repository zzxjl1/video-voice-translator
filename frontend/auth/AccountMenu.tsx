import React, { useEffect, useRef, useState } from 'react';
import { t } from '@/i18n';
import { useAuth } from './AuthProvider';
import { billing as billingApi } from '../net/client';
import type { BillingSummary } from '../net/types';
import BillingPanel from '../components/billing/BillingPanel';

const GoogleMark: React.FC<{ className?: string }> = ({ className = 'h-4 w-4' }) => (
  <svg className={className} viewBox="0 0 48 48" aria-hidden="true">
    <path fill="#EA4335" d="M24 9.5c3.54 0 6.7 1.22 9.2 3.6l6.86-6.86C35.98 2.38 30.48 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.2C12.43 13.72 17.74 9.5 24 9.5Z" />
    <path fill="#4285F4" d="M46.98 24.55c0-1.57-.14-3.09-.4-4.55H24v9.02h12.94c-.56 2.96-2.26 5.48-4.78 7.18l7.73 6C44.5 38.09 46.98 31.94 46.98 24.55Z" />
    <path fill="#FBBC05" d="M10.54 28.58a14.6 14.6 0 0 1 0-9.16l-7.98-6.2a24 24 0 0 0 0 21.56l7.98-6.2Z" />
    <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.9-5.8l-7.73-6c-2.15 1.45-4.92 2.3-8.17 2.3-6.26 0-11.57-4.22-13.46-9.92l-7.98 6.2C6.51 42.62 14.62 48 24 48Z" />
  </svg>
);

export const AccountMenu: React.FC<{ className?: string }> = ({ className = '' }) => {
  const { user, login, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [money, setMoney] = useState<BillingSummary | null>(null);
  const [showBilling, setShowBilling] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // 余额只在打开菜单时取一次：账本变化不频繁，没必要轮询。
  useEffect(() => {
    if (!open || !user) return;
    let alive = true;
    billingApi.summary().then((s) => { if (alive) setMoney(s); }).catch(() => { /* 没登录/接口不可用时不打扰 */ });
    return () => { alive = false; };
  }, [open, user]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  if (!user) {
    return (
      <button
        type="button"
        onClick={() => login()}
        className={`flex h-8 items-center gap-1.5 rounded-full border border-[#e5e5e0] bg-white/90 px-2.5 text-[11px] font-bold text-gray-600 shadow-sm transition-colors hover:border-claude-accent/40 hover:text-claude-accent ${className}`}
      >
        <GoogleMark />
        <span>{t('登录')}</span>
      </button>
    );
  }

  const initial = (user.name || user.email || '?').trim().charAt(0).toUpperCase();

  return (
    <div ref={rootRef} className={`relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={t('账号')}
        className={`flex h-8 w-8 items-center justify-center overflow-hidden rounded-full border border-[#e5e5e0] bg-white text-[11px] font-black text-claude-accent shadow-sm transition-colors hover:border-claude-accent/50 ${open ? 'border-claude-accent/60 ring-2 ring-claude-accent/10' : ''}`}
      >
        {user.picture ? (
          <img
            src={user.picture}
            alt=""
            referrerPolicy="no-referrer"
            className="h-full w-full object-cover"
          />
        ) : initial}
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full z-[100] mt-2 w-64 overflow-hidden rounded-2xl border border-[#e5e5e0] bg-white shadow-xl shadow-black/10"
        >
          <div data-i18n-skip className="border-b border-[#f0f0eb] px-4 py-3">
            <div className="truncate text-[13px] font-bold text-claude-text">{user.name || user.email || t('账号')}</div>
            {user.email && <div className="mt-0.5 truncate text-[11px] text-gray-400">{user.email}</div>}
            <div className="mt-2 flex items-baseline gap-3 text-[11px]">
              <span className="text-gray-400">{t('积分余额')}</span>
              <span data-i18n-skip className="font-bold text-claude-text">
                {money ? `${money.balance_points.toFixed(2)} ${t('积分')}` : '—'}
              </span>
              <button
                type="button"
                onClick={() => { setOpen(false); setShowBilling(true); }}
                className="ml-auto font-bold text-claude-accent hover:underline"
              >
                {t('费用明细')}
              </button>
            </div>
          </div>
          <button
            type="button"
            role="menuitem"
            onClick={() => { setOpen(false); void logout(); }}
            className="flex w-full items-center gap-2 px-4 py-3 text-left text-[12px] font-bold text-gray-600 transition-colors hover:bg-claude-paper hover:text-claude-accent"
          >
            {t('退出登录')}
          </button>
          <div className="flex gap-3 border-t border-[#f0f0eb] px-4 py-2 text-[10px] text-gray-400">
            <a href="/privacy" className="hover:text-claude-accent">Privacy</a>
            <a href="/terms" className="hover:text-claude-accent">Terms</a>
          </div>
        </div>
      )}
      <BillingPanel open={showBilling} onClose={() => setShowBilling(false)} />
    </div>
  );
};
