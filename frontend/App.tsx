
import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { TranscriptionSegment, Speaker } from './types';
import Header from './components/Header';
import VideoUpload from './components/VideoUpload';
import Timeline from './components/Timeline';
import SettingsModal from './components/SettingsModal';
import StreamingLog from './components/StreamingLog';
import { TranscriptionPanel } from './components/TranscriptionPanel';
import {
  uploadVideo,
  translateScript,
  synthesizeSpeech,
  processVideo,
  getVideoStatus,
  resetVideo,
  getVoiceCloneStatus,
  generateVoicePreview,
  exportVideo,
  getExportDownloadUrl,
  getSeparatorInfo,
  getSeparatorToken,
  uploadStems,
  type SeparatorInfo,
  type SeparationMode,
  type SeparationBackends,
} from './services/apiService';
import { getAudioWaveform, getAudioWaveformFromUrl } from './utils/audioProcessor';
import { decodeAudio, separateAndUpload, isModelCached } from './utils/mdx/separatorClient';
import { computeSyncRates, setPreservesPitch } from './utils/playbackSync';

/**
 * Replace a trailing, in-progress log line (identified by `prefix`) instead of
 * appending a new one, so repeated progress updates do not flood the log.
 */
function withTrailingLine(prev: string, prefix: string, text: string): string {
  const lines = prev.split('\n');
  const last = lines.length - 1;
  if (last >= 0 && lines[last].startsWith(prefix)) {
    lines[last] = text;
  } else {
    lines.push(text);
  }
  return lines.join('\n');
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

function detectBrowserLanguage(): string {
  const lang = (navigator.language || '').toLowerCase();
  if (lang.startsWith('zh')) return 'Chinese';
  if (lang.startsWith('ja')) return 'Japanese';
  if (lang.startsWith('ko')) return 'Korean';
  if (lang.startsWith('fr')) return 'French';
  if (lang.startsWith('de')) return 'German';
  if (lang.startsWith('es')) return 'Spanish';
  return 'English';
}

const App: React.FC = () => {
  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [videoId, setVideoId] = useState<string>('');
  const [waveform, setWaveform] = useState<number[]>([]);
  const [duration, setDuration] = useState<number>(0);
  const [currentTime, setCurrentTime] = useState<number>(0);
  const [segments, setSegments] = useState<TranscriptionSegment[]>([]);
  const [speakers, setSpeakers] = useState<Speaker[]>([]);
  const [isTranscribing, setIsTranscribing] = useState<boolean>(false);
  const [isAudioLoading, setIsAudioLoading] = useState<boolean>(false);

  // Batch processing state
  const [isBatchProcessing, setIsBatchProcessing] = useState<boolean>(false);
  const [batchProgress, setBatchProgress] = useState<string>('');

  // Voice clone state
  const [clonedVoices, setClonedVoices] = useState<Record<string, string>>({});
  const [previewingSpeaker, setPreviewingSpeaker] = useState<string>('');

  // Settings State
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [targetLanguage, setTargetLanguage] = useState<string>(detectBrowserLanguage);
  const [enableVoiceClone, setEnableVoiceClone] = useState(false);

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

  // Export (mux the dubbed audio back into a downloadable MP4)
  const [exportUrl, setExportUrl] = useState<string | null>(null);
  const [isExporting, setIsExporting] = useState(false);
  const [exportError, setExportError] = useState<string>('');

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

  // Session Recovery
  useEffect(() => {
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
          const isIncomplete = !isCompleted && !isError && !isUploaded;

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
          if (data.export_url) setExportUrl(data.export_url);

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

          // If error or incomplete — show log and auto-retry
          if (isError || isIncomplete || isUploaded) {
            const reason = isError
              ? `Previous processing failed: ${data.error || 'Unknown error'}`
              : isUploaded
              ? 'Processing has not started yet'
              : `Processing was interrupted at: ${data.status}`;

            setRawLog(`Session recovered for: ${idFromUrl}\n${reason}\n\nAuto-retrying pipeline...\n`);
            setIsLogOpen(true);

            // Auto-retry pipeline. Goes through `startPipeline` so browser
            // separation still happens on this route (there is no File in
            // memory here, so it pulls the audio from the server), and passes
            // the restored backend explicitly.
            startPipeline(
              idFromUrl,
              undefined,
              restoredMode,
              Boolean(data.has_background)
            ).finally(() => {
              setIsTranscribing(false);
            });
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
    setExportUrl(null);
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
            setRawLog(prev => prev + `[${event.progress}/${event.total}] ${event.speaker_id}: ${event.voice_id}\n`);
          } else if (event.status === 'failed') {
            setRawLog(prev => prev + `[${event.progress}/${event.total}] ${event.speaker_id}: FAILED (${event.error})\n`);
          } else if (event.status === 'complete') {
            setRawLog(prev => prev + 'Voice cloning complete.\n');
          }
        }

        // Export events
        if (event.phase === 'export') {
          if (event.status === 'started') {
            setIsExporting(true);
            setExportError('');
            setRawLog(prev => prev + '\n--- Exporting Video (video stream copied) ---\n');
          } else if (event.status === 'mixing' && event.total) {
            const pct = Math.round((event.current / event.total) * 100);
            setRawLog(prev => {
              const lines = prev.split('\n');
              const lastIdx = lines.length - 1;
              if (lines[lastIdx].startsWith('Export progress:')) {
                lines[lastIdx] = `Export progress: ${pct}%`;
              } else {
                lines.push(`Export progress: ${pct}%`);
              }
              return lines.join('\n');
            });
          } else if (event.status === 'muxing') {
            setRawLog(prev => prev + 'Muxing dubbed audio into the video...\n');
          } else if (event.status === 'done') {
            if (event.url) setExportUrl(event.url);
            setRawLog(prev => prev + `Export complete${event.size_mb ? ` (${event.size_mb} MB)` : ''}.\n`);
          } else if (event.status === 'failed') {
            setExportError(event.error || 'Export failed');
            setRawLog(prev => prev + `Export failed: ${event.error}\n`);
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

        // Final done
        if (event.done) {
          setIsExporting(false);
          if (event.export_url) setExportUrl(event.export_url);
          setRawLog(prev => prev + '\n=== All Processing Complete ===\nClosing in 2 seconds...');
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
        exportVideo: true,
      });

      await new Promise(resolve => setTimeout(resolve, 2000));
      setIsLogOpen(false);

    } catch (err) {
      console.error(err);
      setRawLog(prev => prev + `\nERROR: ${err instanceof Error ? err.message : 'Unknown error'}\n`);
    } finally {
      setIsTranscribing(false);
    }
  }, [targetLanguage, enableVoiceClone, separationMode]);

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
    ): Promise<void> => {
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
    [separationMode, runClientSeparation, runPipeline]
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
        setVideoFile(null);
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
            if (data.export_url) setExportUrl(data.export_url);

            if (data.segments && data.segments.length > 0) {
              const recoveredSegments: TranscriptionSegment[] = data.segments.map((seg: any) => ({
                id: seg.id,
                speakerId: seg.speaker_label,
                startTime: seg.start_time,
                endTime: seg.end_time,
                originalText: seg.text,
                translatedText: seg.translated_text || '',
                audioUrl: seg.audio_url || undefined,
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

            if (isError || isUploaded || (!isCompleted && !isError && data.status !== 'uploaded')) {
              const reason = isError
                ? `Previous processing failed: ${data.error || 'Unknown error'}`
                : isUploaded
                ? 'Processing has not started yet'
                : `Processing was interrupted at: ${data.status}`;
              setRawLog(`Session recovered for: ${idFromUrl}\n${reason}\n\nAuto-retrying pipeline...\n`);
              setIsLogOpen(true);
              startPipeline(
                idFromUrl,
                undefined,
                restoredMode,
                Boolean(data.has_background)
              ).finally(() => setIsTranscribing(false));
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
      setRawLog(prev => prev + `Upload complete. Video ID: ${uploadResult.video_id}\n`);

      if (uploadResult.exists) {
        // Video already exists — ask user what to do
        setIsLogOpen(false);
        setIsTranscribing(false);

        const statusData = await getVideoStatus(uploadResult.video_id);

        // Restore settings from server
        setSeparationBackends(statusData.separation_backends ?? {});
        setSeparationMode(
          restoreSeparationMode(statusData, statusData.separation_backends)
        );
        if (statusData.enable_voice_clone !== undefined) setEnableVoiceClone(statusData.enable_voice_clone);
        setExportUrl(statusData.export_url || null);

        const isCompleted = statusData.status === 'completed';
        const isError = statusData.status === 'error';
        const isInProgress = !isCompleted && !isError && statusData.status !== 'uploaded';

        let message = 'This video has been uploaded before.\n\n';
        if (isCompleted) {
          message += 'Processing is fully completed.\n\n';
        } else if (isError) {
          message += `Previous processing failed: ${statusData.error || 'Unknown error'}\n\n`;
        } else if (isInProgress) {
          message += `Processing was interrupted at stage: ${statusData.status}\n\n`;
        }
        message += 'Choose an action:\n• OK = Continue / Retry from where it stopped\n• Cancel = Reset and start over';

        const continueExisting = window.confirm(message);

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
            Boolean(statusData.has_background)
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
          await startPipeline(uploadResult.video_id, file);
        }
      } else {
        // New video — run the full pipeline
        await startPipeline(uploadResult.video_id, file);
      }

    } catch (err) {
      console.error(err);
      setRawLog(prev => prev + `\nERROR: ${err instanceof Error ? err.message : 'Unknown error'}\n`);
      alert("Processing encountered an error. Please check the server logs.");
    } finally {
      setIsTranscribing(false);
      setIsAudioLoading(false);
    }
  }, [targetLanguage, runPipeline, startPipeline]);

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
  }, [videoId, segments, targetLanguage]);

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
   * Server-side export: mux the dubbed audio into the video and expose a
   * download URL. The video stream is copied, so this is fast even without a
   * GPU. `silent` suppresses the extra log lines used by the reprocess flow.
   *
   * Declared before handleReprocess because that callback depends on it.
   */
  const handleExport = useCallback(async (silent = false) => {
    if (!videoId) return null;
    if (!silent) {
      setIsLogOpen(true);
      setRawLog(prev => prev + '\n--- Exporting Video (video stream copied) ---\n');
    }
    setIsExporting(true);
    setExportError('');

    try {
      const result = await exportVideo(videoId);
      setExportUrl(result.url);
      setRawLog(prev =>
        prev + `Export complete${result.size_mb ? ` (${result.size_mb} MB)` : ''}.\n`
      );
      return result.url;
    } catch (e) {
      const message = e instanceof Error ? e.message : 'Export failed';
      setExportError(message);
      setRawLog(prev => prev + `Export failed: ${message}\n`);
      return null;
    } finally {
      setIsExporting(false);
    }
  }, [videoId]);

  const handleDownloadExport = useCallback(() => {
    if (!videoId) return;
    // Use the backend download route so the browser saves the file.
    const url = exportUrl || getExportDownloadUrl(videoId);
    const a = document.createElement('a');
    a.href = url;
    a.download = `translated_${videoId.slice(0, 8)}.mp4`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }, [videoId, exportUrl]);

  const handleReprocess = useCallback(async () => {
    if (!videoId || segments.length === 0) return;

    const confirmReprocess = window.confirm(
      'This will re-translate the entire script and re-generate all audio. Continue?'
    );
    if (!confirmReprocess) return;

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
      const results = await translateScript(videoId, contextPayload, targetLanguage);

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

      // Phase 3: Re-export the video with the new audio
      setRawLog(prev => prev + '\n=== Exporting Video ===\n');
      setBatchProgress('Exporting...');
      await handleExport(true);

      setRawLog(prev => prev + '\n=== All Processing Complete ===\nClosing in 1.5 seconds...');
      await new Promise(resolve => setTimeout(resolve, 1500));
      setIsLogOpen(false);
    } catch (error) {
      console.error("Reprocess failed:", error);
      setRawLog(prev => prev + `\n\nERROR: ${error instanceof Error ? error.message : 'Unknown error'}`);
    } finally {
      setBatchProgress('');
      setIsBatchProcessing(false);
    }
  }, [segments, videoId, targetLanguage, handleSynthesizeSegment, handleExport]);

  // Current subtitle based on video time
  const currentSubtitle = useMemo(() => {
    if (segments.length === 0) return null;
    const seg = segments.find(s => currentTime >= s.startTime && currentTime < s.endTime);
    if (!seg) return null;
    return {
      translated: seg.translatedText,
      original: seg.originalText,
    };
  }, [segments, currentTime]);

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
      />

      {isIndexPage ? (
        <VideoUpload
          onVideoSelect={handleVideoSelect}
          isLoading={isTranscribing || isSeparating}
          targetLanguage={targetLanguage}
          onLanguageChange={setTargetLanguage}
          enableVoiceClone={enableVoiceClone}
          onVoiceCloneChange={handleVoiceCloneChange}
          separationMode={separationMode}
          onSeparationModeChange={handleSeparationModeChange}
          separationBackends={effectiveBackends}
          bgmSeparationLocked={enableVoiceClone}
        />
      ) : (
        <div className="h-screen flex flex-col bg-claude-bg text-claude-text font-sans selection:bg-claude-accent/20 overflow-hidden">
          <Header
            onOpenSettings={() => setIsSettingsOpen(true)}
            onReprocess={handleReprocess}
            targetLanguage={targetLanguage}
            onLanguageChange={setTargetLanguage}
            isProcessing={isBatchProcessing}
            hasSegments={segments.length > 0}
            onExport={() => handleExport(false)}
            onDownloadExport={handleDownloadExport}
            isExporting={isExporting}
            hasExport={!!exportUrl}
            exportError={exportError}
          />

          <main className="flex-grow flex flex-col container mx-auto p-4 lg:p-6 pt-12 lg:pt-14 min-h-0">
            {isBatchProcessing && batchProgress && (
              <div className="mb-4 bg-claude-accent/10 border border-claude-accent/20 rounded-xl px-4 py-2 flex items-center justify-between animate-in slide-in-from-top-2 duration-300">
                <div className="flex items-center gap-3">
                  <div className="w-2 h-2 bg-claude-accent rounded-full animate-pulse"></div>
                  <span className="text-xs font-bold uppercase tracking-wider text-claude-accent">{batchProgress}</span>
                </div>
                <div className="h-1 bg-claude-accent/20 flex-grow mx-8 rounded-full overflow-hidden">
                  <div className="h-full bg-claude-accent animate-progress" style={{ width: '60%' }}></div>
                </div>
              </div>
            )}

            <div className="flex-grow grid grid-cols-1 lg:grid-cols-12 gap-6 min-h-0 items-stretch">
              <div className="w-full lg:col-span-7 flex flex-col gap-4 min-h-0">
                <div className="flex-grow bg-black rounded-2xl overflow-hidden shadow-xl border border-gray-800 relative min-h-[300px]">
                  {videoUrl && (
                    <video
                      ref={videoRef}
                      src={videoUrl}
                      controls
                      muted={!!backgroundAudioUrl}
                      className="w-full h-full object-contain"
                      onTimeUpdate={handleTimeUpdate}
                      onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
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

                  {/* Subtitle Overlay */}
                  {currentSubtitle && (
                    <div className="absolute bottom-10 left-0 right-0 flex flex-col items-center pointer-events-none px-4 z-10">
                      {currentSubtitle.translated && (
                        <div className="bg-black/80 text-white text-base font-bold px-5 py-2 rounded-lg max-w-[90%] text-center leading-relaxed shadow-lg backdrop-blur-sm">
                          {currentSubtitle.translated}
                        </div>
                      )}
                      {currentSubtitle.original && (
                        <div className="bg-black/60 text-gray-300 text-xs px-4 py-1 rounded-md max-w-[85%] text-center leading-relaxed mt-1">
                          {currentSubtitle.original}
                        </div>
                      )}
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
                <TranscriptionPanel
                  segments={segments}
                  speakers={speakers}
                  isTranscribing={isTranscribing}
                  onSegmentUpdate={handleSegmentUpdate}
                  onSynthesize={handleSynthesizeSegment}
                  onRefit={handleRefitSegment}
                  currentTime={currentTime}
                  onSeek={handleSeek}
                  clonedVoices={clonedVoices}
                  onPreviewVoice={handlePreviewVoice}
                  previewingSpeaker={previewingSpeaker}
                />
              </div>
            </div>
          </main>
        </div>
      )}
    </>
  );
};

export default App;
