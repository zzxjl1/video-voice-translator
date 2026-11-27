import React from 'react';
import InfoHint from './InfoHint';
import type {
  SubtitlePreset,
  SubtitleStyle,
  SubtitleStylePatch,
  SubtitleAlignment,
  SubtitleTrack,
} from '../net/types';
import type { HeaderProps } from '../hooks/useWindows';
import { overlayContainerStyle, overlayLineStyle, resolveMetrics, type Rect } from '../utils/subtitleStyle';

/* Shared with TranscriptionPanel — the two panels sit in the same column. */
const CARD =
  'flex-grow flex flex-col h-full bg-[#fbfbf9] border border-[#e5e5e0] ' +
  'rounded-3xl overflow-hidden shadow-sm';
/** 浮动时：窗口外框已经画了边框、圆角和阴影。 */
const CARD_FLOATING = 'flex-grow flex flex-col h-full bg-[#fbfbf9] overflow-hidden';
const CARD_HEAD =
  'px-5 py-4 border-b border-[#e5e5e0] flex items-center justify-between ' +
  'bg-white/60 backdrop-blur-md';
const CARD_TITLE =
  'text-xs font-serif font-bold text-gray-700 flex items-center gap-2 tracking-wide';
const CARD_BODY =
  'flex-grow overflow-y-auto p-4 space-y-3 scroll-smooth';

/**
 * No `hint` on the alignments on purpose. These labels describe themselves; an
 * "i" next to each would be decoration that makes the panel busier without
 * answering anything a reader actually wondered.
 */
const ALIGNMENTS: { value: SubtitleAlignment; label: string }[] = [
  { value: 'top', label: '顶部' },
  { value: 'center', label: '居中' },
  { value: 'bottom', label: '底部' },
];

/**
 * What text a subtitle line contains.
 *
 * A SINGLE choice, not a set. "Bilingual" is not a peer of the other two — it
 * is both of them in one line — so offering all three as tickable options let
 * the user express "translated, and also both", which is not a thing. One
 * choice also keeps the preview honest: only one line can be on screen at a
 * time, so a multi-select could never have been previewed truthfully.
 */
const CONTENTS: { value: SubtitleTrack; label: string; hint?: string }[] = [
  { value: 'translated', label: '译文' },
  { value: 'original', label: '原文' },
  // Only this one carries a hint: "译文" and "原文" mean what they say, but
  // "双语" could plausibly be read as "add a second track" instead of "put both
  // into one line", and that difference is invisible until after the export.
  {
    value: 'bilingual',
    label: '双语',
    hint: '把译文和原文并排放在同一条字幕里（译文在前），不是多加一条轨。',
  },
];

const TEXT_COLOR_PRESETS = ['#FFFFFF', '#FFE066', '#FFCC00', '#7DF9FF', '#FF9AA2'];
const OUTLINE_COLOR_PRESETS = ['#000000', '#FFFFFF', '#374151', '#7F1D1D', '#1E3A8A'];

/**
 * Faces common enough to be worth naming.
 *
 * Deliberately a list and not a text box: the name is written into the subtitle
 * file and resolved by whoever opens it, so a typo produces a silent fallback
 * on their machine and nothing at all on ours.
 */
const FONT_CHOICES = [
  'Source Han Sans',
  'Noto Sans SC',
  'PingFang SC',
  'Microsoft YaHei',
  'SimHei',
  'SimSun',
] as const;

/** 本机能不能渲染这个字体（document.fonts.check 对系统字体名有效）。 */
const isFontAvailable = (family: string): boolean => {
  if (typeof document === 'undefined' || !document.fonts?.check) return true;
  try {
    // 带一段 CJK 样本，否则 check 只测拉丁字形
    return document.fonts.check(`16px "${family}"`, '字幕Aa');
  } catch {
    return true;
  }
};

function useFontAvailability(): Record<string, boolean> {
  const [avail, setAvail] = React.useState<Record<string, boolean>>({});
  React.useEffect(() => {
    document.fonts?.ready.then(() => {
      setAvail(Object.fromEntries(FONT_CHOICES.map((f) => [f, isFontAvailable(f)])));
    });
  }, []);
  return avail;
}

/**
 * 帧对齐选项：值是 fps，显示是文案。
 */
const FPS_CHOICES: Record<number, string> = {
  0: '不对齐', 23.976: '23.976（电影）', 24: '24', 25: '25（PAL）', 30: '30', 60: '60',
};

/**
 * One label column for every row, so the controls line up. Wide enough for the
 * rows that carry an InfoHint.
 */
const LABEL = 'flex items-center gap-1 text-[11px] font-semibold text-[#525252] shrink-0';
const LABEL_MUTED = 'flex items-center gap-1 text-[11px] font-medium text-gray-500 shrink-0 w-[4.5rem]';

const OPTION_BASE =
  'py-2 rounded-xl text-[12px] font-semibold border transition-all duration-200 ' +
  'flex items-center justify-center gap-1 cursor-pointer';
/**
 * Selected = accent border + light tint, NOT a solid fill.
 *
 * That is how the sibling TranscriptionPanel marks the active card
 * (`border-claude-accent` with a `ring-claude-accent/30`), and how its rate
 * badges read (`bg-claude-accent/10 text-claude-accent`). Two solid orange
 * blocks in a row would read as two destructive buttons.
 */
const OPTION_ON =
  'border-claude-accent bg-claude-accent/10 text-claude-accent shadow-sm shadow-claude-accent/20';
const OPTION_OFF = 'bg-white text-gray-600 border-[#e5e5e0] hover:border-[#d1d1cc] hover:bg-claude-paper/50';

/* 样式本体在 index.css 的 .vvt-range（Tailwind 伪元素 arbitrary variant 不生效） */
const RANGE = 'vvt-range w-full';

const ACCENT = '#DA7756';
const TRACK = '#F2F0E9';

/**
 * 分层：`card` = 主要卡片（白底，控件大）；`secondary` = 进阶区（纸色底、行紧凑、
 * 控件小一号）。视觉上"重要的先看到"，但不折叠——进阶项仍在一屏内。
 */
const Group: React.FC<{ title: string; tone?: 'card' | 'secondary'; children: React.ReactNode }> = ({
  title,
  tone = 'card',
  children,
}) => (
  <section
    className={
      tone === 'card'
        ? 'bg-white border border-[#ECEAE3] rounded-2xl px-4 py-3.5'
        : 'bg-claude-paper/40 border border-[#ECEAE3] rounded-2xl px-4 py-3'
    }
  >
    <div className="flex items-center gap-2 mb-3">
      <span
        className={
          tone === 'card'
            ? 'text-[9px] font-bold uppercase tracking-[0.18em] text-claude-accent/80'
            : 'text-[9px] font-bold uppercase tracking-[0.18em] text-gray-400'
        }
      >
        {tone === 'secondary' ? `进阶 · ${title}` : title}
      </span>
      {tone === 'secondary' && <span className="flex-1 border-t border-dashed border-[#E3E1D8]" />}
    </div>
    <div className="space-y-3.5">{children}</div>
  </section>
);

interface SliderProps {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  display: string;
  /** Only pass this where the label alone misleads. See rule 3. */
  hint?: string;
  /** `secondary` = 进阶区的紧凑行（标签左、滑条中、值右）。 */
  tone?: 'card' | 'secondary';
  onChange: (value: number) => void;
}

const Slider: React.FC<SliderProps> = ({ label, value, min, max, step, display, hint, tone = 'card', onChange }) => {
  const pct = max > min ? ((value - min) / (max - min)) * 100 : 0;
  const track = {
    backgroundImage: `linear-gradient(to right, ${ACCENT} 0%, ${ACCENT} ${pct}%, ${TRACK} ${pct}%, ${TRACK} 100%)`,
    backgroundSize: '100% 5px',
    backgroundPosition: 'center',
    backgroundRepeat: 'no-repeat',
  };
  if (tone === 'secondary') {
    return (
      <div className="flex items-center gap-2.5">
        <span className={LABEL_MUTED}>
          {label}
          {hint && <InfoHint text={hint} standalone />}
        </span>
        <input
          type="range"
          aria-label={label}
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={e => onChange(Number(e.target.value))}
          style={track}
          className={RANGE}
        />
        <span className="text-[10px] text-gray-400 w-14 text-right tabular-nums">{display}</span>
      </div>
    );
  }
  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <span className={LABEL}>
          {label}
          {hint && <InfoHint text={hint} standalone />}
        </span>
        <span className="text-[10px] font-bold text-claude-accent bg-claude-accent/10 px-2 py-0.5 rounded-full tabular-nums">
          {display}
        </span>
      </div>
      {/*
        * The range carries NO `title`: the old one was attached to the input, so
        * a tooltip appeared and tracked the pointer for the whole drag.
        */}
      <input
        type="range"
        aria-label={label}
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={e => onChange(Number(e.target.value))}
        style={track}
        className={RANGE}
      />
    </div>
  );
};

/** 开关行：标签左、iOS 式开关右。代替裸 checkbox。 */
const Switch: React.FC<{
  label: string;
  hint?: string;
  checked: boolean;
  tone?: 'card' | 'secondary';
  onChange: (checked: boolean) => void;
}> = ({ label, hint, checked, tone = 'card', onChange }) => (
  <div className="flex items-center justify-between">
    <span className={tone === 'card' ? LABEL : LABEL_MUTED}>
      {label}
      {hint && <InfoHint text={hint} standalone />}
    </span>
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative w-[34px] h-[19px] rounded-full transition-colors duration-150 cursor-pointer ${checked ? 'bg-claude-accent' : 'bg-[#D9D6CD]'}`}
    >
      <span
        className={`absolute top-[2.5px] left-[2.5px] w-[14px] h-[14px] rounded-full bg-white shadow transition-transform duration-150 ${checked ? 'translate-x-[15px]' : ''}`}
      />
    </button>
  </div>
);

interface ColorRowProps {
  label: string;
  value: string;
  presets: string[];
  /** `secondary` = 进阶区的紧凑行。 */
  tone?: 'card' | 'secondary';
  onChange: (value: string) => void;
}

const ColorRow: React.FC<ColorRowProps> = ({ label, value, presets, tone = 'card', onChange }) => {
  const [text, setText] = React.useState<string | null>(null);
  return (
    <div className="flex items-center gap-2.5">
      <span className={tone === 'card' ? LABEL : LABEL_MUTED}>
        {label}
      </span>
      <label className="relative cursor-pointer shrink-0" title="取色器">
        <span
          className="block w-[20px] h-[20px] rounded-full border-2 border-white shadow-[0_0_0_1px_var(--border)]"
          style={{ '--border': '#E6E4DD', background: normaliseHex(value) } as React.CSSProperties}
        />
        <input
          type="color"
          value={normaliseHex(value)}
          onChange={e => { setText(null); onChange(e.target.value); }}
          className="absolute inset-0 opacity-0 cursor-pointer"
        />
      </label>
      <input
        value={text ?? value.toUpperCase()}
        onChange={e => { setText(e.target.value); if (normaliseHex(e.target.value) !== '#FFFFFF' || /^#[0-9a-fA-F]{6}$/.test(e.target.value)) onChange(normaliseHex(e.target.value)); }}
        onFocus={() => setText(value.toUpperCase())}
        spellCheck={false}
        className="w-[70px] text-[10.5px] font-semibold text-gray-500 bg-claude-paper px-2 py-1 rounded-lg text-center tabular-nums focus:outline-none focus:ring-1 focus:ring-claude-accent/40"
      />
      <div className="flex gap-1.5 ml-auto">
        {presets.map(c => (
          <button
            key={c}
            type="button"
            title={c}
            onClick={() => { setText(null); onChange(c); }}
            className="w-[16px] h-[16px] rounded-full border-2 border-white shadow-[0_0_0_1px_rgba(0,0,0,.12)] cursor-pointer hover:scale-110 transition-transform"
            style={{ background: c }}
          />
        ))}
      </div>
    </div>
  );
};

function normaliseHex(value: string): string {
  return /^#[0-9a-fA-F]{6}$/.test(value) ? value : '#FFFFFF';
}

interface SelectRowProps {
  label: string;
  value: string;
  options: string[];
  hint?: string;
  /** `secondary` = 进阶区的紧凑行。 */
  tone?: 'card' | 'secondary';
  onChange: (value: string) => void;
}

const SelectRow: React.FC<SelectRowProps> = ({ label, value, options, hint, tone = 'card', onChange }) => (
  <div className="flex items-center gap-2.5">
    <span className={tone === 'card' ? LABEL : LABEL_MUTED}>
      {label}
      {hint && <InfoHint text={hint} standalone />}
    </span>
    <select
      value={value}
      onChange={e => onChange(e.target.value)}
      className={
        tone === 'card'
          ? 'flex-1 text-[12px] font-semibold text-[#525252] bg-white border border-[#e5e5e0] rounded-xl px-2.5 py-1.5 cursor-pointer focus:outline-none focus:ring-1 focus:ring-claude-accent/40'
          : 'flex-1 ml-auto text-[11px] text-gray-600 bg-white/70 border border-[#e5e5e0] rounded-lg px-2 py-1 cursor-pointer focus:outline-none max-w-[9rem] text-right'
      }
    >
      {!options.includes(value) && <option value={value}>{value}</option>}
      {options.map(option => (
        <option key={option} value={option}>
          {option}
        </option>
      ))}
    </select>
  </div>
);

/**
 * 字体下拉：和 SelectRow 一样，但选项后面标"本机没有"。
 * 值仍然是裸字体名（进样式），显示文案只是给用户看的。
 */
const FontSelectRow: React.FC<{ label: string; value: string; onChange: (value: string) => void }> = ({
  label, value, onChange,
}) => {
  const avail = useFontAvailability();
  // 值不在列表里（老工程存了别的名字）：也列出来，别让下拉显示空
  const options = FONT_CHOICES.includes(value as (typeof FONT_CHOICES)[number])
    ? [...FONT_CHOICES]
    : [value, ...FONT_CHOICES];
  return (
    <SelectRow
      label={label}
      value={value}
      options={options.map((f) => (avail[f] === false ? `${f}（本机没有）` : f))}
      hint="写进字幕文件的字体名。预览和烧录用本机字库渲染，没有的会回退到同类系统字体；标了「本机没有」的在预览里看到的其实是回退效果。MKV/ASS 在观众机器上由播放器解析。"
      tone="secondary"
      // 显示文案可能带「（本机没有）」后缀，写进样式的要是裸字体名
      onChange={(label) => onChange(label.replace(/（本机没有）$/, ''))}
    />
  );
};

/**
 * 顶部实时预览：一块仿视频画面，当前样式直接画在上面——改任何一项立刻看到，
 * 不用理解"4.5% 高"是什么。字号做了放大（在虚拟 150px 高的画面上按比例太小），
 * 但描边/底框/位置/双语分层都是真实的渲染路径（overlayLineStyle）。
 */
const PREVIEW_PRIMARY = '这都不算亚洲人？兄弟，他自曝了！';
const PREVIEW_SECONDARY = 'If this is not Asian, bro, he just incriminated himself!';

const PreviewFrame: React.FC<{ style: SubtitleStyle }> = ({ style }) => {
  const rect: Rect = { left: 0, top: 0, width: 320, height: 150 };
  const metrics = resolveMetrics(style, rect.width, rect.height);
  const fs = Math.min(18, Math.max(11, metrics.fontSize * 2.2));
  const secFs = Math.min(15, Math.max(9.5, metrics.secondaryFontSize * 2.2));
  const lines =
    style.track === 'original'
      ? [{ text: PREVIEW_SECONDARY, sec: false }]
      : style.track === 'bilingual'
        ? [
            { text: PREVIEW_PRIMARY, sec: false },
            { text: PREVIEW_SECONDARY, sec: true },
          ]
        : [{ text: PREVIEW_PRIMARY, sec: false }];
  return (
    <div
      className="relative rounded-2xl overflow-hidden border border-[#E3E1D8] shrink-0"
      style={{
        height: 150,
        background: 'linear-gradient(135deg, #3E4A5B 0%, #2A3442 55%, #1F2833 100%)',
      }}
    >
      {/* 画面感：一点高光 */}
      <div
        className="absolute inset-0"
        style={{ background: 'radial-gradient(circle at 72% 22%, rgba(255,255,255,.13), transparent 46%)' }}
      />
      <div style={overlayContainerStyle(style, rect)}>
        {lines.map((l, i) => (
          <div key={i} style={{ ...overlayLineStyle(style, rect, l.sec, l.text), fontSize: l.sec ? secFs : fs, lineHeight: 1.3 }}>
            {l.text}
          </div>
        ))}
      </div>
      <span className="absolute top-2 right-2.5 text-[9px] font-bold text-white/45 tracking-[0.15em]">预览</span>
    </div>
  );
};

interface SubtitleStylePanelProps {
  style: SubtitleStyle;
  onStyleChange: (patch: SubtitleStylePatch) => void;
  /** 可用预设（后端 subtitle_presets.py 是权威）；空 = 不显示预设组。 */
  presets?: SubtitlePreset[];
  onApplyPreset?: (id: string) => void;
  /** 吸附在右栏时自己画卡片的边框和圆角；浮动时由窗口外框画。 */
  docked?: boolean;
  /** 标题栏同时是窗口的拖动区。 */
  dragProps?: HeaderProps;
  /** 标题栏右边的窗口按钮（吸附 / 弹出、关闭）。 */
  windowControls?: React.ReactNode;
}

const SubtitleStylePanel: React.FC<SubtitleStylePanelProps> = ({
  style,
  onStyleChange,
  presets,
  onApplyPreset,
  docked = true,
  dragProps,
  windowControls,
}) => (
  <div className={docked ? CARD : CARD_FLOATING}>
    <div {...dragProps} className={`${CARD_HEAD} shrink-0 ${dragProps?.className ?? ''}`}>
      <h3 className={CARD_TITLE}>
        <span className="w-1.5 h-1.5 bg-claude-accent rounded-full"></span>
        字幕样式
      </h3>
      {windowControls}
    </div>

    <div className={CARD_BODY}>
      {/* 实时预览：改任何一项立刻看到，不用理解百分比参数 */}
      <PreviewFrame style={style} />

      {/* 预设是"一键到合规"：选完仍能下面微调。放最上面，因为它是起点不是终点。 */}
      {presets && presets.length > 0 && onApplyPreset && (
        <Group title="预设">
          <div className="flex flex-wrap gap-1.5">
            {presets.map(({ id, label, description }) => (
              <button
                key={id}
                type="button"
                onClick={() => onApplyPreset(id)}
                title={description}
                className="px-3 py-1.5 rounded-full text-[11.5px] font-semibold border border-[#e5e5e0] bg-white text-gray-600 hover:border-claude-accent hover:text-claude-accent hover:bg-claude-accent/5 transition-colors cursor-pointer"
              >
                {label}
              </button>
            ))}
          </div>
        </Group>
      )}

      <Group title="轨道与位置">
        <div className="grid grid-cols-3 gap-1.5">
          {CONTENTS.map(({ value, label, hint }) => (
            <button
              key={value}
              type="button"
              onClick={() => onStyleChange({ track: value })}
              className={[OPTION_BASE, style.track === value ? OPTION_ON : OPTION_OFF].join(' ')}
            >
              {label}
              {/* Inside the button, so the tap must not also pick the option. */}
              {hint && <InfoHint text={hint} swallowClick />}
            </button>
          ))}
        </div>
        <div className="grid grid-cols-3 gap-1.5">
          {ALIGNMENTS.map(({ value, label }) => (
            <button
              key={value}
              type="button"
              onClick={() => onStyleChange({ alignment: value })}
              className={[OPTION_BASE, style.alignment === value ? OPTION_ON : OPTION_OFF].join(' ')}
            >
              {label}
            </button>
          ))}
        </div>
      </Group>

      <Group title="样式">
        <Slider
          label="字号"
          value={style.font_size_percent}
          min={1.5}
          max={10}
          step={0.1}
          display={`${style.font_size_percent.toFixed(1)}% 高`}
          hint="相对于视频高度。用百分比是为了让同一套设置在 720p 和 1080p 上看起来一样。"
          onChange={v => onStyleChange({ font_size_percent: v })}
        />
        <Slider
          label="描边粗细"
          value={style.outline_width}
          min={0}
          max={5}
          step={0.1}
          display={`${style.outline_width.toFixed(1)} px`}
          onChange={v => onStyleChange({ outline_width: v })}
        />
        <div className="grid grid-cols-2 gap-x-4">
          <Switch label="加粗" checked={style.bold} onChange={v => onStyleChange({ bold: v })} />
          <Switch label="斜体" checked={style.italic} onChange={v => onStyleChange({ italic: v })} />
          <Switch
            label="半透明底"
            checked={style.background === 'box'}
            onChange={v => onStyleChange({ background: v ? 'box' : 'none' })}
          />
        </div>
        <ColorRow
          label="文字颜色"
          value={style.primary_color}
          presets={TEXT_COLOR_PRESETS}
          onChange={v => onStyleChange({ primary_color: v })}
        />
      </Group>

      <Group title="时间">
        <Slider
          label="最短时长"
          value={style.min_duration}
          min={0.5}
          max={5}
          step={0.1}
          display={`${style.min_duration.toFixed(1)} 秒`}
          hint="一句话再短也至少停留这么久，避免一闪而过。它改变的是字幕出现和消失的时刻，不只是外观。"
          onChange={v => onStyleChange({ min_duration: v })}
        />
        <Slider
          label="说完整留"
          value={style.extend_into_silence}
          min={0}
          max={2}
          step={0.1}
          display={`${style.extend_into_silence.toFixed(1)} 秒`}
          hint="一句话说完后，字幕往后面的静音里多停一会儿再消失，不要话音一落字就没了。"
          onChange={v => onStyleChange({ extend_into_silence: v })}
        />
      </Group>

      {/*
        * 进阶区：纸色底、行紧凑、控件小一号。都是"默认值已经很好、偶尔才动"的项，
        * 和上面的主卡片拉开层次，但不折叠——仍然一屏可见。
        */}
      <Group title="时间轴" tone="secondary">
        <Slider
          label="最长时长"
          value={style.max_duration}
          min={2}
          max={15}
          step={0.5}
          display={`${style.max_duration.toFixed(1)} 秒`}
          hint="一条字幕停太久观众会重读、走神。超过就按行切成连续几条。Netflix 规范是 7 秒。"
          tone="secondary"
          onChange={v => onStyleChange({ max_duration: v })}
        />
        <Slider
          label="阅读速度"
          value={style.max_cps}
          min={4}
          max={15}
          step={0.5}
          display={`${style.max_cps.toFixed(1)} 字/秒`}
          hint="观众每秒能读的字数上限（字母算半个字）。超了会切分或延长。Netflix 规范是 8.5。"
          tone="secondary"
          onChange={v => onStyleChange({ max_cps: v })}
        />
        <Slider
          label="条间间隔"
          value={style.min_gap}
          min={0}
          max={0.2}
          step={0.01}
          display={`${Math.round(style.min_gap * 1000)} 毫秒`}
          hint="相邻两条字幕之间留一点空白，切换才看得出来，不会像闪烁。默认约 1 帧（24fps）。"
          tone="secondary"
          onChange={v => onStyleChange({ min_gap: v })}
        />
        <SelectRow
          label="帧对齐"
          value={FPS_CHOICES[style.fps > 0 ? style.fps : 0] ?? '不对齐'}
          options={Object.values(FPS_CHOICES)}
          hint="字幕的出现/消失对齐到帧边界，不会在两帧之间切换。按视频帧率选。"
          tone="secondary"
          onChange={label => onStyleChange({ fps: Number(Object.entries(FPS_CHOICES).find(([, l]) => l === label)?.[0] ?? 0) })}
        />
      </Group>

      <Group title="排版" tone="secondary">
        {/*
          * Only the MKV/ASS track and the burn-in can carry a font NAME; an MP4
          * `mov_text` track cannot, and the player decides there. The name is
          * resolved on the VIEWER's machine, which is why this is a short list
          * of faces that are actually common rather than free text.
          */}
        <FontSelectRow
          label="字体"
          value={style.font_family}
          onChange={v => onStyleChange({ font_family: v })}
        />
        <Slider
          label="字间距"
          value={style.spacing}
          min={0}
          max={10}
          step={0.5}
          display={style.spacing.toFixed(1)}
          tone="secondary"
          onChange={v => onStyleChange({ spacing: v })}
        />
        {/*
          * "宽度", not "字数": the value counts DISPLAY CELLS, where a CJK
          * character is 1 and a Latin letter is 0.5. Calling it a character
          * count was wrong by a factor of two in the one language where the
          * number is not obvious.
          */}
        <Slider
          label="每行宽度"
          value={style.max_chars_per_line}
          min={6}
          max={40}
          step={1}
          display={`${style.max_chars_per_line} 格`}
          hint="按显示宽度计算：1 个汉字计 1 格，1 个字母计 0.5 格。所以 18 大约等于 18 个汉字，或 36 个字母。"
          tone="secondary"
          onChange={v => onStyleChange({ max_chars_per_line: v })}
        />
        <Slider
          label="最多行数"
          value={style.max_lines}
          min={1}
          max={3}
          step={1}
          display={`${style.max_lines} 行`}
          tone="secondary"
          onChange={v => onStyleChange({ max_lines: v })}
        />
        {/*
          * 模糊：代替硬描边做柔和边缘。和描边同量纲（千分比高度）。
          */}
        <Slider
          label="边缘模糊"
          value={style.blur}
          min={0}
          max={5}
          step={0.1}
          display={style.blur.toFixed(1)}
          hint="给字形边缘加一层柔化，比硬描边更像「浮」在画面上。通常和描边二选一：要模糊就把描边调小。"
          tone="secondary"
          onChange={v => onStyleChange({ blur: v })}
        />
        <ColorRow
          label="描边颜色"
          value={style.outline_color}
          presets={OUTLINE_COLOR_PRESETS}
          tone="secondary"
          onChange={v => onStyleChange({ outline_color: v })}
        />
        {style.track === 'bilingual' && (
          <ColorRow
            label="原文颜色"
            value={style.secondary_color}
            presets={[...TEXT_COLOR_PRESETS, '#D0D0D0']}
            tone="secondary"
            onChange={v => onStyleChange({ secondary_color: v })}
          />
        )}
      </Group>
    </div>
  </div>
);

export default SubtitleStylePanel;
