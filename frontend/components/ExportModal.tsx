import React from 'react';
import type {
  SubtitleCapabilities,
  SubtitleStyle,
  SubtitleTrack,
} from '../services/apiService';
import type { BurnCapability } from '../utils/burnCapability';
import { overlayLineStyle, resolveMetrics, type Rect } from '../utils/subtitleStyle';

/**
 * Export dialog.
 *
 * Kept deliberately short: four choices, one sentence explaining the selected
 * one, and — only when the chosen delivery can actually carry styling — a
 * preview. Anything that is not a decision the user has to make is left out,
 * including the notion of a previous export (the file is produced again on
 * demand and lands in the download folder; surfacing that as state was noise).
 *
 * The one thing it must not leave ambiguous is whether the user's font and
 * colour survive, because that is the failure people blame on the app.
 */

/** Plain-language delivery choice. Mapped to the server's `format` in App.tsx. */
export type SubtitleDelivery = 'off' | 'external' | 'embedded' | 'burn';

interface DeliveryOption {
  value: SubtitleDelivery;
  label: string;
  summary: string;
  /** Whether the user's font/colour/position survive. */
  styleKept: 'na' | 'no' | 'player' | 'yes';
}

const DELIVERIES: DeliveryOption[] = [
  {
    value: 'off',
    label: '不加字幕',
    summary: '不写入字幕。',
    styleKept: 'na',
  },
  {
    value: 'external',
    label: '外挂字幕',
    summary: '视频不含字幕，同时输出独立的 .srt 文件。',
    styleKept: 'no',
  },
  {
    value: 'embedded',
    label: '内嵌字幕',
    summary: '字幕作为轨道写入视频，播放时可开关、可切换语言。',
    styleKept: 'player',
  },
  {
    value: 'burn',
    label: '烧录字幕',
    summary:
      '字幕渲染进画面，任何播放器都可见。由这台设备的浏览器重新编码一遍视频，比内嵌慢，但画面里真的有字。',
    styleKept: 'yes',
  },
];

interface ExportModalProps {
  isOpen: boolean;
  onClose: () => void;
  capabilities: SubtitleCapabilities;
  burnCapability: BurnCapability;
  style: SubtitleStyle;
  delivery: SubtitleDelivery;
  onDeliveryChange: (delivery: SubtitleDelivery) => void;
  keepStyle: boolean;
  onKeepStyleChange: (keep: boolean) => void;
  /** Closes this dialog and switches the right column to the style editor. */
  onOpenStyleEditor: () => void;
  onExport: () => void;
  isExporting: boolean;
  exportError?: string;
  hasCues: boolean;
}

const ExportModal: React.FC<ExportModalProps> = ({
  isOpen,
  onClose,
  capabilities,
  burnCapability,
  style,
  delivery,
  onDeliveryChange,
  keepStyle,
  onKeepStyleChange,
  onOpenStyleEditor,
  onExport,
  isExporting,
  exportError,
  hasCues,
}) => {
  if (!isOpen) return null;

  const blockedReason = (value: SubtitleDelivery): string | null => {
    if (value === 'off' || value === 'external') return null;
    if (value === 'burn') {
      return burnCapability.available
        ? null
        : burnCapability.reason ?? '当前浏览器不支持视频编码';
    }
    const info = capabilities.soft;
    return info && !info.available ? info.reason ?? '服务器不支持内嵌字幕' : null;
  };

  const selected = DELIVERIES.find(d => d.value === delivery) ?? DELIVERIES[0];
  const styleApplies = delivery === 'burn' || (delivery === 'embedded' && keepStyle);
  const styledUnavailable = capabilities.styled ? !capabilities.styled.available : false;
  const blocked = blockedReason(delivery);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md bg-white rounded-2xl border border-gray-200 shadow-xl"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 pt-5">
          <h2 className="text-sm font-semibold text-gray-800">导出视频</h2>
          <button
            type="button"
            onClick={onClose}
            className="w-7 h-7 -mr-1 flex items-center justify-center rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-600 cursor-pointer"
            aria-label="关闭"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="px-6 py-5 flex flex-col gap-5">
          {/* Delivery */}
          <section>
            <div className="grid grid-cols-4 gap-1 p-1 bg-gray-100 rounded-xl">
              {DELIVERIES.map(option => {
                const disabled = blockedReason(option.value) !== null;
                const active = delivery === option.value;
                return (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => !disabled && onDeliveryChange(option.value)}
                    disabled={disabled}
                    title={blockedReason(option.value) ?? option.summary}
                    className={[
                      'py-1.5 rounded-lg text-xs font-medium transition-colors',
                      active
                        ? 'bg-white text-gray-900 shadow-sm'
                        : 'text-gray-500 hover:text-gray-800',
                      disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer',
                    ].join(' ')}
                  >
                    {option.label}
                  </button>
                );
              })}
            </div>
            <p className="text-xs leading-relaxed text-gray-500 mt-2.5">
              {blocked ?? selected.summary}
            </p>
          </section>

          {/* Embedded: the styling trade-off */}
          {delivery === 'embedded' && (
            <label
              className={[
                'flex items-start gap-2.5 select-none',
                styledUnavailable ? 'cursor-not-allowed' : 'cursor-pointer',
              ].join(' ')}
            >
              <input
                type="checkbox"
                className="mt-0.5 accent-claude-accent"
                checked={keepStyle}
                disabled={styledUnavailable}
                onChange={e => onKeepStyleChange(e.target.checked)}
              />
              <span className="flex flex-col gap-0.5">
                <span className="text-xs font-medium text-gray-800">保留字幕样式</span>
                <span className="text-xs leading-relaxed text-gray-500">
                  {styledUnavailable
                    ? styledInfoReason(capabilities)
                    : '输出 .mkv 以保留字体与颜色，部分平台不支持该封装。'}
                </span>
              </span>
            </label>
          )}

          {/* Style — only when the chosen delivery can carry it. The summary
              line matters as much as the thumbnail: it names the settings in
              words, so a subtle difference (bold off, no background box) is
              readable rather than something to squint at. */}
          {styleApplies && (
            <section className="flex flex-col gap-2">
              <span className="text-xs font-medium text-gray-800">字幕样式</span>
              <div className="rounded-xl border border-gray-200 overflow-hidden">
                <div className="relative bg-gray-900" style={{ height: '5rem' }}>
                  <StyleThumbnail style={style} />
                </div>
                <div className="flex items-center justify-between gap-3 px-3 py-2">
                  <span className="text-xs text-gray-500 truncate" title={describeStyle(style)}>
                    {describeStyle(style)}
                  </span>
                  <button
                    type="button"
                    onClick={onOpenStyleEditor}
                    className="shrink-0 text-[11px] font-medium px-2.5 py-1 rounded-full text-claude-accent border border-claude-accent/30 hover:bg-claude-accent/5 transition-colors cursor-pointer"
                  >
                    调整样式 →
                  </button>
                </div>
              </div>
            </section>
          )}

          {!hasCues && (
            <p className="text-xs text-gray-500">尚无译文，请先完成转录与翻译。</p>
          )}

          {exportError && (
            <p className="text-xs text-red-500" title={exportError}>
              导出失败：{exportError.slice(0, 160)}
            </p>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 px-6 pb-5">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 rounded-lg text-xs font-medium text-gray-500 hover:bg-gray-100 hover:text-gray-800 transition-colors cursor-pointer"
          >
            取消
          </button>
          <button
            type="button"
            onClick={onExport}
            disabled={isExporting || !hasCues}
            className="px-4 py-2 rounded-lg text-xs font-medium bg-claude-accent text-white hover:bg-claude-accent/90 transition-colors disabled:bg-gray-200 disabled:text-gray-400 disabled:cursor-not-allowed cursor-pointer"
          >
            {isExporting ? '导出中…' : '导出'}
          </button>
        </div>
      </div>
    </div>
  );
};

function styledInfoReason(capabilities: SubtitleCapabilities): string {
  return capabilities.styled?.reason ?? '服务器不支持该输出格式';
}

/** The style in words, so a subtle change is readable at a glance. */
function describeStyle(style: SubtitleStyle): string {
  const alignment =
    style.alignment === 'top' ? '顶部' : style.alignment === 'center' ? '居中' : '底部';
  const weight = style.bold ? '加粗' : '常规';
  const box = style.background === 'box' ? ' · 带底色' : '';
  return `${alignment} · ${style.primary_color} · ${style.font_size_percent.toFixed(1)}% 高 · ${weight}${box}`;
}

/**
 * A miniature of the subtitle as it will look.
 *
 * Drawn with the same `overlayLineStyle` the real overlay uses, against a
 * synthetic 16:9 frame, so the thumbnail cannot drift from the player.
 */
const StyleThumbnail: React.FC<{ style: SubtitleStyle }> = ({ style }) => {
  const rect: Rect = { left: 0, top: 0, width: 320, height: 180 };
  const metrics = resolveMetrics(style, rect.width, rect.height);
  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent:
          style.alignment === 'top'
            ? 'flex-start'
            : style.alignment === 'center'
              ? 'center'
              : 'flex-end',
        padding: `${rect.height * style.margin_v_percent / 100}px ${rect.width * style.margin_h_percent / 100}px`,
        boxSizing: 'border-box',
      }}
    >
      <div
        style={{
          ...overlayLineStyle(style, rect),
          fontSize: Math.max(7, metrics.fontSize * 0.55),
        }}
      >
        你好，欢迎回来
      </div>
    </div>
  );
};

export default ExportModal;
