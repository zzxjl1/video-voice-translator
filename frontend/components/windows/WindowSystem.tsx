/**
 * 窗口系统的界面：停靠位（右栏，固定大小，标签条只列已打开的窗口）、浮动窗口（自由位置和大小）、
 * 吸附区提示、面板内容的 portal。状态和拖动控制在 hooks/useWindows.ts。
 *
 * 面板内容只渲染一次（portal 进它自己的宿主节点），宿主在停靠位和浮动窗口之间搬动，
 * 所以吸附 / 弹出不会让面板重新挂载 —— 输入框里写了一半的话、编辑到一半的译文都还在。
 */
import React, { useLayoutEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { DIRS, PANEL_IDS, PINNED, TITLES, type Dir, type PanelId } from '../../store/windows';
import { mountHost, type HeaderProps, type WindowsApi } from '../../hooks/useWindows';

/** 面板渲染时拿到的窗口信息：自己是不是吸附着、标题栏的拖动属性、标题栏右边的窗口按钮。 */
export interface PanelCtx {
  docked: boolean;
  headerProps: HeaderProps;
  controls: React.ReactNode;
}

export type PanelRenderers = Record<PanelId, (ctx: PanelCtx) => React.ReactNode>;

// ---------------------------------------------------------------------------
// 标题栏右边的按钮：吸附 / 弹出、关闭（常驻窗口没有关闭）
// ---------------------------------------------------------------------------

const BTN = 'p-1 rounded-md text-gray-300 hover:text-gray-600 hover:bg-claude-paper transition-colors';

export const WindowControls: React.FC<{ docked: boolean; closable: boolean; onToggleDock: () => void; onClose: () => void }> = ({
  docked, closable, onToggleDock, onClose,
}) => (
  <div className="flex items-center gap-0.5 shrink-0">
    <button
      type="button"
      onClick={onToggleDock}
      title={docked ? '弹出为窗口（也可以直接把标题栏拖出来）' : '吸附到右栏（也可以把窗口拖到右侧虚线区）'}
      className={BTN}
    >
      <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
        {docked
          ? <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6M15 3h6v6M10 14L21 3" />
          : <><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M15 3v18" /></>}
      </svg>
    </button>
    {closable && (
      <button type="button" onClick={onClose} title="关闭" className={BTN}>
        <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" /></svg>
      </button>
    )}
  </div>
);

// ---------------------------------------------------------------------------
// 宿主挂载点
// ---------------------------------------------------------------------------

/** 一个空容器，把某个面板的宿主节点挂进来。宿主换了容器，面板内容原地保留。 */
const HostMount: React.FC<{ host: HTMLElement }> = ({ host }) => {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (ref.current) mountHost(host, ref.current);
  }, [host]);
  return <div ref={ref} className="flex flex-col flex-grow min-h-0 h-full" />;
};

// ---------------------------------------------------------------------------
// 面板内容（portal）
// ---------------------------------------------------------------------------

export const WindowPortals: React.FC<{ wm: WindowsApi; panels: PanelRenderers }> = ({ wm, panels }) => (
  <>
    {PANEL_IDS.filter((id) => wm.layout.panels[id].open).map((id) => {
      const docked = wm.layout.panels[id].docked;
      const ctx: PanelCtx = {
        docked,
        headerProps: wm.headerProps(id),
        controls: (
          <WindowControls
            docked={docked}
            closable={!PINNED.includes(id)}
            onToggleDock={() => (docked ? wm.float(id) : wm.dock(id))}
            onClose={() => wm.close(id)}
          />
        ),
      };
      return createPortal(panels[id](ctx), wm.hosts[id], id);
    })}
  </>
);

// ---------------------------------------------------------------------------
// 停靠位
// ---------------------------------------------------------------------------

export const DockSlot: React.FC<{ wm: WindowsApi }> = ({ wm }) => (
  <section ref={(el) => { wm.slotRef.current = el; }} className="flex flex-col min-h-0 min-w-0">
    {/* 标签条只列已打开的停靠窗口；译文 / 助手常驻，字幕样式打开时才多一个 */}
    {wm.docked.length > 1 && (
      <div role="tablist" className="shrink-0 flex gap-1.5 mb-2">
        {wm.docked.map((id) => {
          const on = id === wm.active;
          return (
            <div
              key={id}
              role="tab"
              aria-selected={on}
              {...wm.tabProps(id)}
              className={[
                'px-3.5 py-1.5 rounded-full text-[11px] font-bold tracking-wide cursor-grab active:cursor-grabbing select-none transition-colors border',
                on ? 'bg-claude-accent text-white border-claude-accent shadow-sm' : 'bg-white text-gray-500 border-[#e5e5e0] hover:text-claude-accent hover:border-claude-accent/50',
              ].join(' ')}
            >
              {TITLES[id]}
            </div>
          );
        })}
      </div>
    )}
    <div className="flex-grow min-h-0 relative">
      {/* 没显示的标签也挂着（只是隐藏）：切回来时输入、滚动位置都还在 */}
      {wm.docked.map((id) => (
        <div key={id} className={id === wm.active ? 'h-full flex flex-col min-h-0' : 'hidden'}>
          <HostMount host={wm.hosts[id]} />
        </div>
      ))}
    </div>
  </section>
);

// ---------------------------------------------------------------------------
// 浮动窗口
// ---------------------------------------------------------------------------

/** 浮动窗口的层级区间：高于页面内容，低于顶栏（z-50）和弹窗。 */
const Z_BASE = 30;

/** 八个缩放把手：边是细条，角是小方块；一半在窗口外面，免得盖住滚动条。 */
const HANDLES: Record<Dir, { style: React.CSSProperties; cursor: string }> = {
  n: { style: { left: 14, right: 14, top: -3, height: 7 }, cursor: 'ns-resize' },
  s: { style: { left: 14, right: 14, bottom: -3, height: 7 }, cursor: 'ns-resize' },
  e: { style: { top: 14, bottom: 14, right: -3, width: 7 }, cursor: 'ew-resize' },
  w: { style: { top: 14, bottom: 14, left: -3, width: 7 }, cursor: 'ew-resize' },
  nw: { style: { top: -4, left: -4, width: 16, height: 16 }, cursor: 'nwse-resize' },
  ne: { style: { top: -4, right: -4, width: 16, height: 16 }, cursor: 'nesw-resize' },
  sw: { style: { bottom: -4, left: -4, width: 16, height: 16 }, cursor: 'nesw-resize' },
  se: { style: { bottom: -4, right: -4, width: 16, height: 16 }, cursor: 'nwse-resize' },
};

const FloatingFrame: React.FC<{ wm: WindowsApi; id: PanelId }> = ({ wm, id }) => {
  const { rect, z } = wm.layout.panels[id];
  return (
    <div
      role="dialog"
      aria-label={TITLES[id]}
      data-window={id}
      className="fixed"
      style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h, zIndex: Z_BASE + z }}
      onPointerDownCapture={() => wm.focus(id)}
    >
      <div className="absolute inset-0 flex flex-col rounded-2xl overflow-hidden shadow-2xl shadow-gray-900/25 bg-white border border-[#e5e5e0]">
        <HostMount host={wm.hosts[id]} />
      </div>
      {DIRS.map((d) => (
        <div
          key={d}
          data-resize={d}
          onPointerDown={wm.resizeProps(id, d).onPointerDown}
          className="absolute"
          style={{ ...HANDLES[d].style, touchAction: 'none', cursor: HANDLES[d].cursor }}
        >
          {d === 'se' && (
            <svg viewBox="0 0 16 16" className="w-3 h-3 absolute right-[5px] bottom-[5px] text-gray-300 pointer-events-none">
              <path d="M15 9l-6 6M15 13l-2 2" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" />
            </svg>
          )}
        </div>
      ))}
    </div>
  );
};

/** 拖动窗口时显示的吸附区：松手落在里面就吸附进去。 */
const DockZone: React.FC<{ wm: WindowsApi }> = ({ wm }) => {
  const d = wm.drag;
  if (!d || !d.zone) return null;
  const { x, y, w, h } = d.zone;
  return (
    <div
      aria-hidden="true"
      data-dock-zone={d.over ? 'over' : 'idle'}
      className={[
        'fixed pointer-events-none rounded-3xl border-2 flex items-center justify-center transition-colors duration-150',
        d.over ? 'border-claude-accent bg-claude-accent/10' : 'border-dashed border-claude-accent/40 bg-claude-accent/[0.03]',
      ].join(' ')}
      style={{ left: x, top: y, width: w, height: h, zIndex: Z_BASE }}
    >
      <span className={`text-[11px] font-bold tracking-wide transition-opacity ${d.over ? 'text-claude-accent opacity-100' : 'text-claude-accent/60 opacity-70'}`}>
        {d.over ? '松开，吸附到这里' : '拖到这里可以吸附'}
      </span>
    </div>
  );
};

export const WindowLayer: React.FC<{ wm: WindowsApi }> = ({ wm }) => (
  <>
    <DockZone wm={wm} />
    {wm.floating.map((id) => <FloatingFrame key={id} wm={wm} id={id} />)}
  </>
);
