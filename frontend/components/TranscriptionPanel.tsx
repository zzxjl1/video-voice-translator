
import React, { memo } from 'react';
import { TranscriptionSegment, Speaker } from '../types';
import { formatTime } from '../utils/helpers';

interface TranscriptionPanelProps {
    segments: TranscriptionSegment[];
    speakers: Speaker[];
    isTranscribing: boolean;
    onSegmentUpdate: (segmentId: string, updates: Partial<TranscriptionSegment>) => void;
    onSpeakerNameChange: (id: string, newName: string) => void;
}

const SegmentCard: React.FC<{
    segment: TranscriptionSegment;
    speaker?: Speaker;
    onSegmentUpdate: (segmentId: string, updates: Partial<TranscriptionSegment>) => void;
    onSpeakerNameChange: (id: string, newName: string) => void;
}> = memo(({ segment, speaker, onSegmentUpdate, onSpeakerNameChange }) => {
    return (
        <div className={`p-6 rounded-2xl space-y-5 border transition-all duration-500 bg-white ${segment.audioUrl ? 'border-[#d1d1cc] shadow-md ring-1 ring-[#e5e5e0]/50' : 'border-[#e5e5e0] hover:border-[#d1d1cc] shadow-sm'}`}>
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <div className="w-3 h-3 rounded-full ring-2 ring-white shadow-sm" style={{ backgroundColor: speaker?.color || '#9ca3af' }}></div>
                    <input
                        value={speaker?.name || '...'}
                        onChange={(e) => speaker && onSpeakerNameChange(speaker.id, e.target.value)}
                        className="bg-transparent text-[11px] font-bold uppercase tracking-[0.1em] text-gray-500 focus:outline-none focus:text-claude-accent w-32 border-b border-transparent focus:border-claude-accent/30 transition-colors"
                    />
                </div>
                <div className="flex items-center gap-3">
                    <span className="text-[10px] font-mono text-gray-400 bg-[#f9f9f8] px-2 py-1 rounded-md border border-[#eee]">{formatTime(segment.startTime)}</span>
                    {segment.audioUrl && (
                        <button
                            onClick={() => new Audio(segment.audioUrl).play()}
                            className="p-1.5 bg-gray-50 text-gray-500 rounded-full hover:bg-gray-100 transition border border-[#eee]"
                            title="Play Audio"
                        >
                            <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>
                        </button>
                    )}
                </div>
            </div>

            <div className="flex flex-col gap-4">
                {/* ASR Source Text */}
                <div className="relative group">
                    <div className="flex items-center gap-2 mb-1.5 ml-1">
                        <span className="text-[9px] font-bold uppercase tracking-widest text-gray-400">Original (ASR)</span>
                        <div className="text-gray-300 group-hover:text-claude-accent transition-colors cursor-pointer" title="Click to edit">
                            <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                            </svg>
                        </div>
                    </div>
                    <textarea
                        value={segment.originalText}
                        onChange={(e) => onSegmentUpdate(segment.id, { originalText: e.target.value })}
                        className="w-full bg-[#f9f9f8] p-3 rounded-xl text-sm text-gray-600 border border-[#e5e5e0] focus:border-claude-accent/40 focus:ring-0 transition resize-none font-serif italic leading-relaxed"
                        rows={2}
                    />
                </div>

                {/* Translation Target Text */}
                <div className="relative">
                    <div className="flex items-center gap-2 mb-1.5 ml-1">
                        <span className="text-[9px] font-bold uppercase tracking-widest text-claude-accent/70">Translation</span>
                    </div>
                    <textarea
                        value={segment.translatedText}
                        onChange={(e) => onSegmentUpdate(segment.id, { translatedText: e.target.value })}
                        placeholder="Translation will appear here..."
                        className="w-full bg-white p-3 rounded-xl text-sm text-gray-800 border border-[#d1d1cc] focus:border-claude-accent focus:ring-2 focus:ring-claude-accent/10 transition resize-none placeholder:text-gray-300 font-sans leading-relaxed"
                        rows={2}
                    />
                </div>
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
    segments, speakers, isTranscribing, onSegmentUpdate, onSpeakerNameChange
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
                                onSpeakerNameChange={onSpeakerNameChange}
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
