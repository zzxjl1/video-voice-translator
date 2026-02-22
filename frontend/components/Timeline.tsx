
import React, { useRef, useState } from 'react';
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

interface TooltipInfo {
  x: number;
  y: number;
  segment: TranscriptionSegment;
  speakerName: string;
  color: string;
}

const Timeline: React.FC<TimelineProps> = ({ segments, speakers, duration, currentTime, onSeek, waveform, isLoading }) => {
  const timelineRef = useRef<HTMLDivElement>(null);
  const [tooltip, setTooltip] = useState<TooltipInfo | null>(null);

  const handleSeek = (e: React.MouseEvent<HTMLDivElement, MouseEvent>) => {
    if (!timelineRef.current || duration <= 0) return;
    const timelineRect = timelineRef.current.getBoundingClientRect();
    const clickPosition = e.clientX - timelineRect.left;
    const seekTime = (clickPosition / timelineRect.width) * duration;
    onSeek(Math.max(0, Math.min(duration, seekTime)));
  };

  const speakerMap = new Map<string, Speaker>(speakers.map(s => [s.id, s]));

  const handleSegmentHover = (e: React.MouseEvent, segment: TranscriptionSegment) => {
    if (!timelineRef.current) return;
    const rect = timelineRef.current.getBoundingClientRect();
    const speaker = speakerMap.get(segment.speakerId);
    setTooltip({
      x: e.clientX - rect.left,
      y: -8,
      segment,
      speakerName: speaker?.name || segment.speakerId,
      color: getSpeakerColor(segment.speakerId),
    });
  };

  return (
    <div className="p-4 bg-white border border-claude-border rounded-2xl shadow-sm select-none">
      <div className="flex justify-between text-[10px] font-mono text-gray-400 mb-2 uppercase tracking-widest">
        <span className="tabular-nums">{formatTime(currentTime)}</span>
        <span className="text-gray-300 font-medium flex items-center gap-1.5">
          {isLoading && <span className="w-1 h-1 bg-claude-accent rounded-full animate-pulse"></span>}
          Timeline
        </span>
        <span className="tabular-nums">{formatTime(duration)}</span>
      </div>

      <div
        ref={timelineRef}
        className="relative w-full h-20 bg-gray-50 rounded-lg overflow-visible cursor-pointer border border-gray-100"
        onClick={handleSeek}
      >
        {/* Waveform bars */}
        <div className="absolute inset-0 flex items-center justify-around px-0.5 gap-[1px] z-10 overflow-hidden rounded-lg">
          {waveform.length > 0 ? waveform.map((peak, idx) => {
            const progress = idx / waveform.length;
            const time = progress * duration;
            const activeSeg = segments.find(s => time >= s.startTime && time <= s.endTime);
            const color = activeSeg ? getSpeakerColor(activeSeg.speakerId) : '#d1d5db';
            return (
              <div
                key={idx}
                className="w-[2px] rounded-full shrink-0"
                style={{
                  height: `${Math.max(6, peak * 75)}%`,
                  backgroundColor: color,
                  opacity: 0.5,
                }}
              />
            );
          }) : (
            <div className="w-full h-full flex items-center justify-center">
              <div className="w-1/3 h-px bg-gray-200 animate-pulse"></div>
            </div>
          )}
        </div>

        {/* Segment bottom markers */}
        <div className="absolute bottom-0 left-0 right-0 h-[3px] z-20 overflow-hidden rounded-b-lg">
          {duration > 0 && segments.map(segment => {
            const left = (segment.startTime / duration) * 100;
            const width = ((segment.endTime - segment.startTime) / duration) * 100;
            return (
              <div
                key={segment.id}
                className="absolute h-full"
                style={{
                  left: `${left}%`,
                  width: `${width}%`,
                  backgroundColor: getSpeakerColor(segment.speakerId),
                  opacity: 0.7,
                }}
                onMouseEnter={(e) => handleSegmentHover(e, segment)}
                onMouseMove={(e) => handleSegmentHover(e, segment)}
                onMouseLeave={() => setTooltip(null)}
              />
            );
          })}
        </div>

        {/* Playhead */}
        {duration > 0 && (
          <div
            className="absolute top-0 h-full w-[1.5px] bg-claude-accent z-30 pointer-events-none"
            style={{ left: `${(currentTime / duration) * 100}%` }}
          >
            <div className="absolute -top-0.5 -left-[4px] w-[9px] h-[9px] bg-claude-accent rounded-full border-[1.5px] border-white shadow-sm"></div>
          </div>
        )}

        {/* Tooltip */}
        {tooltip && (
          <div
            className="absolute z-50 pointer-events-none"
            style={{
              left: `${Math.min(Math.max(tooltip.x, 80), timelineRef.current ? timelineRef.current.offsetWidth - 80 : tooltip.x)}px`,
              bottom: '100%',
              transform: 'translateX(-50%)',
              marginBottom: '6px',
            }}
          >
            <div className="bg-gray-800 text-white text-[11px] rounded-lg px-3 py-2 shadow-lg whitespace-nowrap">
              <div className="flex items-center gap-1.5 mb-1">
                <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: tooltip.color }}></span>
                <span className="font-medium">{tooltip.speakerName}</span>
              </div>
              <div className="text-gray-300 text-[10px] font-mono">
                {formatTime(tooltip.segment.startTime)} – {formatTime(tooltip.segment.endTime)}
              </div>
              <div className="text-gray-400 text-[10px] mt-0.5 max-w-[200px] truncate">
                {tooltip.segment.text}
              </div>
              <div className="absolute left-1/2 -translate-x-1/2 top-full w-0 h-0 border-l-[5px] border-l-transparent border-r-[5px] border-r-transparent border-t-[5px] border-t-gray-800"></div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default Timeline;
