/**
 * 编辑页：视频 + 时间轴 + 脚本 / 字幕样式，右边是 AI 助手。
 *
 * 这一页没有自己的业务状态。数据来自 `useProject`（服务端推来的），改动只通过 `run()` 发命令。
 * 本地只有纯界面状态：右栏切到哪个面板、聊天开合、弹窗开合、输入框草稿。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as api from '../../net/client';
import { burnSubtitles } from '../../utils/burnSubtitles';
import { downloadFile } from '../../utils/helpers';
import { probeBurnCapability, type BurnCapability } from '../../utils/burnCapability';
import { getAudioWaveformFromUrl } from '../../utils/audioProcessor';
import { useProject } from '../../hooks/useProject';
import { useMixPlayback } from '../../hooks/useMixPlayback';
import { useWindows } from '../../hooks/useWindows';
import { speakerColor } from '../../store/project';
import type { TimelineSegment } from '../../store/timeline';
import { DEFAULT_SUBTITLE_STYLE } from '../../net/types';
import type { Line, SubtitleCapabilities, SubtitleCue, SubtitleStyle } from '../../net/types';
import { containRect, overlayContainerStyle, overlayLineStyle, stripSubtitleTags, type Rect } from '../../utils/subtitleStyle';
import ChatPanel from '../chat/ChatPanel';
import ScriptPanel, { type ScriptActions } from './ScriptPanel';
import TopBar from '../shell/TopBar';
import { DockSlot, WindowLayer, WindowPortals, type PanelRenderers } from '../windows/WindowSystem';
import SettingsPanel from './SettingsPanel';
import Timeline from '../Timeline';
import SubtitleStylePanel from '../SubtitleStylePanel';
import VoicePanel from './VoicePanel';
import ExportModal, { type SubtitleDelivery } from '../ExportModal';

interface Props {
  project: ReturnType<typeof useProject>;
  onHome: () => void;
}

const SUGGESTIONS = ['这个项目现在什么情况？', '检查一下有没有被误识别成话的音乐', '哪些句子的配音比时间槽长？'];

const EditorPage: React.FC<Props> = ({ project: api_, onHome }) => {
  const { project, chat, flash, ui, clearUi, run, say, stop, confirm } = api_;
  const id = project!.id;
  const lines = project!.segments;
  const dubbing = project!.settings.enableDubbing;

  // 脚本 / 助手 / 字幕样式都是窗口：吸附在右栏，或者拖出来自由摆放
  const wm = useWindows();
  const openWindow = wm.open;
  const [showExport, setShowExport] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  // 从脚本引用到聊天的那一句：结构化值，不在输入框里拼字符串，
  // 所以不会因为 StrictMode 双调用 effect 被追加两次。
  const [quote, setQuote] = useState<{ line: number; label: string } | null>(null);
  const onQuoteRemove = useCallback(() => setQuote(null), []);
  const [waveform, setWaveform] = useState<number[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  // 从脚本点某个说话人的"音色"进来时，音色面板滚到这个说话人
  const [voiceFocus, setVoiceFocus] = useState<string | null>(null);

  // 字幕样式：以服务端为准；拖滑块时本地先行，停下再保存（防抖）
  const [styleOverride, setStyleOverride] = useState<Partial<SubtitleStyle> | null>(null);
  const serverStyle = useMemo<SubtitleStyle>(() => ({ ...DEFAULT_SUBTITLE_STYLE, ...project!.subtitle.style }), [project!.subtitle.style]);
  const style = useMemo(() => ({ ...serverStyle, ...(styleOverride ?? {}) }), [serverStyle, styleOverride]);
  const styleTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const onStyleChange = useCallback((patch: Partial<SubtitleStyle>) => {
    setStyleOverride((prev) => ({ ...(prev ?? {}), ...patch }));
    clearTimeout(styleTimer.current);
    styleTimer.current = setTimeout(async () => {
      const r = await run('set_subtitle_style', patch as Record<string, unknown>);
      if (!r.ok) setNotice(r.error ?? '保存字幕样式失败');
      setStyleOverride(null);
    }, 400);
  }, [run]);
  useEffect(() => () => clearTimeout(styleTimer.current), []);

  // 播放
  const plan = project!.mix?.plan ?? null;
  const backgroundUrl = project!.hasStems ? api.backgroundUrl(id) : null;
  const playback = useMixPlayback({ lines, plan, backgroundUrl, dubbing });
  const { videoRef, backgroundRef, currentTime, duration, mixEntries, seek, play, videoHandlers } = playback;

  // 画面实际落在容器里的哪块区域：`object-contain` 会留黑边，字幕要贴在画面上而不是黑边里。
  const boxRef = useRef<HTMLDivElement>(null);
  const [videoRect, setVideoRect] = useState<Rect | null>(null);
  const measure = useCallback(() => {
    const box = boxRef.current;
    const video = videoRef.current;
    if (box && video) setVideoRect(containRect(box.clientWidth, box.clientHeight, video.videoWidth, video.videoHeight));
  }, [videoRef]);
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    return () => observer.disconnect();
  }, [measure]);

  // 波形
  useEffect(() => {
    let cancelled = false;
    getAudioWaveformFromUrl(api.audioUrl(id), 300).then((p) => { if (!cancelled) setWaveform(p); }).catch(() => {});
    return () => { cancelled = true; };
  }, [id]);

  // 字幕预览 cue：用服务端算（时间随配音时长调整，与导出一致）
  const [cues, setCues] = useState<SubtitleCue[]>([]);
  // 字号/每行字数/行数/最小时长都会改变服务端的断行结果，改样式必须重算 cue，
  // 否则画面断行和导出的 ASS 会对不上。滑块拖动期间做一点防抖。
  const cueKey = useMemo(() => lines.map((l) =>
    `${l.id}:${l.translation}:${l.hidden}:${l.kind}:${l.audioDuration}`).join('|')
    + JSON.stringify([style.track, style.font_size_percent, style.max_chars_per_line,
                      style.max_lines, style.min_duration]), [lines, style]);
  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      api.previewSubtitleCues(id, { track: style.track, style: {
        font_size_percent: style.font_size_percent, max_chars_per_line: style.max_chars_per_line,
        max_lines: style.max_lines, min_duration: style.min_duration, font_family: style.font_family,
      } }).then((r) => { if (!cancelled) setCues(r.cues); }).catch(() => { if (!cancelled) setCues([]); });
    }, 180);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [id, cueKey, style]);
  // 语气标签（[sad] / [gasp]）是给 TTS 的，不该出现在画面上；预览和烧录读同一份 cues，所以只在这一处剥
  const visibleCues = useMemo(
    () => cues.map((c) => ({ ...c, text: stripSubtitleTags(c.text), secondary: stripSubtitleTags(c.secondary),
      lines: c.lines.map(stripSubtitleTags).filter((l) => l.length > 0) })).filter((c) => c.lines.length > 0),
    [cues],
  );
  const activeCue = visibleCues.find((c) => currentTime >= c.start && currentTime < c.end) ?? null;

  // AI 让界面做的事
  useEffect(() => {
    if (!ui) return;
    if (ui.action === 'focus_line') {
      // AI 说"我定位到第 N 句"：切到脚本、滚到那一行、按需播放。直接按 DOM id 滚，
      // 不需要面板再暴露一个"外部要求滚动"的 prop。
      openWindow('script');
      const seg = lines.find((l) => l.line === ui.line);
      if (seg) {
        // 脚本窗口可能刚被打开 / 切成当前标签：等 React 提交后再查 DOM，单帧不够。
        requestAnimationFrame(() => requestAnimationFrame(() => {
          document.getElementById(`line-${seg.id}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }));
      }
      if (ui.play) play(ui.start as number); else seek(ui.start as number);
    } else if (ui.action === 'open_panel') {
      const p = ui.panel as string;
      if (p === 'script' || p === 'subtitle_style') openWindow(p === 'script' ? 'script' : 'subtitle');
      else if (p === 'voice') openWindow('voice');
      else if (p === 'export') setShowExport(true);
      else if (p === 'settings') setShowSettings(true);
    } else if (ui.action === 'play_voice_preview') {
      void new Audio(ui.url as string).play().catch(() => {});
    } else if (ui.action === 'export_ready') {
      const ext = ui.container === 'mkv' ? 'mkv' : 'mp4';
      downloadFile(ui.url as string, `translated_${id.slice(0, 8)}.${ext}`);
    } else if (ui.action === 'browser_action') {
      const what = ui.kind === 'separate_vocals' ? '人声分离'
        : ui.kind === 'burn_subtitles' ? '字幕烧录' : '浏览器操作';
      setNotice(`AI 请求：${ui.reason ? String(ui.reason) : '需要在浏览器里完成' + what}（${what}），请在界面里手动触发`);
    }
    clearUi();
  }, [ui, clearUi, play, seek, id, openWindow]);

  // 导出
  const [capabilities, setCapabilities] = useState<SubtitleCapabilities>({});
  const [burnCap, setBurnCap] = useState<BurnCapability>({ available: false, reason: '检测中…' } as BurnCapability);
  const [delivery, setDelivery] = useState<SubtitleDelivery>('embedded');
  const [keepStyle, setKeepStyle] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [burnProgress, setBurnProgress] = useState<number | null>(null);
  const [exportError, setExportError] = useState('');
  useEffect(() => {
    api.getSubtitleCapabilities().then((r) => setCapabilities(r.capabilities)).catch(() => {});
    probeBurnCapability().then(setBurnCap).catch(() => {});
  }, []);

  const doExport = useCallback(async () => {
    setExporting(true);
    setExportError('');
    try {
      if (delivery === 'burn') {
        if (!visibleCues.length) throw new Error('没有可烧录的字幕：先完成翻译');
        const plain = await run<{ download: string }>('export_video', { format: 'off' });
        if (!plain.ok) throw new Error(plain.error);
        setBurnProgress(0);
        const blob = await burnSubtitles({ videoUrl: api.getExportDownloadUrl(id), cues: visibleCues, style, onProgress: setBurnProgress });
        downloadFile(blob, `translated_${id.slice(0, 8)}.mp4`);
        setShowExport(false);
      } else {
        const format = delivery === 'off' ? 'off'
          : delivery === 'external' ? 'soft'
            : keepStyle ? 'styled' : 'soft';
        const r = await run('export_video', { format });
        if (!r.ok) throw new Error(r.error);
        downloadFile(api.getExportDownloadUrl(id), `translated_${id.slice(0, 8)}.${format === 'styled' ? 'mkv' : 'mp4'}`);
        setShowExport(false);
      }
    } catch (e) {
      setExportError(e instanceof Error ? e.message : String(e));
    } finally {
      setExporting(false);
      setBurnProgress(null);
    }
  }, [delivery, keepStyle, visibleCues, id, run, style]);

  // 脚本里的操作：全都是命令
  const actions = useMemo<ScriptActions>(() => ({
    edit: async (_lineId, field, value, line) => {
      const r = field === 'text'
        ? await run('edit_source', { line, text: value })
        : await run('set_translation', { line, text: value });
      if (!r.ok) setNotice(r.error ?? '修改失败');
    },
    toggleMute: (l: Line) => void run('set_muted', { lines: [l.line], muted: !l.muted }),
    toggleHide: (l: Line) => void run('set_hidden', { lines: [l.line], hidden: !l.hidden }),
    seek,
    redub: async (l: Line) => { const r = await run('redub', { lines: [l.line], force: l.ttsStatus === 'fresh' }); if (!r.ok) setNotice(r.error ?? '配音失败'); },
    retranslate: async (l: Line, hint) => { const r = await run('retranslate', { lines: [l.line], ...(hint ? { length: hint } : {}) }); if (!r.ok) setNotice(r.error ?? '翻译失败'); },
    mark: async (l: Line, kind) => { const r = await run('mark_non_speech', { lines: [l.line], kind }); if (!r.ok) setNotice(r.error ?? '标记失败'); },
    askAbout: (l: Line) => { openWindow('chat'); setQuote({ line: l.line, label: `第 ${l.line} 句` }); },
    openVoice: (speakerId: string) => { setVoiceFocus(speakerId); openWindow('voice'); },
    setVoice: async (l: Line, candidateId: string | null) => {
      const r = await run('set_dub_style', { lines: [l.line], ...(candidateId ? { clone_candidate: candidateId } : { voice: '' }) });
      if (!r.ok) { setNotice(r.error ?? '修改失败'); return; }
      // 改了音色这一句的配音就过期了；重新配音要花钱，所以问一句，不替用户决定
      if (l.kind === 'speech' && l.translation.trim() && !l.muted
        && window.confirm(`第 ${l.line} 句的音色已改，要现在重新配音吗？\n（会产生 TTS 费用；选“取消”则这一句标为「需更新」，之后点顶栏的「补」。）`)) {
        const d = await run('redub', { lines: [l.line] });
        if (!d.ok) setNotice(d.error ?? '配音失败');
      }
    },
  }), [run, seek, openWindow]);

  const colorOf = useCallback((sid: string) => speakerColor(project!, sid), [project]);

  // 时间线用的行：kept = 导出时会把这段原声放回去（有预览地址 = 人声非语言 + 没静音 + 做过人声分离）
  const timelineSegments = useMemo<TimelineSegment[]>(() => lines.map((l) => ({
    id: l.id, speakerId: l.speakerId, start: l.start, end: l.end, kind: l.kind, muted: l.muted, hidden: l.hidden,
    text: l.text, translation: l.translation, kept: (l.kind === 'vocal' || l.kind === 'song') && !l.muted && !!l.audioUrl,
  })), [lines]);

  const fill = useCallback(async () => {
    const translated = project!.needs.translate > 0;
    if (translated) { const r = await run('retranslate', {}); if (!r.ok) { setNotice(r.error ?? '翻译失败'); return; } }
    // needs.dub 是点击时的快照，翻译刚跑完时还是旧值，所以按"这次是否译过 +
    // 是否本来就有缺口"判断；两者都否就别起一个 0 工作量的空任务。
    if (dubbing && (translated || project!.needs.dub > 0)) {
      const r = await run('redub', {});
      if (!r.ok) setNotice(r.error ?? '配音失败');
    }
  }, [project, dubbing, run]);

  // 顶栏的语言切换：会让译文/配音大面积过期，所以先问一下
  const changeLanguage = useCallback(async (language: string) => {
    if (language === project!.settings.targetLanguage) return;
    const has = project!.segments.some((l) => l.translation);
    if (has && !window.confirm(`把目标语言改成 ${language}？\n所有译文和配音都会过期，需要重新翻译和配音。`)) return;
    const r = await run('set_settings', { target_language: language });
    if (!r.ok) setNotice(r.error ?? '修改失败');
  }, [project, run]);

  // 三个窗口的内容。窗口系统只管放在哪、多大；内容是什么在这里定。
  const panels: PanelRenderers = {
    script: (c) => (
      <ScriptPanel
        lines={lines} speakers={project!.speakers} colorOf={colorOf} currentTime={currentTime} flash={flash}
        mix={mixEntries} dubbing={dubbing} actions={actions}
        docked={c.docked} dragProps={c.headerProps} windowControls={c.controls}
      />
    ),
    chat: (c) => (
      <ChatPanel
        chat={chat}
        onSend={(t) => void say(t)}
        onStop={() => void stop()}
        onConfirm={(cid, ok) => void confirm(cid, ok)}
        quote={quote}
        onQuoteRemove={onQuoteRemove}
        suggestions={SUGGESTIONS}
        docked={c.docked}
        dragProps={c.headerProps}
        windowControls={c.controls}
        error={api_.chatError}
        conversations={api_.conversations}
        currentConv={api_.currentConv}
        onNewChat={() => void api_.startNewChat()}
        onOpenChat={(cv) => void api_.openChat(cv)}
        onDeleteChat={(cv) => void api_.deleteChat(cv)}
      />
    ),
    subtitle: (c) => (
      <SubtitleStylePanel
        style={style} onStyleChange={onStyleChange}
        docked={c.docked} dragProps={c.headerProps} windowControls={c.controls}
      />
    ),
    voice: (c) => (
      <VoicePanel
        speakers={project!.speakers} lines={lines} colorOf={colorOf} dubbing={dubbing}
        focusSpeaker={voiceFocus} run={run} onNotice={setNotice}
        docked={c.docked} dragProps={c.headerProps} windowControls={c.controls}
      />
    ),
  };

  return (
    <div className="h-screen flex flex-col bg-claude-bg overflow-hidden">
      <TopBar
        filename={project!.filename}
        targetLanguage={project!.settings.targetLanguage}
        onLanguageChange={changeLanguage}
        needs={project!.needs}
        dubbing={dubbing}
        jobs={project!.jobs}
        undoSummary={project!.undo?.summary ?? null}
        onUndo={() => void run('undo')}
        onFill={fill}
        onCancelJobs={() => void run('cancel_job')}
        onOpenSettings={() => setShowSettings(true)}
        onExport={() => setShowExport(true)}
        onHome={onHome}
        exporting={exporting}
      />

      {notice && (
        <div className="shrink-0 mt-8 bg-amber-50 border-b border-amber-200 px-6 py-2 flex items-center justify-between text-[12px] text-amber-800">
          <span>{notice}</span>
          <button onClick={() => setNotice(null)} className="font-bold hover:text-amber-950">知道了</button>
        </div>
      )}

      {/* 顶栏是 fixed 的（悬停展开时浮在画面上方），内容要给它让出顶部空间。
          右栏是「停靠位」：切换条只在 hover 顶部横杠时浮在上方居中、不占高度；译文 / 助手常驻，
          字幕样式由画面左边的挂耳打开，打开时才多一项，关掉就消失。窗口全被拖成浮动时右栏收起，
          视频撑满。浮动窗口不占栅格。 */}
      <main
        ref={(el) => { wm.mainRef.current = el; }}
        className={`flex-grow min-h-0 grid gap-5 p-5 lg:p-6 pt-10 lg:pt-11 ${wm.docked.length > 0 ? 'lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]' : 'lg:grid-cols-1'}`}
      >
        {/* 左：视频撑满剩余高度 + 时间轴贴底 —— 不再留出空白 */}
        <section className="flex flex-col gap-4 min-h-0 min-w-0">
          {/*
            这里刻意**没有** `overflow-hidden`：字幕样式的挂耳标签要伸到左边缘之外，
            裁掉就看不见了。圆角由 video 自己裁（rounded-2xl）。
          */}
          <div ref={boxRef} className="relative flex-grow min-h-[220px] rounded-2xl bg-black shadow-xl border border-gray-800">
            <video
              ref={videoRef}
              src={api.videoUrl(id)}
              controls
              className="w-full h-full object-contain rounded-2xl bg-black"
              {...videoHandlers}
              onLoadedMetadata={(e) => { videoHandlers.onLoadedMetadata(e); measure(); }}
            />
            {backgroundUrl && <audio ref={backgroundRef} src={backgroundUrl} preload="auto" className="hidden" />}
            {videoRect && activeCue && (
              <div style={overlayContainerStyle(style, videoRect)}>
                {activeCue.lines.map((line, i) => <div key={i} style={overlayLineStyle(style, videoRect)}>{line}</div>)}
              </div>
            )}

            {/* 字幕样式：原版那枚竖排挂耳，贴在画面左边缘**外侧**，像书签一样 */}
            <button
              type="button"
              onClick={() => wm.toggle('subtitle')}
              title="调整字幕的字体、大小、颜色和位置，画面上的字幕会实时跟着变"
              style={{ writingMode: 'vertical-rl' }}
              className={[
                'absolute left-0 top-1/2 -translate-y-1/2 -translate-x-full z-20',
                'px-1 py-3 rounded-l-lg border transition-colors cursor-pointer',
                'text-[10px] font-bold tracking-[0.25em]',
                wm.layout.panels.subtitle.open
                  ? 'bg-claude-accent text-white border-claude-accent shadow-md shadow-claude-accent/25'
                  : 'bg-white text-claude-accent border-claude-accent shadow-sm hover:bg-claude-accent/10',
              ].join(' ')}
            >
              字幕样式
            </button>

            {dubbing && (
              <button
                type="button"
                onClick={() => { setVoiceFocus(null); wm.toggle('voice'); }}
                title="看每个说话人的克隆音色，试听对比、换用"
                style={{ writingMode: 'vertical-rl' }}
                className={[
                  'absolute left-0 top-1/2 mt-[4.5rem] -translate-x-full z-20',
                  'px-1 py-3 rounded-l-lg border transition-colors cursor-pointer',
                  'text-[10px] font-bold tracking-[0.25em]',
                  wm.layout.panels.voice.open
                    ? 'bg-claude-accent text-white border-claude-accent shadow-md shadow-claude-accent/25'
                    : 'bg-white text-claude-accent border-claude-accent shadow-sm hover:bg-claude-accent/10',
                ].join(' ')}
              >
                音色
              </button>
            )}
          </div>

          <div className="shrink-0">
            <Timeline
              segments={timelineSegments}
              speakers={project!.speakers}
              colorOf={colorOf}
              duration={duration}
              currentTime={currentTime}
              onSeek={seek}
              waveform={waveform}
            />
          </div>
        </section>

        {/* 右：停靠位（切换浮在上方居中，不占布局） */}
        {wm.docked.length > 0 && <DockSlot wm={wm} />}
      </main>

      {/* 浮动窗口 + 吸附区提示；面板内容经 portal 渲染进各自的宿主，宿主在停靠位和浮动窗口间搬动 */}
      <WindowLayer wm={wm} />
      <WindowPortals wm={wm} panels={panels} />

      <ExportModal
        isOpen={showExport}
        onClose={() => setShowExport(false)}
        capabilities={capabilities}
        burnCapability={burnCap}
        style={style}
        delivery={delivery}
        onDeliveryChange={setDelivery}
        keepStyle={keepStyle}
        onKeepStyleChange={setKeepStyle}
        onOpenStyleEditor={() => { setShowExport(false); openWindow('subtitle'); }}
        onExport={() => void doExport()}
        isExporting={exporting}
        burnProgress={burnProgress}
        exportError={exportError}
        hasCues={visibleCues.length > 0}
      />

      <SettingsPanel
        open={showSettings}
        onClose={() => setShowSettings(false)}
        settings={project!.settings}
        onChange={async (patch) => {
          const r = await run('set_settings', patch);
          if (!r.ok) setNotice(r.error ?? '修改失败');
          return r.ok;
        }}
        onCustomPrompt={async (prompt) => {
          const r = await run('set_custom_prompt', { prompt });
          if (!r.ok) setNotice(r.error ?? '修改失败');
          return r.ok;
        }}
        hasContent={lines.some((l) => l.translation)}
      />
    </div>
  );
};

export default EditorPage;
