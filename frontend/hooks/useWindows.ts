/**
 * 窗口系统的状态与拖动控制。布局的规则在 store/windows.ts（纯函数）；这里负责：
 *   · 把它接成 React 状态，并存取 localStorage；
 *   · 每个面板一个"宿主"DOM 节点：面板内容用 portal 渲染进宿主，宿主在"停靠位"和"浮动窗口"之间
 *     搬来搬去。内容的 React 组件树位置始终不变，所以吸附 / 弹出时输入框里的草稿、编辑状态不会丢；
 *   · 拖动：标题栏拖动、边角缩放、从停靠位拖出来、拖进吸附区。
 *
 * 指针事件挂在 window 上，而不是用 setPointerCapture：窗口从停靠位"拖出来"的那一刻，被按住的
 * 标题栏会被卸载重建，capture 会跟着丢；window 上的监听不受影响。
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import {
  MIN_SIZE, PANEL_IDS, STORAGE_KEY, TEAR_OFF_PX,
  activeDock, clampRect, dockedOpen, fallbackDockZone, floatingOpen, pointInRect, reduce, resizeRect, restore, serialize,
  type Action, type Dir, type Layout, type PanelId, type Rect, type Viewport,
} from '../store/windows';

export interface HeaderProps {
  onPointerDown: (e: React.PointerEvent) => void;
  style: React.CSSProperties;
  className: string;
  title: string;
}

export interface DragInfo {
  id: PanelId;
  /** 指针现在是否在吸附区里（松手就会吸附）。 */
  over: boolean;
  zone: Rect | null;
}

type Kind = 'move' | 'resize' | 'tab';

interface Session {
  id: PanelId;
  kind: Kind;
  dir?: Dir;
  sx: number;
  sy: number;
  from: Rect;
  /** 还在停靠位里（没拖出来）。 */
  docked: boolean;
  /** 按下时指针在标题栏上的水平比例，拖出来时窗口以同样的比例挂在指针下。 */
  anchorFrac: number;
  anchorY: number;
  moved: boolean;
}

const viewport = (): Viewport => ({ w: window.innerWidth, h: window.innerHeight });

function readStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// 宿主节点：面板内容住在里面，在停靠位 / 浮动窗口之间搬动
// ---------------------------------------------------------------------------

const scrollMemo = new WeakMap<HTMLElement, Map<Element, number>>();

function createHost(): HTMLDivElement {
  const host = document.createElement('div');
  // 宿主自己不参与布局：面板的根节点直接成为容器的子项
  host.style.display = 'contents';
  const memo = new Map<Element, number>();
  scrollMemo.set(host, memo);
  // 节点被移出再放回 DOM，滚动位置会被浏览器清零。滚动事件不冒泡，所以用捕获阶段一直记着。
  host.addEventListener('scroll', (e) => {
    const t = e.target as HTMLElement;
    if (t && typeof t.scrollTop === 'number') memo.set(t, t.scrollTop);
  }, true);
  return host;
}

/** 把宿主挂到容器里（已经在里面就什么都不做），并把滚动位置还原。 */
export function mountHost(host: HTMLElement, container: HTMLElement): void {
  if (host.parentElement === container) return;
  container.appendChild(host);
  scrollMemo.get(host)?.forEach((top, el) => {
    if (top > 0 && el.isConnected) (el as HTMLElement).scrollTo({ top, behavior: 'instant' as ScrollBehavior });
  });
}

// ---------------------------------------------------------------------------

const sameRect = (a: Rect | null, b: Rect | null) =>
  a === b || (!!a && !!b && a.x === b.x && a.y === b.y && a.w === b.w && a.h === b.h);

export function useWindows() {
  const [layout, dispatch] = useReducer(
    (l: Layout, a: Action) => reduce(l, a, viewport()),
    undefined,
    () => restore(readStorage(STORAGE_KEY), viewport()),
  );
  const layoutRef = useRef(layout);
  layoutRef.current = layout;

  // 存：拖动时每帧都在变，停下一小会儿再写
  useEffect(() => {
    const t = setTimeout(() => {
      try {
        window.localStorage.setItem(STORAGE_KEY, serialize(layout));
      } catch { /* 记不住也没关系 */ }
    }, 200);
    return () => clearTimeout(t);
  }, [layout]);

  // 浏览器窗口变小：把浮动窗口拉回可视区域
  useEffect(() => {
    const onResize = () => dispatch({ type: 'viewport' });
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const hosts = useMemo(() => {
    const out = {} as Record<PanelId, HTMLDivElement>;
    for (const id of PANEL_IDS) out[id] = createHost();
    return out;
  }, []);

  const slotRef = useRef<HTMLElement | null>(null);
  const mainRef = useRef<HTMLElement | null>(null);
  const [drag, setDrag] = useState<DragInfo | null>(null);
  const sess = useRef<Session | null>(null);

  /** 吸附区：停靠位显示着就用它自己的矩形；没有显示（里面没窗口）就用右栏大致所在的位置。 */
  const zoneRect = useCallback((): Rect | null => {
    const slot = slotRef.current;
    if (slot) {
      const r = slot.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) return { x: r.left, y: r.top, w: r.width, h: r.height };
    }
    const main = mainRef.current;
    if (!main) return null;
    const r = main.getBoundingClientRect();
    return fallbackDockZone({ x: r.left, y: r.top, w: r.width, h: r.height });
  }, []);

  const cleanupRef = useRef<() => void>(() => {});

  const finish = useCallback((e: PointerEvent | null, cancelled: boolean) => {
    const s = sess.current;
    cleanupRef.current();
    sess.current = null;
    setDrag(null);
    if (!s || cancelled || !e) return;
    if (s.kind === 'tab' && !s.moved) {
      dispatch({ type: 'activate', id: s.id });
      return;
    }
    if (s.kind !== 'resize' && s.moved) {
      const zone = zoneRect();
      if (zone && pointInRect(e.clientX, e.clientY, zone)) dispatch({ type: 'dock', id: s.id });
    }
  }, [zoneRect]);

  const onMove = useCallback((e: PointerEvent) => {
    const s = sess.current;
    if (!s) return;
    const dx = e.clientX - s.sx;
    const dy = e.clientY - s.sy;
    const vp = viewport();

    if (s.kind === 'resize') {
      dispatch({ type: 'setRect', id: s.id, rect: resizeRect(s.from, s.dir!, dx, dy, vp, MIN_SIZE[s.id]) });
      return;
    }

    if (s.docked) {
      if (Math.hypot(dx, dy) < TEAR_OFF_PX) return;
      // 拖出来：窗口用上次浮动时的大小，挂在指针下（水平按标题栏上按下的比例，垂直保持同样的偏移）
      const size = layoutRef.current.panels[s.id].rect;
      const rect = clampRect(
        { x: e.clientX - size.w * s.anchorFrac, y: e.clientY - s.anchorY, w: size.w, h: size.h }, vp, MIN_SIZE[s.id],
      );
      dispatch({ type: 'float', id: s.id, rect });
      s.docked = false;
      s.from = rect;
      s.sx = e.clientX;
      s.sy = e.clientY;
    } else {
      dispatch({ type: 'setRect', id: s.id, rect: { ...s.from, x: s.from.x + dx, y: s.from.y + dy } });
    }
    s.moved = true;

    const zone = zoneRect();
    const over = !!zone && pointInRect(e.clientX, e.clientY, zone);
    setDrag((prev) => (prev && prev.id === s.id && prev.over === over && sameRect(prev.zone, zone) ? prev : { id: s.id, over, zone }));
  }, [zoneRect]);

  const begin = useCallback((id: PanelId, kind: Kind, e: React.PointerEvent, dir?: Dir) => {
    if (e.button !== 0 || sess.current) return;
    const target = e.target as HTMLElement;
    // 按在标题栏的按钮 / 输入框上不该开始拖动
    if (kind !== 'resize' && target.closest('button, a, input, textarea, select, [data-no-drag]')) return;
    e.preventDefault();
    const p = layoutRef.current.panels[id];
    const hr = (e.currentTarget as HTMLElement).getBoundingClientRect();
    sess.current = {
      id, kind, dir, sx: e.clientX, sy: e.clientY, from: p.rect, docked: p.docked,
      anchorFrac: hr.width ? Math.max(0.05, Math.min(0.95, (e.clientX - hr.left) / hr.width)) : 0.5,
      anchorY: e.clientY - hr.top, moved: false,
    };
    dispatch({ type: 'focus', id });

    const up = (ev: PointerEvent) => finish(ev, false);
    const cancel = () => finish(null, true);
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', cancel);
    const prevSelect = document.body.style.userSelect;
    const prevCursor = document.body.style.cursor;
    document.body.style.userSelect = 'none';
    if (kind === 'resize') document.body.style.cursor = getComputedStyle(e.currentTarget as HTMLElement).cursor;
    cleanupRef.current = () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', cancel);
      document.body.style.userSelect = prevSelect;
      document.body.style.cursor = prevCursor;
    };
  }, [finish, onMove]);

  // 组件卸载时别把监听留在 window 上
  useEffect(() => () => cleanupRef.current(), []);

  const headerProps = useCallback((id: PanelId): HeaderProps => ({
    onPointerDown: (e) => begin(id, 'move', e),
    style: { touchAction: 'none' },
    className: 'cursor-grab active:cursor-grabbing select-none',
    title: layoutRef.current.panels[id].docked ? '拖出来变成窗口' : '拖动移动窗口；拖到右侧虚线区松手可吸附',
  }), [begin]);

  const tabProps = useCallback((id: PanelId) => ({
    onPointerDown: (e: React.PointerEvent) => begin(id, 'tab', e),
    style: { touchAction: 'none' } as React.CSSProperties,
    title: '点击切换；拖出来变成窗口',
  }), [begin]);

  const resizeProps = useCallback((id: PanelId, dir: Dir) => ({
    onPointerDown: (e: React.PointerEvent) => begin(id, 'resize', e, dir),
    style: { touchAction: 'none' } as React.CSSProperties,
  }), [begin]);

  const actions = useMemo(() => ({
    open: (id: PanelId) => dispatch({ type: 'open', id }),
    close: (id: PanelId) => dispatch({ type: 'close', id }),
    toggle: (id: PanelId) => dispatch({ type: 'toggle', id }),
    dock: (id: PanelId) => dispatch({ type: 'dock', id }),
    float: (id: PanelId) => dispatch({ type: 'float', id }),
    focus: (id: PanelId) => dispatch({ type: 'focus', id }),
  }), []);

  return {
    layout,
    docked: dockedOpen(layout),
    floating: floatingOpen(layout),
    active: activeDock(layout),
    drag,
    hosts,
    slotRef,
    mainRef,
    headerProps,
    tabProps,
    resizeProps,
    ...actions,
  };
}

export type WindowsApi = ReturnType<typeof useWindows>;
