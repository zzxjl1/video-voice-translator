
import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { TranscriptionSegment, Speaker, GeminiVoice, ModelSettings, DEFAULT_MODEL_SETTINGS } from './types';
import Header from './components/Header';
import VideoUpload from './components/VideoUpload';
import Timeline from './components/Timeline';
import SettingsModal from './components/SettingsModal';
import StreamingLog from './components/StreamingLog';
import { TranscriptionPanel } from './components/TranscriptionPanel';
import { translateText, generateGeminiSpeech, transcribeVideoStreaming, translateWholeScript } from './services/geminiService';
import { getAudioWaveform } from './utils/audioProcessor';
import { createWavUrlFromPcm } from './utils/helpers';

const App: React.FC = () => {
  const [videoFile, setVideoFile] = useState<File | null>(null);
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
  const [globalTtsSource, setGlobalTtsSource] = useState<'gemini' | 'browser'>('gemini');
  
  // Streaming Log State
  const [isLogOpen, setIsLogOpen] = useState(false);
  const [rawLog, setRawLog] = useState('');

  // Settings State
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [modelSettings, setModelSettings] = useState<ModelSettings>(DEFAULT_MODEL_SETTINGS);
  
  const videoRef = useRef<HTMLVideoElement>(null);
  const activeAudiosRef = useRef<Map<string, HTMLAudioElement>>(new Map());
  // Track which browser TTS segments are currently speaking to avoid overlap/spam
  const activeBrowserSegments = useRef<Set<string>>(new Set());

  const videoUrl = useMemo(() => (videoFile ? URL.createObjectURL(videoFile) : null), [videoFile]);

  useEffect(() => {
    return () => {
      if (videoUrl) URL.revokeObjectURL(videoUrl);
      // Clean up any generated audio blob URLs
      segments.forEach(seg => {
        if (seg.audioUrl?.startsWith('blob:')) {
          URL.revokeObjectURL(seg.audioUrl);
        }
      });
      // Cancel any pending speech
      window.speechSynthesis.cancel();
    };
  }, [videoUrl, segments]);

  const handleTimeUpdate = () => {
    if (!videoRef.current) return;
    const time = videoRef.current.currentTime;
    setCurrentTime(time);

    let isAnyAudioPlaying = false;

    // --- Audio File Playback (Gemini) ---
    segments.forEach(seg => {
      if (seg.audioUrl && seg.ttsSource === 'gemini') {
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

    // --- Live Browser TTS Playback ---
    const activeBrowserSegs = segments.filter(seg => 
        seg.ttsSource === 'browser' && 
        seg.translatedText && 
        time >= seg.startTime && 
        time < seg.endTime
    );

    if (activeBrowserSegs.length > 0) {
        isAnyAudioPlaying = true;
        activeBrowserSegs.forEach(seg => {
            if (!activeBrowserSegments.current.has(seg.id)) {
                window.speechSynthesis.cancel();
                activeBrowserSegments.current.clear();
                
                const utterance = new SpeechSynthesisUtterance(seg.translatedText);
                utterance.rate = 1.1; 
                
                utterance.onend = () => activeBrowserSegments.current.delete(seg.id);
                utterance.onerror = () => activeBrowserSegments.current.delete(seg.id);
                
                activeBrowserSegments.current.add(seg.id);
                window.speechSynthesis.speak(utterance);
            }
        });
    } else {
        if (activeBrowserSegments.current.size > 0) {
            window.speechSynthesis.cancel();
            activeBrowserSegments.current.clear();
        }
    }

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
    
    window.speechSynthesis.cancel();
    
    try {
        getAudioWaveform(file, 300).then(peaks => {
          setWaveform(peaks);
          setIsAudioLoading(false);
        });

        // Use more vibrant/distinct colors for the light theme
        const voices = [GeminiVoice.Zephyr, GeminiVoice.Kore, GeminiVoice.Puck, GeminiVoice.Charon, GeminiVoice.Fenrir];
        const colors = ['#2563eb', '#16a34a', '#d97706', '#dc2626', '#9333ea', '#0891b2'];
        let speakerMap: Record<string, Speaker> = {};

        for await (const segmentData of transcribeVideoStreaming(file, modelSettings.transcriptionModel)) {
          const label = segmentData.speakerLabel;
          
          if (!speakerMap[label]) {
            const index = Object.keys(speakerMap).length;
            const newSpeaker: Speaker = {
              id: label,
              name: label,
              color: colors[index % colors.length],
              voice: voices[index % voices.length]
            };
            speakerMap[label] = newSpeaker;
            setSpeakers(prev => [...prev, newSpeaker]);
          }

          const newSegment: TranscriptionSegment = {
            id: `seg-${Date.now()}-${Math.random()}`,
            speakerId: label,
            startTime: segmentData.startTime,
            endTime: segmentData.endTime,
            originalText: segmentData.text,
            translatedText: '',
            status: 'pending',
            ttsSource: globalTtsSource
          };
          
          setSegments(prev => [...prev, newSegment]);
        }
    } catch (err) {
        console.error(err);
        alert("Processing encountered an error. Please check your API key and model selection.");
    } finally {
        setIsTranscribing(false);
    }
  }, [modelSettings, globalTtsSource]);

  const handleSegmentUpdate = useCallback((id: string, text: string) => {
    setSegments(prev => prev.map(s => s.id === id ? { ...s, translatedText: text } : s));
  }, []);

  const handleToggleTtsSource = useCallback((id: string) => {
    setSegments(prev => prev.map(s => s.id === id ? { ...s, ttsSource: s.ttsSource === 'gemini' ? 'browser' : 'gemini' } : s));
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

      if (!segmentToTranslate) return;

      try {
          const trans = await translateText(segmentToTranslate.originalText, 'English', modelSettings.translationModel);
          setSegments(prev => prev.map(s => s.id === id ? { ...s, translatedText: trans, isTranslating: false } : s));
      } catch (e) {
          setSegments(prev => prev.map(s => s.id === id ? { ...s, isTranslating: false } : s));
      }
  };

  const handleSynthesizeSegment = useCallback(async (id: string, forceSource?: 'gemini' | 'browser') => {
    let targetSegment: TranscriptionSegment | undefined;
    setSegments(prev => {
        targetSegment = prev.find(s => s.id === id);
        return prev.map(s => s.id === id ? { ...s, isSynthesizing: true } : s);
    });

    if (!targetSegment) return;
    
    const speaker = speakers.find(sp => sp.id === targetSegment?.speakerId);
    if (!speaker) {
         setSegments(prev => prev.map(s => s.id === id ? { ...s, isSynthesizing: false } : s));
         return;
    }

    const source = forceSource || targetSegment.ttsSource;

    try {
      let audioUrl = '';
      if (source === 'gemini') {
          const base64 = await generateGeminiSpeech(targetSegment.translatedText, speaker.voice, modelSettings.ttsModel);
          if (base64) audioUrl = createWavUrlFromPcm(base64);
      } else {
          window.speechSynthesis.cancel();
          const utterance = new SpeechSynthesisUtterance(targetSegment.translatedText);
          window.speechSynthesis.speak(utterance);
      }
      setSegments(prev => prev.map(s => s.id === id ? { ...s, audioUrl: source === 'gemini' ? audioUrl : undefined, isSynthesizing: false, ttsSource: source } : s));
    } catch (e) {
      console.error("Synthesis failed:", e);
      setSegments(prev => prev.map(s => s.id === id ? { ...s, isSynthesizing: false } : s));
    }
  }, [speakers, modelSettings]);

  const handleBatchTranslate = useCallback(async () => {
    const toProcess = segments.filter(s => !s.translatedText);
    if (toProcess.length === 0) return;

    setIsBatchProcessing(true);
    setBatchProgress('Starting batch translation...');
    setIsLogOpen(true);
    setRawLog('Initializing Translation Stream for entire script...\n');

    try {
        const contextPayload = toProcess.map(s => ({
            id: s.id,
            text: s.originalText,
            speakerId: s.speakerId,
            startTime: s.startTime
        }));

        setRawLog(prev => prev + `\n--- Sending ${toProcess.length} segments with full context ---\n`);

        const handleStreamUpdate = (text: string) => {
            setRawLog(prev => prev + text);
        };

        const results = await translateWholeScript(
            contextPayload, 
            'English', 
            modelSettings.translationModel,
            handleStreamUpdate
        );
        
        setSegments(prev => prev.map(seg => {
            const match = results.find(r => r.id === seg.id);
            if (match) {
                return { ...seg, translatedText: match.translatedText };
            }
            return seg;
        }));
        
        setRawLog(prev => prev + '\n\n--- Translation Complete ---\nClosing stream view in 1.5 seconds...');
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
  }, [segments, modelSettings]);

  const handleBatchTTS = useCallback(async () => {
    setIsBatchProcessing(true);
    let count = 0;
    const toProcess = segments.filter(s => s.translatedText); 
    
    for (const seg of toProcess) {
        if (globalTtsSource === 'gemini' && seg.audioUrl) continue;
        
        setBatchProgress(`Synthesizing ${count + 1}/${toProcess.length}`);
        await handleSynthesizeSegment(seg.id, globalTtsSource);
        count++;
        if (globalTtsSource === 'gemini') {
            await new Promise(r => setTimeout(r, 600)); 
        }
    }
    setBatchProgress('');
    setIsBatchProcessing(false);
  }, [segments, globalTtsSource, handleSynthesizeSegment]);

  const handleSeek = useCallback((time: number) => {
    if (videoRef.current) {
      videoRef.current.currentTime = time;
      activeAudiosRef.current.forEach(a => a.pause());
      activeAudiosRef.current.clear();
      window.speechSynthesis.cancel();
      activeBrowserSegments.current.clear();
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
        settings={modelSettings}
        onSave={setModelSettings}
      />
      
      <StreamingLog 
        isOpen={isLogOpen}
        logs={rawLog}
        onClose={() => setIsLogOpen(false)}
        title="Gemini Translation Stream"
      />

      <main className="flex-grow container mx-auto p-6 lg:p-10">
        {!videoFile ? (
          <div className="max-w-3xl mx-auto mt-16 animate-in slide-in-from-bottom-8 fade-in duration-700">
            <VideoUpload onVideoSelect={handleVideoSelect} isLoading={isTranscribing} />
          </div>
        ) : (
          <div className="flex flex-col lg:grid lg:grid-cols-12 gap-8 items-start">
            <div className="w-full lg:col-span-7 flex flex-col gap-6 lg:sticky lg:top-28">
              {/* Video Player - Keep dark for content contrast, but add subtle border */}
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
                        window.speechSynthesis.cancel();
                    }}
                    onPlay={() => {}}
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

              {/* Actions Area - Styled Cards */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
                 {/* Step 1: Translation */}
                 <div className="bg-white border border-claude-border rounded-2xl p-6 shadow-sm hover:shadow-md transition-all">
                    <div className="flex items-center gap-3 mb-2">
                        <div className="w-8 h-8 rounded-full bg-claude-paper flex items-center justify-center text-claude-text font-serif font-bold text-sm border border-claude-border">1</div>
                        <h3 className="font-serif font-bold text-lg text-gray-800">Context Translation</h3>
                    </div>
                    <p className="text-xs text-gray-500 mb-6 font-sans leading-relaxed">
                        Sends the entire script to Gemini for context-aware translation in a single request.
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
                    
                    <div className="flex bg-claude-paper rounded-xl p-1.5 border border-claude-border mb-4">
                        <button 
                            onClick={() => setGlobalTtsSource('gemini')}
                            className={`flex-1 py-1.5 text-xs font-bold rounded-lg transition ${globalTtsSource === 'gemini' ? 'bg-white text-claude-accent shadow-sm' : 'text-gray-500 hover:text-gray-800'}`}
                        >
                            Gemini AI
                        </button>
                        <button 
                            onClick={() => setGlobalTtsSource('browser')}
                            className={`flex-1 py-1.5 text-xs font-bold rounded-lg transition ${globalTtsSource === 'browser' ? 'bg-white text-claude-accent shadow-sm' : 'text-gray-500 hover:text-gray-800'}`}
                        >
                            Browser
                        </button>
                    </div>

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

            <div className="w-full lg:col-span-5 flex flex-col h-[600px] lg:h-[calc(100vh-140px)]">
              <TranscriptionPanel 
                segments={segments} 
                speakers={speakers}
                isTranscribing={isTranscribing}
                onSegmentUpdate={handleSegmentUpdate}
                onTranslateSegment={handleTranslateSegmentImpl} 
                onSynthesizeSegment={handleSynthesizeSegment}
                onToggleTtsSource={handleToggleTtsSource}
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
