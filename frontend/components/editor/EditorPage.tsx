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
import { speakerColor } from '../../store/project';
import { DEFAULT_SUBTITLE_STYLE } from '../../net/types';
import type { Line, SubtitleCapabilities, SubtitleCue, SubtitleStyle } from '../../net/types';
import { containRect, overlayContainerStyle, overlayLineStyle, stripSubtitleTags, type Rect } from '../../utils/subtitleStyle';
import ChatPanel from '../chat/ChatPanel';
import FloatingChat from '../chat/FloatingChat';
import ScriptPanel, { type ScriptActions } from './ScriptPanel';
import TopBar from '../shell/TopBar';
import SettingsPanel from './SettingsPanel';
import Timeline from '../Timeline';
import SubtitleStylePanel from '../SubtitleStylePanel';
import ExportModal, { type SubtitleDelivery } from '../ExportModal';

type RightPanel = 'script' | 'subtitle';

interface Props {
  project: ReturnType<typeof useProject>;
  file?: File;
  onHome: () => void;
}

const SUGGESTIONS = ['这个项目现在什么情况？', '检查一下有没有被误识别成话的音乐', '哪些句子的配音比时间槽长？'];

const EditorPage: React.FC<Props> = ({ project: api_, file, onHome }) => {
  const { project, chat, flash, ui, clearUi, run, say, stop, confirm, connection } = api_;
  const id = project!.id;
  const lines = project!.segments;
  const dubbing = project!.settings.enableDubbing;

  const [panel, setPanel] = useState<RightPanel>('script');
  const [chatOpen, setChatOpen] = useState(true);
  const [showExport, setShowExport] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [draft, setDraft] = useState('');
  const [focusLine, setFocusLine] = useState<number | null>(null);
  const [waveform, setWaveform] = useState<number[]>([]);
  const [notice, setNotice] = useState<string | null>(null);

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
  const cueKey = useMemo(() => lines.map((l) => `${l.id}:${l.translation}:${l.hidden}:${l.kind}:${l.audioDuration}`).join('|') + style.track, [lines, style.track]);
  useEffect(() => {
    let cancelled = false;
    api.previewSubtitleCues(id, { track: style.track }).then((r) => { if (!cancelled) setCues(r.cues); }).catch(() => { if (!cancelled) setCues([]); });
    return () => { cancelled = true; };
  }, [id, cueKey]);
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
      setFocusLine(ui.line as number);
      setPanel('script');
      if (ui.play) play(ui.start as number); else seek(ui.start as number);
    } else if (ui.action === 'open_panel') {
      const p = ui.panel as string;
      if (p === 'script' || p === 'subtitle_style') setPanel(p === 'script' ? 'script' : 'subtitle');
      else if (p === 'export') setShowExport(true);
      else if (p === 'settings') setShowSettings(true);
    } else if (ui.action === 'play_voice_preview') {
      void new Audio(ui.url as string).play().catch(() => {});
    } else if (ui.action === 'export_ready') {
      downloadFile(ui.url as string, `translated_${id.slice(0, 8)}.mp4`);
    } else if (ui.action === 'browser_action') {
      setNotice(`AI 请求：${ui.action === 'browser_action' && ui.reason ? ui.reason : '需要在浏览器里完成一个操作'}（${ui.action}）`);
    }
    clearUi();
  }, [ui, clearUi, play, seek, id]);

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
        const format = delivery === 'external' || delivery === 'off' ? (delivery === 'off' ? 'off' : 'soft') : 'styled';
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
  }, [delivery, visibleCues, id, run, style]);

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
    askAbout: (l: Line) => { setChatOpen(true); setDraft(`第 ${l.line} 句`); },
  }), [run, seek]);

  const onFocusLine = useCallback((n: number) => {
    const l = lines.find((x) => x.line === n);
    if (!l) return;
    setPanel('script');
    setFocusLine(n);
    seek(l.start);
  }, [lines, seek]);

  const busyIds = useMemo(() => new Set<string>(), []);   // 行锁由服务端判断并在命令里说明原因
  const colorOf = useCallback((sid: string) => speakerColor(project!, sid), [project]);

  const fill = useCallback(async () => {
    if (project!.needs.translate) { const r = await run('retranslate', {}); if (!r.ok) { setNotice(r.error ?? '翻译失败'); return; } }
    // 不看 needs.dub 的快照：翻译刚完成时它还是旧值（新译出的句子会变成"配音过期"），
    // redub 自己按当前指纹只补缺的，没有可补的就空跑一次。
    if (dubbing) { const r = await run('redub', {}); if (!r.ok) setNotice(r.error ?? '配音失败'); }
  }, [project, dubbing, run]);

  // 顶栏的语言切换：会让译文/配音大面积过期，所以先问一下
  const changeLanguage = useCallback(async (language: string) => {
    if (language === project!.settings.targetLanguage) return;
    const has = project!.segments.some((l) => l.translation);
    if (has && !window.confirm(`把目标语言改成 ${language}？\n所有译文和配音都会过期，需要重新翻译和配音。`)) return;
    const r = await run('set_settings', { target_language: language });
    if (!r.ok) setNotice(r.error ?? '修改失败');
  }, [project, run]);

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
        connection={connection}
        exporting={exporting}
        toggleChat={() => setChatOpen((v) => !v)}
        chatOpen={chatOpen}
      />

      {notice && (
        <div className="shrink-0 mt-8 bg-amber-50 border-b border-amber-200 px-6 py-2 flex items-center justify-between text-[12px] text-amber-800">
          <span>{notice}</span>
          <button onClick={() => setNotice(null)} className="font-bold hover:text-amber-950">知道了</button>
        </div>
      )}

      {/* 顶栏是 fixed 的（悬停展开时浮在画面上方），内容要给它让出顶部空间。
          助手是悬浮卡片（不占栅格），所以这里始终是「视频 | 脚本」两栏。 */}
      <main className="flex-grow min-h-0 grid gap-5 p-5 lg:p-6 pt-10 lg:pt-11 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
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
              onClick={() => setPanel(panel === 'subtitle' ? 'script' : 'subtitle')}
              title="调整字幕的字体、大小、颜色和位置，画面上的字幕会实时跟着变"
              style={{ writingMode: 'vertical-rl' }}
              className={[
                'absolute left-0 top-1/2 -translate-y-1/2 -translate-x-full z-20',
                'px-1 py-3 rounded-l-lg border transition-colors cursor-pointer',
                'text-[10px] font-bold tracking-[0.25em]',
                panel === 'subtitle'
                  ? 'bg-claude-accent text-white border-claude-accent shadow-md shadow-claude-accent/25'
                  : 'bg-white text-claude-accent border-claude-accent shadow-sm hover:bg-claude-accent/10',
              ].join(' ')}
            >
              字幕样式
            </button>
          </div>

          <div className="shrink-0">
            <Timeline
              segments={lines.map((l) => ({ id: l.id, speakerId: l.speakerId, startTime: l.start, endTime: l.end, originalText: l.text, translatedText: l.translation, audioPath: l.audioUrl ?? undefined, muted: l.muted, hidden: l.hidden } as never))}
              speakers={project!.speakers.map((s) => ({ id: s.id, name: s.name }) as never)}
              duration={duration}
              currentTime={currentTime}
              onSeek={seek}
              waveform={waveform}
            />
          </div>
        </section>

        {/* 右：脚本（点挂耳时切成字幕样式面板） */}
        <section className="flex flex-col min-h-0 min-w-0">
          {panel === 'script' ? (
            <ScriptPanel
              lines={lines} speakers={project!.speakers} colorOf={colorOf} currentTime={currentTime} flash={flash}
              mix={mixEntries} busyIds={busyIds} dubbing={dubbing} actions={actions}
              focusLine={focusLine} onFocusHandled={() => setFocusLine(null)}
            />
          ) : (
            <SubtitleStylePanel style={style} onStyleChange={onStyleChange} onClose={() => setPanel('script')} />
          )}
        </section>
      </main>

      {/* AI 助手：桌面应用式的浮动窗口 —— 拖标题栏移动、拖右下角改大小。
          不占布局空间，挡到哪里拖开即可。 */}
      {chatOpen && (
        <FloatingChat onClose={() => setChatOpen(false)}>
          {(dragProps) => (
            <ChatPanel
              chat={chat}
              lineCount={lines.length}
              onSend={(t) => void say(t)}
              onStop={() => void stop()}
              onConfirm={(cid, ok) => void confirm(cid, ok)}
              onFocusLine={onFocusLine}
              draft={draft}
              onDraftConsumed={() => setDraft('')}
              suggestions={SUGGESTIONS}
              onClose={() => setChatOpen(false)}
              dragProps={dragProps}
            />
          )}
        </FloatingChat>
      )}

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
        onOpenStyleEditor={() => { setShowExport(false); setPanel('subtitle'); }}
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
      {file && null}
    </div>
  );
};

export default EditorPage;
