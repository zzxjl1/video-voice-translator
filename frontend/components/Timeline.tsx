
import React, { useRef } from 'react';
import { TranscriptionSegment, Speaker } from '../types';
import { formatTime, getSpeakerColor } from '../utils/helpers';

interface TimelineProps {
  segments: TranscriptionSegment[];
  speakers: Speaker[];
  duration: number;
  currentTime: number;
  onSeek: (time: number) => void;
  waveform: number[];
  isLoading?: boolean;
}

const Timeline: React.FC<TimelineProps> = ({ segments, speakers, duration, currentTime, onSeek, waveform, isLoading }) => {
  const timelineRef = useRef<HTMLDivElement>(null);

  const handleSeek = (e: React.MouseEvent<HTMLDivElement, MouseEvent>) => {
    if (!timelineRef.current || duration <= 0) return;
    const timelineRect = timelineRef.current.getBoundingClientRect();
    const clickPosition = e.clientX - timelineRect.left;
    const seekTime = (clickPosition / timelineRect.width) * duration;
    onSeek(Math.max(0, Math.min(duration, seekTime)));
  };

  const speakerMap = new Map<string, Speaker>(speakers.map(s => [s.id, s]));

  return (
    <div className="p-5 bg-white border border-claude-border rounded-2xl shadow-sm select-none">
      <div className="flex justify-between text-[10px] font-mono text-gray-500 mb-3 uppercase tracking-widest">
        <span className="bg-gray-100 px-2 py-1 rounded-md text-gray-600 font-bold">{formatTime(currentTime)}</span>
        <span className="text-claude-accent font-bold flex items-center gap-2">
          {isLoading && <span className="w-1.5 h-1.5 bg-claude-accent rounded-full animate-pulse"></span>}
          Timeline
        </span>
        <span className="bg-gray-100 px-2 py-1 rounded-md text-gray-600 font-bold">{formatTime(duration)}</span>
      </div>

      <div
        ref={timelineRef}
        className="relative w-full h-28 bg-[#1a1a1a] rounded-xl overflow-hidden cursor-pointer shadow-inner group ring-1 ring-black/5"
        onClick={handleSeek}
      >
        {/* Waveform Overlay - Light blue on dark background looks pro */}
        <div className="absolute inset-0 flex items-center justify-around px-1 gap-[1px] z-10 opacity-40">
          {waveform.length > 0 ? waveform.map((peak, idx) => (
            <div
              key={idx}
              className="w-1 bg-white/80 rounded-full transition-all"
              style={{ height: `${Math.max(4, peak * 80)}%` }}
            />
          )) : (
            <div className="w-full h-full flex items-center justify-center">
              <div className="w-1/2 h-0.5 bg-gray-700 animate-pulse rounded-full"></div>
            </div>
          )}
        </div>

        {/* Segments Overlay */}
        <div className="absolute inset-0 z-20">
          {duration > 0 && segments.map(segment => {
            const speaker = speakerMap.get(segment.speakerId);
            const left = (segment.startTime / duration) * 100;
            const width = ((segment.endTime - segment.startTime) / duration) * 100;
            const isActive = currentTime >= segment.startTime && currentTime <= segment.endTime;
            const hasTranslation = !!segment.translatedText;
            const hasAudio = !!segment.audioUrl;

            return (
              <div
                key={segment.id}
                className={`absolute h-full border-l border-white/10 transition-all duration-300 group/segment overflow-hidden ${isActive ? 'bg-opacity-80 z-10' : 'bg-opacity-40 hover:bg-opacity-60'}`}
                style={{
                  left: `${left}%`,
                  width: `${width}%`,
                  backgroundColor: getSpeakerColor(segment.speakerId),
                }}
                title={`${speaker?.name}: ${segment.originalText.substring(0, 20)}...`}
              >
                <div className="absolute bottom-0 left-0 right-0 h-1.5 pointer-events-none">
                  {hasTranslation && (
                    <div className="absolute inset-x-0 bottom-0 h-full bg-white/30" title="Translated"></div>
                  )}
                  {hasAudio && (
                    <div className="absolute inset-x-0 bottom-0 h-full bg-claude-accent shadow-[0_0_5px_rgba(218,119,86,0.8)]" title="Audio Ready"></div>
                  )}
                </div>

                {/* Active Indicator */}
                {isActive && (
                  <div className="absolute top-0 inset-x-0 h-0.5 bg-white shadow-[0_0_10px_white]"></div>
                )}
              </div>
            );
          })}
        </div>

        {/* Playhead */}
        {duration > 0 && (
          <div
            className="absolute top-0 h-full w-[2px] bg-claude-accent z-30 shadow-[0_0_10px_rgba(218,119,86,0.5)] pointer-events-none"
            style={{ left: `${(currentTime / duration) * 100}%` }}
          >
            <div className="absolute -top-1 -left-[5px] w-3 h-3 bg-claude-accent rounded-full border-2 border-white shadow-sm"></div>
          </div>
        )}
      </div>

      {/* Legend */}
      <div className="mt-4 flex gap-6 justify-end text-[10px] text-gray-500 font-bold uppercase tracking-wider">
        <div className="flex items-center gap-2">
          <div className="w-2.5 h-2.5 bg-gray-300 rounded-sm"></div>
          <span>Translation Ready</span>
        </div>
        <div className="flex items-center gap-2">
          <div className="w-2.5 h-2.5 bg-claude-accent rounded-sm"></div>
          <span>Audio Generated</span>
        </div>
      </div>
    </div>
  );
};

export default Timeline;
