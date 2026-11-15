/**
 * 时间线：波形 + 每一句的色块。
 *
 *   · 台词是说话人颜色的实心块；音乐 / 噪音 / 人声非语言是各自颜色的描边块，带图标。
 *     只有导出时真的会保留原声的人声非语言是实心的（它是唯一会出现在成片里的非台词）。
 *   · 同时说话的两句（时间重叠）分到不同的行里画，不叠在一起。
 *   · 鼠标指到任何一块：高亮它占的那段时间，浮出说话人 / 种类、时间、文字和状态。
 *
 * 布局和取色的规则在 store/timeline.ts（纯函数，有测试）；这里只管画。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { formatTime } from '../utils/helpers';
import {
  KIND_META, assignLanes, barColors, blockGeometry, colorOfSegment, formatSpan, isSpeech, minPxFor,
  segmentFlags, showIcon, type TimelineSegment, type TimelineSpeaker,
} from '../store/timeline';

interface TimelineProps {
  segments: TimelineSegment[];
  speakers: TimelineSpeaker[];
  /** 和脚本面板用同一个函数：同一个说话人在两边必须是同一种颜色。 */
  colorOf: (speakerId: string) => string;
  duration: number;
  currentTime: number;
  onSeek: (time: number) => void;
  waveform: number[];
  isLoading?: boolean;
}

const LANE_H = 14;       // 每一行的高度（px）
const LANE_GAP = 1;

/** 小图标（feather 风格，描边）。 */
const ICON_PATHS: Record<string, React.ReactNode> = {
  music: (
    <>
      <path d="M9 18V5l12-2v13" />
      <circle cx="6" cy="18" r="3" />
      <circle cx="18" cy="16" r="3" />
    </>
  ),
  noise: <path d="M9.6 4.6A2 2 0 1 1 11 8H2m10.6 11.4A2 2 0 1 0 14 16H2m15.7-8.3A2.5 2.5 0 1 1 19.5 12H2" />,
  vocal: (
    <>
      <circle cx="12" cy="12" r="10" />
      <path d="M8 14s1.5 2 4 2 4-2 4-2" />
      <path d="M9 9h.01M15 9h.01" />
    </>
  ),
};

export const KindIcon: React.FC<{ kind: string; size?: number; color?: string }> = ({ kind, size = 10, color }) => (
  <svg
    width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color ?? 'currentColor'}
    strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="shrink-0"
  >
    {ICON_PATHS[kind]}
  </svg>
);

interface Hover {
  id: string;
  x: number;
}

const Timeline: React.FC<TimelineProps> = ({
  segments, speakers, colorOf, duration, currentTime, onSeek, waveform, isLoading,
}) => {
  const timelineRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<Hover | null>(null);
  const [width, setWidth] = useState(0);

  // 量容器宽度：块的最小像素宽度、图标显示与否、tooltip 夹边都要用。
  useEffect(() => {
    const el = timelineRef.current;
    if (!el) return;
    setWidth(el.offsetWidth);
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => setWidth(Math.round(entries[0].contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const handleSeek = (e: React.MouseEvent<HTMLDivElement, MouseEvent>) => {
    if (!timelineRef.current || duration <= 0) return;
    const rect = timelineRef.current.getBoundingClientRect();
    onSeek(Math.max(0, Math.min(duration, ((e.clientX - rect.left) / rect.width) * duration)));
  };

  const speakerName = useMemo(() => new Map(speakers.map((s) => [s.id, s.name])), [speakers]);

  // 播放时父组件每帧更新 currentTime；下面这几项只依赖数据，不该跟着重算。
  const { lanes, count: laneCount } = useMemo(() => assignLanes(segments), [segments]);
  const colors = useMemo(() => barColors(waveform.length, duration, segments, colorOf), [waveform.length, duration, segments, colorOf]);
  const geometry = useMemo(
    () => segments.map((s) => blockGeometry(s.start, s.end, duration, width, minPxFor(s))),
    [segments, duration, width],
  );

  const laneAreaH = laneCount * (LANE_H + LANE_GAP) + LANE_GAP;
  const hovered = hover ? segments.findIndex((s) => s.id === hover.id) : -1;
  const hoveredSeg = hovered >= 0 ? segments[hovered] : null;

  const presentKinds = useMemo(
    () => (['music', 'noise', 'vocal'] as const).filter((k) => segments.some((s) => s.kind === k)),
    [segments],
  );

  const onEnter = (e: React.MouseEvent, id: string) => {
    if (!timelineRef.current) return;
    setHover({ id, x: e.clientX - timelineRef.current.getBoundingClientRect().left });
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
        className="relative w-full h-24 bg-gray-50 rounded-lg overflow-visible cursor-pointer border border-gray-100"
        onClick={handleSeek}
      >
        {/* 悬停高亮：这一句占的那段时间（重叠的两句也能一眼看出各自的范围） */}
        {hoveredSeg && duration > 0 && (
          <div
            className="absolute top-0 bottom-0 z-[5] pointer-events-none rounded-sm"
            style={{
              left: `${geometry[hovered].left}%`, width: `${geometry[hovered].width}%`,
              backgroundColor: colorOfSegment(hoveredSeg, colorOf), opacity: 0.12,
            }}
          />
        )}

        {/* 波形：只占色块行上面的那部分 */}
        <div className="absolute left-0 right-0 top-0 z-10 overflow-hidden rounded-t-lg" style={{ bottom: laneAreaH }}>
          {waveform.length > 0 ? waveform.map((peak, idx) => (
            <div
              key={idx}
              className="absolute w-[2px] rounded-full"
              style={{
                left: `${(idx / waveform.length) * 100}%`,
                top: '50%',
                height: `${Math.max(6, peak * 85)}%`,
                transform: 'translateY(-50%)',
                backgroundColor: colors[idx] ?? '#d1d5db',
                opacity: 0.5,
              }}
            />
          )) : (
            <div className="w-full h-full flex items-center justify-center">
              <div className="w-1/3 h-px bg-gray-200 animate-pulse"></div>
            </div>
          )}
        </div>

        {/* 色块：一行一行排，重叠的分开行 */}
        <div className="absolute left-0 right-0 bottom-0 z-20" style={{ height: laneAreaH }}>
          {duration > 0 && segments.map((seg, i) => {
            const g = geometry[i];
            const color = colorOfSegment(seg, colorOf);
            const speech = isSpeech(seg);
            const solid = speech || seg.kept;                 // 台词、以及会保留原声的人声非语言：实心
            const blockPx = (g.width / 100) * width;
            const isHover = hover?.id === seg.id;
            return (
              <div
                key={seg.id}
                role="img"
                aria-label={`${speech ? speakerName.get(seg.speakerId) ?? seg.speakerId : KIND_META[seg.kind as keyof typeof KIND_META]?.label} ${formatSpan(seg.start, seg.end)}`}
                className="absolute flex items-center justify-center overflow-hidden rounded-[3px] transition-[filter,box-shadow] duration-100"
                style={{
                  left: `${g.left}%`,
                  width: `${g.width}%`,
                  top: LANE_GAP + lanes[i] * (LANE_H + LANE_GAP),
                  height: LANE_H,
                  backgroundColor: solid ? color : `${color}26`,
                  border: solid ? 'none' : `1px solid ${color}`,
                  opacity: seg.muted ? 0.4 : solid ? 0.88 : 1,
                  color: solid ? '#fff' : color,
                  boxShadow: isHover ? `0 0 0 1.5px ${color}` : undefined,
                  filter: isHover ? 'brightness(1.08)' : undefined,
                  backgroundImage: seg.muted
                    ? 'repeating-linear-gradient(135deg, transparent 0 3px, rgba(255,255,255,0.65) 3px 5px)' : undefined,
                }}
                onMouseEnter={(e) => onEnter(e, seg.id)}
                onMouseMove={(e) => onEnter(e, seg.id)}
                onMouseLeave={() => setHover(null)}
              >
                {!speech && showIcon(blockPx) && <KindIcon kind={seg.kind} size={10} />}
              </div>
            );
          })}
        </div>

        {/* 播放头 */}
        {duration > 0 && (
          <div
            className="absolute top-0 h-full w-[1.5px] bg-claude-accent z-30 pointer-events-none"
            style={{ left: `${(currentTime / duration) * 100}%` }}
          >
            <div className="absolute -top-0.5 -left-[4px] w-[9px] h-[9px] bg-claude-accent rounded-full border-[1.5px] border-white shadow-sm"></div>
          </div>
        )}

        {/* Tooltip */}
        {hover && hoveredSeg && (
          <div
            className="absolute z-50 pointer-events-none"
            style={{
              left: `${Math.min(Math.max(hover.x, 100), Math.max(100, width - 100))}px`,
              bottom: '100%',
              transform: 'translateX(-50%)',
              marginBottom: '6px',
            }}
          >
            <div className="bg-gray-800 text-white text-[11px] rounded-lg px-3 py-2 shadow-lg whitespace-nowrap">
              <div className="flex items-center gap-1.5 mb-1">
                {isSpeech(hoveredSeg) ? (
                  <>
                    <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: colorOf(hoveredSeg.speakerId) }}></span>
                    <span className="font-medium">{speakerName.get(hoveredSeg.speakerId) ?? hoveredSeg.speakerId}</span>
                  </>
                ) : (
                  <>
                    <span style={{ color: colorOfSegment(hoveredSeg, colorOf) }}><KindIcon kind={hoveredSeg.kind} size={12} /></span>
                    <span className="font-medium">{KIND_META[hoveredSeg.kind as keyof typeof KIND_META]?.label}</span>
                  </>
                )}
              </div>
              <div className="text-gray-300 text-[10px] font-mono">
                {formatSpan(hoveredSeg.start, hoveredSeg.end)}
                <span className="text-gray-500"> · {(hoveredSeg.end - hoveredSeg.start).toFixed(1)}s</span>
              </div>
              {isSpeech(hoveredSeg) ? (
                <div className="text-gray-400 text-[10px] mt-0.5 max-w-[220px] truncate">
                  {/* 有译文就显示译文（用户看到的就是这句），否则回退原文 */}
                  {hoveredSeg.translation || hoveredSeg.text}
                </div>
              ) : (
                <div className="text-gray-400 text-[10px] mt-0.5 max-w-[240px] whitespace-normal">
                  {KIND_META[hoveredSeg.kind as keyof typeof KIND_META]?.hint}
                </div>
              )}
              {segmentFlags(hoveredSeg).length > 0 && (
                <div className="flex gap-1 mt-1 flex-wrap">
                  {segmentFlags(hoveredSeg).map((f) => (
                    <span key={f} className="px-1.5 py-px rounded bg-white/10 text-[9px] text-gray-200">{f}</span>
                  ))}
                </div>
              )}
              <div className="absolute left-1/2 -translate-x-1/2 top-full w-0 h-0 border-l-[5px] border-l-transparent border-r-[5px] border-r-transparent border-t-[5px] border-t-gray-800"></div>
            </div>
          </div>
        )}
      </div>

      {/* 图例：只列出这个工程里真有的种类 */}
      {presentKinds.length > 0 && (
        <div className="flex items-center gap-3 mt-2 text-[10px] text-gray-400">
          {presentKinds.map((k) => (
            <span key={k} className="flex items-center gap-1">
              <span style={{ color: KIND_META[k].color }}><KindIcon kind={k} size={10} /></span>
              {KIND_META[k].label}
            </span>
          ))}
        </div>
      )}
    </div>
  );
};

export default Timeline;
