/**
 * The "recent projects" memory.
 *
 * There is no login, so this browser's localStorage is the INDEX and the
 * server's `data/<videoId>/` directory is the only fact source. What is stored
 * here is which projects this browser has touched — nothing more. Everything
 * shown to the user (filename, status) is re-validated against the server
 * before display, and an entry the server no longer has is dropped rather than
 * shown as a dead link.
 *
 * Deliberately not a server-side "list all projects": any visitor who obtains a
 * videoId can open that project, so a server-wide index would hand every
 * project to everyone. localStorage keeps the list per browser, which is the
 * correct scope until the app has users.
 */

const KEY = 'vvt.recentProjects';
const MAX_ENTRIES = 8;

interface StoredEntry {
  videoId: string;
  /** Backfilled from the server at validation time; may be empty. */
  filename: string;
  /** Epoch ms of the last time this project was opened in this browser. */
  updatedAt: number;
}

export interface RecentProject extends StoredEntry {
  /** Server status at validation time; absent when the server was unreachable. */
  status?: string;
}

function read(): StoredEntry[] {
  try {
    const raw = window.localStorage.getItem(KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed)
      ? parsed.filter(e => e && typeof e.videoId === 'string')
      : [];
  } catch {
    // Corrupt or unavailable storage — the list is a convenience, not a record.
    return [];
  }
}

function write(entries: StoredEntry[]): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(entries.slice(0, MAX_ENTRIES)));
  } catch {
    // Full or blocked storage: skip saving. Next touch tries again.
  }
}

/** Record that this project was just opened in this browser; newest first. */
export function touchRecentProject(videoId: string, filename = ''): void {
  if (!videoId) return;
  const rest = read().filter(entry => entry.videoId !== videoId);
  write([{ videoId, filename, updatedAt: Date.now() }, ...rest]);
}

export function forgetRecentProject(videoId: string): void {
  write(read().filter(entry => entry.videoId !== videoId));
}

/**
 * Validate the stored list against the server and return it display-ready.
 *
 * Per entry: the lookup resolving means backfill the filename and carry the
 * live status; it returning null means 404 — the server no longer has the
 * project (cleaned up, or created on another machine), drop it; the lookup
 * THROWING means the server is merely unreachable — keep the entry without a
 * status, so a temporarily down backend does not erase the user's history.
 *
 * `lookup` is injected rather than imported so this module stays decoupled from
 * the API service and tests can pass a stub.
 */
export async function loadRecentProjects(
  lookup: (videoId: string) => Promise<{ filename?: string; status?: string } | null>,
): Promise<RecentProject[]> {
  const validated = await Promise.all(
    read().map(async entry => {
      try {
        const data = await lookup(entry.videoId);
        if (!data) return null;
        return { ...entry, filename: data.filename || entry.filename, status: data.status };
      } catch {
        return { ...entry };
      }
    }),
  );
  const kept = validated.filter((entry): entry is RecentProject => entry !== null);
  write(kept.map(({ videoId, filename, updatedAt }) => ({ videoId, filename, updatedAt })));
  return kept.sort((a, b) => b.updatedAt - a.updatedAt);
}

/** "3 分钟前". Coarse on purpose — this label only orders a list. */
export function formatRelativeTime(epochMs: number): string {
  const seconds = Math.max(0, Math.floor((Date.now() - epochMs) / 1000));
  if (seconds < 60) return '刚刚';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  return new Date(epochMs).toLocaleDateString();
}
