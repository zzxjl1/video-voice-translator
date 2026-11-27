import React from 'react';
import { t } from '@/i18n';
import { LanguageSwitch } from '@/i18n';
import { useAuth } from './AuthProvider';

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (cfg: Record<string, unknown>) => void;
          prompt: () => void;
        };
      };
    };
  }
}

/**
 * Google One Tap：页面右上角的账号浮层。GIS 把签好的 ID Token（JWT）直接送进
 * 回调，POST 给后端 /api/auth/google/onetap 验签建 session——无跳转、无授权码。
 * 机会性：浏览器里登录着 Google 账号且不在冷却期才弹；不弹也不影响下面的
 * 「使用 Google 登录」跳转按钮（那是兜底）。
 */
const GoogleOneTap: React.FC = () => {
  React.useEffect(() => {
    let cancelled = false;
    fetch('/api/auth/me')
      .then((r) => r.json())
      .then((d) => {
        const clientId = d?.google_client_id;
        if (cancelled || !clientId) return;
        // GIS 脚本在 index.html 里 async 加载：轮询等它就绪（最多 5s）
        const t0 = Date.now();
        const timer = window.setInterval(() => {
          if (cancelled) { window.clearInterval(timer); return; }
          const gis = window.google?.accounts?.id;
          if (!gis) {
            if (Date.now() - t0 > 5000) window.clearInterval(timer);
            return;
          }
          window.clearInterval(timer);
          gis.initialize({
            client_id: clientId,
            auto_select: true,
            callback: (res: { credential?: string }) => {
              if (!res.credential) return;
              fetch('/api/auth/google/onetap', {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ credential: res.credential }),
              })
                .then((r) => { if (r.ok) window.location.reload(); })
                .catch(() => {});
            },
          });
          gis.prompt();
        }, 200);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);
  return null;
};

const GoogleMark: React.FC = () => (
  <svg className="h-5 w-5" viewBox="0 0 48 48" aria-hidden="true">
    <path fill="#EA4335" d="M24 9.5c3.54 0 6.7 1.22 9.2 3.6l6.86-6.86C35.98 2.38 30.48 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.2C12.43 13.72 17.74 9.5 24 9.5Z" />
    <path fill="#4285F4" d="M46.98 24.55c0-1.57-.14-3.09-.4-4.55H24v9.02h12.94c-.56 2.96-2.26 5.48-4.78 7.18l7.73 6C44.5 38.09 46.98 31.94 46.98 24.55Z" />
    <path fill="#FBBC05" d="M10.54 28.58a14.6 14.6 0 0 1 0-9.16l-7.98-6.2a24 24 0 0 0 0 21.56l7.98-6.2Z" />
    <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.9-5.8l-7.73-6c-2.15 1.45-4.92 2.3-8.17 2.3-6.26 0-11.57-4.22-13.46-9.92l-7.98 6.2C6.51 42.62 14.62 48 24 48Z" />
  </svg>
);

export const AuthGate: React.FC = () => {
  const { login, error, clearError } = useAuth();
  return (
    <div className="relative flex min-h-screen flex-col items-center justify-center bg-claude-bg p-6">
      <GoogleOneTap />
      <div className="absolute right-6 top-5 z-30">
        <LanguageSwitch />
      </div>
      <div className="w-full max-w-sm rounded-3xl border border-claude-border bg-white p-8 text-center shadow-sm">
        <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-claude-accent text-white shadow-lg shadow-claude-accent/20">
          <svg className="h-7 w-7" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 0 0 6-6v-1.5a6 6 0 0 0-6-6 6 6 0 0 0-6 6v1.5a6 6 0 0 0 6 6Z" />
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 12.75a3 3 0 0 0 3-3v-1.5a3 3 0 0 0-3-3 3 3 0 0 0-3 3v1.5a3 3 0 0 0 3 3Z" />
          </svg>
        </div>
        <h1 className="font-serif text-xl font-bold text-claude-text">{t('登录以继续')}</h1>
        <p className="mt-2 text-[12px] leading-relaxed text-gray-400">
          {t('登录后才能上传和导出视频')}
        </p>
        {error && (
          <button
            type="button"
            onClick={clearError}
            className="mt-4 w-full rounded-xl bg-red-50 px-3 py-2 text-left text-[11px] text-red-600"
          >
            {t('登录失败')}
          </button>
        )}
        <button
          type="button"
          onClick={() => login()}
          className="mt-6 flex w-full items-center justify-center gap-2.5 rounded-xl border border-[#e5e5e0] bg-white px-4 py-3 text-[13px] font-bold text-gray-700 shadow-sm transition-colors hover:border-claude-accent/40 hover:text-claude-accent"
        >
          <GoogleMark />
          {t('使用 Google 登录')}
        </button>
        <div className="mt-5 flex justify-center gap-4 text-[10px] text-gray-400">
          <a href="/privacy" className="hover:text-claude-accent">Privacy</a>
          <a href="/terms" className="hover:text-claude-accent">Terms</a>
        </div>
      </div>
    </div>
  );
};
