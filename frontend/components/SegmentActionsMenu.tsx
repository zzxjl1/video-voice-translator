import React, { useState } from 'react';
import type { VoiceOption } from '../services/apiService';

export interface SegmentAction {
  label: string;
  onSelect: () => void;
  /** Shown greyed with the reason, rather than hidden. */
  disabledReason?: string;
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

const ActionRow: React.FC<{ action: SegmentAction; onRun: (a: SegmentAction) => void }> = ({ action, onRun }) => (
  <button
    type="button"
    disabled={Boolean(action.disabledReason)}
    title={action.disabledReason}
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
 * Screens rather than one long list: the actions, the submenus behind them, and
 * — behind the voice row — the voices. A single menu holding all of it would be
 * 26 items tall for Chinese before any action was reachable.
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
  /** 当前打开的二级菜单（null = 顶层）。 */
  const [submenu, setSubmenu] = useState<SegmentAction | null>(null);
  /** 正在输入自定义要求的那一项（null = 没在输入）。 */
  const [prompting, setPrompting] = useState<SegmentAction | null>(null);
  const [draft, setDraft] = useState('');

  const close = () => {
    setOpen(false);
    setPickingVoice(false);
    setSubmenu(null);
    setPrompting(null);
    setDraft('');
  };

  const currentLabel = currentVoice
    ? voices.find(v => v.id === currentVoice)?.label ?? currentVoice
    : '自动';

  const runAction = (action: SegmentAction) => {
    if (action.disabledReason) return;
    if (action.children) {
      setSubmenu(action);
      return;
    }
    if (action.prompt) {
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
            ) : submenu ? (
              <>
                <BackRow label="返回" onClick={() => setSubmenu(null)} />
                <div className="px-3 pb-1 text-[10px] font-bold uppercase tracking-wider text-gray-400">
                  {submenu.label}
                </div>
                <div className="border-t border-gray-100">
                  {submenu.children?.map(child => (
                    <ActionRow key={child.label} action={child} onRun={runAction} />
                  ))}
                </div>
              </>
            ) : (
              <>
                {actions.map(action => (
                  <ActionRow key={action.label} action={action} onRun={runAction} />
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
    </div>
  );
};

export default SegmentActionsMenu;
