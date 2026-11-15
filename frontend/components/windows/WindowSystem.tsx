/**
 * 窗口系统的界面：停靠位（右栏，固定大小；切换条 hover 顶部横杠才在上方居中浮出、不占布局）、
 * 浮动窗口（自由位置和大小）、
 * 吸附区提示、面板内容的 portal。状态和拖动控制在 hooks/useWindows.ts。
 *
 * 面板内容只渲染一次（portal 进它自己的宿主节点），宿主在停靠位和浮动窗口之间搬动，
 * 所以吸附 / 弹出不会让面板重新挂载 —— 输入框里写了一半的话、编辑到一半的译文都还在。
 */
import React, { useLayoutEffect, useRef, useState } from 'react';
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

export const DockSlot: React.FC<{ wm: WindowsApi }> = ({ wm }) => {
  const [hovered, setHovered] = useState(false);
  const [stuck, setStuck] = useState(false);       // 触屏没有 hover：点一下横条也能展开
  const hasWindows = wm.docked.length > 0;
  const visible = hasWindows && (hovered || stuck);

  return (
    <section
      ref={(el) => { wm.slotRef.current = el; }}
      className="relative flex flex-col min-h-0 min-w-0"
    >
      <div className="flex-grow min-h-0 relative">
        {/* 没显示的标签也挂着（只是隐藏）：切回来时输入、滚动位置都还在 */}
        {wm.docked.map((id) => (
          <div key={id} className={id === wm.active ? 'h-full flex flex-col min-h-0' : 'hidden'}>
            <HostMount host={wm.hosts[id]} />
          </div>
        ))}
      </div>

      {/* 顶部中央的一小块热区：只有移近这根横杠才展开切换，右栏其它地方不打扰。
          横杠和切换条都住在热区里，鼠标从横杠移到切换条时不会掉出 hover 范围。 */}
      {hasWindows && (
        <div
          className="group absolute top-0 left-1/2 -translate-x-1/2 z-30 h-6 w-24"
          onMouseEnter={() => setHovered(true)}
          onMouseLeave={() => { setHovered(false); setStuck(false); }}
          onPointerDown={(e) => { if (e.pointerType !== 'mouse') setStuck((v) => !v); }}
        >
          {/* 收起态：一根 3px 横杠，纯装饰 */}
          <span
            aria-hidden="true"
            className={[
              'pointer-events-none absolute top-2 left-1/2 -translate-x-1/2 h-[3px] w-7 rounded-full bg-gray-300/70',
              'transition-opacity duration-200',
              visible ? 'opacity-0' : 'opacity-100 group-hover:opacity-0',
            ].join(' ')}
          />

          {/* 切换条：hover 热区才淡入，浮在右栏上方居中，不占布局 */}
          <div
            role="tablist"
            aria-label="已打开的窗口"
            aria-hidden={!visible}
            className={[
              'absolute top-1.5 left-1/2 -translate-x-1/2 w-max',
              'flex items-center gap-0.5 rounded-full px-1 py-1',
              'border border-[#e5e5e0] bg-white/90 backdrop-blur-xl shadow-[0_10px_30px_rgba(0,0,0,0.12)]',
              'transition-all duration-200 ease-out',
              visible
                ? 'opacity-100 translate-y-0 pointer-events-auto'
                : 'opacity-0 -translate-y-2 pointer-events-none group-hover:opacity-100 group-hover:translate-y-0 group-hover:pointer-events-auto',
            ].join(' ')}
          >
            {wm.docked.map((id) => {
              const on = id === wm.active;
              return (
                <div
                  key={id}
                  role="tab"
                  aria-selected={on}
                  tabIndex={visible ? 0 : -1}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); wm.open(id); }
                  }}
                  {...wm.tabProps(id)}
                  className={[
                    'px-3 py-1.5 rounded-full text-[11px] font-bold tracking-wide whitespace-nowrap',
                    'cursor-grab active:cursor-grabbing select-none transition-colors',
                    on
                      ? 'bg-claude-accent text-white shadow-sm shadow-claude-accent/25'
                      : 'text-gray-500 hover:bg-claude-paper hover:text-claude-accent',
                  ].join(' ')}
                >
                  {TITLES[id]}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
};

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
