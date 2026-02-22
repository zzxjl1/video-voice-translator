
import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { TranscriptionSegment, Speaker } from './types';
import Header from './components/Header';
import VideoUpload from './components/VideoUpload';
import Timeline from './components/Timeline';
import SettingsModal from './components/SettingsModal';
import StreamingLog from './components/StreamingLog';
import { TranscriptionPanel } from './components/TranscriptionPanel';
import { uploadVideo, transcribeVideo, translateScript, synthesizeSpeech, separateAudio } from './services/apiService';
import { getAudioWaveform } from './utils/audioProcessor';

const LANGUAGES = [
  'English',
  'Chinese',
  'Japanese',
  'Korean',
  'French',
  'German',
  'Spanish',
];

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

  // Settings State
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [targetLanguage, setTargetLanguage] = useState<string>(detectBrowserLanguage);

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
  useEffect(() => {
    // If we have a local file, it's our source
    if (videoFile) {
      const url = URL.createObjectURL(videoFile);
      setVideoUrl(url);
      console.log("Created stable local video URL:", url);
      return () => {
        URL.revokeObjectURL(url);
        console.log("Revoked local video URL:", url);
      };
    }
    // If no local file but we have an ID (session recovery), use the server URL
    else if (videoId) {
      const serverUrl = `/api/videos/${videoId}/video`;
      setVideoUrl(serverUrl);
    } else {
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
      // Set background audio URL for recovered sessions
      setBackgroundAudioUrl(`/api/videos/${idFromUrl}/audio/background`);

      import('./services/apiService').then(({ getVideoStatus }) => {
        setIsTranscribing(true);
        getVideoStatus(idFromUrl)
          .then(data => {
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
          })
          .catch(err => console.error("Session recovery failed:", err))
          .finally(() => setIsTranscribing(false));
      });
    }
  }, []);

  // Update URL on videoId change
  useEffect(() => {
    if (videoId && !window.location.pathname.includes(videoId)) {
      window.history.pushState({}, '', `/${videoId}`);
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

  const handleVideoSelect = useCallback(async (file: File) => {
    setVideoFile(file);
    setIsAudioLoading(true);
    setIsTranscribing(true);
    setSegments([]);
    setSpeakers([]);
    setRawLog('');
    setIsLogOpen(true);

    try {
      getAudioWaveform(file, 300).then(peaks => {
        setWaveform(peaks);
        setIsAudioLoading(false);
      });

      setRawLog(prev => prev + 'Uploading video to server...\n');
      const uploadResult = await uploadVideo(file);
      setVideoId(uploadResult.video_id);
      setRawLog(prev => prev + `Upload complete. Video ID: ${uploadResult.video_id}\n`);

      // === Phase 0: Vocal Separation ===
      setRawLog(prev => prev + '\n--- Starting Vocal Separation ---\n');
      setRawLog(prev => prev + 'Separating vocals from background audio (this may take a few minutes on CPU)...\n');
      try {
        const sepResult = await separateAudio(uploadResult.video_id);
        setBackgroundAudioUrl(sepResult.background_url);
        setRawLog(prev => prev + 'Vocal separation complete.\n');
      } catch (sepErr) {
        setRawLog(prev => prev + `Vocal separation failed: ${sepErr instanceof Error ? sepErr.message : 'Unknown error'}\n`);
        setRawLog(prev => prev + 'Continuing without separation (will use original audio)...\n');
      }
      
      // === Phase 1: ASR ===
      setRawLog(prev => prev + '\nStarting Ali ASR transcription...\n');
      const segmentData = await transcribeVideo(uploadResult.video_id);
      setRawLog(prev => prev + `Transcription complete. Found ${segmentData.length} segments.\n`);

      const uniqueLabels = Array.from(new Set(segmentData.map(seg => seg.speaker_label)));
      setSpeakers(uniqueLabels.map(label => ({ id: label, name: label })));

      const newSegments: TranscriptionSegment[] = segmentData.map(seg => {
        return {
          id: seg.id,
          speakerId: seg.speaker_label,
          startTime: seg.start_time,
          endTime: seg.end_time,
          originalText: seg.text,
          translatedText: seg.translated_text || '',
          audioUrl: (seg as any).audio_url || undefined,
          status: 'pending' as const,
        };
      });

      setSpeakers(uniqueLabels.map(label => ({ id: label, name: label })));
      setSegments(newSegments);

      setRawLog(prev => prev + '\n--- Transcription Ready ---\n');

      // === Phase 2: Auto Translate ===
      setRawLog(prev => prev + `\n--- Starting Translation (${targetLanguage}) ---\n`);
      setRawLog(prev => prev + `Sending ${newSegments.length} segments with full context...\n`);

      const contextPayload = newSegments.map(s => ({
        id: s.id,
        text: s.originalText,
        speaker_id: s.speakerId,
        start_time: s.startTime,
      }));

      const translationResults = await translateScript(uploadResult.video_id, contextPayload, targetLanguage);

      const translatedSegments = newSegments.map(seg => {
        const match = translationResults.find(r => r.id === seg.id);
        return match ? { ...seg, translatedText: match.translated_text } : seg;
      });
      setSegments(translatedSegments);

      setRawLog(prev => prev + `Translation complete. ${translationResults.length} segments translated.\n`);

      // === Phase 3: Auto TTS ===
      const withTranslation = translatedSegments.filter(s => s.translatedText);
      if (withTranslation.length > 0) {
        setRawLog(prev => prev + `\n--- Starting Audio Synthesis (${withTranslation.length} segments) ---\n`);

        let ttsCount = 0;
        for (const seg of withTranslation) {
          ttsCount++;
          setRawLog(prev => prev + `[${ttsCount}/${withTranslation.length}] Segment ${seg.id}: Synthesizing...`);

          try {
            const result = await synthesizeSpeech(uploadResult.video_id, seg.id, seg.translatedText);
            const audioUrl = result.audio_url;

            setSegments(prev => prev.map(s => s.id === seg.id ? { ...s, audioUrl } : s));
            setRawLog(prev => prev + ` Done.\n`);
          } catch (e) {
            console.error(`TTS failed for segment ${seg.id}:`, e);
            setRawLog(prev => prev + ` FAILED.\n`);
          }

          await new Promise(r => setTimeout(r, 200));
        }

        setRawLog(prev => prev + `\n--- Audio Synthesis Complete ---\n`);
      }

      setRawLog(prev => prev + '\n=== All Processing Complete ===\nClosing in 2 seconds...');
      await new Promise(resolve => setTimeout(resolve, 2000));
      setIsLogOpen(false);

    } catch (err) {
      console.error(err);
      setRawLog(prev => prev + `\nERROR: ${err instanceof Error ? err.message : 'Unknown error'}\n`);
      alert("Processing encountered an error. Please check the server logs.");
    } finally {
      setIsTranscribing(false);
    }
  }, [targetLanguage]);

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

  const handleBatchTranslate = useCallback(async () => {
    if (!videoId || segments.length === 0) return;

    // If all segments already have translations, ask user to confirm re-translation
    const untranslated = segments.filter(s => !s.translatedText);
    const toProcess = untranslated.length > 0 ? untranslated : segments;

    if (untranslated.length === 0) {
      const confirmRetranslate = window.confirm(
        'All segments are already translated. Do you want to re-translate the entire script?'
      );
      if (!confirmRetranslate) return;
    }

    setIsBatchProcessing(true);
    setBatchProgress('Starting batch translation...');
    setIsLogOpen(true);
    setRawLog('Initializing Translation for entire script...\n');

    try {
      const contextPayload = toProcess.map(s => ({
        id: s.id,
        text: s.originalText,
        speaker_id: s.speakerId,
        start_time: s.startTime,
      }));

      setRawLog(prev => prev + `\n--- Sending ${toProcess.length} segments with full context ---\n`);

      const results = await translateScript(videoId, contextPayload, targetLanguage);

      setSegments(prev => prev.map(seg => {
        const match = results.find(r => r.id === seg.id);
        if (match) {
          return { ...seg, translatedText: match.translated_text };
        }
        return seg;
      }));

      setRawLog(prev => prev + '\n\n--- Translation Complete ---\nClosing in 1.5 seconds...');
      await new Promise(resolve => setTimeout(resolve, 1500));
      setIsLogOpen(false);

    } catch (error) {
      console.error("Batch translation failed:", error);
      setRawLog(prev => prev + `\n\nERROR: ${error instanceof Error ? error.message : 'Unknown error'}`);
      await new Promise(resolve => setTimeout(resolve, 5000));
      alert("Batch translation failed. Check the log for details.");
    } finally {
      setBatchProgress('');
      setIsBatchProcessing(false);
    }
  }, [segments, videoId, targetLanguage]);

  const handleBatchTTS = useCallback(async () => {
    if (!videoId) return;

    const withTranslation = segments.filter(s => s.translatedText);
    if (withTranslation.length === 0) {
      alert('No translated segments found. Please translate first.');
      return;
    }

    const unsynced = withTranslation.filter(s => !s.audioUrl);
    let toProcess = unsynced;

    if (unsynced.length === 0) {
      const confirmRegenerate = window.confirm(
        'All segments already have audio. Do you want to re-generate audio for the entire script?'
      );
      if (!confirmRegenerate) return;
      toProcess = withTranslation;
    }

    setIsBatchProcessing(true);
    setIsLogOpen(true);
    setRawLog('Initializing Audio Synthesis...\n');
    let count = 0;

    try {
      for (const seg of toProcess) {
        count++;
        const label = `[${count}/${toProcess.length}] Segment ${seg.id}`;
        setRawLog(prev => prev + `\n${label}: Synthesizing...`);
        setBatchProgress(`Synthesizing ${count}/${toProcess.length}`);
        await handleSynthesizeSegment(seg.id);
        setRawLog(prev => prev + ` Done.`);
        await new Promise(r => setTimeout(r, 300));
      }

      setRawLog(prev => prev + '\n\n--- Audio Synthesis Complete ---\nClosing in 1.5 seconds...');
      await new Promise(resolve => setTimeout(resolve, 1500));
      setIsLogOpen(false);
    } catch (error) {
      console.error("Batch TTS failed:", error);
      setRawLog(prev => prev + `\n\nERROR: ${error instanceof Error ? error.message : 'Unknown error'}`);
    } finally {
      setBatchProgress('');
      setIsBatchProcessing(false);
    }
  }, [segments, videoId, handleSynthesizeSegment]);

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
      />

      <StreamingLog
        isOpen={isLogOpen}
        logs={rawLog}
        onClose={() => setIsLogOpen(false)}
        title="Processing Log"
      />

      {isIndexPage ? (
        <VideoUpload
          onVideoSelect={handleVideoSelect}
          isLoading={isTranscribing}
          targetLanguage={targetLanguage}
          onLanguageChange={setTargetLanguage}
        />
      ) : (
        <div className="h-screen flex flex-col bg-claude-bg text-claude-text font-sans selection:bg-claude-accent/20 overflow-hidden">
          <Header
            onOpenSettings={() => setIsSettingsOpen(true)}
            onBatchTranslate={handleBatchTranslate}
            onBatchTTS={handleBatchTTS}
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
