import React from 'react';
import type { SeparationMode, SeparationBackends } from '../services/apiService';

/**
 * Backend picker for vocal separation.
 *
 * Replaces the old on/off switch, which could not express *how* separation
 * happens — that was fixed by the server's SEPARATION_MODE and the UI had no
 * say. Options are greyscaled when the server reports them as unusable, with
 * the server's own reason shown, so the user is never offered something that
 * would fail later.
 *
 * Two options only: the browser backend and "off". There is intentionally no
 * self-hosted (PyTorch) option (no GPU on the deployment) and no third-party
 * API backend — the 302.AI one was removed on 2026-10-08 as unreliable from
 * mainland networks.
 */

const OPTIONS: { mode: SeparationMode; label: string; hint: string }[] = [
  {
    mode: 'client',
    label: 'In-browser',
    hint: 'Runs the MDX-Net model locally via WebGPU/WASM. Free and private; the first run downloads a 64 MB model, cached afterwards.',
  },
  {
    mode: 'off',
    label: 'Off',
    hint: 'Keep the original mixed audio. Fastest, no separation, no extra download.',
  },
];

interface SeparationModeSelectorProps {
  value: SeparationMode;
  onChange: (mode: SeparationMode) => void;
  backends: SeparationBackends;
  /** Voice cloning needs separated vocals, so "Off" is not selectable then. */
  locked?: boolean;
}

const SeparationModeSelector: React.FC<SeparationModeSelectorProps> = ({
  value,
  onChange,
  backends,
  locked,
}) => {
  /**
   * 为什么这个选项不能选。
   *
   * 关于 Off 的那句：说的是**"Off 不可用"，不是"Off 被要求"**。原文案是
   * `Required while Voice Cloning is enabled`，而它会以 `Off: …` 的形式显示在
   * 当前选中项那一行上 —— 于是整句读成"要求关闭分离"，与它想表达的意思正好相反
   * （克隆需要人声轨，所以不能关）。改成直接说原因，放哪个位置读起来都对。
   */
  const reasonFor = (mode: SeparationMode): string | null => {
    if (locked && mode === 'off') return 'Voice Cloning needs isolated vocals';
    const info = backends[mode];
    if (mode !== 'off' && info && !info.available) {
      return info.reason ?? 'Not available on this server';
    }
    return null;
  };

  const selectedReason = reasonFor(value);

  // A disabled button that does not say why reads as broken, so the reason is
  // printed inline rather than hidden in a hover tooltip.
  const blockedNotes = OPTIONS.filter(o => o.mode !== 'off')
    .map(o => ({ label: o.label, reason: reasonFor(o.mode) }))
    .filter((n): n is { label: string; reason: string } => n.reason !== null);

  return (
    <div className="w-full">
      <div className="grid grid-cols-3 gap-1.5">
        {OPTIONS.map(({ mode, label }) => {
          const blocked = reasonFor(mode);
          const disabled = blocked !== null;
          const selected = value === mode;

          return (
            <button
              key={mode}
              type="button"
              onClick={() => !disabled && onChange(mode)}
              disabled={disabled}
              title={blocked ?? OPTIONS.find(o => o.mode === mode)?.hint}
              className={[
                'py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-200 border',
                selected
                  ? 'bg-claude-accent text-white border-claude-accent shadow-sm shadow-claude-accent/25'
                  : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50 hover:text-gray-800',
                disabled ? 'opacity-40 cursor-not-allowed hover:bg-white' : 'cursor-pointer',
              ].join(' ')}
            >
              {label}
            </button>
          );
        })}
      </div>

      {/* 「克隆开着、却停在 Off」这一种要单独说：它不是"当前选择的原因"，而是
          "没有可用后端、只能这样"。落到 `{label}: {reason}` 那种形式会被读反。 */}
      {locked && value === 'off' ? (
        <p className="text-[10px] leading-snug mt-1.5 px-0.5 text-amber-600">
          Off is selected because no separation backend can run right now — Voice
          Cloning will use the original mixed audio.
        </p>
      ) : selectedReason ? (
        <p className="text-[10px] leading-snug mt-1.5 px-0.5 text-amber-600">
          {OPTIONS.find(o => o.mode === value)?.label}: {selectedReason}
        </p>
      ) : blockedNotes.length > 0 ? (
        <p className="text-[10px] leading-snug mt-1.5 px-0.5 text-amber-600">
          {blockedNotes.map(n => `${n.label} unavailable — ${n.reason}`).join(' · ')}
        </p>
      ) : (
        <p className="text-[10px] leading-snug mt-1.5 px-0.5 text-gray-400">
          {OPTIONS.find(o => o.mode === value)?.hint}
        </p>
      )}
    </div>
  );
};

export default SeparationModeSelector;
