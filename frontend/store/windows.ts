/**
 * 窗口布局：脚本与译文 / AI 助手 / 字幕样式 三个面板，各自是一个窗口。
 *
 *   · 吸附（docked）：固定在右栏那一个"停靠位"里，大小由布局决定，不能调。标签条只列已打开的
 *     停靠窗口，一次显示一个；译文 / 助手是常驻窗口（一直开着、关不掉），字幕样式默认不打开。
 *   · 浮动（floating）：自由位置、自由大小（四边四角都能拖），点哪个哪个在最上面。
 *   · 关闭（open=false）：收起。窗口的位置、吸附与否都记着，再打开回到原样；常驻窗口不适用。
 *
 * 这里全是纯函数（没有 React / DOM），所以能在 node 里直接测：状态转移、缩放几何、吸附判定、
 * 存取。拖动时的指针事件在 hooks/useWindows.ts。
 */

export type PanelId = 'script' | 'chat' | 'subtitle';
export const PANEL_IDS: readonly PanelId[] = ['script', 'chat', 'subtitle'];
/** 常驻窗口：一直开着，没有关闭按钮，所以右栏的译文 / 助手切换永远都在。 */
export const PINNED: readonly PanelId[] = ['script', 'chat'];

export interface Rect { x: number; y: number; w: number; h: number }
export interface Viewport { w: number; h: number }

export interface PanelWin {
  open: boolean;
  docked: boolean;
  /** 浮动时的位置和大小；吸附时不用，但记着，弹出来时回到这里。 */
  rect: Rect;
  /** 浮动窗口的叠放顺序（1 起，越大越靠上）。吸附的窗口为 0。 */
  z: number;
}

export interface Layout {
  panels: Record<PanelId, PanelWin>;
  /** 停靠位里当前显示哪一个（只在有多个窗口吸附时有意义）。 */
  active: PanelId | null;
}

export const TITLES: Record<PanelId, string> = {
  script: '脚本 & 译文',
  chat: 'AI 助手',
  subtitle: '字幕样式',
};

export const MIN_SIZE: Record<PanelId, { w: number; h: number }> = {
  script: { w: 360, h: 300 },
  chat: { w: 320, h: 320 },
  subtitle: { w: 320, h: 300 },
};

/** 顶栏收起时的高度：浮动窗口不要压住它。 */
export const TOP_INSET = 44;
/** 拖到边缘外时，至少留这么多像素在可视区域内，免得再也抓不回来。 */
export const KEEP_VISIBLE = 48;
/** 拖动超过这么多像素才算"拖出来"，低于它按点击处理（标签条上的点击是切换标签）。 */
export const TEAR_OFF_PX = 6;

// ---------------------------------------------------------------------------
// 几何
// ---------------------------------------------------------------------------

export function clampRect(rect: Rect, vp: Viewport, min: { w: number; h: number }): Rect {
  const w = Math.max(min.w, Math.min(rect.w, vp.w - 16));
  const h = Math.max(min.h, Math.min(rect.h, vp.h - TOP_INSET - 8));
  const x = Math.max(KEEP_VISIBLE - w, Math.min(rect.x, vp.w - KEEP_VISIBLE));
  const y = Math.max(TOP_INSET, Math.min(rect.y, vp.h - KEEP_VISIBLE));
  return { x, y, w, h };
}

export type Dir = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';
export const DIRS: readonly Dir[] = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];

/**
 * 从某条边或某个角拖动 (dx, dy) 之后的矩形。
 * 对边不动；宽高不小于最小值（碰到最小值就停，窗口不会被"推着走"）；不会拖出可视区域。
 */
export function resizeRect(
  from: Rect, dir: Dir, dx: number, dy: number, vp: Viewport, min: { w: number; h: number },
): Rect {
  let left = from.x;
  let top = from.y;
  let right = from.x + from.w;
  let bottom = from.y + from.h;
  if (dir.includes('e')) right += dx;
  if (dir.includes('w')) left += dx;
  if (dir.includes('s')) bottom += dy;
  if (dir.includes('n')) top += dy;

  if (dir.includes('w')) left = Math.max(0, Math.min(left, right - min.w));
  if (dir.includes('e')) right = Math.min(vp.w, Math.max(right, left + min.w));
  if (dir.includes('n')) top = Math.max(TOP_INSET, Math.min(top, bottom - min.h));
  if (dir.includes('s')) bottom = Math.min(vp.h, Math.max(bottom, top + min.h));

  return { x: left, y: top, w: right - left, h: bottom - top };
}

export function pointInRect(px: number, py: number, r: Rect): boolean {
  return px >= r.x && px <= r.x + r.w && py >= r.y && py <= r.y + r.h;
}

/**
 * 停靠位没显示出来（里面没有窗口）时，拖动窗口用的"吸附区"：右栏大致所在的那一块。
 * 停靠位显示着的时候用它自己量出来的矩形，这个只是兜底，不要求和栅格逐像素一致。
 */
export function fallbackDockZone(main: Rect): Rect {
  const w = main.w * 0.4;
  return { x: main.x + main.w - w - 12, y: main.y + 8, w, h: Math.max(0, main.h - 16) };
}

// ---------------------------------------------------------------------------
// 默认布局
// ---------------------------------------------------------------------------

export function defaultRect(id: PanelId, vp: Viewport): Rect {
  const min = MIN_SIZE[id];
  let r: Rect;
  if (id === 'chat') {
    r = { x: vp.w - 400 - 16, y: TOP_INSET + 12, w: 400, h: Math.min(560, vp.h - TOP_INSET - 24) };
  } else if (id === 'script') {
    r = { x: Math.round(vp.w * 0.45), y: TOP_INSET + 20, w: Math.min(560, Math.round(vp.w * 0.5)), h: vp.h - TOP_INSET - 56 };
  } else {
    r = { x: vp.w - 380 - 440, y: TOP_INSET + 36, w: 380, h: Math.min(640, vp.h - TOP_INSET - 56) };
  }
  return clampRect(r, vp, min);
}

/** 初始：译文和助手都吸附在右栏（两个标签，默认显示译文），字幕样式收着，由画面左边缘的挂耳打开。 */
export function defaultLayout(vp: Viewport): Layout {
  const mk = (id: PanelId, open: boolean, docked: boolean): PanelWin => ({ open, docked, rect: defaultRect(id, vp), z: 0 });
  return normalize({
    panels: { script: mk('script', true, true), chat: mk('chat', true, true), subtitle: mk('subtitle', false, true) },
    active: 'script',
  });
}

// ---------------------------------------------------------------------------
// 派生
// ---------------------------------------------------------------------------

/** 吸附且打开的窗口，按固定顺序（标签条里的顺序稳定，不随点击变）。 */
export function dockedOpen(layout: Layout): PanelId[] {
  return PANEL_IDS.filter((id) => layout.panels[id].open && layout.panels[id].docked);
}

export function floatingOpen(layout: Layout): PanelId[] {
  return PANEL_IDS
    .filter((id) => layout.panels[id].open && !layout.panels[id].docked)
    .sort((a, b) => layout.panels[a].z - layout.panels[b].z);
}

/** 停靠位当前显示的那个：记着的 active 还有效就用它，否则第一个；没有则 null（停靠位该收起）。 */
export function activeDock(layout: Layout): PanelId | null {
  const docked = dockedOpen(layout);
  if (layout.active && docked.includes(layout.active)) return layout.active;
  return docked[0] ?? null;
}

/** 整理：active 有效、浮动窗口的 z 压成 1..n（顺序不变）、吸附的 z 清零。 */
export function normalize(layout: Layout): Layout {
  const panels = { ...layout.panels };
  const floating = PANEL_IDS
    .filter((id) => panels[id].open && !panels[id].docked)
    .sort((a, b) => panels[a].z - panels[b].z || PANEL_IDS.indexOf(a) - PANEL_IDS.indexOf(b));
  for (const id of PANEL_IDS) {
    const rank = floating.indexOf(id) + 1;
    panels[id] = { ...panels[id], z: panels[id].open && !panels[id].docked ? rank : 0 };
  }
  const next: Layout = { panels, active: layout.active };
  next.active = activeDock(next);
  return next;
}

// ---------------------------------------------------------------------------
// 状态转移
// ---------------------------------------------------------------------------

export type Action =
  | { type: 'open'; id: PanelId }
  | { type: 'close'; id: PanelId }
  | { type: 'toggle'; id: PanelId }
  | { type: 'dock'; id: PanelId }
  | { type: 'float'; id: PanelId; rect?: Rect }
  | { type: 'focus'; id: PanelId }
  | { type: 'activate'; id: PanelId }
  | { type: 'setRect'; id: PanelId; rect: Rect }
  | { type: 'viewport' };

const TOP = 1e9;

export function reduce(layout: Layout, action: Action, vp: Viewport): Layout {
  if (action.type === 'viewport') {
    const panels = { ...layout.panels };
    for (const id of PANEL_IDS) panels[id] = { ...panels[id], rect: clampRect(panels[id].rect, vp, MIN_SIZE[id]) };
    return normalize({ ...layout, panels });
  }
  const p = layout.panels[action.id];
  const set = (id: PanelId, patch: Partial<PanelWin>, active: PanelId | null = layout.active): Layout =>
    normalize({ panels: { ...layout.panels, [id]: { ...layout.panels[id], ...patch } }, active });

  switch (action.type) {
    case 'open':
      // 浮动的打开就放到最上面；吸附的打开就成为当前标签
      return p.docked ? set(action.id, { open: true }, action.id) : set(action.id, { open: true, z: TOP });
    case 'close':
      // 常驻窗口关不掉；顶栏已经没有开关按钮，关了就再也回不来了
      return PINNED.includes(action.id) ? layout : set(action.id, { open: false });
    case 'toggle':
      // 常驻窗口的 toggle 等于"切到它 / 置顶"，不是关闭
      return reduce(layout, { type: p.open && !PINNED.includes(action.id) ? 'close' : 'open', id: action.id }, vp);
    case 'dock':
      return set(action.id, { open: true, docked: true }, action.id);
    case 'float': {
      const rect = clampRect(action.rect ?? p.rect, vp, MIN_SIZE[action.id]);
      return set(action.id, { open: true, docked: false, rect, z: TOP });
    }
    case 'focus': {
      if (!p.open || p.docked) return layout;
      const stack = floatingOpen(layout);
      return stack[stack.length - 1] === action.id ? layout : set(action.id, { z: TOP });   // 已经在最上面就别动
    }
    case 'activate':
      return p.open && p.docked ? normalize({ ...layout, active: action.id }) : layout;
    case 'setRect':
      return set(action.id, { rect: clampRect(action.rect, vp, MIN_SIZE[action.id]) });
  }
}

// ---------------------------------------------------------------------------
// 存取
// ---------------------------------------------------------------------------

/** 布局版本号：默认布局改了就升一号，别再复用浏览器里旧版的开合状态。 */
export const STORAGE_KEY = 'vvt.windows.v2';
/** 旧版只有助手一个浮动窗口，位置存在这个 key 里。 */
export const LEGACY_CHAT_KEY = 'vvt.chat.window';

const isRect = (r: unknown): r is Rect =>
  !!r && typeof r === 'object' && ['x', 'y', 'w', 'h'].every((k) => Number.isFinite((r as Record<string, unknown>)[k]));

export function serialize(layout: Layout): string {
  const panels: Record<string, { open: boolean; docked: boolean; rect: Rect }> = {};
  for (const id of PANEL_IDS) {
    const { open, docked, rect } = layout.panels[id];
    panels[id] = { open, docked, rect };
  }
  return JSON.stringify({ panels, active: layout.active, floating: floatingOpen(layout) });
}

/**
 * 读回布局。任何一块坏了（缺字段、类型不对、JSON 坏）只那一块用默认值，不影响别的；
 * 没有存过但有旧版的助手窗口位置，就沿用它。
 */
export function restore(raw: string | null, legacyChat: string | null, vp: Viewport): Layout {
  const base = defaultLayout(vp);
  const panels = { ...base.panels };

  if (legacyChat) {
    try {
      const r = JSON.parse(legacyChat);
      if (isRect(r)) panels.chat = { ...panels.chat, rect: clampRect(r, vp, MIN_SIZE.chat) };
    } catch { /* 旧值坏了就用默认位置 */ }
  }

  let active: PanelId | null = base.active;
  let order: PanelId[] = [];
  if (raw) {
    try {
      const j = JSON.parse(raw);
      for (const id of PANEL_IDS) {
        const s = j?.panels?.[id];
        if (!s || typeof s !== 'object') continue;
        panels[id] = {
          open: typeof s.open === 'boolean' ? s.open : panels[id].open,
          docked: typeof s.docked === 'boolean' ? s.docked : panels[id].docked,
          rect: isRect(s.rect) ? clampRect(s.rect, vp, MIN_SIZE[id]) : panels[id].rect,
          z: 0,
        };
      }
      if (PANEL_IDS.includes(j?.active)) active = j.active;
      if (Array.isArray(j?.floating)) order = j.floating.filter((x: unknown): x is PanelId => PANEL_IDS.includes(x as PanelId));
    } catch { /* 整个坏了就是默认布局 */ }
  }
  // 常驻窗口不信任存档里的 open：万一被写成关闭，界面上已经没有入口把它打开
  for (const id of PINNED) panels[id] = { ...panels[id], open: true };
  // 浮动窗口的叠放顺序：存的顺序（后面的在上）；没存的按固定顺序
  order.forEach((id, i) => { panels[id] = { ...panels[id], z: i + 1 }; });
  return normalize({ panels, active });
}
