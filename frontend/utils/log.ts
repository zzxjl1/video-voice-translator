/**
 * Small structured browser logger. Keep it dependency-free so it can be used
 * from the API layer, event stream, React pages and the browser TTS worker.
 */
type Level = 'debug' | 'info' | 'warn' | 'error';

const PREFIX = '[vvt]';
const LEVEL_STYLE: Record<Level, string> = {
  debug: 'color:#9ca3af',
  info: 'color:#8b5cf6',
  warn: 'color:#d97706',
  error: 'color:#dc2626',
};

function emit(level: Level, scope: string, message: string, data?: unknown): void {
  const args: unknown[] = [`%c${PREFIX}[${scope}] ${message}`, LEVEL_STYLE[level]];
  if (data !== undefined) args.push(data);
  if (level === 'error') console.error(...args);
  else if (level === 'warn') console.warn(...args);
  else if (level === 'debug') console.debug(...args);
  else console.info(...args);
}

export const log = {
  debug: (scope: string, message: string, data?: unknown) => emit('debug', scope, message, data),
  info: (scope: string, message: string, data?: unknown) => emit('info', scope, message, data),
  warn: (scope: string, message: string, data?: unknown) => emit('warn', scope, message, data),
  error: (scope: string, message: string, data?: unknown) => emit('error', scope, message, data),
};

/** Never put session/query tokens or signatures in a log line. */
const SECRET_PARAM = /token|sig|signature|secret|key|auth|credential|password|code|state|expires|^v$/i;

export function safeUrl(input: string): string {
  try {
    const url = new URL(input, 'http://local');
    for (const key of [...url.searchParams.keys()]) {
      if (SECRET_PARAM.test(key)) url.searchParams.set(key, '…');
    }
    return `${url.pathname}${url.search}`;
  } catch {
    return input.replace(/([?&](?:token|sig|signature|key|auth_continue)=)[^&]+/gi, '$1…');
  }
}

export function shortId(id: string | null | undefined): string {
  if (!id) return '—';
  return id.length > 10 ? `${id.slice(0, 8)}…` : id;
}
