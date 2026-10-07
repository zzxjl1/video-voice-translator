
import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { TranscriptionSegment, Speaker } from './types';
import Header from './components/Header';
import VideoUpload from './components/VideoUpload';
import ExportModal, { type SubtitleDelivery } from './components/ExportModal';
import SubtitleStylePanel from './components/SubtitleStylePanel';
import Timeline from './components/Timeline';
import SettingsModal from './components/SettingsModal';
import StreamingLog from './components/StreamingLog';
import { TranscriptionPanel } from './components/TranscriptionPanel';
import { differFromProject } from './utils/settingsDiff';
import {
  uploadVideo,
  translateScript,
  synthesizeSpeech,
  processVideo,
  getVideoStatus,
  resetVideo,
  cancelProcessing,
  setSegmentHidden,
  getVoiceCloneStatus,
  getLanguageVoices,
  getVoices,
  setSpeakerVoice,
  generateVoicePreview,
  exportVideo,
  getExportDownloadUrl,
  getSeparatorInfo,
  getSeparatorToken,
  uploadStems,
  getSubtitleCapabilities,
  getSubtitleStyle,
  saveSubtitleStyle,
  previewSubtitleCues,
  getSrtDownloadUrl,
  srtFilename,
  DEFAULT_SUBTITLE_STYLE,
  type SeparatorInfo,
  type RunState,
  type SeparationMode,
  type SeparationBackends,
  type SubtitleCapabilities,
  type SubtitleCue,
  type SubtitleExportFormat,
  type SubtitleStyle,
  type SubtitleStylePatch,
  type VoiceOption,
  API_BASE,
  setSegmentMuted,
} from './services/apiService';
import { getAudioWaveform, getAudioWaveformFromUrl } from './utils/audioProcessor';
import { decodeAudio, separateAndUpload, isModelCached } from './utils/mdx/separatorClient';
import { computeSyncRates, setPreservesPitch } from './utils/playbackSync';
import {
  containRect,
  cueAtTime,
  overlayContainerStyle,
  overlayLineStyle,
  type Rect,
} from './utils/subtitleStyle';
import { probeBurnCapability, type BurnCapability } from './utils/burnCapability';
import { burnSubtitles } from './utils/burnSubtitles';
import { detectBrowserLanguage } from './utils/languages';
import {
  loadRecentProjects,
  touchRecentProject,
  type RecentProject,
} from './utils/recentProjects';

/**
 * Replace a trailing, in-progress log line (identified by `prefix`) instead of
 * appending a new one, so repeated progress updates do not flood the log.
 *
 * The result ALWAYS ends with a newline, including the line being rewritten.
 *
 * That is not cosmetic. `prev + 'text\n'` only starts a new line if `prev`
 * already ends in one, so leaving the progress line open meant the next thing
 * logged was glued onto it — and that happened at every single call site:
 *
 *     Burn 100%Burn complete (45.7 MB).
 *     Separating: 100% (...)Browser separation complete.
 *     Model download: 87% (12/14 MB)Loading the model...
 *
 * Terminating here fixes all of them at once, and keeps fixing them, because
 * no caller has to remember. (Stripping the newline before splitting is what
 * lets a rewritten line be recognised on the next update: without it the last
 * element would be the empty string after the newline and never match.)
 */
function withTrailingLine(prev: string, prefix: string, text: string): string {
  const body = prev.endsWith('\n') ? prev.slice(0, -1) : prev;
  const lines = body === '' ? [] : body.split('\n');
  const last = lines.length - 1;
  if (last >= 0 && lines[last].startsWith(prefix)) {
    lines[last] = text;
  } else {
    lines.push(text);
  }
  return `${lines.join('\n')}\n`;
}

/**
 * Trigger a browser download for a URL or a Blob.
 *
 * `download` matters: without it the browser may navigate or pick the filename
 * from the URL, which drops the extension the backend chose (and a styled
 * export is an .mkv, not an .mp4).
 *
 * The Blob case is burn-in, where the file is produced in this tab and has no
 * server URL. One helper rather than two, so the anchor wiring and the revoke
 * live in a single place.
 */
function downloadFile(source: string | Blob, filename: string): void {
  const isBlob = typeof source !== 'string';
  const url = isBlob ? URL.createObjectURL(source as Blob) : (source as string);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  if (isBlob) {
    // Long enough that the click has certainly been handled, short enough not
    // to keep a whole video alive in memory for the rest of the session.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }
}

/** Human-readable names for the separation backends. */
const SEPARATION_MODE_LABEL: Record<SeparationMode, string> = {
  client: 'in-browser (MDX-Net)',
  api: '302.AI',
  off: 'off',
};

/**
 * Decide which backend to adopt from a server payload.
 *
 * Prefers the persisted `separation_mode` and falls back to the legacy
 * `enable_bgm_separation` boolean for state written by older versions. A stored
 * backend that can no longer run (say the browser model was removed from the
 * server) degrades to "off" instead of queueing a job that would silently lose
 * separation.
 */
function restoreSeparationMode(
  data: { separation_mode?: string; enable_bgm_separation?: boolean },
  backends?: SeparationBackends,
): SeparationMode {
  const stored = (data.separation_mode ??
    (data.enable_bgm_separation ? 'client' : 'off')) as SeparationMode;
  if (stored === 'off') return 'off';
  return (backends?.[stored]?.available ?? true) ? stored : 'off';
}

/*
 * `detectBrowserLanguage` now lives in `utils/languages.ts`, beside the list it
 * has to agree with.
 *
 * It used to be here: a five-entry prefix table, while the list beside it held
 * ten languages. Browsers set to Portuguese, Italian, Vietnamese or Indonesian
 * therefore fell through to English even though all four are offered as
 * targets. Two declarations of one intent, in two files, is what allowed that —
 * so the guess moved next to the list, and the tags it matches on now live ON
 * the options rather than in a table that can fall out of step.
 */

/**
 * 后端阶段名 → 界面说法。用户看到的应该是"在做什么"，不是内部阶段 id。
 * 与 pipeline_service 的 phase 名一一对应。
 */
const RUN_STEP_LABEL: Record<string, string> = {
  separation: '人声分离',
  asr: '语音识别',
  mm_enhance: '多模态增强（omni 正在听）',
  translation: '翻译',
  voice_clone: '声音复刻',
  tts: '语音合成',
};

const App: React.FC = () => {
  const [videoFile, setVideoFile] = useState<File | null>(null);
  /**
   * A file the user has chosen but not started yet.
   *
   * Kept apart from `videoFile` on purpose: `isIndexPage` is `!videoFile &&
   * !videoId`, so putting a merely-selected file into `videoFile` would tear the
   * landing page down the instant a file was picked — before the user has had a
   * chance to choose a language or turn cloning on. Choosing and starting are
   * two steps now, and this is what keeps them two.
   */
  const [pendingVideoFile, setPendingVideoFile] = useState<File | null>(null);
  const [videoId, setVideoId] = useState<string>('');
  const [waveform, setWaveform] = useState<number[]>([]);
  const [duration, setDuration] = useState<number>(0);
  const [currentTime, setCurrentTime] = useState<number>(0);
  const [segments, setSegments] = useState<TranscriptionSegment[]>([]);
  const [speakers, setSpeakers] = useState<Speaker[]>([]);
  const [isTranscribing, setIsTranscribing] = useState<boolean>(false);
  /**
   * 服务器上的工作流状态（哪一步、跑了多久、能不能停）。来源是 /status 的
   * `run` 字段 —— 不自己维护一份进度：这份状态必须能跨页面刷新、跨标签页
   * 存在，前端内存里的状态做不到。
   */
  const [runState, setRunState] = useState<RunState | null>(null);
  /** 已发出取消请求，等服务器确认（当前 provider 请求跑完才停）。 */
  const [isCancelling, setIsCancelling] = useState(false);
  const [resumeAfterCancel, setResumeAfterCancel] = useState<
    { vid: string; mode: SeparationMode; stems: boolean } | null
  >(null);
  const [isAudioLoading, setIsAudioLoading] = useState<boolean>(false);
  /**
   * 打开一个"上次没跑完"的工程时弹出的选择框。
   *
   * 为什么必须有这一步：未完成的工程打开后如果什么都不做，页面是空的（没有
   * 转写、没有音频），用户会以为工程坏了、也进不去下一步；而如果像以前那样
   * 静默自动续跑，用户又失去了"我上次取消了，别自己又跑起来"的控制权。所以
   * 摆出一个明确的选择：继续跑 / 从头开始 / 先看看。
   */
  const [resumePrompt, setResumePrompt] = useState<
    | {
        vid: string;
        note: string;
        mode: SeparationMode;
        stems: boolean;
      }
    | null
  >(null);

  // Batch processing state
  const [isBatchProcessing, setIsBatchProcessing] = useState<boolean>(false);
  const [batchProgress, setBatchProgress] = useState<string>('');

  // Voice clone state
  const [clonedVoices, setClonedVoices] = useState<Record<string, string>>({});
  const [previewingSpeaker, setPreviewingSpeaker] = useState<string>('');
  /**
   * System voices for the current language, and which speaker is pinned to
   * which. Only populated while voice cloning is OFF.
   *
   * Cloned voices win over system ones (`/tts` resolves explicit > cloned >
   * assigned, and the pipeline does the same), so offering the picker while
   * cloning is on would offer choices that change nothing. An empty list is
   * what hides it.
   */
  const [voiceOptions, setVoiceOptions] = useState<VoiceOption[]>([]);
  const [pinnedVoices, setPinnedVoices] = useState<Record<string, string>>({});
  /** Target language has no built-in voice; dubbing it needs voice cloning. */
  const [voicesNeedCloning, setVoicesNeedCloning] = useState(false);

  // Settings State
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [targetLanguage, setTargetLanguage] = useState<string>(detectBrowserLanguage);
  /**
   * Chinese dialect for the dub. '' means Mandarin, which is also what the
   * backend expects for "no instruction" — see config.tts_instruction.
   *
   * Only Chinese has documented dialects, and only the backend decides whether
   * a given value is real, so this is forwarded as-is rather than filtered here.
   */
  const [targetAccent, setTargetAccent] = useState<string>('');
  const [enableVoiceClone, setEnableVoiceClone] = useState(false);
  // 语气模仿 / 智能选材 —— 后者依赖克隆开关，随工程持久化并在恢复时还原
  const [mmEnhance, setMmEnhance] = useState(false);
  const [cloneSmartPick, setCloneSmartPick] = useState(false);
  // 注入 DS 翻译的自定义要求。默认空 = 标准行为；随工程持久化（reprocess 与
  // 恢复会话都要重放当初的要求），恢复时从 /status 的 custom_prompt 还原。
  const [customPrompt, setCustomPrompt] = useState('');

  useEffect(() => {
    if (!videoId || segments.length === 0 || enableVoiceClone) {
      setVoiceOptions([]);
      setPinnedVoices({});
      setVoicesNeedCloning(false);
      return;
    }
    let cancelled = false;
    getVoices(videoId, targetLanguage)
      .then(list => {
        if (cancelled) return;
        // The server decides which languages have built-in voices; `voices`
        // being empty AND needsVoiceCloning being set is the case the menu
        // explains rather than leaving blank.
        setVoiceOptions(list.voices);
        setPinnedVoices(list.assigned);
        setVoicesNeedCloning(list.needsVoiceCloning);
      })
      .catch(() => {
        // Not fatal: the picker simply does not appear. Synthesis still works,
        // it just stays on whatever `assign_voice_for_speaker` chose.
        if (!cancelled) {
          setVoiceOptions([]);
          setVoicesNeedCloning(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [videoId, targetLanguage, enableVoiceClone, segments.length]);

  const handlePickVoice = useCallback(
    async (speakerId: string, voiceId: string) => {
      if (!videoId) return;
      /*
       * Applied locally first. The picker is a leaf control and the choice is
       * usually kept, so waiting for a round trip would make the label feel
       * broken; the previous map is restored if the server disagrees.
       */
      const previous = pinnedVoices;
      setPinnedVoices(prev => {
        const next = { ...prev };
        if (voiceId) next[speakerId] = voiceId;
        else delete next[speakerId];
        return next;
      });
      try {
        await setSpeakerVoice(videoId, speakerId, voiceId);
      } catch (e) {
        setPinnedVoices(previous);
        const message = e instanceof Error ? e.message : String(e);
        setRawLog(prev => prev + `Could not set the voice: ${message}\n`);
      }
    },
    [videoId, pinnedVoices],
  );

  // Vocal separation. `separationMode` is the single source of truth; the old
  // on/off boolean is derived from it so the two can never disagree.
  // Starts "off" to match the server default and is corrected on mount from
  // GET /api/models/separator.
  const [separationMode, setSeparationMode] = useState<SeparationMode>('off');
  const [isSeparating, setIsSeparating] = useState(false);

  // Which backends the server can actually run, and why the others cannot.
  // Filled from /models/separator (on mount) and /status (per video).
  const [separationBackends, setSeparationBackends] = useState<SeparationBackends>({});
  const [separatorInfo, setSeparatorInfo] = useState<SeparatorInfo | null>(null);
  // Why GET /api/models/separator failed, surfaced in the backend picker so a
  // greyed-out option explains itself instead of looking broken.
  const [separatorInfoError, setSeparatorInfoError] = useState<string>('');

  // Export. No "last export" state is kept: the server rebuilds the file on
  // demand and the browser downloads it, so remembering a URL only created
  // state to keep in sync (and a button that had to change meaning).
  const [isExporting, setIsExporting] = useState(false);
  /**
   * Burn-in progress, 0..1, or null when not burning.
   *
   * Separate from the export spinner because the two waits are nothing alike:
   * a server-side export answers in under a second, while burning re-encodes
   * the whole video on this machine. A spinner cannot tell the user whether
   * that is a 10-second wait or a five-minute one.
   */
  const [burnProgress, setBurnProgress] = useState<number | null>(null);
  const [exportError, setExportError] = useState<string>('');

  // ------------------------------------------------------------------
  // Subtitles
  //
  // The style drives three things at once: the overlay over the player, the
  // track embedded in the export, and (for burn-in) the canvas renderer. Only
  // the timing and the line breaking come from the server, because those depend
  // on probed audio durations — recomputing them here would drift from what the
  // export actually contains.
  // ------------------------------------------------------------------
  const [subtitleCapabilities, setSubtitleCapabilities] = useState<SubtitleCapabilities>({});
  const [subtitleStyle, setSubtitleStyle] = useState<SubtitleStyle>(DEFAULT_SUBTITLE_STYLE);
  // What the user picked in the export dialog, in plain terms. The server's
  // `format` is DERIVED from this (and from `keepEmbeddedStyle`) rather than
  // stored, so the two cannot disagree.
  const [subtitleDelivery, setSubtitleDelivery] = useState<SubtitleDelivery>('embedded');
  const [keepEmbeddedStyle, setKeepEmbeddedStyle] = useState(false);
  // There is deliberately no separate export-track state. Which text appears is
  // `subtitleStyle.track`, and that single value drives the preview overlay, the
  // .srt download and the embedded track. A second control for it in the export
  // dialog only LOOKED like the same setting while changing nothing but the file.
  
  // Cues with the server's adapted timing, for the overlay.
  const [subtitleCues, setSubtitleCues] = useState<SubtitleCue[]>([]);
  /**
   * Stand-in cue from the server, shown only while the style panel is open and
   * no real cue is on screen. Without it, opening the editor on a gap between
   * lines leaves the user adjusting sliders against a bare picture.
   */
  const [sampleCue, setSampleCue] = useState<SubtitleCue | null>(null);
  // Set when the cue request failed, so the failure is visible instead of the
  // subtitles silently vanishing.
  const [subtitleError, setSubtitleError] = useState<string>('');
  const [burnCapability, setBurnCapability] = useState<BurnCapability>({
    available: false,
    reason: 'checking this browser…',
    codecs: [],
  });

  // Where the video actually renders inside its box, so the overlay lands on
  // the picture rather than on the letterbox bars.
  const [videoRect, setVideoRect] = useState<Rect | null>(null);
  const videoBoxRef = useRef<HTMLDivElement | null>(null);
  // Two independent entrances, on purpose: the Header's Export button opens the
  // delivery dialog, and the pill on the video's left edge opens the style
  // editor. They are different questions ("what goes in the file" vs "what does
  // the text look like") and folding them into one panel is what made the
  // earlier single button ambiguous.
  const [showExportModal, setShowExportModal] = useState(false);
  const [showStylePanel, setShowStylePanel] = useState(false);

  // Merge what /models/separator said about the browser backend with what
  // /status reported for the rest, so the picker works before any video exists.
  const effectiveBackends: SeparationBackends = {
    ...separationBackends,
    client: separationBackends.client ?? {
      available: Boolean(separatorInfo?.available),
      reason: separatorInfo
        ? null
        : separatorInfoError
          ? `could not reach GET /api/models/separator — ${separatorInfoError}`
          : 'the separator model is not available on the server',
    },
    off: { available: true, reason: null },
  };

  /** Best backend that can actually run: browser first (free), then the paid API. */
  const pickAvailableSeparationMode = (): SeparationMode | null => {
    for (const mode of ['client', 'api'] as SeparationMode[]) {
      if (effectiveBackends[mode]?.available) return mode;
    }
    return null;
  };

  const handleVoiceCloneChange = (v: boolean) => {
    setEnableVoiceClone(v);
    if (!v) return;

    // Cloning sounds much better on isolated vocals, so switch a working
    // backend on automatically instead of asking. Only speak up when nothing
    // can run — and then say exactly which backends are out and why.
    if (separationMode !== 'off') return;

    const best = pickAvailableSeparationMode();
    if (best) {
      setSeparationMode(best);
      setRawLog(
        prev => prev + `Vocal separation enabled automatically (${SEPARATION_MODE_LABEL[best]}).\n`
      );
      return;
    }

    const reasons = (['client', 'api'] as SeparationMode[])
      .map(m => `${SEPARATION_MODE_LABEL[m]} — ${effectiveBackends[m]?.reason ?? 'unavailable'}`)
      .join('; ');
    window.alert(
      'Voice cloning works best with vocal separation, but no backend can run ' +
      `right now: ${reasons}. Cloning will use the original mixed audio instead.`
    );
  };

  /** Choosing a backend also turns separation on/off ("off" == disabled). */
  const handleSeparationModeChange = (mode: SeparationMode) => {
    // Cloning without separation would waste the clone quality, and the old UI
    // locked it the same way.
    if (enableVoiceClone && mode === 'off') return;
    setSeparationMode(mode);
  };

  // Streaming Log State
  const [isLogOpen, setIsLogOpen] = useState(false);
  const [rawLog, setRawLog] = useState('');

  const videoRef = useRef<HTMLVideoElement>(null);
  const backgroundAudioRef = useRef<HTMLAudioElement>(null);
  const activeAudiosRef = useRef<Map<string, HTMLAudioElement>>(new Map());

  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [backgroundAudioUrl, setBackgroundAudioUrl] = useState<string | null>(null);

  const isIndexPage = !videoFile && !videoId;

  // Management of video source URL
  // Use a ref to track the current blob URL to avoid recreating it when videoId changes
  const blobUrlRef = useRef<string | null>(null);

  useEffect(() => {
    if (videoFile) {
      // Only create a new blob URL if we don't already have one for this file
      if (!blobUrlRef.current) {
        const url = URL.createObjectURL(videoFile);
        blobUrlRef.current = url;
        setVideoUrl(url);
      }
      return () => {
        if (blobUrlRef.current) {
          URL.revokeObjectURL(blobUrlRef.current);
          blobUrlRef.current = null;
        }
      };
    }
    else if (videoId) {
      const serverUrl = `/api/videos/${videoId}/video`;
      setVideoUrl(serverUrl);
    } else {
      blobUrlRef.current = null;
      setVideoUrl(null);
    }
  }, [videoFile, videoId]);

  // Discover how separation should run (browser vs server) on mount.
  useEffect(() => {
    getSeparatorInfo()
      .then(info => {
        setSeparatorInfo(info);
        setSeparationBackends(info.backends ?? {});
        // Mirror the server default, but never start on a backend that cannot
        // actually run (that is how the browser backend used to get "fixed" to
        // an unusable mode).
        const defaultBackendUsable =
          info.backends?.[info.mode]?.available ?? info.available;
        setSeparationMode(info.default_enabled && defaultBackendUsable ? info.mode : 'off');
      })
      .catch(err => {
        // Either the model file is missing server-side (the endpoint 404s with
        // instructions) or the request never reached the backend (wrong origin,
        // no dev proxy). Keep the message so the picker can say which.
        console.warn('Separator model unavailable:', err);
        setSeparatorInfo(null);
        setSeparatorInfoError(err instanceof Error ? err.message : String(err));
      });
  }, []);

  // Which subtitle deliveries this server can perform, and the style to start
  // from. Failing this is not fatal — with no capabilities every non-"off"
  // format reads as unavailable, which is the safe direction to fail in.
  useEffect(() => {
    getSubtitleCapabilities()
      .then(response => {
        setSubtitleCapabilities(response.capabilities);
        setSubtitleStyle(response.default_style);
      })
      .catch(err => {
        console.warn('Subtitle capabilities unavailable:', err);
        setSubtitleCapabilities({});
      });
  }, []);

  // Whether THIS browser can burn subtitles in. Memoised inside the probe:
  // `isConfigSupported` enumerates real encoders, so it is not free.
  useEffect(() => {
    let cancelled = false;
    probeBurnCapability().then(capability => {
      if (!cancelled) setBurnCapability(capability);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // The saved style for a video, so reopening a session shows what was chosen.
  useEffect(() => {
    if (!videoId) {
      setSubtitleCues([]);
      return;
    }
    let cancelled = false;
    getSubtitleStyle(videoId)
      .then(response => {
        if (!cancelled) setSubtitleStyle(response.style);
      })
      .catch(err => console.warn('Subtitle style unavailable:', err));
    return () => {
      cancelled = true;
    };
  }, [videoId]);

  // How many segments actually have a translation: the cue list only changes
  // when this does, so depending on it avoids re-fetching on every unrelated
  // segment update (TTS progress touches segments constantly).
  const translatedCount = useMemo(
    () => segments.filter(segment => segment.translatedText).length,
    [segments],
  );

  /** 隐藏集合的指纹：让上面的 cue 拉取在"隐藏"变化时重跑。 */
  const hiddenSignature = useMemo(
    () => segments.filter(s => s.hidden).map(s => s.id).join(','),
    [segments]
  );

  // Pull the cues. Debounced, and only for the parts that cannot be computed
  // locally: the TIMING comes from probed audio durations, and the line
  // breaking is done by the same code the export uses, so the preview cannot
  // disagree with the file.
  useEffect(() => {
    if (!videoId) {
      setSubtitleCues([]);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      previewSubtitleCues(videoId, {
        track: subtitleStyle.track,
        style: {
          max_chars_per_line: subtitleStyle.max_chars_per_line,
          max_lines: subtitleStyle.max_lines,
          // `min_duration` changes when a cue ENDS, so unlike the purely
          // cosmetic fields it cannot be applied locally — it has to go back to
          // the same code the export uses.
          min_duration: subtitleStyle.min_duration,
        },
      })
        .then(response => {
          if (cancelled) return;
          setSubtitleCues(response.cues);
          setSampleCue(response.sample ?? null);
          setSubtitleError('');
        })
        .catch(err => {
          if (cancelled) return;
          // Surface the failure and KEEP the last good list. There is
          // deliberately no local fallback that re-derives cues from segments:
          // that would use the unadapted timing, so the player and the export
          // would show subtitles at different moments — a second rendering path
          // silently disagreeing with the first.
          const message = err instanceof Error ? err.message : String(err);
          console.warn('Subtitle cues unavailable:', err);
          setSubtitleError(message);
        });
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [
    videoId,
    translatedCount,
    // 隐藏的行不进字幕（后端过滤），所以隐藏集合一变就必须重新拉一次 ——
    // 只依赖 translatedCount 的话，点了"隐藏"画面上的字幕不会消失。
    hiddenSignature,
    subtitleStyle.track,
    subtitleStyle.max_chars_per_line,
    subtitleStyle.max_lines,
    subtitleStyle.min_duration,
  ]);

  // Where the video actually renders inside its box. `object-contain` leaves
  // letterbox bars, and measuring against the box would push the subtitles into
  // those bars whenever the video's aspect ratio differs from the container's.
  //
  // Extracted as a callback so `onLoadedMetadata` can call it directly: the
  // intrinsic size only becomes known there, and depending on `duration` alone
  // silently misses the case where two videos happen to be the same length.
  const measureVideoRect = useCallback(() => {
    const box = videoBoxRef.current;
    if (!box) return;
    const video = videoRef.current;
    setVideoRect(
      containRect(
        box.clientWidth,
        box.clientHeight,
        video?.videoWidth ?? 0,
        video?.videoHeight ?? 0,
      ),
    );
  }, []);

  useEffect(() => {
    const box = videoBoxRef.current;
    if (!box) return;
    measureVideoRect();
    const observer = new ResizeObserver(() => measureVideoRect());
    observer.observe(box);
    return () => observer.disconnect();
  }, [videoUrl, videoFile, duration, measureVideoRect]);

  /**
   * 只自动开工一次。
   *
   * 开发模式下 React.StrictMode 会把 effect 执行两遍（mount → cleanup →
   * 再 mount，实例和 ref 都保留），而这段恢复逻辑里"接着跑"是会真的发请求的
   * 副作用 —— 于是打开工程一次，服务器上就并排跑起两条管线，日志交错、翻译
   * 两遍、TTS 抢同一批分段。这是在真实日志里复现过的，不是理论风险。
   * （后端也有并发守卫兜底，但前端不该主动制造重复。）
   */
  const recoveryDoneRef = useRef(false);

  // Session Recovery
  useEffect(() => {
    if (recoveryDoneRef.current) {
      // 这一行就是 StrictMode 的证据：开发模式下 effect 会执行两遍，第二遍
      // 到这里被挡下。没有它，第二遍会再发一次 /process（并发）。
      console.log('[recovery] effect 第二遍执行 → 已跳过（不会重复发起管线）');
      return;
    }
    recoveryDoneRef.current = true;
    console.log('[recovery] effect 执行（自动恢复/接着跑只会发生这一次）');

    const pathParts = window.location.pathname.split('/').filter(Boolean);
    const idFromUrl = pathParts[0];

    if (idFromUrl && /^[a-f0-9]{32}$/i.test(idFromUrl)) {
      console.log("Attempting session recovery for:", idFromUrl);
      setVideoId(idFromUrl);
      setBackgroundAudioUrl(`/api/videos/${idFromUrl}/audio/background`);

      setIsTranscribing(true);
      getVideoStatus(idFromUrl)
        .then(data => {
          const isCompleted = data.status === 'completed';
          const isError = data.status === 'error';
          const isUploaded = data.status === 'uploaded';

          // Restore settings from server
          if (data.separation_backends) setSeparationBackends(data.separation_backends);
          // Resolve the saved backend once: the retry below runs before this
          // state update is rendered, so it cannot read it back off state.
          const restoredMode = restoreSeparationMode(
            data,
            data.separation_backends
          );
          setSeparationMode(restoredMode);
          if (data.enable_voice_clone !== undefined) setEnableVoiceClone(data.enable_voice_clone);
          if (data.mm_enhance !== undefined) setMmEnhance(data.mm_enhance);
          if (data.clone_smart_pick !== undefined) setCloneSmartPick(data.clone_smart_pick);

          /*
           * Resume the PROJECT's choices, not this browser's.
           *
           * Browser-language detection only picks a landing-page starting value;
           * a recovered session has to come back exactly as this project was
           * configured — the language it was dubbed into, the accent (without
           * which a later refit would quietly switch a Cantonese dub to
           * Mandarin), and the subtitle look the preview was showing when the
           * user left. Empty fields mean the pipeline never chose: keep the
           * client's initial values.
           */
          if (data.target_language) setTargetLanguage(data.target_language);
          if (data.accent !== undefined && data.accent !== null) setTargetAccent(data.accent);
          // 注入的翻译要求同属工程配置：不还原的话 reprocess 会按默认行为
          // 重翻，悄悄丢掉当初的要求。
          if (data.custom_prompt) setCustomPrompt(data.custom_prompt);
          if (data.subtitle_style && Object.keys(data.subtitle_style).length > 0) {
            setSubtitleStyle(prev => ({ ...prev, ...data.subtitle_style }));
          }

          // This browser has now touched this project; remember it for the
          // landing page's recent list.
          touchRecentProject(idFromUrl, data.filename || '');

          // Always load existing segments/speakers
          if (data.segments && data.segments.length > 0) {
            const recoveredSegments: TranscriptionSegment[] = data.segments.map((seg: any) => ({
              id: seg.id,
              speakerId: seg.speaker_label,
              startTime: seg.start_time,
              endTime: seg.end_time,
              originalText: seg.text,
              translatedText: seg.translated_text || '',
              audioUrl: seg.audio_url || undefined,
              muted: (seg as { muted?: boolean }).muted ?? false,
              hidden: (seg as { hidden?: boolean }).hidden ?? false,
              status: (seg.translated_text ? 'ready' : 'pending') as any,
            }));

            if (data.speakers && data.speakers.length > 0) {
              setSpeakers(data.speakers);
            } else {
              const uniqueLabels = Array.from(new Set(recoveredSegments.map(s => s.speakerId)));
              setSpeakers(uniqueLabels.map(label => ({ id: label, name: label })));
            }
            setSegments(recoveredSegments);
          }

          setRunState(data.run ?? null);

          if (data.run?.active) {
            /*
             * 这个工程已经有一条在跑（另一个标签页开的，或本页刷新前那条还
             * 活着）。这里【不发起任何请求】——服务器对重复请求是 409 拒绝，
             * 而不是把它接到别人的运行上：一条管线的设置属于发起它的那个人，
             * 后来者拿到的应该是"现在不能开始"，不是一个不是自己配的任务。
             * 状态条会显示它在哪一步，并给出「取消运行」。
             */
            console.log(
              '[recovery] 服务器报告该工程已在处理中 → 不发起，仅显示状态:',
              {
                step: data.run.step,
                step_status: data.run.step_status,
                elapsed_s: data.run.elapsed_s,
              }
            );
            setIsTranscribing(false);
            setRawLog(
              `Session recovered for: ${idFromUrl}\n` +
              `这个工程正在处理中（当前步骤：${RUN_STEP_LABEL[data.run.step] ?? data.run.step}）。\n` +
              '本页不参与该次运行；需要的话可以点「取消运行」，或在它结束后再发起。\n'
            );
          } else if (!isCompleted) {
            /*
             * 上次没跑完（取消 / 失败 / 中断 / 还没开始）。
             *
             * 不静默续跑：用户可能是主动取消的，"自己又跑起来了"是越权；也不
             * 什么都不做：那样页面是空的，用户进不去下一步。摆出选择框，让他
             * 自己决定。
             */
            const note = isError
              ? `上次处理失败：${data.error || '未知错误'}`
              : isUploaded
              ? '这个工程还没开始处理'
              : data.status === 'cancelled'
              ? `上次已取消（停在：${RUN_STEP_LABEL[data.cancelled_step] ?? data.cancelled_step ?? '处理中'}）`
              : `上次中断在：${data.status}`;
            console.log('[recovery] 未完成 → 弹出"继续跑/从头开始"选择', {
              status: data.status,
              cancelled_step: data.cancelled_step,
            });
            setIsTranscribing(false);
            setRawLog(`Session recovered for: ${idFromUrl}\n${note}。\n`);
            const offer = {
              vid: idFromUrl,
              note,
              mode: restoredMode,
              stems: Boolean(data.has_background),
            };
            setResumeAfterCancel(offer);
            setResumePrompt(offer);
          } else {
            // Completed — just show editor, generate waveform from server audio
            setIsTranscribing(false);
            setIsAudioLoading(true);
            getAudioWaveformFromUrl(`/api/videos/${idFromUrl}/audio`, 300).then(peaks => {
              setWaveform(peaks);
              setIsAudioLoading(false);
            });
            // Load cloned voice map if any
            getVoiceCloneStatus(idFromUrl).then(res => {
              if (res.cloned_voices && Object.keys(res.cloned_voices).length > 0) {
                setClonedVoices(res.cloned_voices);
              }
            }).catch(() => {});
          }
        })
        .catch(err => {
          console.error("Session recovery failed:", err);
          setIsTranscribing(false);
        });
    }
  }, []);

  // Update URL on videoId change
  useEffect(() => {
    if (videoId && !window.location.pathname.includes(videoId)) {
      window.history.pushState({ videoId }, '', `/${videoId}`);
    }
  }, [videoId]);

  // Recent-projects index for the landing page. localStorage only remembers
  // WHICH projects this browser touched; the server validates each one (and
  // supplies filename/status) before anything is displayed.
  //
  // Raw fetch rather than getVideoStatus, deliberately: the contract below
  // distinguishes "404 — the project is gone, drop the entry" from "the server
  // is unreachable — keep it". getVideoStatus throws for both, which would
  // make every validation failure look like a live entry.
  const [recentProjects, setRecentProjects] = useState<RecentProject[]>([]);
  useEffect(() => {
    // The list only renders on the landing page — and must RELOAD when the
    // user returns to it, so a project touched this session (or one whose
    // status changed) shows its current filename/status instead of whatever
    // was true at first mount. Inside a project the card is not rendered.
    if (videoId) return;
    loadRecentProjects(async id => {
      const res = await fetch(`${API_BASE}/videos/${id}/status`);
      if (res.status === 404) return null;
      if (!res.ok) throw new Error(`status ${res.status}`);
      const data = await res.json();
      return { filename: data.filename, status: data.status };
    })
      .then(setRecentProjects)
      .catch(() => {});
  }, [videoId]);

  const handleTimeUpdate = () => {
    if (!videoRef.current) return;
    const video = videoRef.current;
    const time = video.currentTime;
    setCurrentTime(time);

    const bgAudio = backgroundAudioRef.current;
    const hasBgAudio = !!bgAudio && backgroundAudioUrl;

    // If we have background audio, video should always be muted
    if (hasBgAudio) {
      video.muted = true;
    }

    let isAnyTTSPlaying = false;
    let targetVideoRate = 1.0;

    segments.forEach(seg => {
      if (seg.audioUrl) {
        const shouldBePlaying = time >= seg.startTime && time < seg.endTime;
        const existingAudio = activeAudiosRef.current.get(seg.id);

        if (shouldBePlaying) {
          isAnyTTSPlaying = true;

          // How fast this line must play to fill its slot. The server already
          // fitted it (speech_rate + ffprobe measurement), so this is normally
          // within a few percent — and the video rate stays at exactly 1.
          const { audioRate, videoRate } = computeSyncRates(
            seg.actualDuration,
            seg.endTime - seg.startTime,
          );
          if (videoRate !== 1) targetVideoRate = videoRate;

          if (!existingAudio) {
            try {
              const audio = new Audio(seg.audioUrl);
              // Stretching must not shift pitch; do not rely on the default.
              setPreservesPitch(audio);
              audio.playbackRate = audioRate;

              // Progress-based sync
              const videoDuration = seg.endTime - seg.startTime;
              const progress = (time - seg.startTime) / videoDuration;
              if (seg.actualDuration) {
                audio.currentTime = Math.max(0, progress * seg.actualDuration);
              } else {
                audio.currentTime = Math.max(0, time - seg.startTime);
              }

              audio.play().catch(e => console.warn("Audio play blocked:", e));
              activeAudiosRef.current.set(seg.id, audio);
            } catch (e) {
              console.error("Failed to start audio segment:", e);
            }
          } else {
            if (existingAudio.playbackRate !== audioRate) {
              existingAudio.playbackRate = audioRate;
            }
          }
        } else if (existingAudio) {
          existingAudio.pause();
          activeAudiosRef.current.delete(seg.id);
        }
      }
    });

    video.playbackRate = targetVideoRate;

    // Sync background audio playback rate with video
    if (bgAudio && hasBgAudio) {
      if (bgAudio.playbackRate !== targetVideoRate) {
        bgAudio.playbackRate = targetVideoRate;
      }
      // Keep background audio at constant volume
      bgAudio.volume = 1.0;
    }

    // If no background audio, use original behavior for video volume
    if (!hasBgAudio) {
      video.volume = isAnyTTSPlaying ? 0.1 : 1.0;
    }
  };

  const runPipeline = useCallback(async (
    vid: string,
    file?: File,
    opts: { separationMode?: SeparationMode } = {},
  ) => {
    setIsTranscribing(true);
    setRawLog('');
    setIsLogOpen(true);
    setExportError('');
    setIsExporting(false);

    if (file) {
      setIsAudioLoading(true);
      getAudioWaveform(file, 300).then(peaks => {
        setWaveform(peaks);
        setIsAudioLoading(false);
      });
    }

    try {
      setRawLog(prev => prev + '--- Starting Server-Side Processing ---\n');

      let lastSepPct = -1;
      let ttsTotal = 0;

      await processVideo(vid, targetLanguage, (event) => {
        /*
         * 紧凑单行，每条都打：并发时的"交替出现"正是靠这个看出来的。
         *
         * 注意 `done` 这个字段是**双关**的：翻译进度事件里它是"已译条数"
         * （数字），只有整条流的最后一条才是布尔 true。所以判断一律用
         * `=== true` —— 用真值判断会把每个翻译进度事件都当成"全部完成"。
         */
        console.log(
          '[pipeline] 事件',
          [
            event.phase ?? '',
            event.status ?? '',
            event.progress != null ? `${event.progress}/${event.total ?? '?'}` : '',
            // 没有 phase/status 的事件（resume_phase 等）把它的键显出来，
            // 否则日志里只剩一个 '-'，什么也看不出。
            !event.phase && !event.status
              ? Object.keys(event).map(k => `${k}=${event[k]}`).join(' ')
              : '',
          ]
            .filter(Boolean).join(' ') || '-',
          event.cancelled
            ? '← CANCELLED'
            : event.done === true
            ? '← DONE'
            : event.error
            ? `← ERROR ${event.error}`
            : ''
        );

        // Resume info
        if (event.resume_phase) {
          const phase = event.resume_phase;
          if (phase !== 'separation') {
            setRawLog(prev => prev + `Resuming from phase: ${phase}\n`);
          }
        }

        // Separation events
        if (event.phase === 'separation') {
          if (event.status === 'started') {
            setRawLog(prev => prev + 'Separating vocals from background audio...\n');
          } else if (event.status === 'unavailable') {
            // The server refused the backend we asked for and carried on
            // without separation. Remember that so we stop offering it, and
            // report the actual reason instead of a generic "not available".
            const failedMode = event.mode as SeparationMode | undefined;
            if (failedMode && failedMode !== 'off') {
              setSeparationBackends(prev => ({
                ...prev,
                [failedMode]: {
                  available: false,
                  reason: event.message || 'unavailable',
                },
              }));
            }
            setSeparationMode('off');
            setRawLog(prev => prev + `Vocal separation unavailable: ${event.message || 'backend unavailable'}\n`);
          } else if (event.status === 'failed') {
            setRawLog(prev => prev + `Vocal separation failed: ${event.error || 'unknown error'} (continuing without it)\n`);
          } else if (event.status === 'skipped') {
            // `skipped` means separation did NOT run — the backend reworked this
            // event from "already done, nothing to do" into "no isolated vocals,
            // continuing with the original mixed audio". The old wording here
            // said the exact opposite of what had happened.
            const why =
              event.message ||
              (event.mode === 'off'
                ? 'separation is off'
                : 'using the original mixed audio');
            const label = event.mode ? ` [${event.mode}]` : '';
            setRawLog(prev => prev + `Vocal separation skipped${label} — ${why}\n`);
          } else if (event.progress !== undefined) {
            const pct = event.progress;
            if (pct !== lastSepPct) {
              lastSepPct = pct;
              setRawLog(prev => {
                const lines = prev.split('\n');
                const lastIdx = lines.length - 1;
                if (lines[lastIdx].startsWith('Separation progress:')) {
                  lines[lastIdx] = `Separation progress: ${pct}%`;
                } else {
                  lines.push(`Separation progress: ${pct}%`);
                }
                return lines.join('\n');
              });
            }
          } else if (event.status === 'done') {
            if (event.background_url) {
              setBackgroundAudioUrl(event.background_url);
            }
            setRawLog(prev => prev + '\nVocal separation complete.\n');
          }
        }

        // ASR events
        if (event.phase === 'asr') {
          if (event.status === 'started') {
            setRawLog(prev => prev + '\nStarting ASR transcription...\n');
          } else if (event.status === 'skipped') {
            const segs = event.segments || [];
            const spks = event.speakers || [];
            const newSegments: TranscriptionSegment[] = segs.map((seg: any) => ({
              id: seg.id,
              speakerId: seg.speaker_label,
              startTime: seg.start_time,
              endTime: seg.end_time,
              originalText: seg.text,
              translatedText: seg.translated_text || '',
              status: (seg.translated_text ? 'ready' : 'pending') as any,
            }));
            setSpeakers(spks.map((s: any) => ({ id: s.id, name: s.name })));
            setSegments(newSegments);
            setRawLog(prev => prev + `ASR: already done (${segs.length} segments), skipping.\n`);
          } else if (event.status === 'done') {
            const segs = event.segments || [];
            const spks = event.speakers || [];

            const newSegments: TranscriptionSegment[] = segs.map((seg: any) => ({
              id: seg.id,
              speakerId: seg.speaker_label,
              startTime: seg.start_time,
              endTime: seg.end_time,
              originalText: seg.text,
              translatedText: '',
              status: 'pending' as const,
            }));

            setSpeakers(spks.map((s: any) => ({ id: s.id, name: s.name })));
            setSegments(newSegments);
            setRawLog(prev => prev + `Transcription complete. Found ${segs.length} segments.\n`);
          }
        }

        /*
         * Multimodal enhancement (omni listens to the audio and corrects /
         * annotates the ASR lines). These events existed since the feature was
         * written but were never rendered — so enabling 多模态增强 looked like
         * nothing happened, and the multi-second (or, with an unreachable audio
         * URL, multi-minute) omni call looked like a hang right after ASR.
         */
        if (event.phase === 'mm_enhance') {
          if (event.status === 'started') {
            setRawLog(prev => prev + `\n--- Multimodal Enhancement (${event.count} segments) ---\n`);
          } else if (event.status === 'listening') {
            setRawLog(prev => prev + 'omni is listening to the audio (correcting + tagging)...\n');
          } else if (event.status === 'done') {
            setRawLog(
              prev =>
                prev +
                (event.adjusted > 0
                  ? `Enhancement done: ${event.adjusted} line(s) corrected or tagged.\n`
                  : 'Enhancement done: no line needed changing.\n')
            );
          } else if (event.status === 'failed') {
            setRawLog(
              prev =>
                prev +
                `Enhancement failed (continuing with the raw ASR text): ${event.error}\n`
            );
          }
        }

        // Voice cloning events (progress, and what 智能选材 picked)
        if (event.phase === 'voice_clone') {
          if (event.status === 'picking') {
            setRawLog(prev => prev + `Picking reference segments for ${event.speaker_id} (omni)...\n`);
          } else if (event.status === 'pick_failed') {
            setRawLog(
              prev =>
                prev +
                `Smart pick unavailable for ${event.speaker_id} (falling back to rules): ${event.error}\n`
            );
          } else if (event.status === 'picked' && Array.isArray(event.picked)) {
            setRawLog(
              prev => prev + `  picked ${event.picked.length} segment(s): ${event.picked.join(', ')}\n`
            );
          }
        }

        // Translation events
        if (event.phase === 'translation') {
          if (event.status === 'started') {
            setRawLog(prev => prev + `\n--- Starting Translation (${targetLanguage}) ---\n`);
            setRawLog(prev => prev + `Sending ${event.count} segments with full context...\n`);
          } else if (event.status === 'skipped') {
            const translations = event.translations || [];
            setSegments(prev => prev.map(seg => {
              const match = translations.find((r: any) => r.id === seg.id);
              return match ? { ...seg, translatedText: match.translated_text } : seg;
            }));
            setRawLog(prev => prev + `Translation: already done (${translations.length} segments), skipping.\n`);
          } else if (event.status === 'progress') {
            setRawLog(prev => prev + `Translated ${event.done}/${event.total}.\n`);
          } else if (event.status === 'repairing') {
            setRawLog(prev => prev + `Retrying ${event.count} dropped segment(s)...\n`);
          } else if (event.status === 'repair_done') {
            setRawLog(prev => prev + (
              event.still_failed > 0
                ? `Repair recovered ${event.recovered}; ${event.still_failed} segment(s) still failed — those lines will have no dub audio.\n`
                : `Repair recovered all ${event.recovered} dropped segment(s).\n`
            ));
          } else if (event.status === 'done') {
            const translations = event.translations || [];
            setSegments(prev => prev.map(seg => {
              const match = translations.find((r: any) => r.id === seg.id);
              return match ? { ...seg, translatedText: match.translated_text } : seg;
            }));
            setRawLog(prev => prev + `Translation complete. ${translations.length} segments translated.\n`);
          }
        }

        // Voice Clone events
        if (event.phase === 'voice_clone') {
          if (event.status === 'started') {
            setRawLog(prev => prev + `\n--- Voice Cloning (${event.total} speakers) ---\n`);
          } else if (event.status === 'cloning') {
            setRawLog(prev => prev + `[${event.progress}/${event.total}] Cloning voice for ${event.speaker_id}...\n`);
          } else if (event.status === 'done' && event.voice_id) {
            setClonedVoices(prev => ({ ...prev, [event.speaker_id]: event.voice_id }));
            setRawLog(prev => prev + (
              event.reused
                ? `[${event.progress}/${event.total}] ${event.speaker_id}: voice ALREADY EXISTS, reusing (${event.voice_id})\n`
                : `[${event.progress}/${event.total}] ${event.speaker_id}: new voice cloned (${event.voice_id})\n`
            ));
          } else if (event.status === 'failed') {
            setRawLog(prev => prev + `[${event.progress}/${event.total}] ${event.speaker_id}: FAILED (${event.error})\n`);
          } else if (event.status === 'complete') {
            setRawLog(prev => prev + 'Voice cloning complete.\n');
          }
        }

        // TTS events
        if (event.phase === 'tts') {
          if (event.status === 'started') {
            ttsTotal = event.total || 0;
            const alreadyDone = event.already_done || 0;
            setRawLog(prev => prev + `\n--- Starting Audio Synthesis (${ttsTotal - alreadyDone} remaining of ${ttsTotal} total) ---\n`);
          } else if (event.progress !== undefined) {
            const { progress, total, segment_id, audio_url, tts_error } = event;
            if (tts_error) {
              setRawLog(prev => prev + `[${progress}/${total}] Segment ${segment_id}: FAILED.\n`);
            } else {
              setRawLog(prev => prev + `[${progress}/${total}] Segment ${segment_id}: Done.\n`);
              if (audio_url) {
                setSegments(prev => prev.map(s =>
                  s.id === segment_id ? { ...s, audioUrl: audio_url } : s
                ));
              }
            }
          } else if (event.status === 'done') {
            setRawLog(prev => prev + '\n--- Audio Synthesis Complete ---\n');
          }
        }

        // 某个阶段被取消（协作式取消，停在该阶段边界）
        if (event.status === 'cancelled') {
          console.log('[pipeline] 收到 cancelled 事件:', event);
          setRawLog(
            prev => prev + `\n${RUN_STEP_LABEL[event.phase] ?? event.phase}：已取消。\n`
          );
        }

        // Final done
        if (event.done === true) {
          /*
           * `=== true`，不是真值判断：翻译进度事件里的 `done` 是"已译条数"
           * （数字），真值判断会让每个进度事件都被当成"全部完成" —— 结果是
           * 跑到一半就打印 All Processing Complete、把 isTranscribing 关掉、
           * 并把状态条的 active 置为 false（状态条中途消失）。
           *
           * 管线到合成为止，导出是用户的单独动作 —— 这里不再有关联的导出状态
           * 要收尾（isExporting 只由 handleExport 自己管）。
           */
          if (event.cancelled) {
            /*
             * 用户叫停的收尾。要说清三件事：停在哪、东西还在、怎么继续 ——
             * "停了"本身不足以让人放心关掉页面。
             */
            setRawLog(
              prev =>
                prev +
                `\n=== 已取消（停在：${RUN_STEP_LABEL[event.step] ?? event.step ?? '处理中'}）===\n` +
                '已完成的产物都已保存在这个工程里，随时可以继续。\n'
            );
            setIsCancelling(false);
          } else {
            setRawLog(prev => prev + '\n=== All Processing Complete ===\nClosing in 2 seconds...');
          }
          setIsTranscribing(false);
          setRunState(prev => (prev ? { ...prev, active: false } : prev));
        }

        // Error
        if (event.error) {
          setRawLog(prev => prev + `\nERROR: ${event.error}\n`);
        }
      }, {
        // Always report the backend honestly, including when the browser
        // already produced the stems. Forcing "off" in that case was actively
        // misleading: the backend then logged "separation skipped [off]" for a
        // run whose stems exist and ARE mixed into the export. With "client"
        // the backend checks the files itself and reports "done [client]",
        // which is what actually happened.
        //
        // Letting the server decide also removes a redundant flag: it knows
        // whether the stems exist, so the client no longer has to tell it.
        //
        // `opts.separationMode` lets a caller that has just read the saved
        // choice off the server (the mount and back/forward effects) use it
        // immediately. Relying on `setSeparationMode` there does not work: the
        // state update is not rendered yet, so the closure still holds the
        // initial value and the job would be sent as "off".
        separationMode: opts.separationMode ?? separationMode,
        enableVoiceClone,
        mmEnhance,
        cloneSmartPick,
        customPrompt: customPrompt.trim().slice(0, 500),
        // Chinese dialect for the whole dub. The backend ignores it unless the
        // target really is Chinese.
        accent: targetAccent,
      });

      await new Promise(resolve => setTimeout(resolve, 2000));
      setIsLogOpen(false);

    } catch (err) {
      console.error(err);
      setRawLog(prev => prev + `\nERROR: ${err instanceof Error ? err.message : 'Unknown error'}\n`);
    } finally {
      setIsTranscribing(false);
    }
  }, [targetLanguage, targetAccent, enableVoiceClone, separationMode, mmEnhance, cloneSmartPick, customPrompt]);

  /**
   * Run vocal separation locally in the browser and hand the stems to the
   * server. This is what makes separation possible on a small CPU-only host:
   * the backend never loads PyTorch and there is no per-minute API cost.
   *
   * Returns true when the stems were uploaded successfully.
   */
  const runClientSeparation = useCallback(async (vid: string, file?: File): Promise<boolean> => {
    setIsSeparating(true);
    try {
      const info = separatorInfo ?? (await getSeparatorInfo());
      if (!info?.available) {
        throw new Error('the separation model is not available on the server');
      }

      setRawLog(prev => prev + '\n--- Separating vocals in the browser ---\n');

      // Short-lived, video-bound token: the model download and the stem upload
      // are both gated, so this server cannot be used as an open CDN.
      const { token } = await getSeparatorToken(vid);

      if (!(await isModelCached(info))) {
        setRawLog(prev => prev + `Downloading the ${info.size_mb} MB model (cached for next time)...\n`);
      }

      // Prefer the in-memory file: no extra transfer. After a page reload there
      // is none, yet the pipeline can still be started from the URL, so fall
      // back to the audio the server has already extracted. `decodeAudio`
      // accepts any Blob, so both sources share the same code path — that is
      // what makes separation possible on the retry routes.
      let source: Blob;
      if (file) {
        source = file;
      } else {
        // Not `/audio`: that file is the 16 kHz MONO track ASR uses, and
        // separating it would band-limit the output to 8 kHz and discard the
        // stereo image. This endpoint produces a stereo 44.1 kHz source from
        // the original upload, and caches it server-side.
        setRawLog(prev => prev + 'Fetching a stereo separation source from the server...\n');
        const response = await fetch(
          `/api/videos/${vid}/audio/stereo?token=${encodeURIComponent(token)}`
        );
        if (!response.ok) {
          throw new Error(
            `could not fetch a separation source (HTTP ${response.status})`
          );
        }
        source = await response.blob();
      }

      setRawLog(prev => prev + 'Decoding audio locally...\n');
      const audio = await decodeAudio(source);
      setRawLog(prev =>
        prev + `Decoded ${audio.duration.toFixed(1)}s @ ${audio.sampleRate} Hz.\n`
      );

      await separateAndUpload({
        audio,
        info,
        token,
        onLog: (message) => setRawLog(prev => prev + message + '\n'),
        onProgress: (stage, payload) => {
          if (stage === 'model') {
            const pct = payload.percent.toFixed(0);
            const mb = (payload.loaded / 1048576).toFixed(1);
            const totalMb = (payload.total / 1048576).toFixed(1);
            setRawLog(prev =>
              withTrailingLine(prev, 'Model download:', `Model download: ${pct}% (${mb}/${totalMb} MB)`)
            );
          } else if (stage === 'separate') {
            const pct = Math.round((payload.done / Math.max(1, payload.total)) * 100);
            setRawLog(prev =>
              withTrailingLine(
                prev,
                'Separating:',
                `Separating: ${pct}% (chunk ${payload.done}/${payload.total}, window ${payload.window}/${payload.windows})`
              )
            );
          }
        },
        // Fetch a fresh token for the upload: separation of a long video can
        // outlive the token issued for the model download.
        upload: async (vocals, background) => {
          const fresh = await getSeparatorToken(vid);
          return uploadStems(vid, vocals, background, fresh.token);
        },
      });

      setBackgroundAudioUrl(`/api/videos/${vid}/audio/background`);
      setRawLog(prev => prev + 'Browser separation complete.\n');
      return true;
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setRawLog(prev => prev + `Browser separation failed: ${message}\nContinuing without separation.\n`);
      return false;
    } finally {
      setIsSeparating(false);
    }
  }, [separatorInfo]);

  /**
   * Separate in the browser when the user asked for it, then start the server
   * pipeline and tell it not to repeat the work.
   *
   * EVERY entry point has to go through here. The session-recovery and
   * back/forward handlers used to call `runPipeline` directly, which silently
   * skipped client separation: the job ran with `separation_mode=client`, the
   * browser never uploaded stems, and the backend reported "the browser did not
   * upload separation stems" for a run whose own log showed no separation
   * attempt at all.
   *
   * `file` is optional because those routes have no File in memory; separation
   * then pulls the audio from the server instead.
   */
  /**
   * 请求取消某个工程的运行，并等它**真的**停下来。
   *
   * 取消是协作式的：服务器会让当前那个 provider 请求跑完（一次 omni 可能
   * 两分钟），所以这里必须轮询 `/status` 直到 `run.active === false`，不能
   * 点完就往下走 —— 否则新运行会撞上还没退出的旧运行。
   *
   * 返回是否真的停下了。超时（5 分钟）返回 false，调用方就不要发起。
   */
  const stopRunningPipeline = useCallback(async (vid: string): Promise<boolean> => {
    console.log('[pipeline] 用户选择取消旧的运行 → 先取消再开始');
    setRawLog(prev => prev + '\n正在取消上一次运行（等当前请求结束）…\n');
    try {
      const res = await cancelProcessing(vid);
      if (!res.cancelling) {
        setRawLog(prev => prev + '上一次运行已经结束了，直接开始新的。\n');
        return true;
      }
    } catch (err) {
      setRawLog(
        prev => prev + `取消请求失败：${err instanceof Error ? err.message : err}\n`
      );
      return false;
    }

    const deadline = Date.now() + 5 * 60 * 1000;
    let waited = 0;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      waited += 1;
      let stillRunning = true;
      try {
        stillRunning = (await getVideoStatus(vid)).run?.active ?? false;
      } catch {
        // 状态取不到就继续等，不把"网络抖动"当成"已取消"。
        continue;
      }
      if (!stillRunning) {
        console.log(`[pipeline] 上一次运行已取消（等了 ${waited}s）`);
        setRawLog(prev => prev + `上一次运行已取消（等了 ${waited}s），开始新的运行。\n`);
        return true;
      }
      if (waited % 5 === 0) {
        setRawLog(
          prev => prev + `  …仍在取消中（已等 ${waited}s，等待当前请求结束）\n`
        );
      }
    }
    console.warn('[pipeline] 等待取消超时（5 分钟）→ 本次不发起');
    setRawLog(
      prev => prev + '等待超时：上一次运行还没被取消掉，本次不发起新的。\n'
    );
    return false;
  }, []);

  /**
   * 同一个工程同一时刻只允许一个写者。
   *
   * 开工前问服务器：有没有一条运行在跑？有的话给用户一次明确选择 ——
   * "停掉旧的重新开始，还是算了"。选"算了"就什么都不发（不是被拒绝后报错，
   * 而是根本不进去）；选取消就请求取消并**等它真的停下**再返回 true。
   *
   * `interactive=false` 用于后台路径（自动恢复、前进后退）：页面加载时蹦确认
   * 框是骚扰，它们直接跳过这一步。
   *
   * 服务器还有一道 409 兜底（给非前端客户端和竞态用），但那不该是用户看到的
   * 东西 —— 用户看到的应该是这个选择框。
   */
  const ensureProjectFree = useCallback(
    async (vid: string, what: string, interactive = true): Promise<boolean> => {
      let live: RunState | null = null;
      try {
        live = (await getVideoStatus(vid)).run ?? null;
      } catch {
        return true; // 问不到就不在这里拦，交给服务器
      }
      if (!live?.active) return true;

      const stepLabel = RUN_STEP_LABEL[live.step ?? ''] ?? live.step ?? '启动中';
      const elapsed = Math.round(live.elapsed_s ?? 0);
      console.log('[pipeline] 该工程已有运行:', { step: live.step, elapsed_s: elapsed, what });

      if (!interactive) {
        console.log('[pipeline] 后台路径 → 跳过，不干扰正在进行的运行');
        setRawLog(prev => prev + `\n这个工程正在处理中（${stepLabel}），本步不执行。\n`);
        return false;
      }

      const stopAndStart = window.confirm(
        `这个工程正在处理中（当前：${stepLabel}，已跑 ${elapsed}s）。\n\n` +
          `• 确定 = 取消它，然后${what}\n` +
          '• 取消 = 什么都不做（旧的继续跑）'
      );
      if (!stopAndStart) {
        console.log('[pipeline] 用户选择不取消 → 不继续');
        setRawLog(prev => prev + `\n已取消：这个工程仍在处理中（${stepLabel}）。\n`);
        return false;
      }
      return await stopRunningPipeline(vid);
    },
    [stopRunningPipeline]
  );

  const startPipeline = useCallback(
    async (
      vid: string,
      file?: File,
      modeOverride?: SeparationMode,
      /**
       * The server already holds vocals.wav + background.wav for this video.
       * Comes from the status endpoint's `has_background` flag.
       */
      stemsAlreadyPresent = false,
      /**
       * 调用来源，只用于诊断日志。想知道"这次管线是谁发起的、有没有被重复
       * 发起"，看控制台里这一行 + apiService 里的 `[pipeline #n]` 就够了。
       */
      source = 'unknown',
    ): Promise<void> => {
      console.log(
        `[startPipeline] 来源=${source} video=${vid.slice(0, 8)} ` +
          `mode=${modeOverride ?? separationMode} stemsPresent=${stemsAlreadyPresent}`
      );

      // 用户主动发起的路径才弹"停旧开新"的选择框；恢复类路径直接跳过。
      const userInitiated = source.startsWith('upload:') || source.startsWith('user:');
      if (!(await ensureProjectFree(vid, '开始新的运行', userInitiated))) return;

      // An explicit mode wins: the caller may have just read the saved choice
      // off the server and the corresponding state update is not rendered yet.
      const mode = modeOverride ?? separationMode;

      // Only run MDX-Net when the stems are actually missing. Re-running it
      // costs the whole runtime download (~28 MB of wasm) plus the model plus a
      // full separation pass plus a re-upload, and the result would just
      // overwrite identical files. The retry routes hit this every time they
      // resume a job whose separation had already succeeded.
      if (mode === 'client' && !stemsAlreadyPresent) {
        await runClientSeparation(vid, file);
      }

      // Always report the honest mode. The backend checks the files on disk and
      // reports separation as "done" or "not performed" itself, so there is no
      // flag here for the client to get wrong.
      await runPipeline(vid, file, { separationMode: mode });
    },
    [separationMode, runClientSeparation, runPipeline, ensureProjectFree]
  );

  // Handle browser back/forward navigation
  useEffect(() => {
    const handlePopState = () => {
      const pathParts = window.location.pathname.split('/').filter(Boolean);
      const idFromUrl = pathParts[0];

      if (!idFromUrl || !/^[a-f0-9]{32}$/i.test(idFromUrl)) {
        // Navigated back to index — reset all state
        setVideoId('');
        setVideoFile(null);
    setPendingVideoFile(null);
        setVideoUrl(null);
        setSegments([]);
        setSpeakers([]);
        setWaveform([]);
        setDuration(0);
        setCurrentTime(0);
        setIsTranscribing(false);
        setIsLogOpen(false);
        setRawLog('');
        setBackgroundAudioUrl(null);
        setIsBatchProcessing(false);
        setBatchProgress('');
      } else if (idFromUrl !== videoId) {
        // Forward navigation to a /{md5} page — recover session
        setVideoId(idFromUrl);
        // Filename is unknown here; the landing list backfills it from the
        // server the next time it validates.
        touchRecentProject(idFromUrl);
        setVideoFile(null);
    setPendingVideoFile(null);
        setSegments([]);
        setSpeakers([]);
        setWaveform([]);
        setDuration(0);
        setCurrentTime(0);
        setBackgroundAudioUrl(`/api/videos/${idFromUrl}/audio/background`);

        setIsTranscribing(true);
        getVideoStatus(idFromUrl)
          .then(data => {
            const isCompleted = data.status === 'completed';
            const isError = data.status === 'error';
            const isUploaded = data.status === 'uploaded';

            // Restore settings from server
            if (data.separation_backends) setSeparationBackends(data.separation_backends);
            // Resolve the saved backend once: the retry below runs before this
            // state update is rendered, so it cannot read it back off state.
            const restoredMode = restoreSeparationMode(
              data,
              data.separation_backends
            );
            setSeparationMode(restoredMode);
            if (data.enable_voice_clone !== undefined) setEnableVoiceClone(data.enable_voice_clone);
          if (data.mm_enhance !== undefined) setMmEnhance(data.mm_enhance);
          if (data.clone_smart_pick !== undefined) setCloneSmartPick(data.clone_smart_pick);

            if (data.segments && data.segments.length > 0) {
              const recoveredSegments: TranscriptionSegment[] = data.segments.map((seg: any) => ({
                id: seg.id,
                speakerId: seg.speaker_label,
                startTime: seg.start_time,
                endTime: seg.end_time,
                originalText: seg.text,
                translatedText: seg.translated_text || '',
                audioUrl: seg.audio_url || undefined,
              muted: (seg as { muted?: boolean }).muted ?? false,
              hidden: (seg as { hidden?: boolean }).hidden ?? false,
                status: (seg.translated_text ? 'ready' : 'pending') as any,
              }));
              if (data.speakers && data.speakers.length > 0) {
                setSpeakers(data.speakers);
              } else {
                const uniqueLabels = Array.from(new Set(recoveredSegments.map(s => s.speakerId)));
                setSpeakers(uniqueLabels.map(label => ({ id: label, name: label })));
              }
              setSegments(recoveredSegments);
            }

              setRunState(data.run ?? null);

            if (!isCompleted) {
              // 同上（会话恢复那条路）：未完成一律弹选择框，不静默续跑。
              const note = isError
                ? `上次处理失败：${data.error || '未知错误'}`
                : isUploaded
                ? '这个工程还没开始处理'
                : data.status === 'cancelled'
                ? `上次已取消（停在：${RUN_STEP_LABEL[data.cancelled_step] ?? data.cancelled_step ?? '处理中'}）`
                : `上次中断在：${data.status}`;
              setIsTranscribing(false);
              setRawLog(`Session recovered for: ${idFromUrl}\n${note}。\n`);
              const offer = {
                vid: idFromUrl,
                note,
                mode: restoredMode,
                stems: Boolean(data.has_background),
              };
              setResumeAfterCancel(offer);
              setResumePrompt(offer);
            } else {
              setIsTranscribing(false);
              setIsAudioLoading(true);
              getAudioWaveformFromUrl(`/api/videos/${idFromUrl}/audio`, 300).then(peaks => {
                setWaveform(peaks);
                setIsAudioLoading(false);
              });
              getVoiceCloneStatus(idFromUrl).then(res => {
                if (res.cloned_voices && Object.keys(res.cloned_voices).length > 0) {
                  setClonedVoices(res.cloned_voices);
                }
              }).catch(() => {});
            }
          })
          .catch(err => {
            console.error("Forward navigation recovery failed:", err);
            setIsTranscribing(false);
          });
      }
    };

    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
    // `startPipeline` carries the current separation mode, so a stale `runPipeline`
    // can no longer be used to start a job without browser separation.
  }, [videoId, startPipeline]);

  /** The user picked a file. Nothing is uploaded until they press start. */
  const handleFilePicked = useCallback((file: File) => {
    setPendingVideoFile(file);
  }, []);

  const handleVideoSelect = useCallback(async (file: File) => {
    setVideoFile(file);
    setIsAudioLoading(true);
    setIsTranscribing(true);
    setSegments([]);
    setSpeakers([]);
    setRawLog('');
    setIsLogOpen(true);

    try {
      setRawLog(prev => prev + 'Uploading video to server...\n');
      const uploadResult = await uploadVideo(file);
      setVideoId(uploadResult.video_id);
      touchRecentProject(uploadResult.video_id, file.name);
      setRawLog(prev => prev + `Upload complete. Video ID: ${uploadResult.video_id}\n`);

      if (uploadResult.exists) {
        /*
         * 这段视频处理过。三道判断，顺序就是这个重要性：
         *
         *   1. 设置与工程里存的**完全一样**，且上次是取消/失败/中断 → 弹框问
         *      "继续还是重来"。这是他唯一需要拿主意的情形。
         *   2. 设置不一样 → 直接清空重跑，不问。改了设置之后有些步骤本来就失效
         *      了（换语言要重翻、换分离方式要重分离），问"要不要继续"是在问一
         *      个不成立的选项：旧进度对不上新设置。
         *   3. 没跑过（uploaded）→ 直接开始，没什么可继续也没什么可清。
         *
         * 设置完全一样 + 已完成 → 打开编辑器（它确实做完了）。
         */
        setIsLogOpen(false);
        setIsTranscribing(false);

        const statusData = await getVideoStatus(uploadResult.video_id);

        /*
         * 只刷新"服务端能跑哪些后端"这份能力清单，**不恢复工程里存的设置**。
         *
         * 这条路是用户亲手配好设置、按了开始处理，所以本次运行的设置就是页面上
         * 那一份。原来这里把 separation_mode 和 enable_voice_clone 覆盖回旧值，
         * 结果是"语言是新的、分离和克隆是旧的"这种混合配置，看起来就是"选了继
         * 续，设置自己跳回旧的"。工程里的旧设置只用来跟当前设置做比较。
         */
        setSeparationBackends(statusData.separation_backends ?? {});

        const isCompleted = statusData.status === 'completed';
        const isError = statusData.status === 'error';
        const isUploaded = statusData.status === 'uploaded';

        const differing = differFromProject(statusData, {
          targetLanguage,
          targetAccent,
          separationMode,
          enableVoiceClone,
          mmEnhance,
          cloneSmartPick,
          customPrompt,
        });
        console.log('[upload] 同一视频已存在:', {
          status: statusData.status,
          settingsDiffer: differing,
        });

        if (isUploaded) {
          setRawLog('这个视频上传过但还没处理，直接开始。\n');
          setIsLogOpen(true);
          setIsTranscribing(true);
          await startPipeline(uploadResult.video_id, file, undefined, false, 'upload:never-run');
          return;
        }

        if (differing.length > 0) {
          // 旧进度对新设置无效，直接清空重跑。把差异列出来，用户才知道为什么
          // 没弹框、为什么要重新跑。
          setRawLog(
            '检测到设置与上次不同：' + differing.join('、') + '\n' +
            '这些设置会改变部分步骤的结果，旧进度已清空，用当前设置重新处理…\n'
          );
          setIsLogOpen(true);
          setIsTranscribing(true);
          setSegments([]);
          setSpeakers([]);
          await resetVideo(uploadResult.video_id);
          await startPipeline(uploadResult.video_id, file, undefined, false, 'upload:settings-changed');
          return;
        }

        // 设置完全一样。已完成就是做完了，直接打开；取消/失败/中断才需要问。
        let message = 'This video has been uploaded before.\n\n';
        if (isCompleted) {
          message += 'Processing is fully completed.\n\n';
        } else if (isError) {
          message += `Previous processing failed: ${statusData.error || 'Unknown error'}\n\n`;
        } else {
          message += `Processing was interrupted at stage: ${statusData.status}\n\n`;
        }
        message +=
          'Choose an action:\n• OK = Continue / Retry from where it stopped\n• Cancel = Reset and start over';

        const continueExisting = isCompleted ? true : window.confirm(message);

        if (continueExisting) {
          // Continue / retry — load existing data first
          if (statusData.segments && statusData.segments.length > 0) {
            const recoveredSegments: TranscriptionSegment[] = statusData.segments.map((seg: any) => ({
              id: seg.id,
              speakerId: seg.speaker_label,
              startTime: seg.start_time,
              endTime: seg.end_time,
              originalText: seg.text,
              translatedText: seg.translated_text || '',
              audioUrl: seg.audio_url || undefined,
              muted: (seg as { muted?: boolean }).muted ?? false,
              hidden: (seg as { hidden?: boolean }).hidden ?? false,
              status: (seg.translated_text ? 'ready' : 'pending') as any,
            }));
            if (statusData.speakers && statusData.speakers.length > 0) {
              setSpeakers(statusData.speakers);
            }
            setSegments(recoveredSegments);
          }
          if (statusData.has_background) {
            setBackgroundAudioUrl(`/api/videos/${uploadResult.video_id}/audio/background`);
          }

          if (isCompleted) {
            // Already done — just show the editor
            getAudioWaveform(file, 300).then(peaks => {
              setWaveform(peaks);
              setIsAudioLoading(false);
            });
            getVoiceCloneStatus(uploadResult.video_id).then(res => {
              if (res.cloned_voices && Object.keys(res.cloned_voices).length > 0) {
                setClonedVoices(res.cloned_voices);
              }
            }).catch(() => {});
            return;
          }

          // Resume pipeline. Pass the existing stems so a video that was
          // already separated is not separated again in the browser.
          await startPipeline(
            uploadResult.video_id,
            file,
            undefined,
            Boolean(statusData.has_background),
            'upload:resume-existing'
          );
        } else {
          // Reset and re-process
          setRawLog('Resetting video data...\n');
          setIsLogOpen(true);
          setIsTranscribing(true);
          setSegments([]);
          setSpeakers([]);
          await resetVideo(uploadResult.video_id);
          setRawLog(prev => prev + 'Reset complete. Starting fresh...\n');
          await startPipeline(uploadResult.video_id, file, undefined, false, 'upload:after-reset');
        }
      } else {
        // New video — run the full pipeline
        await startPipeline(uploadResult.video_id, file, undefined, false, 'upload:new-video');
      }

    } catch (err) {
      console.error(err);
      setRawLog(prev => prev + `\nERROR: ${err instanceof Error ? err.message : 'Unknown error'}\n`);
      alert("Processing encountered an error. Please check the server logs.");
    } finally {
      setIsTranscribing(false);
      setIsAudioLoading(false);
    }
  }, [
    targetLanguage,
    targetAccent,
    separationMode,
    enableVoiceClone,
    mmEnhance,
    cloneSmartPick,
    customPrompt,
    runPipeline,
    startPipeline,
  ]);

  /**
   * Start the job for the file that was picked.
   *
   * Declared after `handleVideoSelect` because it calls it — a useCallback's
   * dependency array is read during render, so referencing a `const` defined
   * further down would throw before it ever ran.
   */
  const handleStartProcessing = useCallback(() => {
    if (pendingVideoFile) void handleVideoSelect(pendingVideoFile);
  }, [pendingVideoFile, handleVideoSelect]);

  /**
   * 取消当前处理。
   *
   * 协作式：服务器会让当前正在进行的那个请求（一次 ASR / 一次 omni / 一行
   * TTS）跑完，然后在步骤边界停下 —— 粒度就是"一次请求"，因为这已经是能安全
   * 中断的最小单位（provider 已经计费，硬中断还可能留下半个文件）。停下来的
   * 工程状态是 CANCELLED（已取消），已完成的阶段产物都留着，之后从断点继续。
   */
  const handleCancelProcessing = useCallback(async () => {
    if (!videoId || isCancelling) return;
    setIsCancelling(true);
    console.log('[cancel] 请求取消 video=' + videoId.slice(0, 8));
    setRawLog(prev => prev + '\n已请求取消：当前请求跑完就会停在这一步…\n');
    try {
      const res = await cancelProcessing(videoId);
      console.log('[cancel] 服务器回应:', res);
      if (!res.cancelling) {
        setRawLog(prev => prev + `没有正在处理的管线（${res.reason ?? '已结束'}）。\n`);
      }
    } catch (err) {
      setRawLog(prev => prev + `取消失败：${err instanceof Error ? err.message : err}\n`);
    } finally {
      // 不在这里复位 isCancelling：真正的复位信号是 runState.cancelling 变回
      // false（事件流会送来 cancelled）。给一个兜底超时，避免按钮永远卡住。
      window.setTimeout(() => setIsCancelling(false), 3000);
    }
  }, [videoId, isCancelling]);

  /** 从断点继续（未完成的工程不会自动重跑，等用户按这里或选择框）。 */
  const handleResumeCancelled = useCallback(() => {
    if (!resumeAfterCancel) return;
    setRawLog(prev => prev + '\n从断点继续处理…\n');
    setIsLogOpen(true);
    setIsTranscribing(true);
    setResumeAfterCancel(null);
    setResumePrompt(null);
    startPipeline(
      resumeAfterCancel.vid,
      undefined,
      resumeAfterCancel.mode,
      resumeAfterCancel.stems,
      'user:resume-cancelled'
    ).finally(() => setIsTranscribing(false));
  }, [resumeAfterCancel, startPipeline]);

  /** 从头开始：清空已有进度（保留原视频）再重跑一遍。 */
  const handleRestartFromScratch = useCallback(async () => {
    if (!resumePrompt) return;
    const { vid, mode } = resumePrompt;
    setResumePrompt(null);
    setResumeAfterCancel(null);
    setRawLog(prev => prev + '\n从头开始：清空已有进度（保留原视频）…\n');
    setIsLogOpen(true);
    setIsTranscribing(true);
    try {
      await resetVideo(vid);
      await startPipeline(vid, undefined, mode, false, 'user:restart-from-scratch');
    } catch (err) {
      setRawLog(
        prev => prev + `\n重新开始失败：${err instanceof Error ? err.message : err}\n`
      );
    } finally {
      setIsTranscribing(false);
    }
  }, [resumePrompt, startPipeline]);

  const handleDismissResumePrompt = useCallback(() => {
    window.location.href = '/'
  }, []);

  /**
   * 工作流状态轮询。
   *
   * 只在"可能有事发生"的时候轮询：本页在跑，或服务器报告有活跃运行（比如
   * 另一个标签页开的）。空闲工程不产生任何轮询流量；工程切换时先取一次，
   * 这样打开一个"别人正在跑"的工程立刻就能看到状态条。
   */
  useEffect(() => {
    if (!videoId) {
      setRunState(null);
      return;
    }
    let stopped = false;
    /** 上一次打过的运行状态指纹，避免轮询刷屏。 */
    const lastRunStamp = { current: '' };

    const fetchRun = async () => {
      try {
        const data = await getVideoStatus(videoId);
        if (stopped) return;
        setRunState(data.run ?? null);
        // 只在变化时打，避免轮询刷屏。
        const stamp = `${data.run?.active}|${data.run?.step}`;
        if (stamp !== lastRunStamp.current) {
          lastRunStamp.current = stamp;
          const r = data.run;
          // 单行输出：和 `[pipeline …]` 那些行同一个形式，复制粘贴不会只剩一个
          // 行号（对象形式的日志被折叠后就是这样丢内容的）。
          console.log(
            `[run ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}] ` +
              `active=${r?.active ?? false} step=${r?.step ?? '-'} ` +
              `status=${r?.step_status ?? '-'} ` +
              `progress=${r?.progress ?? '-'}/${r?.total ?? '-'} ` +
              `cancelling=${r?.cancelling ?? false} elapsed=${r?.elapsed_s ?? '-'}s ` +
              `cancelled_step=${data.cancelled_step ?? '-'}`
          );
        }
      } catch {
        /* 状态取不到不该影响界面 */
      }
    };

    void fetchRun();
    if (!isTranscribing && !runState?.active) return;

    const id = window.setInterval(fetchRun, 2500);
    return () => {
      stopped = true;
      window.clearInterval(id);
    };
  }, [videoId, isTranscribing, runState?.active]);

  const handleTranslateSegmentImpl = useCallback(async (id: string, textToTranslate?: string) => {
    // Find the latest segment data from state
    const segmentToTranslate = segments.find(s => s.id === id);
    if (!segmentToTranslate || !videoId) return null;

    // Use the latest text passed from the update handler or fallback to current state
    const text = textToTranslate || segmentToTranslate.originalText;
    if (!text) return null;

    setSegments(prev => prev.map(s => s.id === id ? { ...s, isTranslating: true } : s));

    try {
      const results = await translateScript(
        videoId,
        [{
          id: segmentToTranslate.id,
          text: text,
          speaker_id: segmentToTranslate.speakerId,
          start_time: segmentToTranslate.startTime,
          end_time: segmentToTranslate.endTime,
        }],
        targetLanguage
      );

      if (results.length > 0) {
        const translatedText = results[0].translated_text;
        setSegments(prev => prev.map(s => s.id === id ? { ...s, translatedText, isTranslating: false } : s));
        return translatedText;
      }
    } catch (e) {
      console.error("Translation failed:", e);
    } finally {
      setSegments(prev => prev.map(s => s.id === id ? { ...s, isTranslating: false } : s));
    }
    return null;
  }, [videoId, targetLanguage, segments]);

  const handleSynthesizeSegment = useCallback(async (id: string, textToSynthesize?: string) => {
    const targetSegment = segments.find(s => s.id === id);
    if (!targetSegment || !videoId) return;

    const text = textToSynthesize || targetSegment.translatedText;
    if (!text) return;

    setSegments(prev => prev.map(s => s.id === id ? { ...s, isSynthesizing: true } : s));

    try {
      // Always aim at this line's own time slot. Every synthesis path has to
      // fit, not just the batch pipeline: the playback clamp was tightened on
      // the assumption that the server keeps lines close to their slot, so an
      // unfitted re-synthesis here would be the one case that gets cut short.
      const result = await synthesizeSpeech(videoId, targetSegment.id, text, undefined, {
        targetDuration: Math.max(0.5, targetSegment.endTime - targetSegment.startTime),
        targetLanguage,
        // Without this, re-recording ONE line would drop it back to Mandarin
        // while the rest of the video stayed in the chosen dialect.
        accent: targetAccent,
      });
      // Cache-buster: the file at this URL has just been replaced, and without
      // it the browser serves the previous take and `actualDuration` never
      // updates. Clearing actualDuration forces `onLoadedMetadata` to re-measure.
      setSegments(prev => prev.map(s => s.id === id
        ? { ...s, audioUrl: `${result.audio_url}?v=${Date.now()}`, actualDuration: undefined, isSynthesizing: false }
        : s));
    } catch (e) {
      console.error("Synthesis failed:", e);
    } finally {
      setSegments(prev => prev.map(s => s.id === id ? { ...s, isSynthesizing: false } : s));
    }
  }, [videoId, segments, targetLanguage, targetAccent]);

  /**
   * Re-synthesize one line so it fits its own time slot.
   *
   * Delegates to `handleSynthesizeSegment`, which already aims every synthesis
   * at the slot — this exists for the clearer log line the "Refit" action next
   * to a Duration Mismatch badge produces.
   */
  const handleRefitSegment = useCallback(async (id: string) => {
    setRawLog(prev => prev + `Refitting ${id} to fit its time slot...\n`);
    await handleSynthesizeSegment(id);
  }, [handleSynthesizeSegment]);

  const handleSegmentUpdate = useCallback((id: string, updates: Partial<TranscriptionSegment>) => {
    setSegments(prev => prev.map(s => {
      if (s.id === id) {
        // Clear audio URL if text is changing (will be re-synthesized)
        if ((updates.originalText !== undefined || updates.translatedText !== undefined) && s.audioUrl) {
          return { ...s, ...updates, audioUrl: undefined };
        }
        return { ...s, ...updates };
      }
      return s;
    }));

    // Auto-trigger Processing Chain
    if (updates.originalText !== undefined) {
      // Chain: ASR -> Translation -> TTS
      handleTranslateSegmentImpl(id, updates.originalText).then(newTranslation => {
        if (newTranslation) {
          handleSynthesizeSegment(id, newTranslation);
        }
      });
    } else if (updates.translatedText !== undefined) {
      // Chain: Translation -> TTS
      handleSynthesizeSegment(id, updates.translatedText);
    }
  }, [handleTranslateSegmentImpl, handleSynthesizeSegment]);

  /**
   * Apply a style change immediately and persist it on a short delay.
   *
   * Immediate because the overlay is pure CSS derived from these numbers; the
   * save is debounced because dragging a slider would otherwise fire a request
   * per pixel. Wrapping changes are re-fetched by the cue effect instead, so the
   * preview keeps using the server's line breaking rather than a second
   * implementation of it.
   *
   * Declared before handleExport because that callback depends on
   * handleDownloadSrt below, and a callback referenced in a dependency array has
   * to already be initialised when the array is evaluated.
   */
  /**
   * The server's subtitle format, derived from the plain-language choice.
   *
   * "external" means the video carries no subtitle track at all — the .srt is a
   * separate file — so it maps to "off" for the export itself. Derived rather
   * than stored so the two can never fall out of step.
   *
   * Declared up here because `handleExport` reads it during render (it is in
   * that callback's dependency array), which would otherwise be a TDZ error.
   */
  const effectiveSubtitleFormat: SubtitleExportFormat =
    subtitleDelivery === 'embedded'
      ? keepEmbeddedStyle
        ? 'styled'
        : 'soft'
      : subtitleDelivery === 'burn'
        ? 'burn'
        : 'off';

  const styleSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const handleSubtitleStyleChange = useCallback(
    (patch: SubtitleStylePatch) => {
      setSubtitleStyle(prev => ({ ...prev, ...patch }));
      if (!videoId) return;
      if (styleSaveTimer.current) clearTimeout(styleSaveTimer.current);
      styleSaveTimer.current = setTimeout(() => {
        saveSubtitleStyle(videoId, patch).catch(err =>
          console.warn('Could not save subtitle style:', err)
        );
      }, 500);
    },
    [videoId],
  );

  const handleDownloadSrt = useCallback(() => {
    if (!videoId) return;
    const track = subtitleStyle.track;
    downloadFile(getSrtDownloadUrl(videoId, track), srtFilename(videoId, track));
  }, [videoId, subtitleStyle.track]);

  /**
   * Server-side export: mux the dubbed audio into the video and expose a
   * download URL. The video stream is copied, so this is fast even without a
   * GPU. `silent` suppresses the extra log lines used by the reprocess flow.
   *
   * Declared before handleReprocess because that callback depends on it.
   */
  const handleExport = useCallback(async (silent = false) => {
    if (!videoId) return null;

    /*
     * Burn-in keeps the log panel CLOSED; every other delivery opens it.
     *
     * The log is a full-screen terminal — `fixed inset-0 z-[9999]` — so while it
     * is up it covers the export dialog completely. Burn-in has its own progress
     * bar in that dialog now, and popping a black overlay on top of it hides the
     * one thing the user is watching. The other deliveries finish in well under
     * a second and leave no other trace, so for them the log is the only place
     * the result appears at all.
     *
     * Burn-in's lines are still written to the log, so opening it by hand shows
     * the history.
     */
    const burning = subtitleDelivery === 'burn';
    if (!silent && !burning) {
      setIsLogOpen(true);
      setRawLog(prev => prev + '\n--- Exporting Video (video stream copied) ---\n');
    }

    /**
     * Hand the log panel back when the work is done.
     *
     * A non-silent export opens it and nothing used to close it, so the panel
     * stayed on screen long after the file had already downloaded. The pause
     * matches the pipeline's, for the same reason: the last line should be
     * legible before the panel goes away.
     *
     * A silent export (the re-process flow) leaves the panel alone — its caller
     * owns it and closes it itself. An error leaves it up on purpose, so the
     * message can be read.
     */
    const releaseLog = async () => {
      if (silent) return;
      setRawLog(prev => prev + '\nClosing in 2 seconds...');
      await new Promise(resolve => setTimeout(resolve, 2000));
      setIsLogOpen(false);
    };

    setIsExporting(true);
    setExportError('');

    try {
      /*
       * Burn-in runs HERE, before the normal export.
       *
       * The server cannot do it (no libass, no CJK font, and re-encoding is not
       * affordable on this host — see capabilities()["burn_server"]), so it
       * produces the dubbed video with NO subtitle track and this tab paints
       * the lines into the picture. Same division of labour as in-browser vocal
       * separation: the heavy rasterisation stays on the user's machine.
       */
      if (subtitleDelivery === 'burn') {
        if (subtitleCues.length === 0) {
          // Burning nothing would produce a clean video with no subtitles and
          // no sign anything was skipped — the one outcome worth refusing.
          throw new Error(
            '没有可烧录的字幕。先完成翻译；如果已经翻译过，请检查画面上的字幕是否加载成功。',
          );
        }

        const plain = await exportVideo(videoId, { enabled: false, format: 'off' });
        const source = plain.url || getExportDownloadUrl(videoId);

        setRawLog(
          prev =>
            prev +
            '--- Burning subtitles in the browser ---\n' +
            'Re-encoding on this device; leaving the tab open is enough.\n',
        );

        // 0 rather than null, so the bar appears the moment work starts instead
        // of after the first progress callback — the gap before that is the
        // demuxing, which can take a while on a long video.
        setBurnProgress(0);

        const blob = await burnSubtitles({
          videoUrl: source,
          cues: subtitleCues,
          style: subtitleStyle,
          onProgress: fraction => {
            setBurnProgress(fraction);
            setRawLog(prev =>
              withTrailingLine(prev, 'Burn', `Burn ${Math.round(fraction * 100)}%`),
            );
          },
        });

        setRawLog(prev => prev + `Burn complete (${(blob.size / 1_048_576).toFixed(1)} MB).\n`);
        downloadFile(blob, `translated_${videoId.slice(0, 8)}.mp4`);
        setShowExportModal(false);
        return null;
      }

      const result = await exportVideo(videoId, {
        enabled: effectiveSubtitleFormat !== 'off',
        format: effectiveSubtitleFormat,
        // One track, with the content the style panel says. "Bilingual" already
        // means both languages in one line, so a second track would be a
        // confusing way to express the same thing.
        tracks: [subtitleStyle.track],
        default_track: subtitleStyle.track,
      });

      const trackNote =
        result.subtitle_tracks && result.subtitle_tracks.length > 0
          ? ` with ${result.subtitle_tracks.length} subtitle track${result.subtitle_tracks.length > 1 ? 's' : ''}`
          : '';
      setRawLog(prev =>
        prev +
        `Export complete${result.size_mb ? ` (${result.size_mb} MB)` : ''}${trackNote}` +
        `${result.container === 'mkv' ? ' [MKV — a styled track needs Matroska]' : ''}.\n`
      );

      // Download straight away. The file on the server IS the deliverable, and
      // parking it behind a second button meant tracking a URL and a container
      // just to label that button — visible complexity for no decision.
      downloadFile(
        result.url || getExportDownloadUrl(videoId),
        `translated_${videoId.slice(0, 8)}.${result.container ?? 'mp4'}`,
      );

      // "External" keeps the video free of subtitle tracks, so the .srt is part
      // of what was asked for — not an extra button next to it.
      if (subtitleDelivery === 'external') {
        handleDownloadSrt();
      }
      setShowExportModal(false);
      await releaseLog();
      return result.url;
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Export failed';
      setExportError(message);
      setRawLog(prev => prev + `Export failed: ${message}\n`);
      return null;
    } finally {
      setIsExporting(false);
      // Covers every exit, including the error paths: a bar stuck part-way is
      // worse than no bar, because it looks like work still in flight.
      setBurnProgress(null);
    }
  }, [
    videoId,
    effectiveSubtitleFormat,
    subtitleStyle.track,
    subtitleDelivery,
    handleDownloadSrt,
    // Only the burn branch reads these: it renders the cue list the preview is
    // currently showing, in the style the preview is currently showing it in.
    subtitleCues,
    subtitleStyle,
  ]);

  /**
   * Re-translate everything, re-synthesize everything. Returns whether the
   * user ACCEPTED the confirmation — the settings dialog uses that to decide
   * between closing and rolling its edits back, so "cancel" cannot leave the
   * app showing settings that no run has adopted.
   */
  /** 静音 / 取消静音一行：服务端持久化 + 本地同步（静音即无配音音频）。 */
  /**
   * 隐藏/取消隐藏一行的字幕。
   *
   * 乐观更新（先改界面再发请求，失败回滚），与静音一致 —— 这是个高频的
   * "扫一遍随手点"的操作，等一个来回再变色会很难用。
   */
  const handleToggleHide = useCallback(async (segmentId: string, hidden: boolean) => {
    if (!videoId) return;
    setSegments(prev => prev.map(s => (s.id === segmentId ? { ...s, hidden } : s)));
    try {
      await setSegmentHidden(videoId, segmentId, hidden);
    } catch (e) {
      console.error('Failed to toggle hidden:', e);
      setSegments(prev => prev.map(s => (s.id === segmentId ? { ...s, hidden: !hidden } : s)));
    }
  }, [videoId]);

  const handleToggleMute = useCallback(async (segmentId: string, muted: boolean) => {
    if (!videoId) return;
    setSegments(prev => prev.map(s => (
      s.id === segmentId ? { ...s, muted, audioUrl: muted ? undefined : s.audioUrl } : s
    )));
    try {
      await setSegmentMuted(videoId, segmentId, muted);
    } catch (e) {
      // 失败就把本地状态拨回去，别让界面显示一个服务端没记住的状态。
      setSegments(prev => prev.map(s => (s.id === segmentId ? { ...s, muted: !muted } : s)));
      setRawLog(prev => prev + `\n静音失败: ${e instanceof Error ? e.message : 'unknown'}\n`);
    }
  }, [videoId]);

  /**
   * 只重译这一行：「长一点 / 短一点」调该行的长度预算，自定义要求注入一句话
   * 的要求。译完立刻重新配音 —— 这两个选项要改的本来就是"这行占多长"，
   * 只换文字不换音频等于没生效。静音行保持静音，不自动补音频。
   */
  const handleRetranslateLine = useCallback(async (
    segmentId: string,
    opts: { lengthHint?: 'longer' | 'shorter'; customPrompt?: string },
  ) => {
    const seg = segments.find(s => s.id === segmentId);
    if (!videoId || !seg) return;
    setSegments(prev => prev.map(s => (s.id === segmentId ? { ...s, isTranslating: true } : s)));
    try {
      const results = await translateScript(
        videoId,
        [{
          id: seg.id,
          text: seg.originalText,
          speaker_id: seg.speakerId,
          start_time: seg.startTime,
          end_time: seg.endTime,
        }],
        targetLanguage,
        opts.customPrompt ?? '',
        opts.lengthHint,
      );
      const match = results.find(r => r.id === segmentId);
      const newText = match?.translated_text;
      if (!newText) throw new Error('没有拿到新的译文');
      setSegments(prev => prev.map(s => (
        s.id === segmentId
          ? { ...s, translatedText: newText, isTranslating: false, audioUrl: undefined }
          : s
      )));
      if (!seg.muted) {
        await handleSynthesizeSegment(segmentId, newText);
      }
    } catch (e) {
      setSegments(prev => prev.map(s => (s.id === segmentId ? { ...s, isTranslating: false } : s)));
      setRawLog(prev => prev + `\n重译失败: ${e instanceof Error ? e.message : 'unknown'}\n`);
    }
  }, [segments, videoId, targetLanguage, handleSynthesizeSegment]);

  const handleReprocess = useCallback(async (): Promise<boolean> => {
    if (!videoId || segments.length === 0) return true;

    // 重做会重写同一批音频文件，所以它和"跑管线"是同一种占用：先解决冲突，
    // 再让用户确认这件事本身（顺序反了的话，用户会先确认一个做不了的操作）。
    if (!(await ensureProjectFree(videoId, '重新翻译并重做全部音频'))) return false;

    const confirmReprocess = window.confirm(
      'This will re-translate the entire script and re-generate all audio. Continue?'
    );
    if (!confirmReprocess) return false;

    setIsBatchProcessing(true);
    setIsLogOpen(true);
    setRawLog('');
    setBatchProgress('Translating...');

    try {
      // Phase 1: Re-translate all
      setRawLog('=== Re-translating Full Script ===\n');
      const contextPayload = segments.map(s => ({
        id: s.id,
        text: s.originalText,
        speaker_id: s.speakerId,
        start_time: s.startTime,
        end_time: s.endTime,
      }));

      setRawLog(prev => prev + `Sending ${segments.length} segments with full context...\n`);
      const results = await translateScript(
        videoId, contextPayload, targetLanguage, customPrompt.trim().slice(0, 500)
      );

      setSegments(prev => prev.map(seg => {
        const match = results.find(r => r.id === seg.id);
        return match ? { ...seg, translatedText: match.translated_text, audioUrl: undefined } : seg;
      }));

      setRawLog(prev => prev + `Translation complete. ${results.length} segments translated.\n`);

      // Phase 2: Re-synthesize all
      setRawLog(prev => prev + '\n=== Re-synthesizing All Audio ===\n');
      const toSynthesize = results.map(r => r.id);
      let count = 0;

      for (const segId of toSynthesize) {
        count++;
        const label = `[${count}/${toSynthesize.length}] Segment ${segId}`;
        setRawLog(prev => prev + `${label}: Synthesizing...`);
        setBatchProgress(`Synthesizing ${count}/${toSynthesize.length}`);
        await handleSynthesizeSegment(segId);
        setRawLog(prev => prev + ` Done.\n`);
        await new Promise(r => setTimeout(r, 200));
      }

      // 导出不在这里做 —— 那是 Export 按钮的事。Reprocess 只负责把翻译和
      // 音频重做到最新；出片交给用户主动触发，和首次处理完的行为一致。
      // 注意此时服务端此前的导出文件（如有）已基于旧音频：提示一句，
      // 免得用户拿着旧文件当成重做的成果。
      setRawLog(
        prev =>
          prev +
          '\n=== Reprocess Complete ===\n' +
          'Translation and audio are up to date. Use Export to produce the video' +
          ' — any previously exported file was built from the old audio.\n' +
          'Closing in 1.5 seconds...'
      );
      await new Promise(resolve => setTimeout(resolve, 1500));
      setIsLogOpen(false);
      return true;
    } catch (error) {
      console.error("Reprocess failed:", error);
      setRawLog(prev => prev + `\n\nERROR: ${error instanceof Error ? error.message : 'Unknown error'}`);
      return true;
    } finally {
      setBatchProgress('');
      setIsBatchProcessing(false);
    }
  }, [segments, videoId, targetLanguage, customPrompt, handleSynthesizeSegment, ensureProjectFree]);

  /**
   * The cue to show right now, straight from the server's list.
   *
   * There is deliberately NO local fallback that re-derives cues from
   * `segments`. It would use the unadapted segment times while the export uses
   * the times extended to cover the dubbed audio, so the player and the file
   * would disagree about when a line appears. A visible error (below) is more
   * honest than a second renderer that quietly drifts.
   */
  const overlayCue = useMemo(() => {
    /*
     * A real cue always wins, whether or not the style editor is open.
     */
    const cue = cueAtTime(subtitleCues, currentTime);
    if (cue) return cue;

    /*
     * Nothing to draw at this instant — a gap between lines, or the playhead
     * parked outside every cue. With the editor open that used to mean tuning
     * sliders against a bare picture, so fall back to the server's sample cue:
     * built by the same `Cue.lines()` the export runs, so previewing it cannot
     * teach the wrong thing about line breaking.
     *
     * The editor briefly took the picture over completely instead. That was
     * wrong: it made the line under the playhead impossible to check, which is
     * one of the reasons to open the editor in the first place. The sample
     * fills gaps; it does not replace the video.
     */
    return showStylePanel ? sampleCue : null;
  }, [subtitleCues, currentTime, showStylePanel, sampleCue]);

  const handlePreviewVoice = useCallback(async (speakerId: string) => {
    if (!videoId || previewingSpeaker) return;
    setPreviewingSpeaker(speakerId);
    try {
      const audioUrl = await generateVoicePreview(videoId, speakerId);
      const audio = new Audio(audioUrl + '?t=' + Date.now());
      audio.play().catch(e => console.warn("Preview play blocked:", e));
      audio.onended = () => setPreviewingSpeaker('');
    } catch (e) {
      console.error("Voice preview failed:", e);
      setPreviewingSpeaker('');
    }
  }, [videoId, previewingSpeaker]);

  const handleSeek = useCallback((time: number) => {
    if (videoRef.current) {
      videoRef.current.currentTime = time;
      activeAudiosRef.current.forEach(a => a.pause());
      activeAudiosRef.current.clear();
      videoRef.current.volume = backgroundAudioUrl ? 0 : 1.0;
      // Sync background audio
      if (backgroundAudioRef.current) {
        backgroundAudioRef.current.currentTime = time;
      }
    }
  }, [backgroundAudioUrl]);

  return (
    <>
      <SettingsModal
        isOpen={isSettingsOpen}
        onClose={() => setIsSettingsOpen(false)}
        onSave={() => {
          // 分离后端 / 克隆开关都改变管线的做法，改了就必须重跑一遍，
          // 否则屏幕上还是旧设置产出的配音。handleReprocess 自己带确认，
          // 用户点保存后还会看到一次代价说明（重翻 + 重合成）。
          //
          // 必须把它的结果【返回】给弹窗：false 代表用户在确认里点了取消，
          // 弹窗据此回滚设置并留在原地，而不是关掉留下一个改了但没重跑的状态。
          return handleReprocess();
        }}
        enableVoiceClone={enableVoiceClone}
        onVoiceCloneChange={handleVoiceCloneChange}
        separationMode={separationMode}
        onSeparationModeChange={handleSeparationModeChange}
        separationBackends={effectiveBackends}
        bgmSeparationLocked={enableVoiceClone}
      />

      <StreamingLog
        isOpen={isLogOpen}
        logs={rawLog}
        onClose={() => setIsLogOpen(false)}
        title="Processing Log"
        isProcessing={isTranscribing}
        // 处理中这个浮层盖住整个页面，状态条上的「取消运行」点不到，
        // 所以取消入口必须在这层里也有一份。
        onCancel={() => void handleCancelProcessing()}
        isCancelling={isCancelling || Boolean(runState?.cancelling)}
        stepLabel={
          runState?.active ? RUN_STEP_LABEL[runState.step ?? ''] ?? runState.step : null
        }
      />

      {isIndexPage ? (
        <VideoUpload
          onFilePicked={handleFilePicked}
          pendingFileName={pendingVideoFile?.name}
          onStart={handleStartProcessing}
          isLoading={isTranscribing || isSeparating}
          targetLanguage={targetLanguage}
          onLanguageChange={setTargetLanguage}
          targetAccent={targetAccent}
          onAccentChange={setTargetAccent}
          enableVoiceClone={enableVoiceClone}
          onVoiceCloneChange={handleVoiceCloneChange}
          separationMode={separationMode}
          onSeparationModeChange={handleSeparationModeChange}
          separationBackends={effectiveBackends}
          bgmSeparationLocked={enableVoiceClone}
          mmEnhance={mmEnhance}
          onMmEnhanceChange={setMmEnhance}
          cloneSmartPick={cloneSmartPick}
          onCloneSmartPickChange={setCloneSmartPick}
          customPrompt={customPrompt}
          onCustomPromptChange={setCustomPrompt}
          recentProjects={recentProjects}
          onOpenProject={id => window.location.assign(`/${id}`)}
        />
      ) : (
        <div className="h-screen flex flex-col bg-claude-bg text-claude-text font-sans selection:bg-claude-accent/20 overflow-hidden">
          <Header
            onOpenSettings={() => setIsSettingsOpen(true)}
            onReprocess={handleReprocess}
            targetLanguage={targetLanguage}
            onLanguageChange={setTargetLanguage}
            targetAccent={targetAccent}
            onAccentChange={setTargetAccent}
            isProcessing={isBatchProcessing}
            hasSegments={segments.length > 0}
            onExport={() => setShowExportModal(true)}
            isExporting={isExporting}
            exportError={exportError}
          />

          {/* Export dialog. Owns "what goes into the file" only; the look of the
              text is edited from the pill on the video. */}
          <ExportModal
            isOpen={showExportModal}
            onClose={() => setShowExportModal(false)}
            capabilities={subtitleCapabilities}
            burnCapability={burnCapability}
            style={subtitleStyle}
            delivery={subtitleDelivery}
            onDeliveryChange={setSubtitleDelivery}
            keepStyle={keepEmbeddedStyle}
            onKeepStyleChange={setKeepEmbeddedStyle}
            onOpenStyleEditor={() => {
              // Close first: the style panel is meant to be judged against the
              // picture, which this dialog covers.
              setShowExportModal(false);
              setShowStylePanel(true);
            }}
            onExport={() => handleExport(false)}
            isExporting={isExporting}
            burnProgress={burnProgress}
            exportError={exportError}
            hasCues={segments.some(segment => Boolean(segment.translatedText))}
          />

          {/*
            打开未完成工程时的选择框。三个都列出来：继续跑（从断点）、从头开始
            （清空进度，动作更大所以放第二位并写明后果）、先看看（收起，页面仍
            可用）。用自建对话框而不是 window.confirm —— 原生弹窗的按钮只能叫
            "确定/取消"，而"取消"在这里既像"取消运行"又像"取消弹窗"，太容易点错。
          */}
          {resumePrompt && (
            <div className="fixed inset-0 z-[9998] flex items-center justify-center bg-black/30 p-6">
              <div className="w-full max-w-sm rounded-2xl bg-white p-5 shadow-2xl">
                <h3 className="text-sm font-bold text-gray-800">这个工程上次没有跑完</h3>
                <p className="mt-2 text-xs leading-relaxed text-gray-500">
                  {resumePrompt.note}。已完成的阶段都保存在服务器上。
                </p>
                <div className="mt-4 flex flex-col gap-2">
                  <button
                    onClick={handleResumeCancelled}
                    className="w-full rounded-xl bg-claude-accent px-4 py-2.5 text-sm font-bold text-white transition hover:bg-claude-accentHover cursor-pointer"
                  >
                    继续跑
                  </button>
                  <button
                    onClick={() => void handleRestartFromScratch()}
                    className="w-full rounded-xl border border-gray-300 px-4 py-2.5 text-sm font-bold text-gray-600 transition hover:border-red-300 hover:text-red-600 cursor-pointer"
                  >
                    从头开始
                  </button>
                  <button
                    onClick={handleDismissResumePrompt}
                    className="w-full rounded-xl px-4 py-2 text-xs text-gray-400 transition hover:text-gray-600 cursor-pointer"
                  >
                    返回主页
                  </button>
                </div>
              </div>
            </div>
          )}

          <main className="flex-grow flex flex-col container mx-auto p-4 lg:p-6 pt-12 lg:pt-14 min-h-0">
            {/*
              工作流状态条。它是"这个工程此刻在做什么"的唯一权威显示 —— 数据
              来自 /status.run（服务器），不是本地推测，所以刷新页面、换标签页
              看到的都一样。有活跃运行时给「取消运行」，被取消停在断点时给「继续」。
            */}
            {(runState?.active || resumeAfterCancel) && (
              <div className="mb-4 bg-white border border-gray-200/80 rounded-xl px-4 py-2 flex items-center justify-between gap-4 shadow-sm">
                <div className="flex items-center gap-3 min-w-0">
                  <div
                    className={`w-2 h-2 rounded-full shrink-0 ${
                      runState?.active ? 'bg-claude-accent animate-pulse' : 'bg-amber-400'
                    }`}
                  />
                  <span className="text-xs font-bold text-gray-700 truncate">
                    {runState?.active
                      ? `正在处理：${RUN_STEP_LABEL[runState.step ?? ''] ?? runState.step ?? ''}${
                          runState.total
                            ? `（${runState.progress ?? 0}/${runState.total}）`
                            : ''
                        }`
                      : (resumeAfterCancel?.note ?? '上次没有跑完')}
                  </span>
                  {runState?.active && (
                    <span className="text-[11px] text-gray-400 tabular-nums shrink-0">
                      {Math.round(runState.elapsed_s)}s
                    </span>
                  )}
                </div>
                {runState?.active ? (
                  <button
                    onClick={() => void handleCancelProcessing()}
                    disabled={isCancelling || runState.cancelling}
                    className="shrink-0 rounded-lg border border-gray-300 px-3 py-1 text-xs font-bold text-gray-600 transition hover:border-red-300 hover:text-red-600 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
                  >
                    {isCancelling || runState.cancelling ? '正在取消…' : '取消运行'}
                  </button>
                ) : (
                  <button
                    onClick={handleResumeCancelled}
                    className="shrink-0 rounded-lg bg-claude-accent px-3 py-1 text-xs font-bold text-white transition hover:bg-claude-accentHover cursor-pointer"
                  >
                    继续处理
                  </button>
                )}
              </div>
            )}

            {isBatchProcessing && batchProgress && (
              <div className="mb-4 bg-claude-accent/10 border border-claude-accent/20 rounded-xl px-4 py-2 flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div className="w-2 h-2 bg-claude-accent rounded-full animate-pulse"></div>
                  <span className="text-xs font-bold uppercase tracking-wider text-claude-accent">{batchProgress}</span>
                </div>
                <div className="h-1 bg-claude-accent/20 flex-grow mx-8 rounded-full overflow-hidden">
                  <div className="h-full bg-claude-accent" style={{ width: '60%' }}></div>
                </div>
              </div>
            )}

            <div className="flex-grow grid grid-cols-1 lg:grid-cols-12 gap-6 min-h-0 items-stretch">
              <div className="w-full lg:col-span-7 flex flex-col gap-4 min-h-0">
                {/*
                  NOTE: no `overflow-hidden` here. It existed only to clip the
                  video to the frame's rounded corners, and it would also clip
                  the style tab below, which has to hang past the left edge. The
                  video clips ITSELF instead (`rounded-2xl` on the element), so
                  the frame still has no square corners.

                  Nothing else needs clipping: the subtitle overlay is sized to
                  the video's rendered rect and wraps inside it.
                */}
                <div
                  ref={videoBoxRef}
                  className="flex-grow bg-black rounded-2xl shadow-xl border border-gray-800 relative min-h-[300px]"
                >
                  {videoUrl && (
                    <video
                      ref={videoRef}
                      src={videoUrl}
                      controls
                      muted={!!backgroundAudioUrl}
                      // Clips its own corners, replacing the `overflow-hidden`
                      // the frame used to carry. See the note on the frame.
                      className="w-full h-full object-contain rounded-2xl"
                      onTimeUpdate={handleTimeUpdate}
                      onLoadedMetadata={(e) => {
                        setDuration(e.currentTarget.duration);
                        // The intrinsic size is only known now, and that is
                        // what the overlay positions against.
                        measureVideoRect();
                      }}
                      onPause={() => {
                        activeAudiosRef.current.forEach(a => a.pause());
                        backgroundAudioRef.current?.pause();
                      }}
                      onPlay={() => {
                        backgroundAudioRef.current?.play().catch(() => {});
                      }}
                      onSeeked={() => {
                        if (backgroundAudioRef.current && videoRef.current) {
                          backgroundAudioRef.current.currentTime = videoRef.current.currentTime;
                        }
                      }}
                    />
                  )}

                  {/* Hidden background audio element */}
                  {backgroundAudioUrl && (
                    <audio
                      ref={backgroundAudioRef}
                      src={backgroundAudioUrl}
                      preload="auto"
                      className="hidden"
                    />
                  )}

                  {/* Subtitle Overlay.
                      Style-driven and positioned on the video's rendered rect
                      rather than the container, so it lands on the picture and
                      not in the letterbox bars. Every size comes from the same
                      percentages the ASS file and the burn-in use. */}
                  {videoRect && overlayCue && (
                    <div style={overlayContainerStyle(subtitleStyle, videoRect)}>
                      {overlayCue.lines.map((line, index) => (
                        <div key={index} style={overlayLineStyle(subtitleStyle, videoRect)}>
                          {line}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Style tab, hanging on the OUTSIDE of the frame's left edge.
                      It used to sit INside, over the picture, as a dark
                      translucent pill — that styling was there to stay legible
                      on top of whatever footage was underneath, which also meant
                      it covered part of the very frame it is used to judge.

                      `-translate-x-full` puts its right edge on the frame edge
                      and its body in the page gutter; `rounded-l-lg` so the
                      rounded side faces outward, like a tab on a book cover.
                      The colours are light now because it sits on the page, not
                      on the video, so a translucent dark pill would be wrong. */}
                  <button
                    type="button"
                    onClick={() => setShowStylePanel(v => !v)}
                    title="调整字幕的字体、大小、颜色和位置，画面上的字幕会实时跟着变"
                    style={{ writingMode: 'vertical-rl' }}
                    className={[
                      'absolute left-0 top-1/2 -translate-y-1/2 -translate-x-full z-20',
                      'px-1 py-3 rounded-l-lg border transition-colors cursor-pointer',
                      'text-[10px] font-bold tracking-[0.25em]',
                      showStylePanel
                        ? 'bg-claude-accent text-white border-claude-accent shadow-md shadow-claude-accent/25'
                        : 'bg-white text-claude-accent border-claude-accent shadow-sm hover:bg-claude-accent/10',
                    ].join(' ')}
                  >
                    字幕样式
                  </button>

                  {/* A failed cue fetch is surfaced here instead of being masked
                      by a local re-render: the last good list stays on screen,
                      and the user is told the preview may be stale. */}
                  {subtitleError && (
                    <div className="absolute left-8 bottom-3 z-20 flex items-center gap-2 px-2.5 py-1.5 rounded-lg bg-red-500/90 text-white text-[10px] font-semibold shadow-lg">
                      <span>字幕加载失败，画面上的字幕可能不是最新的</span>
                      <button
                        type="button"
                        onClick={() => {
                          setSubtitleError('');
                          setSubtitleCues([]);
                        }}
                        className="underline decoration-white/60 hover:decoration-white cursor-pointer"
                        title={subtitleError}
                      >
                        重试
                      </button>
                    </div>
                  )}
                </div>

                <div className="flex-shrink-0">
                  <Timeline
                    segments={segments}
                    speakers={speakers}
                    duration={duration}
                    currentTime={currentTime}
                    onSeek={handleSeek}
                    waveform={waveform}
                    isLoading={isAudioLoading}
                  />
                </div>
              </div>

              <div className="w-full lg:col-span-5 flex flex-col min-h-0">
                {/* The right column hosts two views. The style editor lives here
                    rather than floating over the video: the picture keeps its
                    full size and nothing is cropped, which is what makes the
                    live preview trustworthy. The pill on the video switches
                    between them — no second set of tabs saying the same thing.
                    Both views stay MOUNTED and are toggled with `hidden`.
                    The script editor holds per-segment edit state and its own
                    scroll position; unmounting it to show the style panel would
                    silently discard whatever the user had half-typed. */}
                <div
                  className={
                    showStylePanel ? 'hidden' : 'flex-grow flex flex-col min-h-0'
                  }
                >
                  <TranscriptionPanel
                    segments={segments}
                    speakers={speakers}
                    isTranscribing={isTranscribing}
                    onSegmentUpdate={handleSegmentUpdate}
                    onSynthesize={handleSynthesizeSegment}
                    onRefit={handleRefitSegment}
                    onToggleMute={handleToggleMute}
                    onToggleHide={handleToggleHide}
                    onRetranslate={handleRetranslateLine}
                    currentTime={currentTime}
                    onSeek={handleSeek}
                    clonedVoices={clonedVoices}
                    onPreviewVoice={handlePreviewVoice}
                    previewingSpeaker={previewingSpeaker}
                    voices={voiceOptions}
                    pinnedVoices={pinnedVoices}
                    onPickVoice={handlePickVoice}
                    voicesNeedCloning={voicesNeedCloning}
                  />
                </div>

                <div
                  className={
                    showStylePanel ? 'flex-grow flex flex-col min-h-0' : 'hidden'
                  }
                >
                  {/*
                    No `overflow-y-auto` here: the panel is now the same card as
                    TranscriptionPanel and owns its own header + scroll area.
                    Scrolling it from outside would scroll the header away with
                    the content.
                  */}
                  <SubtitleStylePanel
                    style={subtitleStyle}
                    onStyleChange={handleSubtitleStyleChange}
                    onClose={() => setShowStylePanel(false)}
                  />
                </div>
              </div>
            </div>
          </main>
        </div>
      )}
    </>
  );
};

export default App;
