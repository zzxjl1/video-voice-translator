
import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { TranscriptionSegment, Speaker } from './types';
import Header from './components/Header';
import VideoUpload from './components/VideoUpload';
import Timeline from './components/Timeline';
import SettingsModal from './components/SettingsModal';
import StreamingLog from './components/StreamingLog';
import { TranscriptionPanel } from './components/TranscriptionPanel';
import { uploadVideo, translateScript, synthesizeSpeech, processVideo, getVideoStatus, resetVideo, getVoiceCloneStatus, generateVoicePreview } from './services/apiService';
import { getAudioWaveform, getAudioWaveformFromUrl } from './utils/audioProcessor';

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
  const [enableBgmSeparation, setEnableBgmSeparation] = useState(true);

  const handleVoiceCloneChange = (v: boolean) => {
    setEnableVoiceClone(v);
    if (v) setEnableBgmSeparation(true);
  };
  const handleBgmSeparationChange = (v: boolean) => {
    if (enableVoiceClone) return;
    setEnableBgmSeparation(v);
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

          // Restore switch settings from server
          if (data.enable_bgm_separation !== undefined) setEnableBgmSeparation(data.enable_bgm_separation);
          if (data.enable_voice_clone !== undefined) setEnableVoiceClone(data.enable_voice_clone);

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

            // Auto-retry pipeline
            runPipeline(idFromUrl).finally(() => {
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

          // Calculate desired speed factor
          let audioRate = 1.0;
          if (seg.actualDuration) {
            const targetDuration = seg.endTime - seg.startTime;
            const idealFactor = seg.actualDuration / targetDuration;

            // Audio takes the first hit (clamped 0.75x - 1.5x)
            audioRate = Math.min(Math.max(idealFactor, 0.75), 1.5);

            // Video takes the rest (clamped 0.8x - 1.5x)
            const remainingFactor = audioRate / idealFactor;
            targetVideoRate = Math.min(Math.max(remainingFactor, 0.8), 1.5);
          }

          if (!existingAudio) {
            try {
              const audio = new Audio(seg.audioUrl);
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

  const runPipeline = useCallback(async (vid: string, file?: File) => {
    setIsTranscribing(true);
    setRawLog('');
    setIsLogOpen(true);

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
          } else if (event.status === 'skipped') {
            if (event.background_url) {
              setBackgroundAudioUrl(event.background_url);
            }
            setRawLog(prev => prev + 'Vocal separation: already done, skipping.\n');
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
          setRawLog(prev => prev + '\n=== All Processing Complete ===\nClosing in 2 seconds...');
        }

        // Error
        if (event.error) {
          setRawLog(prev => prev + `\nERROR: ${event.error}\n`);
        }
      }, { enableBgmSeparation, enableVoiceClone });

      await new Promise(resolve => setTimeout(resolve, 2000));
      setIsLogOpen(false);

    } catch (err) {
      console.error(err);
      setRawLog(prev => prev + `\nERROR: ${err instanceof Error ? err.message : 'Unknown error'}\n`);
    } finally {
      setIsTranscribing(false);
    }
  }, [targetLanguage, enableBgmSeparation, enableVoiceClone]);

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

            // Restore switch settings from server
            if (data.enable_bgm_separation !== undefined) setEnableBgmSeparation(data.enable_bgm_separation);
            if (data.enable_voice_clone !== undefined) setEnableVoiceClone(data.enable_voice_clone);

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
              runPipeline(idFromUrl).finally(() => setIsTranscribing(false));
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
  }, [videoId, runPipeline]);

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

        // Restore switch settings from server
        if (statusData.enable_bgm_separation !== undefined) setEnableBgmSeparation(statusData.enable_bgm_separation);
        if (statusData.enable_voice_clone !== undefined) setEnableVoiceClone(statusData.enable_voice_clone);

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

          // Resume pipeline
          await runPipeline(uploadResult.video_id, file);
        } else {
          // Reset and re-process
          setRawLog('Resetting video data...\n');
          setIsLogOpen(true);
          setIsTranscribing(true);
          setSegments([]);
          setSpeakers([]);
          await resetVideo(uploadResult.video_id);
          setRawLog(prev => prev + 'Reset complete. Starting fresh...\n');
          await runPipeline(uploadResult.video_id, file);
        }
      } else {
        // New video — run full pipeline
        await runPipeline(uploadResult.video_id, file);
      }

    } catch (err) {
      console.error(err);
      setRawLog(prev => prev + `\nERROR: ${err instanceof Error ? err.message : 'Unknown error'}\n`);
      alert("Processing encountered an error. Please check the server logs.");
    } finally {
      setIsTranscribing(false);
      setIsAudioLoading(false);
    }
  }, [targetLanguage, runPipeline]);

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
      const result = await synthesizeSpeech(videoId, targetSegment.id, text);
      const audioUrl = result.audio_url;
      setSegments(prev => prev.map(s => s.id === id ? { ...s, audioUrl, isSynthesizing: false } : s));
    } catch (e) {
      console.error("Synthesis failed:", e);
    } finally {
      setSegments(prev => prev.map(s => s.id === id ? { ...s, isSynthesizing: false } : s));
    }
  }, [videoId, segments]);

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
  }, [segments, videoId, targetLanguage, handleSynthesizeSegment]);

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
        enableBgmSeparation={enableBgmSeparation}
        onBgmSeparationChange={handleBgmSeparationChange}
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
          isLoading={isTranscribing}
          targetLanguage={targetLanguage}
          onLanguageChange={setTargetLanguage}
          enableVoiceClone={enableVoiceClone}
          onVoiceCloneChange={handleVoiceCloneChange}
          enableBgmSeparation={enableBgmSeparation}
          onBgmSeparationChange={handleBgmSeparationChange}
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
