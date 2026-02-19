
import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { TranscriptionSegment, Speaker } from './types';
import Header from './components/Header';
import VideoUpload from './components/VideoUpload';
import Timeline from './components/Timeline';
import SettingsModal from './components/SettingsModal';
import StreamingLog from './components/StreamingLog';
import { TranscriptionPanel } from './components/TranscriptionPanel';
import { uploadVideo, transcribeVideo, translateScript, synthesizeSpeech } from './services/apiService';
import { getAudioWaveform } from './utils/audioProcessor';

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
  const [targetLanguage, setTargetLanguage] = useState<string>('English');

  // Streaming Log State
  const [isLogOpen, setIsLogOpen] = useState(false);
  const [rawLog, setRawLog] = useState('');

  const videoRef = useRef<HTMLVideoElement>(null);
  const activeAudiosRef = useRef<Map<string, HTMLAudioElement>>(new Map());

  const videoUrl = useMemo(() => (videoFile ? URL.createObjectURL(videoFile) : null), [videoFile]);

  // Handle video URL cleanup
  useEffect(() => {
    return () => {
      if (videoUrl) {
        console.log("Revoking video URL:", videoUrl);
        URL.revokeObjectURL(videoUrl);
      }
    };
  }, [videoUrl]);

  // Handle segment audio URL cleanup (only when segments change)
  useEffect(() => {
    const urlsToRevoke = segments
      .map(seg => seg.audioUrl)
      .filter((url): url is string => !!url && url.startsWith('blob:'));

    return () => {
      urlsToRevoke.forEach(url => {
        URL.revokeObjectURL(url);
      });
    };
  }, [segments]);

  const handleTimeUpdate = () => {
    if (!videoRef.current) return;
    const time = videoRef.current.currentTime;
    setCurrentTime(time);

    let isAnyAudioPlaying = false;

    segments.forEach(seg => {
      if (seg.audioUrl) {
        const shouldBePlaying = time >= seg.startTime && time < seg.endTime;
        const existingAudio = activeAudiosRef.current.get(seg.id);

        if (shouldBePlaying) {
          isAnyAudioPlaying = true;
          if (!existingAudio) {
            try {
              const audio = new Audio(seg.audioUrl);
              audio.currentTime = Math.max(0, time - seg.startTime);
              audio.play().catch(e => console.warn("Audio play blocked:", e));
              activeAudiosRef.current.set(seg.id, audio);
            } catch (e) {
              console.error("Failed to start audio segment:", e);
            }
          }
        } else if (existingAudio) {
          existingAudio.pause();
          activeAudiosRef.current.delete(seg.id);
        }
      }
    });

    if (isAnyAudioPlaying) {
      videoRef.current.volume = 0.1;
    } else {
      videoRef.current.volume = 1.0;
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

      setRawLog(prev => prev + 'Starting Ali ASR transcription...\n');
      const segmentData = await transcribeVideo(uploadResult.video_id);
      setRawLog(prev => prev + `Transcription complete. Found ${segmentData.length} segments.\n`);

      const colors = ['#2563eb', '#16a34a', '#d97706', '#dc2626', '#9333ea', '#0891b2'];
      let speakerMap: Record<string, Speaker> = {};

      const newSegments: TranscriptionSegment[] = segmentData.map(seg => {
        const label = seg.speaker_label;
        if (!speakerMap[label]) {
          const index = Object.keys(speakerMap).length;
          speakerMap[label] = {
            id: label,
            name: label,
            color: colors[index % colors.length],
          };
        }

        return {
          id: seg.id,
          speakerId: label,
          startTime: seg.start_time,
          endTime: seg.end_time,
          originalText: seg.text,
          translatedText: seg.translated_text || '',
          status: 'pending' as const,
        };
      });

      setSpeakers(Object.values(speakerMap));
      setSegments(newSegments);

      setRawLog(prev => prev + '\n--- Transcription Ready ---\n');
      await new Promise(resolve => setTimeout(resolve, 1500));
      setIsLogOpen(false);

    } catch (err) {
      console.error(err);
      setRawLog(prev => prev + `\nERROR: ${err instanceof Error ? err.message : 'Unknown error'}\n`);
      alert("Processing encountered an error. Please check the server logs.");
    } finally {
      setIsTranscribing(false);
    }
  }, []);

  const handleSegmentUpdate = useCallback((id: string, text: string) => {
    setSegments(prev => prev.map(s => s.id === id ? { ...s, translatedText: text } : s));
  }, []);

  const handleTranslateSegmentImpl = async (id: string) => {
    let segmentToTranslate: TranscriptionSegment | undefined;
    setSegments(prev => {
      segmentToTranslate = prev.find(s => s.id === id);
      if (segmentToTranslate) {
        return prev.map(s => s.id === id ? { ...s, isTranslating: true } : s);
      }
      return prev;
    });

    if (!segmentToTranslate || !videoId) return;

    try {
      const results = await translateScript(
        videoId,
        [{
          id: segmentToTranslate.id,
          text: segmentToTranslate.originalText,
          speaker_id: segmentToTranslate.speakerId,
          start_time: segmentToTranslate.startTime,
        }],
        targetLanguage
      );
      if (results.length > 0) {
        setSegments(prev => prev.map(s => s.id === id ? { ...s, translatedText: results[0].translated_text, isTranslating: false } : s));
      }
    } catch (e) {
      console.error("Translation failed:", e);
      setSegments(prev => prev.map(s => s.id === id ? { ...s, isTranslating: false } : s));
    }
  };

  const handleSynthesizeSegment = useCallback(async (id: string) => {
    let targetSegment: TranscriptionSegment | undefined;
    setSegments(prev => {
      targetSegment = prev.find(s => s.id === id);
      return prev.map(s => s.id === id ? { ...s, isSynthesizing: true } : s);
    });

    if (!targetSegment || !videoId) return;

    try {
      const result = await synthesizeSpeech(videoId, targetSegment.translatedText);
      const audioBlob = new Blob(
        [Uint8Array.from(atob(result.audio_base64), c => c.charCodeAt(0))],
        { type: result.content_type }
      );
      const audioUrl = URL.createObjectURL(audioBlob);
      setSegments(prev => prev.map(s => s.id === id ? { ...s, audioUrl, isSynthesizing: false } : s));
    } catch (e) {
      console.error("Synthesis failed:", e);
      setSegments(prev => prev.map(s => s.id === id ? { ...s, isSynthesizing: false } : s));
    }
  }, [videoId]);

  const handleBatchTranslate = useCallback(async () => {
    const toProcess = segments.filter(s => !s.translatedText);
    if (toProcess.length === 0 || !videoId) return;

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
    setIsBatchProcessing(true);
    let count = 0;
    const toProcess = segments.filter(s => s.translatedText && !s.audioUrl);

    for (const seg of toProcess) {
      setBatchProgress(`Synthesizing ${count + 1}/${toProcess.length}`);
      await handleSynthesizeSegment(seg.id);
      count++;
      await new Promise(r => setTimeout(r, 300));
    }
    setBatchProgress('');
    setIsBatchProcessing(false);
  }, [segments, videoId, handleSynthesizeSegment]);

  const handleSeek = useCallback((time: number) => {
    if (videoRef.current) {
      videoRef.current.currentTime = time;
      activeAudiosRef.current.forEach(a => a.pause());
      activeAudiosRef.current.clear();
      videoRef.current.volume = 1.0;
    }
  }, []);

  const handleSpeakerNameChange = useCallback((id: string, newName: string) => {
    setSpeakers(prev => prev.map(s => s.id === id ? { ...s, name: newName } : s));
  }, []);

  return (
    <div className="min-h-screen flex flex-col bg-claude-bg text-claude-text font-sans selection:bg-claude-accent/20">
      <Header onOpenSettings={() => setIsSettingsOpen(true)} />

      <SettingsModal
        isOpen={isSettingsOpen}
        onClose={() => setIsSettingsOpen(false)}
        targetLanguage={targetLanguage}
        onSave={setTargetLanguage}
      />

      <StreamingLog
        isOpen={isLogOpen}
        logs={rawLog}
        onClose={() => setIsLogOpen(false)}
        title="Processing Log"
      />

      <main className="flex-grow container mx-auto p-6 lg:p-10">
        {!videoFile ? (
          <div className="max-w-3xl mx-auto mt-16 animate-in slide-in-from-bottom-8 fade-in duration-700">
            <VideoUpload onVideoSelect={handleVideoSelect} isLoading={isTranscribing} />
          </div>
        ) : (
          <div className="flex flex-col lg:grid lg:grid-cols-12 gap-8 items-start">
            <div className="w-full lg:col-span-7 flex flex-col gap-6 lg:sticky lg:top-28">
              {/* Video Player */}
              <div className="aspect-video bg-black rounded-2xl overflow-hidden shadow-xl border border-gray-800">
                {videoUrl && (
                  <video
                    ref={videoRef}
                    src={videoUrl}
                    controls
                    className="w-full h-full"
                    onTimeUpdate={handleTimeUpdate}
                    onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
                    onPause={() => {
                      activeAudiosRef.current.forEach(a => a.pause());
                    }}
                    onPlay={() => { }}
                  />
                )}
              </div>

              <Timeline
                segments={segments}
                speakers={speakers}
                duration={duration}
                currentTime={currentTime}
                onSeek={handleSeek}
                waveform={waveform}
                isLoading={isAudioLoading}
              />

              {/* Actions Area */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
                {/* Step 1: Translation */}
                <div className="bg-white border border-claude-border rounded-2xl p-6 shadow-sm hover:shadow-md transition-all">
                  <div className="flex items-center gap-3 mb-2">
                    <div className="w-8 h-8 rounded-full bg-claude-paper flex items-center justify-center text-claude-text font-serif font-bold text-sm border border-claude-border">1</div>
                    <h3 className="font-serif font-bold text-lg text-gray-800">Context Translation ({targetLanguage})</h3>
                  </div>
                  <p className="text-xs text-gray-500 mb-6 font-sans leading-relaxed">
                    Sends the entire script for context-aware translation to {targetLanguage}.
                  </p>
                  <button
                    onClick={handleBatchTranslate}
                    disabled={isBatchProcessing || isTranscribing || segments.length === 0}
                    className="w-full flex items-center justify-center gap-2 px-6 py-3 bg-white text-gray-700 rounded-xl text-sm font-semibold transition disabled:opacity-50 disabled:cursor-not-allowed border border-gray-300 hover:border-claude-accent hover:text-claude-accent shadow-sm"
                  >
                    {isBatchProcessing && batchProgress.startsWith('Starting') ? (
                      <>
                        <div className="w-4 h-4 border-2 border-gray-300 border-t-claude-accent rounded-full animate-spin"></div>
                        <span>Initializing...</span>
                      </>
                    ) : (
                      <>
                        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 5h12M9 3v2m1.048 9.5A18.022 18.022 0 016.412 9m6.088 9h7M11 21l5-10 5 10M12.751 5C11.783 10.77 8.07 15.61 3 18.129" /></svg>
                        Translate Full Script
                      </>
                    )}
                  </button>
                </div>

                {/* Step 2: Synthesis */}
                <div className="bg-white border border-claude-border rounded-2xl p-6 shadow-sm hover:shadow-md transition-all">
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-3">
                      <div className="w-8 h-8 rounded-full bg-claude-paper flex items-center justify-center text-claude-text font-serif font-bold text-sm border border-claude-border">2</div>
                      <h3 className="font-serif font-bold text-lg text-gray-800">Voice Synthesis</h3>
                    </div>
                  </div>

                  <p className="text-xs text-gray-500 mb-6 font-sans leading-relaxed">
                    Generate AI voice audio for all translated segments.
                  </p>

                  <button
                    onClick={handleBatchTTS}
                    disabled={isBatchProcessing || isTranscribing || segments.length === 0}
                    className="w-full flex items-center justify-center gap-2 px-6 py-3 bg-claude-accent hover:bg-claude-accentHover text-white rounded-xl text-sm font-semibold transition disabled:opacity-50 disabled:cursor-not-allowed shadow-md shadow-claude-accent/20"
                  >
                    {isBatchProcessing && batchProgress.startsWith('Synthesizing') ? (
                      <>
                        <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin"></div>
                        <span>{batchProgress}</span>
                      </>
                    ) : (
                      <>
                        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" /></svg>
                        Generate Audio
                      </>
                    )}
                  </button>
                </div>
              </div>

            </div>

            <div className="w-full lg:col-span-12 flex flex-col h-[600px] lg:h-[calc(100vh-140px)] mt-8">
              <TranscriptionPanel
                segments={segments}
                speakers={speakers}
                isTranscribing={isTranscribing}
                onSegmentUpdate={handleSegmentUpdate}
                onTranslateSegment={handleTranslateSegmentImpl}
                onSynthesizeSegment={handleSynthesizeSegment}
                onSpeakerNameChange={handleSpeakerNameChange}
              />
            </div>
          </div>
        )}
      </main>
    </div>
  );
};

export default App;
