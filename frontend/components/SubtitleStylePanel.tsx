import React from 'react';
import InfoHint from './InfoHint';
import type {
  SubtitlePreset,
  SubtitleAlignment,
  SubtitleStyle,
  SubtitleStylePatch,
  SubtitleTrack,
} from '../net/types';
import type { HeaderProps } from '../hooks/useWindows';

/**
 * Subtitle style editor. It replaces the SCRIPT & TRANSLATION column while open.
 *
 * It is a SIBLING of TranscriptionPanel, not a separate thing, so it is built
 * from the same parts: the same card shell, the same header bar with an accent
 * dot, the same label treatment, and the same warm greys (#fbfbf9 / #e5e5e0 /
 * #d1d1cc / claude-paper). It used to be a bare scrolling column with its own
 * bespoke typography, which read as a different product bolted into the same
 * slot. Keep the constants below in sync with TranscriptionPanel.
 *
 * ---------------------------------------------------------------------------
 * Rules this file follows, each of which it broke before:
 *
 * 1. ONE control per decision. "What text a line contains" used to be here AND
 *    in the export dialog, as two separate pieces of state; picking a value in
 *    one left the preview and the .srt reading the other.
 *
 * 2. This panel is a STYLE LAB, with no banners. It used to carry a notice
 *    about cases where the chosen delivery would ignore the style ("MP4 内嵌
 *    字幕不带样式…"). That is gone: the explanation of MP4 vs MKV belongs
 *    next to the "保留字幕样式" checkbox in the export dialog, which is where
 *    the choice is actually made. Repeating it here only interrupted the one
 *    thing this panel is for.
 *
 *    The principle behind it still holds where a control is genuinely
 *    unusable: a disabled option must say why, inline and never in a tooltip
 *    (see SeparationModeSelector and InfoHint.tsx). Nothing here is unusable —
 *    every value you set is saved and used by every delivery that can carry it.
 *
 * 3. An icon goes ONLY where the label alone misleads, and nowhere else. Three
 *    cases qualify here: what "bilingual" means, the font-size unit (a
 *    percentage of the frame height is not self-evident), and what a "width" of
 *    18 is measured in. "顶部 / 居中 / 底部" does NOT qualify — the labels
 *    already say what they do. A row of "i" on all thirteen controls was an
 *    earlier version, and it read as noise.
 * ---------------------------------------------------------------------------
 */

/* Shared with TranscriptionPanel — the two panels sit in the same column. */
const CARD =
  'flex-grow flex flex-col h-full bg-[#fbfbf9] border border-[#e5e5e0] ' +
  'rounded-3xl overflow-hidden shadow-sm';
/** 浮动时：窗口外框已经画了边框、圆角和阴影。 */
const CARD_FLOATING = 'flex-grow flex flex-col h-full bg-[#fbfbf9] overflow-hidden';
const CARD_HEAD =
  'px-6 py-5 border-b border-[#e5e5e0] flex items-center justify-between ' +
  'bg-white/60 backdrop-blur-md';
const CARD_TITLE =
  'text-xs font-serif font-bold text-gray-700 flex items-center gap-2 tracking-wide';
const CARD_BODY =
  'flex-grow overflow-y-auto p-6 space-y-5 scroll-smooth';

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
/** 帧对齐选项：值是 fps，显示是文案。 */
const FPS_CHOICES: Record<number, string> = {
  0: '不对齐', 23.976: '23.976（电影）', 24: '24', 25: '25（PAL）', 30: '30', 60: '60',
};

/**
 * 字体选项。`family` 是写进样式的名字（进 ASS、进预览的 font-family）。
 *
 * 用户机器上装了哪个，用户自己未必知道——`useFontAvailability` 在面板里
 * 用 document.fonts.check() 探测一遍，下拉里把"本机没有"的标出来，省得选了
 * 一个不存在的字体然后疑惑预览为什么长得不像想象。
 *
 * `Source Han Sans` 是开源名，但 Mac/Win 都不预装（Mac 是 PingFang，Win 是
 * 微软雅黑），所以在大多数机器上它靠 `FONT_FALLBACKS` 回退到系统黑体。
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
 * One label column for every row, so the controls line up. Wide enough for the
 * rows that carry an InfoHint.
 */
const LABEL = 'flex items-center gap-1 text-[10px] font-semibold text-gray-500 w-[5.25rem] shrink-0';

const OPTION_BASE =
  'py-1.5 rounded-xl text-[11px] font-semibold border transition-all duration-200 ' +
  'flex items-center justify-center gap-1 cursor-pointer';
/**
 * Selected = accent border + light tint, NOT a solid fill.
 *
 * That is how the sibling TranscriptionPanel marks the active card
 * (`border-claude-accent` with a `ring-claude-accent/30`), and how its rate
 * badges read (`bg-claude-accent/10 text-claude-accent`). Two solid orange
 * slabs were the loudest thing in the panel and the furthest from that
 * restraint — the sibling uses the accent as a small, repeated signal.
 */
const OPTION_ON =
  'bg-claude-accent/10 text-claude-accent border-claude-accent shadow-sm shadow-claude-accent/10';
const OPTION_OFF = 'bg-white text-gray-600 border-[#e5e5e0] hover:border-[#d1d1cc] hover:bg-claude-paper/50';

/**
 * The app accent, for the one place it has to be spelled out: a range input
 * cannot read a Tailwind class inside an inline gradient.
 */
const ACCENT = '#DA7756';
const TRACK = '#F2F0E9';

/**
 * A range input styled to match the rest of the card.
 *
 * The native control is NOT usable as-is: its appearance is decided by the
 * browser and the OS, so the same markup renders as a heavy near-black bar on
 * one platform and a thin line on another. Measured here: the unfilled track
 * came out rgb(59,59,59) — an almost black slab in a card built from #fbfbf9
 * and #e5e5e0. Everything else in this panel is drawn by us, so this is too.
 *
 * The filled portion is an inline gradient rather than `::-moz-range-progress`,
 * because that pseudo-element does not exist in WebKit and the two would then
 * disagree. The gradient is painted on the input itself, which is why the
 * track pseudo-elements are made transparent below.
 */
const RANGE =
  'flex-1 h-4 appearance-none bg-transparent cursor-pointer ' +
  '[&::-webkit-slider-runnable-track]:bg-transparent ' +
  '[&::-moz-range-track]:bg-transparent ' +
  '[&::-moz-range-progress]:bg-transparent ' +
  '[&::-webkit-slider-thumb]:appearance-none ' +
  '[&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:h-3.5 ' +
  '[&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-white ' +
  '[&::-webkit-slider-thumb]:border [&::-webkit-slider-thumb]:border-[#d1d1cc] ' +
  '[&::-webkit-slider-thumb]:shadow-sm [&::-webkit-slider-thumb]:transition-colors ' +
  '[&::-webkit-slider-thumb]:hover:border-claude-accent ' +
  '[&::-moz-range-thumb]:w-3.5 [&::-moz-range-thumb]:h-3.5 ' +
  '[&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:bg-white ' +
  '[&::-moz-range-thumb]:border [&::-moz-range-thumb]:border-[#d1d1cc]';

/**
 * A titled band of related controls.
 *
 * The title uses the same treatment as a field label in TranscriptionPanel
 * (accent, uppercase, wide tracking) so the two panels label things the same
 * way. `first:` drops the rule on the top group, which sits right under the
 * header where a line would just be a second border.
 */
const Group: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
  <section className="flex flex-col gap-2.5 border-t border-[#e5e5e0]/70 pt-3 first:border-t-0 first:pt-0">
    <span className="text-[9px] font-bold uppercase tracking-widest text-claude-accent/70">
      {title}
    </span>
    {children}
  </section>
);

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
      {/* 预设是"一键到合规"：选完仍能下面微调。放最上面，因为它是起点不是终点。 */}
      {presets && presets.length > 0 && onApplyPreset && (
        <Group title="预设">
          <div className="grid grid-cols-3 gap-1.5">
            {presets.map(({ id, label, description }) => (
              <button
                key={id}
                type="button"
                onClick={() => onApplyPreset(id)}
                title={description}
                className={[OPTION_BASE, OPTION_OFF].join(' ')}
              >
                {label}
              </button>
            ))}
          </div>
        </Group>
      )}

      <Group title="字幕内容">
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
      </Group>

      <Group title="位置与大小">
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

        {/* The unit is the one thing here that is not self-evident: "4.5% 高"
            means nothing without knowing it is measured against the frame. */}
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
          label="上下边距"
          value={style.margin_v_percent}
          min={0}
          max={30}
          step={0.5}
          display={`${style.margin_v_percent.toFixed(1)}% 高`}
          onChange={v => onStyleChange({ margin_v_percent: v })}
        />

        {/* Measured against the frame WIDTH, unlike the row above — hence the
            different unit. It is what keeps a long line off the edges, and it
            maps to MarginL/MarginR in the ASS track. */}
        <Slider
          label="左右边距"
          value={style.margin_h_percent}
          min={0}
          max={30}
          step={0.5}
          display={`${style.margin_h_percent.toFixed(1)}% 宽`}
          onChange={v => onStyleChange({ margin_h_percent: v })}
        />
      </Group>

      <Group title="颜色">
        <ColorField
          label="文字颜色"
          value={style.primary_color}
          presets={TEXT_COLOR_PRESETS}
          onChange={v => onStyleChange({ primary_color: v })}
        />
        {/* Same control as the text colour. It used to be a bare swatch with no
            hex field and no presets, so changing the outline meant opening the OS
            picker while the row above offered five ready-made choices. */}
        <ColorField
          label="描边颜色"
          value={style.outline_color}
          presets={OUTLINE_COLOR_PRESETS}
          onChange={v => onStyleChange({ outline_color: v })}
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

        {/* The drop shadow behind the text. Its scale is the same as the
            outline's (0–8, clamped server-side), and it lands in the ASS
            `Shadow` field. */}
        <Slider
          label="阴影"
          value={style.shadow}
          min={0}
          max={5}
          step={0.5}
          display={style.shadow.toFixed(1)}
          hint="文字后面的一层柔和阴影，画面很亮时能托住字幕。和描边是两回事：描边贴字形，阴影在更后面。"
          onChange={v => onStyleChange({ shadow: v })}
        />
        {/*
          * A checkbox's own text IS its label, so these sit at the left edge
          * like the row labels above rather than getting a second, redundant
          * label column ("字幕底衬" next to "半透明底").
          */}
        {/* 模糊：代替硬描边做柔和边缘。和描边同量纲（千分比高度）。 */}
        <Slider
          label="边缘模糊"
          value={style.blur}
          min={0}
          max={5}
          step={0.1}
          display={style.blur.toFixed(1)}
          hint="给字形边缘加一层柔化，比硬描边更像「浮」在画面上。通常和描边二选一：要模糊就把描边调小。"
          onChange={v => onStyleChange({ blur: v })}
        />
        <Check
          label="半透明底"
          checked={style.background === 'box'}
          onChange={v => onStyleChange({ background: v ? 'box' : 'none' })}
        />
        {/* 双语时原文的颜色；单轨时这一项没效果，所以只在双语时显示。 */}
        {style.track === 'bilingual' && (
          <ColorField
            label="原文颜色"
            value={style.secondary_color}
            presets={[...TEXT_COLOR_PRESETS, '#D0D0D0']}
            onChange={v => onStyleChange({ secondary_color: v })}
          />
        )}
      </Group>

      <Group title="排版">
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
        <div className="flex gap-3">
          <Check label="加粗" checked={style.bold} onChange={v => onStyleChange({ bold: v })} />
          <Check label="斜体" checked={style.italic} onChange={v => onStyleChange({ italic: v })} />
        </div>
        <Slider
          label="字间距"
          value={style.spacing}
          min={0}
          max={10}
          step={0.5}
          display={style.spacing.toFixed(1)}
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
          onChange={v => onStyleChange({ max_chars_per_line: v })}
        />
        <Slider
          label="最多行数"
          value={style.max_lines}
          min={1}
          max={3}
          step={1}
          display={`${style.max_lines} 行`}
          onChange={v => onStyleChange({ max_lines: v })}
        />
      </Group>

      {/*
        * Its own group because it is a different KIND of setting: everything
        * above changes how a line LOOKS, this changes how long it STAYS. Mixing
        * it into 排版 would hide that it moves the timeline.
        */}
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
          label="最长时长"
          value={style.max_duration}
          min={2}
          max={15}
          step={0.5}
          display={`${style.max_duration.toFixed(1)} 秒`}
          hint="一条字幕停太久观众会重读、走神。超过就按行切成连续几条。Netflix 规范是 7 秒。"
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
          onChange={v => onStyleChange({ max_cps: v })}
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
        <Slider
          label="条间间隔"
          value={style.min_gap}
          min={0}
          max={0.2}
          step={0.01}
          display={`${Math.round(style.min_gap * 1000)} 毫秒`}
          hint="相邻两条字幕之间留一点空白，切换才看得出来，不会像闪烁。默认约 1 帧（24fps）。"
          onChange={v => onStyleChange({ min_gap: v })}
        />
        <SelectRow
          label="帧对齐"
          value={FPS_CHOICES[style.fps > 0 ? style.fps : 0] ?? '不对齐'}
          options={Object.values(FPS_CHOICES)}
          hint="字幕的出现/消失对齐到帧边界，不会在两帧之间切换。按视频帧率选。"
          onChange={label => onStyleChange({ fps: Number(Object.entries(FPS_CHOICES).find(([, l]) => l === label)?.[0] ?? 0) })}
        />
      </Group>
    </div>
  </div>
);

function normaliseHex(value: string): string {
  return /^#[0-9a-fA-F]{6}$/.test(value) ? value : '#FFFFFF';
}

interface ColorFieldProps {
  label: string;
  value: string;
  presets: string[];
  onChange: (value: string) => void;
}

const ColorField: React.FC<ColorFieldProps> = ({ label, value, presets, onChange }) => (
  <div className="flex items-center gap-2">
    <span className={LABEL}>{label}</span>
    <input
      type="color"
      aria-label={`${label}（取色器）`}
      value={normaliseHex(value)}
      onChange={e => onChange(e.target.value.toUpperCase())}
      className="w-7 h-6 shrink-0 rounded-md border border-[#e5e5e0] cursor-pointer bg-white"
    />
    <input
      type="text"
      aria-label={`${label}（十六进制）`}
      value={value}
      onChange={e => onChange(e.target.value)}
      className="w-[4.25rem] text-[10px] font-mono border border-[#e5e5e0] rounded-md px-1.5 py-0.5"
    />
    {/*
      * Deliberately NOT `ml-auto`. Pushing the swatches to the right edge
      * detached them from the swatch and hex they belong to and made the row
      * read as two separate controls; they are one control with quick picks.
      */}
    <div className="flex gap-1">
      {presets.map(color => (
        /*
         * `title` here is a NAME, not an explanation — it tells you which hex a
         * dot stands for. That is the one case the native attribute is right
         * for, so it stays.
         */
        <button
          key={color}
          type="button"
          title={color}
          aria-label={color}
          onClick={() => onChange(color)}
          style={{ backgroundColor: color }}
          className="w-4 h-4 rounded-full border border-[#d1d1cc] cursor-pointer"
        />
      ))}
    </div>
  </div>
);

interface SelectRowProps {
  label: string;
  value: string;
  options: string[];
  hint?: string;
  onChange: (value: string) => void;
}

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
      // 显示文案可能带「（本机没有）」后缀，写进样式的要是裸字体名
      onChange={(label) => onChange(label.replace(/（本机没有）$/, ''))}
    />
  );
};

const SelectRow: React.FC<SelectRowProps> = ({ label, value, options, hint, onChange }) => (
  <div className="flex items-center gap-2">
    <span className={LABEL}>
      {label}
      {hint && <InfoHint text={hint} standalone />}
    </span>
    <select
      aria-label={label}
      value={value}
      onChange={e => onChange(e.target.value)}
      className="flex-1 text-[11px] text-gray-700 bg-white border border-[#e5e5e0] rounded-lg px-2 py-1 cursor-pointer"
    >
      {/*
        * A video saved with a font that is not in the list must still render
        * its own value. Without this the select would snap to the first option
        * on mount and quietly rewrite the style of every older video.
      */}
      {!options.includes(value) && <option value={value}>{value}</option>}
      {options.map(option => (
        <option key={option} value={option}>
          {option}
        </option>
      ))}
    </select>
  </div>
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
  onChange: (value: number) => void;
}

const Slider: React.FC<SliderProps> = ({
  label,
  value,
  min,
  max,
  step,
  display,
  hint,
  onChange,
}) => {
  const pct = max > min ? ((value - min) / (max - min)) * 100 : 0;

  return (
    <div className="flex items-center gap-2">
      <span className={LABEL}>
        {label}
        {hint && <InfoHint text={hint} standalone />}
      </span>
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
        // The filled portion. A 4px bar centred in a taller box, so the thumb
        // has room to sit on the track without being clipped.
        style={{
          backgroundImage: `linear-gradient(to right, ${ACCENT} 0%, ${ACCENT} ${pct}%, ${TRACK} ${pct}%, ${TRACK} 100%)`,
          backgroundSize: '100% 4px',
          backgroundPosition: 'center',
          backgroundRepeat: 'no-repeat',
        }}
        className={RANGE}
      />
      <span className="text-[10px] text-gray-400 w-12 text-right tabular-nums">{display}</span>
    </div>
  );
};

interface CheckProps {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}

const Check: React.FC<CheckProps> = ({ label, checked, onChange }) => (
  <label className="flex items-center gap-1.5 text-[10px] font-semibold text-gray-500 cursor-pointer">
    <input
      type="checkbox"
      checked={checked}
      onChange={e => onChange(e.target.checked)}
      className="accent-claude-accent"
    />
    {label}
  </label>
);

export default SubtitleStylePanel;
