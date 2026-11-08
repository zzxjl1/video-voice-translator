/**
 * 脚本面板：每句一张卡。
 *
 * 和旧版 `TranscriptionPanel` 的根本区别：**这里没有任何本地状态副本**。
 *   · 编辑文字 = 发一个命令（edit_source / set_translation），等服务端的增量回来再重绘；
 *   · 静音 / 隐藏 = 发命令；
 *   · 行是否"过期""没翻译"来自服务端算好的 `trStatus` / `ttsStatus` / `trSuspect`。
 * 所以用户在这里的操作和 AI 的操作走同一条路径，界面自然同步；AI 刚改过的行会高亮几秒。
 */
import React, { memo, useEffect, useMemo, useRef, useState } from 'react';
import type { Line, MixPlanSegment, SpeakerView } from '../../net/types';
import type { Flash } from '../../hooks/useProject';
import { formatTime } from '../../utils/helpers';
import { splitSubtitleTags, SUBTITLE_TAG_RE } from '../../utils/subtitleStyle';

export interface ScriptActions {
  edit: (id: string, field: 'text' | 'translation', value: string, line: number) => void;
  toggleMute: (line: Line) => void;
  toggleHide: (line: Line) => void;
  seek: (time: number) => void;
  redub: (line: Line) => void;
  retranslate: (line: Line, hint?: 'longer' | 'shorter') => void;
  mark: (line: Line, kind: 'music' | 'noise' | 'speech') => void;
  askAbout: (line: Line) => void;
}

interface Props {
  lines: Line[];
  speakers: SpeakerView[];
  colorOf: (speakerId: string) => string;
  currentTime: number;
  flash: Flash;
  mix: Map<string, MixPlanSegment>;
  /** 正在被任务处理的行（不能编辑）。 */
  busyIds: Set<string>;
  dubbing: boolean;
  actions: ScriptActions;
  focusLine?: number | null;
  onFocusHandled?: () => void;
}

const STATUS: Record<string, { text: string; tone: string }> = {
  stale: { text: '需更新', tone: 'bg-amber-50 text-amber-700 border-amber-200' },
  missing: { text: '待生成', tone: 'bg-gray-50 text-gray-500 border-gray-200' },
};

const ScriptPanel: React.FC<Props> = ({
  lines, speakers, colorOf, currentTime, flash, mix, busyIds, dubbing, actions, focusLine, onFocusHandled,
}) => {
  const listRef = useRef<HTMLDivElement>(null);
  const speakerById = useMemo(() => new Map(speakers.map((s) => [s.id, s])), [speakers]);
  const activeId = useMemo(() => lines.find((l) => currentTime >= l.start && currentTime < l.end)?.id ?? null, [lines, currentTime]);

  // 播放时让当前句留在视野中间
  useEffect(() => {
    if (!activeId) return;
    const el = document.getElementById(`line-${activeId}`);
    const box = listRef.current;
    if (el && box) {
      box.scrollTo({ top: Math.max(0, el.offsetTop - box.offsetTop - box.clientHeight / 2 + el.clientHeight / 2), behavior: 'smooth' });
    }
  }, [activeId]);

  // AI / 聊天里的行号标签要求定位到某一句
  useEffect(() => {
    if (focusLine == null) return;
    const target = lines.find((l) => l.line === focusLine);
    if (target) {
      const el = document.getElementById(`line-${target.id}`);
      const box = listRef.current;
      if (el && box) {
        box.scrollTo({ top: Math.max(0, el.offsetTop - box.offsetTop - 24), behavior: 'smooth' });
      }
    }
    onFocusHandled?.();
  }, [focusLine, lines, onFocusHandled]);

  return (
    <div className="flex flex-col h-full bg-[#fbfbf9] border border-[#e5e5e0] rounded-3xl overflow-hidden shadow-sm">
      <div className="px-6 py-5 border-b border-[#e5e5e0] flex items-center justify-between bg-white/60 backdrop-blur-md">
        <h3 className="text-xs font-serif font-bold text-gray-700 flex items-center gap-2 tracking-wide">
          <span className="w-1.5 h-1.5 bg-claude-accent rounded-full" />
          SCRIPT &amp; TRANSLATION
        </h3>
        <span className="text-[10px] font-bold uppercase tracking-widest text-gray-400">{lines.length} 句</span>
      </div>

      <div ref={listRef} className="flex-grow overflow-y-auto p-6 space-y-6 scroll-smooth">
        {lines.length === 0 ? (
          <div className="h-full flex items-center justify-center text-xs text-gray-400">还没有转写结果</div>
        ) : (
          lines.map((line) => (
            <LineCard
              key={line.id}
              line={line}
              speaker={speakerById.get(line.speakerId)}
              color={colorOf(line.speakerId)}
              active={line.id === activeId}
              flashBy={flash[line.id]}
              mixEntry={mix.get(line.id)}
              busy={busyIds.has(line.id)}
              dubbing={dubbing}
              actions={actions}
            />
          ))
        )}
      </div>
    </div>
  );
};

// ----------------------------------------------------------------------------

interface CardProps {
  line: Line;
  speaker?: SpeakerView;
  color: string;
  active: boolean;
  flashBy?: 'user' | 'ai' | 'system';
  mixEntry?: MixPlanSegment;
  busy: boolean;
  dubbing: boolean;
  actions: ScriptActions;
}

const LineCard = memo(({ line, speaker, color, active, flashBy, mixEntry, busy, dubbing, actions }: CardProps) => {
  const slot = line.end - line.start;
  const nonSpeech = line.kind !== 'speech';
  const ratio = line.audioDuration && slot > 0 ? line.audioDuration / slot : null;
  const misfit = ratio !== null && (ratio > 1.15 || ratio < 0.8);
  const [menu, setMenu] = useState(false);

  const ring = flashBy === 'ai'
    ? 'ring-2 ring-claude-accent border-claude-accent shadow-lg shadow-claude-accent/10'
    : flashBy
      ? 'ring-2 ring-sky-300 border-sky-300'
      : active
        ? 'ring-2 ring-claude-accent/30 border-claude-accent shadow-lg scale-[1.01]'
        : line.muted || nonSpeech
          ? 'border-dashed border-[#d8d8d2] opacity-70'
          : 'border-[#e5e5e0] hover:border-[#d1d1cc] shadow-sm hover:shadow-md';

  return (
    <div id={`line-${line.id}`} className={`relative p-6 rounded-2xl border bg-white space-y-5 transition-all duration-500 ${ring}`}>
      {flashBy === 'ai' && (
        <span className="absolute -top-2.5 left-5 px-2 py-px rounded-full bg-claude-accent text-white text-[9px] font-bold uppercase tracking-widest shadow-sm">
          AI 刚改过
        </span>
      )}

      {/* 头部 */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2.5 min-w-0 flex-wrap">
          <span className="text-[10px] font-mono font-bold text-gray-300 tabular-nums w-5 text-right">{line.line}</span>
          <span className="w-2.5 h-2.5 rounded-full ring-2 ring-white shadow-sm shrink-0" style={{ backgroundColor: color }} />
          <span className="text-[11px] font-bold uppercase tracking-[0.1em] text-gray-500 truncate">{speaker?.name ?? `Speaker ${line.speakerId}`}</span>
          {nonSpeech && <Tag tone="bg-purple-50 text-purple-600 border-purple-200">{line.kind === 'music' ? '音乐' : '噪音'}</Tag>}
          {line.muted && <Tag tone="bg-gray-100 text-gray-500 border-gray-200">已静音</Tag>}
          {line.hidden && <Tag tone="bg-amber-50 text-amber-600 border-amber-200">字幕隐藏</Tag>}
          {line.trSuspect && <Tag tone="bg-red-50 text-red-600 border-red-200" title="译文看起来还是原文的文字，可能根本没翻译">疑似未翻译</Tag>}
          {!nonSpeech && STATUS[line.trStatus] && <Tag tone={STATUS[line.trStatus].tone}>译文{STATUS[line.trStatus].text}</Tag>}
          {dubbing && !nonSpeech && !line.muted && STATUS[line.ttsStatus] && <Tag tone={STATUS[line.ttsStatus].tone}>配音{STATUS[line.ttsStatus].text}</Tag>}
          {busy && <Tag tone="bg-claude-accent/10 text-claude-accent border-claude-accent/30">处理中</Tag>}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <button
            onClick={() => actions.seek(line.start)}
            className="text-[10px] font-mono text-gray-400 bg-[#f9f9f8] px-2 py-1 rounded-md border border-[#eee] hover:text-claude-accent hover:border-claude-accent/30 transition-colors tabular-nums"
            title="跳到这一句"
          >
            {formatTime(line.start)} – {formatTime(line.end)}
          </button>
          <button
            onClick={() => actions.askAbout(line)}
            className="p-1.5 rounded-md hover:bg-claude-paper text-gray-400 hover:text-claude-accent transition-colors"
            title="就这一句问 AI"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M21 12c0 4.4-4 8-9 8a9.9 9.9 0 01-4-.8L3 20l1.3-3.9A7.6 7.6 0 013 12c0-4.4 4-8 9-8s9 3.6 9 8z" /></svg>
          </button>
          <div className="relative">
            <button onClick={() => setMenu((v) => !v)} className="p-1.5 rounded-md hover:bg-claude-paper text-gray-400 hover:text-gray-600 transition-colors" title="更多">
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.8" /><circle cx="12" cy="12" r="1.8" /><circle cx="19" cy="12" r="1.8" /></svg>
            </button>
            {menu && <LineMenu line={line} dubbing={dubbing} actions={actions} onClose={() => setMenu(false)} />}
          </div>
        </div>
      </div>

      {/* 正文 */}
      <div className="space-y-4">
        <Editable label="Original" value={line.text} secondary disabled={busy} onCommit={(v) => actions.edit(line.id, 'text', v, line.line)} />
        {line.reviewedText && line.reviewedText !== line.text && (
          <div className="text-[11px] text-gray-400 leading-relaxed -mt-2 pl-0.5">
            <span className="font-bold uppercase tracking-widest text-[9px] text-claude-accent/70 mr-1.5">审听版</span>{line.reviewedText}
          </div>
        )}
        {!nonSpeech && (
          <Editable label="Translation" value={line.translation} placeholder="译文会出现在这里…" disabled={busy} onCommit={(v) => actions.edit(line.id, 'translation', v, line.line)} />
        )}
      </div>

      {/* 配音 */}
      {dubbing && !nonSpeech && !line.muted && (
        <div className="pt-1">
          {line.audioUrl ? (
            <div className="space-y-2.5">
              <audio src={line.audioUrl} controls preload="none" className="w-full h-8 opacity-70 hover:opacity-100 transition-opacity" />
              <div className="flex items-center gap-2 flex-wrap">
                {line.ttsInstruction && <Tag tone="bg-purple-50 text-purple-600 border-purple-200" title={line.ttsInstruction}>语气：{clip(line.ttsInstruction, 18)}</Tag>}
                {line.ttsRate && <Tag tone="bg-gray-50 text-gray-500 border-gray-200">语速 {line.ttsRate}x</Tag>}
                {mixEntry && mixEntry.atempo !== 1 && <Tag tone="bg-claude-accent/10 text-claude-accent border-claude-accent/20">变速 {mixEntry.atempo.toFixed(2)}x</Tag>}
                {mixEntry && mixEntry.videoRate !== 1 && <Tag tone="bg-amber-50 text-amber-700 border-amber-200">视频 {mixEntry.videoRate.toFixed(2)}x</Tag>}
                {misfit && ratio !== null && (
                  <Tag tone="bg-amber-50 text-amber-700 border-amber-200">
                    配音 {line.audioDuration!.toFixed(1)}s / 时间槽 {slot.toFixed(1)}s · {ratio > 1 ? '偏长' : '偏短'}
                  </Tag>
                )}
              </div>
            </div>
          ) : line.translation && line.trStatus !== 'stale' ? (
            <button
              onClick={() => actions.redub(line)}
              disabled={busy}
              className="w-full py-2.5 rounded-xl text-[10px] font-bold uppercase tracking-[0.15em] border bg-white border-claude-border text-claude-accent hover:bg-claude-paper active:scale-[0.98] transition-all disabled:opacity-40"
            >
              {busy ? '合成中…' : line.ttsStatus === 'stale' ? '重新生成配音' : '生成配音'}
            </button>
          ) : null}
        </div>
      )}
    </div>
  );
});
LineCard.displayName = 'LineCard';

const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n) + '…' : s);

const Tag: React.FC<{ tone: string; title?: string; children: React.ReactNode }> = ({ tone, title, children }) => (
  <span title={title} className={`px-1.5 py-px rounded-md border text-[9px] font-bold tracking-wide whitespace-nowrap ${tone}`}>{children}</span>
);

// ----------------------------------------------------------------------------

interface EditableProps {
  label: string;
  value: string;
  placeholder?: string;
  secondary?: boolean;
  disabled?: boolean;
  onCommit: (value: string) => void;
}

/**
 * 失焦时提交。本地只存"正在输入的草稿"：外部值（服务端推来的）变了而用户没在编辑，
 * 就跟着变 —— 这是 AI 改了这一句之后，输入框里的字立刻更新的原因。
 */
/**
 * 可编辑的一段文字（原文 / 译文）。
 *
 * 两个细节是重构前就有的，别弄丢：
 *
 * 1. **情感标签高亮**：`[sarcastic]`、`【讽刺】` 这类标签是给合成器的指令，不是台词。
 *    它们是**镜像层**画的色块 —— 底下那个 textarea 的文字被设成透明，只有光标可见。
 *    为什么不用 contenteditable：镜像 + 透明 textarea 能保持原生输入法、撤销、选区的
 *    全部行为，而画色块只需要一个绝对定位的 div 跟着排版走。
 *    任何影响**宽度**的样式（padding / 边框 / 字号）都不能加在标签 span 上，
 *    否则镜像文字会和下面的输入框错位。
 *
 * 2. **hover 才出现编辑入口**：平时是只读展示，鼠标移上去整块虚化、中间浮出「编辑」
 *    药丸。点它才进入编辑，失焦即保存。这样一屏几十句不会全是输入框的样子。
 */
const Editable: React.FC<EditableProps> = ({ label, value, placeholder, secondary, disabled, onCommit }) => {
  const [draft, setDraft] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const shown = draft ?? value;
  const parts = useMemo(() => splitSubtitleTags(shown), [shown]);
  const hasTags = parts.some((p) => p.isTag);
  const dirty = draft !== null && draft.trim() !== value.trim();

  /**
   * 页面正在被刷新 / 关闭 / 跳走。
   *
   * 浏览器在卸载时会对聚焦的输入框触发 blur —— 如果这里照常提交，用户刷新页面的那一刻，
   * 输入框里没敲完的半截内容就会覆盖这一句的原文（真实浏览器测试里发生过）。卸载期间的
   * blur 一律不提交：没点"别处"保存的草稿就是没保存，刷新后它自然消失。
   */
  const leaving = useRef(false);
  useEffect(() => {
    const mark = () => { leaving.current = true; };
    window.addEventListener('beforeunload', mark);
    window.addEventListener('pagehide', mark);
    return () => { window.removeEventListener('beforeunload', mark); window.removeEventListener('pagehide', mark); };
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (el) {
      el.style.height = 'auto';
      el.style.height = `${el.scrollHeight}px`;
    }
  }, [shown, editing]);

  /** Esc 放弃：blur 也会触发 commit，所以要先立个旗子，否则"放弃"变成了"保存"。 */
  const cancelling = useRef(false);

  const commit = () => {
    const discard = cancelling.current;
    cancelling.current = false;
    if (!discard && !leaving.current && dirty) onCommit(draft!.trim());
    setDraft(null);
    setEditing(false);
  };

  const typography = secondary ? 'font-serif italic text-[13px]' : 'text-[14px]';
  const textColor = secondary ? 'text-gray-500' : 'text-claude-text';

  return (
    <div className="group/field">
      <div className="text-[9px] font-bold uppercase tracking-[0.15em] text-gray-300 mb-1.5 flex items-center gap-2">
        {label}
        {dirty && <span className="normal-case tracking-normal font-normal text-amber-500">未保存 · 点击别处保存，Esc 放弃</span>}
      </div>

      <div
        className={`relative rounded-xl border transition-all duration-300 ${
          secondary ? 'bg-[#f9f9f8]' : 'bg-white'
        } ${
          editing
            ? 'border-claude-accent/40 ring-2 ring-claude-accent/10'
            : 'border-[#e5e5e0] hover:border-[#d1d1cc]'
        } ${disabled ? 'opacity-50' : ''}`}
      >
        {/* 镜像层：标签画成色块；非编辑态 hover 时整块虚化 */}
        {hasTags && (
          <div
            aria-hidden="true"
            className={`pointer-events-none absolute inset-0 overflow-hidden px-3 py-2 leading-relaxed whitespace-pre-wrap break-words ${typography} ${textColor} transition-all duration-300 ${
              editing ? '' : 'group-hover/field:blur-[2px]'
            }`}
          >
            {parts.map((part, index) =>
              part.isTag ? (
                // 不影响宽度的样式才安全：颜色 + 圆角，不加 padding/边框/字号
                <span key={index} className="rounded-[3px] bg-claude-accent/15 text-claude-accent">{part.text}</span>
              ) : (
                <span key={index}>{part.text}</span>
              ),
            )}
          </div>
        )}

        <textarea
          ref={ref}
          value={shown}
          rows={1}
          readOnly={!editing || disabled}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Escape') { cancelling.current = true; (e.target as HTMLTextAreaElement).blur(); return; }
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { (e.target as HTMLTextAreaElement).blur(); return; }
            // 退格整块删标签：光标紧跟在 [sad] 后面时按一次退格删掉整个标签（连同分隔空格），
            // 而不是先啃掉一个 "]" —— 半个标签留在那儿既不是标签也不是正文。
            if (e.key === 'Backspace' && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey) {
              const el = e.currentTarget;
              if (el.selectionStart !== el.selectionEnd) return;
              const caret = el.selectionStart;
              const before = shown.slice(Math.max(0, caret - 40), caret);
              const match = before.match(SUBTITLE_TAG_RE);
              if (!match) return;
              e.preventDefault();
              const start = caret - match[0].length;
              setDraft(shown.slice(0, start) + shown.slice(caret));
              requestAnimationFrame(() => el.setSelectionRange(start, start));
            }
          }}
          className={`relative w-full resize-none overflow-hidden bg-transparent outline-none px-3 py-2 leading-relaxed
            whitespace-pre-wrap break-words ${typography}
            ${hasTags ? 'text-transparent selection:bg-claude-accent/25' : `${textColor}`}
            ${hasTags ? 'caret-claude-accent' : ''}
            ${!editing ? 'cursor-default' : ''}
            placeholder:text-gray-300`}
        />

        {/* 只读时：hover 浮出编辑入口（旧版的「Edit」药丸） */}
        {!editing && !disabled && (
          <button
            type="button"
            onClick={() => {
              setEditing(true);
              requestAnimationFrame(() => {
                const el = ref.current;
                if (!el) return;
                el.focus();
                el.setSelectionRange(el.value.length, el.value.length);
              });
            }}
            className="absolute inset-0 flex items-center justify-center opacity-0 group-hover/field:opacity-100 transition-opacity duration-300 bg-white/10 backdrop-blur-[0.5px] cursor-text"
          >
            <span className="px-4 py-1.5 bg-white/90 border border-[#d1d1cc] rounded-full text-[10px] font-bold uppercase tracking-widest shadow-sm hover:scale-105 transition-transform text-gray-600">
              编辑
            </span>
          </button>
        )}
      </div>
    </div>
  );
};

// ----------------------------------------------------------------------------

const LineMenu: React.FC<{ line: Line; dubbing: boolean; actions: ScriptActions; onClose: () => void }> = ({ line, dubbing, actions, onClose }) => {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onDown = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) onClose(); };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey); };
  }, [onClose]);

  const item = (label: string, run: () => void, hint?: string, danger?: boolean) => (
    <button
      key={label}
      onClick={() => { run(); onClose(); }}
      className={`w-full text-left px-3.5 py-2 text-[12px] hover:bg-claude-paper transition-colors ${danger ? 'text-red-500' : 'text-claude-text'}`}
    >
      <div className="font-medium">{label}</div>
      {hint && <div className="text-[10px] text-gray-400 mt-px">{hint}</div>}
    </button>
  );

  const nonSpeech = line.kind !== 'speech';
  return (
    <div ref={ref} className="absolute right-0 top-full mt-1.5 w-52 bg-white border border-claude-border rounded-xl shadow-xl z-30 py-1.5 overflow-hidden">
      {!nonSpeech && item('重新翻译', () => actions.retranslate(line))}
      {!nonSpeech && item('译文长一点', () => actions.retranslate(line, 'longer'))}
      {!nonSpeech && item('译文短一点', () => actions.retranslate(line, 'shorter'))}
      {dubbing && !nonSpeech && item('重新配音', () => actions.redub(line))}
      <div className="my-1 border-t border-claude-border/60" />
      {item(line.muted ? '取消静音' : '静音', () => actions.toggleMute(line), line.muted ? undefined : '这一格不配音')}
      {item(line.hidden ? '显示字幕' : '隐藏字幕', () => actions.toggleHide(line), line.hidden ? undefined : '配音照常')}
      <div className="my-1 border-t border-claude-border/60" />
      {nonSpeech
        ? item('标回语音', () => actions.mark(line, 'speech'))
        : item('标为音乐 / 非语音', () => actions.mark(line, 'music'), '不翻译、不配音、不出字幕', true)}
    </div>
  );
};

export default ScriptPanel;
