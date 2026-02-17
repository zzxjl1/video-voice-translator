
import React, { memo } from 'react';
import { TranscriptionSegment, Speaker } from '../types';
import { formatTime } from '../utils/helpers';

interface TranscriptionPanelProps {
  segments: TranscriptionSegment[];
  speakers: Speaker[];
  isTranscribing: boolean;
  onSegmentUpdate: (segmentId: string, newTranslatedText: string) => void;
  onTranslateSegment: (segmentId: string) => void;
  onSynthesizeSegment: (segmentId: string) => void;
  onToggleTtsSource: (segmentId: string) => void;
  onSpeakerNameChange: (id: string, newName: string) => void;
}

const SegmentCard: React.FC<{
    segment: TranscriptionSegment;
    speaker?: Speaker;
    onSegmentUpdate: (segmentId: string, newTranslatedText: string) => void;
    onTranslateSegment: (segmentId: string) => void;
    onSynthesizeSegment: (segmentId: string) => void;
    onToggleTtsSource: (segmentId: string) => void;
    onSpeakerNameChange: (id: string, newName: string) => void;
}> = memo(({ segment, speaker, onSegmentUpdate, onTranslateSegment, onSynthesizeSegment, onToggleTtsSource, onSpeakerNameChange }) => {
    return (
        <div className={`p-6 rounded-2xl space-y-4 border transition-all duration-500 ${segment.audioUrl ? 'bg-white border-claude-accent/30 shadow-md ring-1 ring-claude-accent/10' : 'bg-white border-claude-border hover:border-gray-300 shadow-sm'}`}>
            <div className="flex items-center justify-between">
                <div className="flex items-center space-x-3">
                    <div className="w-2.5 h-2.5 rounded-full ring-2 ring-white shadow-sm" style={{ backgroundColor: speaker?.color || '#9ca3af' }}></div>
                    <input 
                      value={speaker?.name || '...'} 
                      onChange={(e) => speaker && onSpeakerNameChange(speaker.id, e.target.value)}
                      className="bg-transparent text-xs font-bold uppercase tracking-wider text-gray-700 focus:outline-none focus:text-claude-accent w-32 border-b border-transparent focus:border-claude-accent transition-colors"
                    />
                </div>
                <div className="flex items-center space-x-3">
                    <span className="text-[10px] font-mono text-gray-400 bg-claude-paper px-2 py-1 rounded-md">{formatTime(segment.startTime)}</span>
                    <button 
                        onClick={() => onToggleTtsSource(segment.id)}
                        className="text-[10px] uppercase font-bold text-gray-400 hover:text-claude-accent transition cursor-pointer"
                    >
                        {segment.ttsSource}
                    </button>
                </div>
            </div>

            <div className="grid grid-cols-1 gap-4">
                <div className="bg-claude-paper/50 p-3 rounded-lg border border-transparent">
                    <p className="text-sm text-gray-600 font-serif leading-relaxed italic">"{segment.originalText}"</p>
                </div>
                <div className="relative">
                    <textarea
                        value={segment.translatedText}
                        onChange={(e) => onSegmentUpdate(segment.id, e.target.value)}
                        placeholder="Translation will appear here..."
                        className="w-full bg-white p-3 rounded-lg text-sm text-gray-800 border border-claude-border focus:border-claude-accent focus:ring-1 focus:ring-claude-accent/20 transition resize-none placeholder:text-gray-400 font-sans"
                        rows={2}
                    />
                </div>
            </div>
            
            <div className="flex items-center justify-between pt-1">
                 <div className="flex items-center gap-2">
                    {segment.audioUrl && (
                        <button 
                            onClick={() => new Audio(segment.audioUrl).play()}
                            className="p-2 bg-claude-accent/10 text-claude-accent rounded-full hover:bg-claude-accent/20 transition"
                            title="Play Audio"
                        >
                            <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
                        </button>
                    )}
                 </div>
                 <div className="flex gap-2">
                    <button 
                        onClick={() => onTranslateSegment(segment.id)}
                        disabled={segment.isTranslating}
                        className="text-xs font-medium px-4 py-2 rounded-lg bg-gray-100 text-gray-600 hover:bg-gray-200 disabled:opacity-50 transition border border-gray-200"
                    >
                        {segment.isTranslating ? 'Translating...' : 'Translate'}
                    </button>
                    <button 
                        onClick={() => onSynthesizeSegment(segment.id)}
                        disabled={segment.isSynthesizing || !segment.translatedText}
                        className="text-xs font-medium px-4 py-2 rounded-lg bg-claude-accent text-white hover:bg-claude-accentHover disabled:opacity-50 disabled:cursor-not-allowed transition shadow-sm"
                    >
                        {segment.isSynthesizing ? 'Generating...' : 'Synthesize Voice'}
                    </button>
                 </div>
            </div>
        </div>
    );
});

export const TranscriptionPanel: React.FC<TranscriptionPanelProps> = memo(({ 
    segments, speakers, isTranscribing, onSegmentUpdate, onTranslateSegment, onSynthesizeSegment, onToggleTtsSource, onSpeakerNameChange 
}) => {
    const speakerMap = new Map(speakers.map(s => [s.id, s]));

    return (
        <div className="flex-grow flex flex-col h-full bg-claude-paper/50 border border-claude-border rounded-3xl overflow-hidden shadow-sm">
            <div className="px-6 py-5 border-b border-claude-border flex items-center justify-between bg-white/80 backdrop-blur-md">
                <h3 className="text-sm font-serif font-bold text-gray-800 flex items-center gap-2">
                    <span className="w-2 h-2 bg-claude-accent rounded-full"></span>
                    Script & Translation
                </h3>
                {isTranscribing && (
                    <div className="flex items-center gap-2">
                        <span className="text-xs font-medium text-claude-accent">Streaming</span>
                        <div className="flex gap-1">
                            <span className="w-1 h-1 bg-claude-accent rounded-full animate-bounce [animation-delay:-0.3s]"></span>
                            <span className="w-1 h-1 bg-claude-accent rounded-full animate-bounce [animation-delay:-0.15s]"></span>
                            <span className="w-1 h-1 bg-claude-accent rounded-full animate-bounce"></span>
                        </div>
                    </div>
                )}
            </div>
            
            <div className="flex-grow overflow-y-auto p-6 space-y-5 custom-scrollbar bg-claude-bg">
                {segments.length === 0 && !isTranscribing ? (
                    <div className="flex flex-col items-center justify-center h-full text-center space-y-4 opacity-40">
                        <svg className="w-16 h-16 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1} d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z" /></svg>
                        <p className="text-sm font-serif text-gray-500">Video transcript will appear here...</p>
                    </div>
                ) : (
                    <>
                        {segments.map(segment => (
                            <SegmentCard 
                                key={segment.id} 
                                segment={segment} 
                                speaker={speakerMap.get(segment.speakerId)}
                                onSegmentUpdate={onSegmentUpdate}
                                onTranslateSegment={onTranslateSegment}
                                onSynthesizeSegment={onSynthesizeSegment}
                                onToggleTtsSource={onToggleTtsSource}
                                onSpeakerNameChange={onSpeakerNameChange}
                            />
                        ))}
                        {isTranscribing && (
                            <div className="p-8 rounded-2xl border-2 border-dashed border-claude-border animate-pulse flex justify-center">
                                <span className="text-xs uppercase font-bold text-gray-400 tracking-widest">Listening...</span>
                            </div>
                        )}
                    </>
                )}
            </div>
        </div>
    );
});
