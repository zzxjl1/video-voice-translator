
import React, { memo } from 'react';
import { TranscriptionSegment, Speaker } from '../types';
import { formatTime, getSpeakerColor } from '../utils/helpers';

interface TranscriptionPanelProps {
    segments: TranscriptionSegment[];
    speakers: Speaker[];
    isTranscribing: boolean;
    currentTime: number;
    onSegmentUpdate: (segmentId: string, updates: Partial<TranscriptionSegment>) => void;
    onSynthesize: (segmentId: string) => void;
    onSeek: (time: number) => void;
}


const EditableTextArea: React.FC<{
    label: string,
    value: string,
    onUpdate: (val: string) => void,
    placeholder?: string,
    className?: string,
    isSecondary?: boolean,
    isLoading?: boolean
}> = ({ label, value, onUpdate, placeholder, className, isSecondary, isLoading }) => {
    const [isEditing, setIsEditing] = React.useState(false);
    const [localValue, setLocalValue] = React.useState(value);
    const textareaRef = React.useRef<HTMLTextAreaElement>(null);

    // Sync local value when global value changes (e.g. from batch processing)
    React.useEffect(() => {
        if (!isEditing) {
            setLocalValue(value);
        }
    }, [value, isEditing]);

    const handleEditClick = () => {
        setIsEditing(true);
        setTimeout(() => textareaRef.current?.focus(), 0);
    };

    const handleBlur = () => {
        setIsEditing(false);
        if (localValue !== value) {
            onUpdate(localValue); // Trigger update/auto-processing only if changed and on blur
        }
    };

    return (
        <div className="relative group">
            <div className="flex items-center justify-between mb-1.5 ml-1">
                <span className={`text-[9px] font-bold uppercase tracking-widest ${isSecondary ? 'text-gray-400' : 'text-claude-accent/70'}`}>
                    {label}
                </span>
            </div>

            <div className="relative overflow-hidden rounded-xl border border-[#e5e5e0] transition-all duration-300 bg-white">
                <textarea
                    ref={textareaRef}
                    value={localValue}
                    readOnly={!isEditing || isLoading}
                    onBlur={handleBlur}
                    onChange={(e) => setLocalValue(e.target.value)}
                    placeholder={placeholder}
                    className={`w-full p-3 text-sm transition-all duration-300 resize-none outline-none leading-relaxed ${isSecondary ? 'bg-[#f9f9f8] text-gray-600 font-serif italic' : 'bg-white text-gray-800 font-sans'
                        } ${!isEditing ? 'group-hover:blur-[2px] transition-all' : 'blur-0'} ${className} ${isLoading ? 'opacity-40 pointer-events-none' : ''}`}
                    rows={2}
                />

                {!isEditing && !isLoading && (
                    <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity duration-300 bg-white/10 backdrop-blur-[0.5px]">
                        <button
                            onClick={handleEditClick}
                            className="px-4 py-1.5 bg-white/90 border border-[#d1d1cc] rounded-full text-[10px] font-bold uppercase tracking-widest shadow-sm hover:scale-105 transition-transform text-gray-600"
                        >
                            Edit
                        </button>
                    </div>
                )}

                {isLoading && (
                    <div className="absolute inset-0 flex flex-col items-center justify-center bg-white/60 backdrop-blur-[1px] animate-in fade-in duration-300">
                        <div className="flex gap-1 mb-1">
                            <span className="w-1 h-1 bg-claude-accent rounded-full animate-bounce [animation-delay:-0.3s]"></span>
                            <span className="w-1 h-1 bg-claude-accent rounded-full animate-bounce [animation-delay:-0.15s]"></span>
                            <span className="w-1 h-1 bg-claude-accent rounded-full animate-bounce"></span>
                        </div>
                        <span className="text-[9px] font-bold uppercase tracking-[0.1em] text-claude-accent">Re-translating...</span>
                    </div>
                )}
            </div>
        </div>
    );
};

const SegmentCard: React.FC<{
    segment: TranscriptionSegment;
    speaker?: Speaker;
    isActive?: boolean;
    onSegmentUpdate: (segmentId: string, updates: Partial<TranscriptionSegment>) => void;
    onSynthesize: (segmentId: string) => void;
    onSeek: (time: number) => void;
}> = memo(({ segment, speaker, isActive, onSegmentUpdate, onSynthesize, onSeek }) => {
    const speakerColor = speaker ? getSpeakerColor(speaker.id) : '#9ca3af';

    // Calculate speed stats
    let audioRate = 1.0;
    let videoRate = 1.0;
    if (segment.actualDuration) {
        const targetDuration = segment.endTime - segment.startTime;
        const idealFactor = segment.actualDuration / targetDuration;
        audioRate = Math.min(Math.max(idealFactor, 0.75), 1.5);
        videoRate = Math.min(Math.max(audioRate / idealFactor, 0.8), 1.5);
    }

    return (
        <div
            id={`segment-${segment.id}`}
            className={`p-6 rounded-2xl space-y-5 border transition-all duration-500 bg-white ${isActive ? 'ring-2 ring-claude-accent/30 border-claude-accent shadow-lg scale-[1.01]' :
                segment.audioUrl ? 'border-[#d1d1cc] shadow-md ring-1 ring-[#e5e5e0]/50' : 'border-[#e5e5e0] hover:border-[#d1d1cc] shadow-sm'
                }`}
        >
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <div className="w-3 h-3 rounded-full ring-2 ring-white shadow-sm" style={{ backgroundColor: speakerColor }}></div>
                    <span className="text-[11px] font-bold uppercase tracking-[0.1em] text-gray-500">
                        {speaker?.name || '...'}
                    </span>
                </div>
                <div className="flex items-center gap-3">
                    {segment.actualDuration && (segment.actualDuration / (segment.endTime - segment.startTime) > 1.875 || segment.actualDuration / (segment.endTime - segment.startTime) < 0.5) && (
                        <div className="flex items-center gap-1.5 px-2 py-1 bg-amber-50 border border-amber-200 rounded-md animate-pulse">
                            <svg className="w-3 h-3 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                            </svg>
                            <span className="text-[9px] font-bold text-amber-700 uppercase tracking-wider">Duration Mismatch</span>
                        </div>
                    )}
                    <span className="text-[10px] font-mono text-gray-400 bg-[#f9f9f8] px-2 py-1 rounded-md border border-[#eee]">
                        {formatTime(segment.startTime)} – {formatTime(segment.endTime)} ({(segment.endTime - segment.startTime).toFixed(2)}s)
                    </span>
                    <button
                        onClick={() => onSeek(segment.startTime)}
                        className="p-1.5 rounded-md hover:bg-claude-paper text-gray-400 hover:text-claude-accent transition-colors"
                        title="Locate in Timeline"
                    >
                        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z" />
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 11a3 3 0 11-6 0 3 3 0 016 0z" />
                        </svg>
                    </button>
                </div>
            </div>

            <div className="flex flex-col gap-5">
                <EditableTextArea
                    label="Original (ASR)"
                    value={segment.originalText}
                    onUpdate={(val) => onSegmentUpdate(segment.id, { originalText: val })}
                    isSecondary
                />

                <EditableTextArea
                    label="Translation"
                    value={segment.translatedText}
                    onUpdate={(val) => onSegmentUpdate(segment.id, { translatedText: val })}
                    placeholder="Translation will appear here..."
                    isLoading={segment.isTranslating}
                />
            </div>

            <div className="pt-2">
                {segment.audioUrl ? (
                    <div className="animate-in fade-in slide-in-from-top-2 duration-500">
                        <audio
                            src={segment.audioUrl}
                            controls
                            onLoadedMetadata={(e) => {
                                const duration = e.currentTarget.duration;
                                if (duration && segment.actualDuration !== duration) {
                                    onSegmentUpdate(segment.id, { actualDuration: duration });
                                }
                            }}
                            className="w-full h-8 opacity-70 hover:opacity-100 transition-opacity"
                        />
                        {segment.actualDuration && (
                            <div className="mt-3 flex items-center gap-2 px-3 py-2 bg-claude-paper/50 rounded-xl border border-claude-border/50 animate-in fade-in duration-500">
                                <div className={`flex items-center gap-1.5 px-2 py-0.5 rounded-md text-[9px] font-bold uppercase tracking-wider ${audioRate !== 1.0 ? 'bg-claude-accent/10 text-claude-accent' : 'bg-gray-100 text-gray-400'}`}>
                                    Audio: {audioRate.toFixed(2)}x
                                </div>
                                <div className={`flex items-center gap-1.5 px-2 py-0.5 rounded-md text-[9px] font-bold uppercase tracking-wider ${videoRate !== 1.0 ? 'bg-amber-100 text-amber-700' : 'bg-gray-100 text-gray-400'}`}>
                                    Video: {videoRate.toFixed(2)}x
                                </div>
                                {/* <span className="text-[10px] text-gray-400 ml-auto font-medium">Synced Duration</span> */}
                            </div>
                        )}
                    </div>
                ) : (
                    (segment.translatedText || segment.isTranslating) && (
                        <div className="animate-in fade-in duration-300">
                            <button
                                onClick={() => !segment.isSynthesizing && !segment.isTranslating && onSynthesize(segment.id)}
                                disabled={segment.isSynthesizing || segment.isTranslating}
                                className={`w-full py-2.5 rounded-xl text-[10px] font-bold uppercase tracking-[0.15em] transition-all flex items-center justify-center gap-3 shadow-sm border ${segment.isSynthesizing || segment.isTranslating
                                    ? 'bg-[#f9f9f8] border-[#e5e5e0] text-gray-400'
                                    : 'bg-white border-claude-border text-[#da7756] hover:bg-claude-paper active:scale-[0.98]'
                                    }`}
                            >
                                {segment.isSynthesizing ? (
                                    <>
                                        <div className="flex gap-1">
                                            <span className="w-1 h-1 bg-current rounded-full animate-bounce [animation-delay:-0.3s]"></span>
                                            <span className="w-1 h-1 bg-current rounded-full animate-bounce [animation-delay:-0.15s]"></span>
                                            <span className="w-1 h-1 bg-current rounded-full animate-bounce"></span>
                                        </div>
                                        <span>Re-synthesizing...</span>
                                    </>
                                ) : segment.isTranslating ? (
                                    <>
                                        <span>Waiting for translation...</span>
                                    </>
                                ) : (
                                    <>
                                        <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" /></svg>
                                        Generate Voice
                                    </>
                                )}
                            </button>
                        </div>
                    )
                )}
            </div>
        </div>
    );
});

export const TranscriptionPanel: React.FC<TranscriptionPanelProps> = memo(({
    segments, speakers, isTranscribing, currentTime, onSegmentUpdate, onSynthesize, onSeek
}) => {
    const speakerMap = new Map(speakers.map(s => [s.id, s]));
    const listRef = React.useRef<HTMLDivElement>(null);

    // Auto-scroll logic
    React.useEffect(() => {
        const activeSegment = segments.find(s => currentTime >= s.startTime && currentTime < s.endTime);
        if (activeSegment && listRef.current) {
            const el = document.getElementById(`segment-${activeSegment.id}`);
            const container = listRef.current;
            if (el && container) {
                // Calculate position relative to container
                const targetTop = el.offsetTop - container.offsetTop - (container.clientHeight / 2) + (el.clientHeight / 2);
                container.scrollTo({
                    top: Math.max(0, targetTop),
                    behavior: 'smooth'
                });
            }
        }
    }, [currentTime, segments]);

    return (
        <div className="flex-grow flex flex-col h-full bg-[#fbfbf9] border border-[#e5e5e0] rounded-3xl overflow-hidden shadow-sm">
            <div className="px-6 py-5 border-b border-[#e5e5e0] flex items-center justify-between bg-white/60 backdrop-blur-md">
                <h3 className="text-xs font-serif font-bold text-gray-700 flex items-center gap-2 tracking-wide">
                    <span className="w-1.5 h-1.5 bg-claude-accent rounded-full"></span>
                    SCRIPT & TRANSLATION
                </h3>
                {isTranscribing && (
                    <div className="flex items-center gap-2">
                        <span className="text-[10px] uppercase font-bold tracking-tighter text-claude-accent/80">Processing</span>
                        <div className="flex gap-0.5">
                            <span className="w-0.5 h-2 bg-claude-accent/30 animate-pulse"></span>
                            <span className="w-0.5 h-2 bg-claude-accent/50 animate-pulse [animation-delay:0.2s]"></span>
                            <span className="w-0.5 h-2 bg-claude-accent/70 animate-pulse [animation-delay:0.4s]"></span>
                        </div>
                    </div>
                )}
            </div>

            <div
                ref={listRef}
                className="flex-grow overflow-y-auto p-6 space-y-6 custom-scrollbar scroll-smooth"
            >
                {segments.length === 0 && !isTranscribing ? (
                    <div className="flex flex-col items-center justify-center h-full text-center space-y-4 opacity-40">
                        <div className="w-12 h-12 rounded-full border border-dashed border-gray-300 flex items-center justify-center">
                            <svg className="w-6 h-6 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1} d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" /></svg>
                        </div>
                        <p className="text-xs font-medium text-gray-400 tracking-tight">Waiting for transcription...</p>
                    </div>
                ) : (
                    <>
                        {segments.map(segment => (
                            <SegmentCard
                                key={segment.id}
                                segment={segment}
                                speaker={speakerMap.get(segment.speakerId)}
                                isActive={currentTime >= segment.startTime && currentTime < segment.endTime}
                                onSegmentUpdate={onSegmentUpdate}
                                onSynthesize={onSynthesize}
                                onSeek={onSeek}
                            />
                        ))}
                        {isTranscribing && (
                            <div className="p-8 rounded-2xl border border-dashed border-[#e5e5e0] animate-pulse flex justify-center bg-white/40">
                                <span className="text-[10px] uppercase font-bold text-gray-300 tracking-[0.2em]">Analyzing...</span>
                            </div>
                        )}
                    </>
                )}
            </div>
        </div>
    );
});
