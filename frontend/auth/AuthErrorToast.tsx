import React from 'react';
import { t } from '@/i18n';
import { useAuth } from './AuthProvider';

/** Small dismissible banner for ?auth_error=... returning from Google. */
export const AuthErrorToast: React.FC = () => {
  const { error, clearError } = useAuth();
  if (!error) return null;
  return (
    <button
      type="button"
      onClick={clearError}
      className="fixed left-1/2 top-20 z-[120] -translate-x-1/2 rounded-2xl border border-red-100 bg-red-50 px-4 py-2.5 text-[12px] font-semibold text-red-600 shadow-lg"
    >
      {t('登录失败')}
    </button>
  );
};
