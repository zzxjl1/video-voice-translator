import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';
import * as api from '../net/client';
import type { AuthUser } from '../net/types';

interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  error: string | null;
  login: (returnTo?: string) => void;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  clearError: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

const safeReturnTo = (value: string): string => {
  if (!value.startsWith('/') || value.startsWith('//') || value.includes('\\')) return '/';
  return value;
};

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const result = await api.me();
      setUser(result.user);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('auth_continue') === '1') {
      const target = safeReturnTo(params.get('auth_return_to') || '/');
      params.delete('auth_continue');
      params.delete('auth_return_to');
      const query = params.toString();
      window.history.replaceState(null, '', window.location.pathname + (query ? `?${query}` : ''));
      window.location.assign(`/api/auth/google/start?return_to=${encodeURIComponent(target)}`);
      return;
    }
    const authError = params.get('auth_error');
    if (authError) {
      setError(authError);
      params.delete('auth_error');
      const query = params.toString();
      window.history.replaceState(null, '', window.location.pathname + (query ? `?${query}` : ''));
    }
    void refresh();
  }, [refresh]);

  const login = useCallback((returnTo?: string) => {
    const target = safeReturnTo(returnTo ?? `${window.location.pathname}${window.location.search}`);
    // Google's web redirect URI is localhost, not 127.0.0.1. If the dev runner
    // printed 127.0.0.1, hop to localhost first so the OAuth browser-binding
    // cookie and callback cookie share the same host.
    if (window.location.hostname === '127.0.0.1' || window.location.hostname === '[::1]') {
      const next = new URL(window.location.href);
      next.hostname = 'localhost';
      next.searchParams.set('auth_continue', '1');
      next.searchParams.set('auth_return_to', target);
      window.location.assign(next.toString());
      return;
    }
    window.location.assign(`/api/auth/google/start?return_to=${encodeURIComponent(target)}`);
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      // The local state is still cleared; the server-side session may already be gone.
    }
    setUser(null);
    window.location.assign('/');
  }, []);

  return (
    <AuthContext.Provider value={{
      user, loading, error, login, logout, refresh,
      clearError: () => setError(null),
    }}>
      {children}
    </AuthContext.Provider>
  );
};

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside AuthProvider');
  return value;
}
