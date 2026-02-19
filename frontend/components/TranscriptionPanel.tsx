
import React, { memo } from 'react';
import { TranscriptionSegment, Speaker } from '../types';
import { formatTime, getSpeakerColor } from '../utils/helpers';

interface TranscriptionPanelProps {
    segments: TranscriptionSegment[];
    speakers: Speaker[];
    isTranscribing: boolean;
    onSegmentUpdate: (segmentId: string, updates: Partial<TranscriptionSegment>) => void;
    onSynthesize: (segmentId: string) => void;
}


const EditableTextArea: React.FC<{
    label: string,
    value: string,
    onUpdate: (val: string) => void,
    placeholder?: string,
    className?: string,
    isSecondary?: boolean
}> = ({ label, value, onUpdate, placeholder, className, isSecondary }) => {
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
                    readOnly={!isEditing}
                    onBlur={handleBlur}
                    onChange={(e) => setLocalValue(e.target.value)}
                    placeholder={placeholder}
                    className={`w-full p-3 text-sm transition-all duration-300 resize-none outline-none leading-relaxed ${isSecondary ? 'bg-[#f9f9f8] text-gray-600 font-serif italic' : 'bg-white text-gray-800 font-sans'
                        } ${!isEditing ? 'group-hover:blur-[2px] transition-all' : 'blur-0'} ${className}`}
                    rows={2}
                />

                {!isEditing && (
                    <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity duration-300 bg-white/10 backdrop-blur-[0.5px]">
                        <button
                            onClick={handleEditClick}
                            className="px-4 py-1.5 bg-white/90 border border-[#d1d1cc] rounded-full text-[10px] font-bold uppercase tracking-widest shadow-sm hover:scale-105 transition-transform text-gray-600"
                        >
                            Edit
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
};

const SegmentCard: React.FC<{
    segment: TranscriptionSegment;
    speaker?: Speaker;
    onSegmentUpdate: (segmentId: string, updates: Partial<TranscriptionSegment>) => void;
    onSynthesize: (segmentId: string) => void;
}> = memo(({ segment, speaker, onSegmentUpdate, onSynthesize }) => {
    const speakerColor = speaker ? getSpeakerColor(speaker.id) : '#9ca3af';

    return (
        <div className={`p-6 rounded-2xl space-y-5 border transition-all duration-500 bg-white ${segment.audioUrl ? 'border-[#d1d1cc] shadow-md ring-1 ring-[#e5e5e0]/50' : 'border-[#e5e5e0] hover:border-[#d1d1cc] shadow-sm'}`}>
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <div className="w-3 h-3 rounded-full ring-2 ring-white shadow-sm" style={{ backgroundColor: speakerColor }}></div>
                    <span className="text-[11px] font-bold uppercase tracking-[0.1em] text-gray-500">
                        {speaker?.name || '...'}
                    </span>
                </div>
                <div className="flex items-center gap-3">
                    <span className="text-[10px] font-mono text-gray-400 bg-[#f9f9f8] px-2 py-1 rounded-md border border-[#eee]">{formatTime(segment.startTime)}</span>
                </div>
            </div>

            {segment.audioUrl ? (
                <div className="px-1 animate-in fade-in slide-in-from-top-2 duration-500">
                    <audio
                        src={segment.audioUrl}
                        controls
                        className="w-full h-8 opacity-70 hover:opacity-100 transition-opacity"
                    />
                </div>
            ) : (
                segment.translatedText && !segment.isSynthesizing && (
                    <div className="px-1">
                        <button
                            onClick={() => onSynthesize(segment.id)}
                            className="w-full py-2 bg-claude-bg border border-claude-border rounded-xl text-[10px] font-bold uppercase tracking-widest text-[#da7756] hover:bg-claude-paper transition-colors flex items-center justify-center gap-2 shadow-sm"
                        >
                            <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" /></svg>
                            Generate Voice
                        </button>
                    </div>
                )
            )}

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
                />
            </div>

            {(segment.isTranslating || segment.isSynthesizing) && (
                <div className="flex items-center gap-2 px-1">
                    <div className="flex gap-1">
                        <span className="w-1 h-1 bg-claude-accent/50 rounded-full animate-bounce [animation-delay:-0.3s]"></span>
                        <span className="w-1 h-1 bg-claude-accent/50 rounded-full animate-bounce [animation-delay:-0.15s]"></span>
                        <span className="w-1 h-1 bg-claude-accent/50 rounded-full animate-bounce"></span>
                    </div>
                    <span className="text-[10px] font-medium text-gray-400">
                        {segment.isTranslating ? 'Re-translating...' : 'Re-synthesizing...'}
                    </span>
                </div>
            )}
        </div>
    );
});

export const TranscriptionPanel: React.FC<TranscriptionPanelProps> = memo(({
    segments, speakers, isTranscribing, onSegmentUpdate, onSynthesize
}) => {
    const speakerMap = new Map(speakers.map(s => [s.id, s]));

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

            <div className="flex-grow overflow-y-auto p-6 space-y-6 custom-scrollbar">
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
                                onSegmentUpdate={onSegmentUpdate}
                                onSynthesize={onSynthesize}
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
