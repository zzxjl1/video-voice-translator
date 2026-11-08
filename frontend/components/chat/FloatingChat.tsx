/**
 * 助手窗口：像桌面应用的浮动窗口 —— 拖标题栏移动、拖右下角改大小、关掉就收起。
 *
 * 不占布局空间（`fixed`，内容照常铺满整宽），挡住什么就把它拖开。位置和尺寸记在
 * localStorage 里，下次打开还在老地方。
 *
 * 拖动用 pointer 事件 + `setPointerCapture`：指针移出窗口、甚至移出浏览器窗口，
 * 事件仍然回到被按住的元素上，不会出现"拖快了就跟丢"。标题栏由 `ChatPanel`
 * 自己那一条充当（`children` 是个渲染函数，把拖动用的 props 交进去），这样不会
 * 出现两层标题。
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';

const KEY = 'vvt.chat.window';
const MIN_W = 320;
const MIN_H = 320;
const DEFAULT_W = 400;
const DEFAULT_H = 560;
/** 至少留这么多像素在可视区域内，免得拖到屏幕外再也抓不回来。 */
const KEEP_VISIBLE = 48;
/** 顶栏收起时的高度：窗口不要压住它。 */
const TOP_INSET = 44;

export interface Rect { x: number; y: number; w: number; h: number }

export interface DragProps {
  onPointerDown: (e: React.PointerEvent) => void;
  onPointerMove: (e: React.PointerEvent) => void;
  onPointerUp: (e: React.PointerEvent) => void;
  onPointerCancel: (e: React.PointerEvent) => void;
  style: React.CSSProperties;
  className: string;
  title: string;
}

function viewport() {
  return { vw: window.innerWidth, vh: window.innerHeight };
}

function clamp(rect: Rect): Rect {
  const { vw, vh } = viewport();
  const w = Math.max(MIN_W, Math.min(rect.w, vw - 16));
  const h = Math.max(MIN_H, Math.min(rect.h, vh - TOP_INSET - 8));
  const x = Math.max(KEEP_VISIBLE - w, Math.min(rect.x, vw - KEEP_VISIBLE));
  const y = Math.max(TOP_INSET, Math.min(rect.y, vh - KEEP_VISIBLE));
  return { x, y, w, h };
}

function initial(): Rect {
  const { vw, vh } = viewport();
  return clamp({
    x: vw - DEFAULT_W - 16,
    y: TOP_INSET + 12,
    w: DEFAULT_W,
    h: Math.min(DEFAULT_H, vh - TOP_INSET - 24),
  });
}

function load(): Rect {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw) {
      const r = JSON.parse(raw) as Partial<Rect>;
      if ([r.x, r.y, r.w, r.h].every((v) => typeof v === 'number')) return clamp(r as Rect);
    }
  } catch {
    /* 存储不可用就用默认位置 */
  }
  return initial();
}

interface Props {
  children: (dragProps: DragProps) => React.ReactNode;
}

const FloatingChat: React.FC<Props> = ({ children }) => {
  const [rect, setRect] = useState<Rect>(load);
  const drag = useRef<{ mode: 'move' | 'resize'; sx: number; sy: number; from: Rect } | null>(null);

  useEffect(() => {
    const onResize = () => setRect((r) => clamp(r));
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const persist = useCallback((r: Rect) => {
    try {
      window.localStorage.setItem(KEY, JSON.stringify(r));
    } catch {
      /* 记不住也没关系 */
    }
  }, []);

  const start = (mode: 'move' | 'resize') => (e: React.PointerEvent) => {
    // 按在标题栏的按钮上不该开始拖动
    if (e.button !== 0 || (e.target as HTMLElement).closest('button')) return;
    e.preventDefault();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    drag.current = { mode, sx: e.clientX, sy: e.clientY, from: rect };
  };

  const move = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.sx;
    const dy = e.clientY - d.sy;
    setRect(d.mode === 'move'
      ? clamp({ ...d.from, x: d.from.x + dx, y: d.from.y + dy })
      : clamp({ ...d.from, w: d.from.w + dx, h: d.from.h + dy }));
  };

  const end = (e: React.PointerEvent) => {
    if (!drag.current) return;
    drag.current = null;
    (e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId);
    setRect((r) => { persist(r); return r; });
  };

  const dragProps: DragProps = {
    onPointerDown: start('move'),
    onPointerMove: move,
    onPointerUp: end,
    onPointerCancel: end,
    style: { touchAction: 'none' },
    className: 'cursor-grab active:cursor-grabbing select-none',
    title: '拖动可移动窗口',
  };

  return (
    <div
      className="fixed z-40 flex flex-col rounded-2xl overflow-hidden shadow-2xl shadow-gray-900/25 bg-white"
      style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
    >
      {children(dragProps)}

      {/* 右下角调大小 */}
      <div
        onPointerDown={start('resize')}
        onPointerMove={move}
        onPointerUp={end}
        onPointerCancel={end}
        title="拖动改变大小"
        className="absolute right-0 bottom-0 w-4 h-4 cursor-nwse-resize touch-none group z-10"
        style={{ touchAction: 'none' }}
      >
        <svg viewBox="0 0 16 16" className="w-full h-full text-gray-300 group-hover:text-claude-accent transition-colors">
          <path d="M15 9l-6 6M15 13l-2 2" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" />
        </svg>
      </div>
    </div>
  );
};

export default FloatingChat;
