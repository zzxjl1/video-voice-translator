import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { VoiceOption } from '../services/apiService';

export interface SegmentAction {
  label: string;
  onSelect: () => void;
  /** Shown greyed with the reason, rather than hidden. */
  disabledReason?: string;
  /**
   * 一句话说明这一项到底做什么，作为 hover 提示（不占行高）。
   * 「静音」和「隐藏」很容易混：一个管声音、一个管画面上的字。
   */
  hint?: string;
  /** 二级菜单：有 children 就显示右箭头，点进去是第二屏。 */
  children?: SegmentAction[];
  /** 需要一行文字输入（自定义要求）：点进去是输入屏。 */
  prompt?: {
    placeholder: string;
    submitLabel?: string;
    onSubmit: (value: string) => void;
  };
}

interface SegmentActionsMenuProps {
  /** Whose voice the picker would change. Shown so that is never a guess. */
  speakerName: string;
  /**
   * System voices to choose from. EMPTY means the picker is not offered —
   * that is how "voice cloning is on" is expressed, because a cloned voice
   * overrides any system voice the user could pick and offering the choice
   * would be offering something that does nothing.
   */
  voices: VoiceOption[];
  /**
   * True when the target language has no built-in voice at all.
   *
   * Distinct from `voices` being empty for other reasons, and worth saying out
   * loud: the user is looking for a voice picker that is not there, and "turn on
   * voice cloning" is the answer. Silence would read as a missing feature.
   */
  needsVoiceCloning?: boolean;
  /** The pinned voice, or undefined for "automatic". */
  currentVoice?: string;
  onPickVoice: (voiceId: string) => void;
  actions: SegmentAction[];
}

const ChevronRight = () => (
  <svg className="w-3 h-3 shrink-0 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M9 5l7 7-7 7" />
  </svg>
);

const ChevronLeft = () => (
  <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M15 19l-7-7 7-7" />
  </svg>
);

const BackRow: React.FC<{ label: string; onClick: () => void }> = ({ label, onClick }) => (
  <button
    type="button"
    onClick={onClick}
    className="w-full flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider text-gray-400 hover:text-gray-700"
  >
    <ChevronLeft />
    {label}
  </button>
);

/**
 * 级联面板的尺寸。宽度只按内容给：二级项最长的是「自定义要求」五个字，
 * text-xs 下约 60px，加左右内边距 24px 后 128px 已经很宽松 —— 再宽就是
 * 一片空白的白块。高度按三项内容估算，只用于夹取位置。
 */
const FLYOUT_WIDTH = 128;
const FLYOUT_HEIGHT = 92;

const ActionRow: React.FC<{ action: SegmentAction; onRun: (a: SegmentAction) => void }> = ({ action, onRun }) => (
  <button
    type="button"
    disabled={Boolean(action.disabledReason)}
    title={action.disabledReason ?? action.hint}
    onClick={() => onRun(action)}
    className="w-full flex items-center justify-between gap-2 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 transition-colors disabled:text-gray-300 disabled:hover:bg-transparent disabled:cursor-not-allowed"
  >
    <span>{action.label}</span>
    {action.children ? <ChevronRight /> : null}
  </button>
);

/**
 * The "⋯" menu on a transcript row.
 *
 * Submenus cascade on HOVER (a flyout next to the row, Windows-Start style),
 * not by swapping the panel to a second screen: swapping costs a click to
 * enter, a click to leave, and a "back" row, for two or three short items
 * that are meant to be scanned and picked quickly.
 *
 * The voice list stays a click-in screen — it is 26 items for Chinese and
 * would make a poor flyout.
 */
const SegmentActionsMenu: React.FC<SegmentActionsMenuProps> = ({
  speakerName,
  voices,
  needsVoiceCloning,
  currentVoice,
  onPickVoice,
  actions,
}) => {
  const [open, setOpen] = useState(false);
  const [pickingVoice, setPickingVoice] = useState(false);
  /**
   * 鼠标当前悬停的父项（有二级菜单的那一行），null = 没有级联展开。
   *
   * 二级菜单是 hover 飞出的（Windows 开始菜单那种），不是"点进去换一屏"：
   * 换屏要两次点击、还多一个"返回"要处理，而这里的二级项（长一点/短一点/
   * 自定义要求）本来就是要快速扫一眼顺手点的。hover 让它一步可达。
   *
   * 坐标是【视口坐标】：面板用 portal 挂到 body 上、fixed 定位，见下方
   * 渲染处的解释。
   */
  const [flyout, setFlyout] = useState<{ label: string; left: number; top: number } | null>(null);
  const closeTimer = useRef<number | null>(null);
  /** 父项 DOM 节点：点击兜底展开时要用它量位置（hover 事件自带 currentTarget）。 */
  const anchorRef = useRef<Record<string, HTMLDivElement | null>>({});
  /** 正在输入自定义要求的那一项（null = 没在输入）。 */
  const [prompting, setPrompting] = useState<SegmentAction | null>(null);
  const [draft, setDraft] = useState('');

  const close = () => {
    setOpen(false);
    setPickingVoice(false);
    setFlyout(null);
    setPrompting(null);
    setDraft('');
    if (closeTimer.current) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  };

  /** 取消待执行的关闭（指针进入了级联面板/父项时）。 */
  const cancelFlyoutClose = () => {
    if (closeTimer.current) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  };

  /**
   * 展开某个父项的级联菜单。方向固定向【右】——标准级联方向。
   *
   * 位置按父项的视口矩形算：只在两种极端下夹回窗口内（宁可压住父项，也不
   * 让菜单出屏）——窗口比 128px 还窄，或行贴着右边缘。正常情况下它就在
   * 父项右侧 4px 处。
   */
  const openFlyout = (label: string, el?: HTMLElement | null) => {
    cancelFlyoutClose();
    const rect = el?.getBoundingClientRect();
    if (!rect) return;
    const left = Math.min(rect.right + 4, window.innerWidth - FLYOUT_WIDTH - 8);
    let top = rect.top - 4;
    // 高度按三项估算：底部放不下就上移，别让最后一项掉出屏幕。
    const over = top + FLYOUT_HEIGHT + 8 - window.innerHeight;
    if (over > 0) top -= over;
    setFlyout({ label, left, top });
  };

  /**
   * 延迟关闭。面板挂在 body 上（不是这一行的子节点），指针从父项移过去时
   * 会先离开父项 —— 这 180ms 就是给这段路程的：指针一进面板，面板自己的
   * onMouseEnter 会取消它。同时也兜住"手一抖"。
   */
  const scheduleFlyoutClose = () => {
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    closeTimer.current = window.setTimeout(() => setFlyout(null), 180);
  };

  // fixed 定位不跟随滚动：一旦页面（或内部滚动列表）动了，面板就会和父项
  // 脱节 —— 直接收起，和系统菜单的行为一致。
  useEffect(() => {
    if (!flyout) return;
    const dismiss = () => setFlyout(null);
    window.addEventListener('scroll', dismiss, true);
    window.addEventListener('resize', dismiss);
    return () => {
      window.removeEventListener('scroll', dismiss, true);
      window.removeEventListener('resize', dismiss);
    };
  }, [flyout]);

  /** 当前展开的那一项（按 label 找回来，面板内容由它决定）。 */
  const flyoutAction = flyout ? actions.find(a => a.label === flyout.label) : null;

  const currentLabel = currentVoice
    ? voices.find(v => v.id === currentVoice)?.label ?? currentVoice
    : '自动';

  const runAction = (action: SegmentAction) => {
    if (action.disabledReason) return;
    if (action.children) {
      // 点击也展开（触屏没有 hover；桌面端是 hover 之外的兜底）。
      openFlyout(action.label, anchorRef.current?.[action.label]);
      return;
    }
    if (action.prompt) {
      setFlyout(null);
      setPrompting(action);
      setDraft('');
      return;
    }
    action.onSelect();
    close();
  };

  return (
    <div className="relative">
      <button
        type="button"
        aria-label="更多操作"
        aria-expanded={open}
        onClick={() => (open ? close() : setOpen(true))}
        className="p-1.5 rounded-md hover:bg-claude-paper text-gray-400 hover:text-claude-accent transition-colors"
        title="更多操作"
      >
        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor">
          <circle cx="12" cy="5" r="1.8" />
          <circle cx="12" cy="12" r="1.8" />
          <circle cx="12" cy="19" r="1.8" />
        </svg>
      </button>

      {open && (
        <>
          {/* Click-away: a full-screen layer instead of a document listener,
              so there is nothing to add and forget to remove. */}
          <span className="fixed inset-0 z-30" onClick={close} />

          <div className="absolute right-0 top-full mt-1 z-40 w-60 bg-white rounded-xl border border-gray-200 shadow-xl py-1 text-left">
            {pickingVoice ? (
              <>
                <BackRow label="返回" onClick={() => setPickingVoice(false)} />
                {/* Wrapped rather than truncated: the part that gets cut is
                    "affects all of this speaker's lines", which is the whole
                    reason this list is reached from a per-LINE menu. */}
                <div className="px-3 pb-1.5 text-[10px] leading-snug text-gray-400">
                  {speakerName} 的音色 · 影响这个说话人的所有台词
                </div>
                <div className="max-h-52 overflow-y-auto border-t border-gray-100">
                  <button
                    type="button"
                    onClick={() => {
                      onPickVoice('');
                      close();
                    }}
                    className={`w-full text-left px-3 py-1.5 text-xs transition-colors ${
                      !currentVoice
                        ? 'bg-claude-accent/10 text-claude-accent font-bold'
                        : 'text-gray-600 hover:bg-gray-50'
                    }`}
                  >
                    自动
                  </button>
                  {voices.map(voice => (
                    <button
                      key={voice.id}
                      type="button"
                      onClick={() => {
                        onPickVoice(voice.id);
                        close();
                      }}
                      className={`w-full text-left px-3 py-1.5 text-xs transition-colors ${
                        currentVoice === voice.id
                          ? 'bg-claude-accent/10 text-claude-accent font-bold'
                          : 'text-gray-600 hover:bg-gray-50'
                      }`}
                    >
                      {voice.label}
                    </button>
                  ))}
                </div>
              </>
            ) : prompting ? (
              <>
                <BackRow label="返回" onClick={() => setPrompting(null)} />
                <div className="px-3 pb-1.5 text-[10px] leading-snug text-gray-400">
                  只对这句话生效，会覆盖默认的翻译要求。
                </div>
                <div className="px-3 pb-2">
                  <textarea
                    autoFocus
                    rows={3}
                    value={draft}
                    onChange={e => setDraft(e.target.value.slice(0, 500))}
                    placeholder={prompting.prompt?.placeholder}
                    className="w-full resize-none rounded-lg border border-gray-200 px-2 py-1.5 text-xs leading-relaxed text-gray-700 placeholder:text-gray-300 focus:outline-none focus:border-claude-accent/50"
                  />
                  <button
                    type="button"
                    disabled={!draft.trim()}
                    onClick={() => {
                      prompting.prompt?.onSubmit(draft.trim());
                      close();
                    }}
                    className="mt-1.5 w-full rounded-lg bg-claude-accent px-3 py-1.5 text-xs font-bold text-white transition hover:bg-claude-accentHover disabled:bg-gray-200 disabled:text-gray-400"
                  >
                    {prompting.prompt?.submitLabel ?? '重新翻译'}
                  </button>
                </div>
              </>
            ) : (
              <>
                {actions.map(action => (
                  <div
                    key={action.label}
                    ref={el => {
                      anchorRef.current[action.label] = el;
                    }}
                    className="relative"
                    onMouseEnter={e =>
                      action.children && openFlyout(action.label, e.currentTarget)
                    }
                    onMouseLeave={() => action.children && scheduleFlyoutClose()}
                  >
                    <ActionRow action={action} onRun={runAction} />
                  </div>
                ))}

                {voices.length > 0 ? (
                  <>
                    <div className="border-t border-gray-100 my-1" />
                    <button
                      type="button"
                      onClick={() => setPickingVoice(true)}
                      className="w-full flex items-center justify-between gap-2 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 transition-colors"
                    >
                      <span>
                        {speakerName} 的音色
                        <span className="ml-1.5 text-claude-accent font-bold">{currentLabel}</span>
                      </span>
                      <ChevronRight />
                    </button>
                  </>
                ) : needsVoiceCloning ? (
                  <>
                    <div className="border-t border-gray-100 my-1" />
                    <p className="px-3 py-1.5 text-[11px] leading-snug text-amber-600">
                      没有内置音色能说这个目标语言。开启声音复刻后，用说话人自己的音色来配。
                    </p>
                  </>
                ) : null}
              </>
            )}
          </div>
        </>
      )}

      {/*
        级联面板 portal 到 body。
        ─────────────────────────────────────────────────────────────
        不能就地绝对定位（更不是 z-index 能救的）：这个菜单挂在转写列表里，
        祖先有 `rounded-3xl overflow-hidden` 的面板容器和 `overflow-y-auto`
        的滚动列表 —— 后者会把 x 轴也变成裁切轴（CSS 里一轴 visible、另一轴
        非 visible 时，visible 那侧会计算成 auto）。被滚动容器裁掉的元素，
        z-index 再高也出不来。
        portal + fixed 让面板脱离那些裁剪与层叠上下文，同时用视口坐标贴住
        父项；z-[100] 只是保证它压过菜单面板（z-40）和点击遮罩（z-30）。
      */}
      {flyoutAction &&
        createPortal(
          <div
            style={{ left: flyout!.left, top: flyout!.top, width: FLYOUT_WIDTH }}
            className="fixed z-[100] rounded-xl border border-gray-200 bg-white py-1 shadow-xl"
            onMouseEnter={cancelFlyoutClose}
            onMouseLeave={scheduleFlyoutClose}
          >
            {flyoutAction.children?.map(child => (
              <ActionRow key={child.label} action={child} onRun={runAction} />
            ))}
          </div>,
          document.body
        )}
    </div>
  );
};

export default SegmentActionsMenu;
