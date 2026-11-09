/**
 * 配音预览：视频 + 每句配音 + 背景轨，按服务端的混音计划同步播放。
 *
 * 逻辑是从旧 `App.tsx` 的 `handleTimeUpdate` / `handleSeek` / `bedGainAt` **原样搬出来**的，
 * 只换了数据来源（新视图的 `Line`、`MixPlan`）。策略（速率、裁剪、垫底下压）只在后端
 * `mix_service`，这里只"求值"：预览与导出同源，所以不会出现“预览是一个样、成片是另一个样”。
 *
 * 一个新增的、对 AI 同步很关键的点：配音的 `audioUrl` 带 `?v=<指纹>`。AI 改了某句重新配音后，
 * URL 变了，旧的 `Audio` 对象会因为 URL 不一致而被丢掉重建 —— 不会继续播旧的那段。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Line, MixPlan, MixPlanSegment } from '../net/types';
import { setPreservesPitch } from '../utils/playbackSync';

const clamp01 = (v: number) => Math.max(0, Math.min(1, v));

/**
 * 垫底在 t 时刻的增益：对 plan.bed.duck 的分段线性断点插值 —— 与导出端的 np.interp 是
 * 同一条包络的同一形式。plan 还没到（首帧 / 仅字幕模式）返回 null，调用方自行退化。
 */
export function bedGainAt(plan: MixPlan | null, t: number): number | null {
  const bed = plan?.bed;
  if (!bed) return null;
  const duck = bed.duck ?? [];
  if (!duck.length) return bed.gain;
  if (t <= duck[0][0]) return bed.gain * duck[0][1];
  for (let i = 1; i < duck.length; i += 1) {
    if (t <= duck[i][0]) {
      const [t0, g0] = duck[i - 1];
      const [t1, g1] = duck[i];
      const f = t1 > t0 ? (t - t0) / (t1 - t0) : 1;
      return bed.gain * (g0 + (g1 - g0) * f);
    }
  }
  return bed.gain * duck[duck.length - 1][1];
}

interface Options {
  lines: Line[];
  plan: MixPlan | null;
  /** 有分离出的背景轨就用它（视频本身静音）；没有就直接用视频的原声并按包络压低。 */
  backgroundUrl: string | null;
  dubbing: boolean;
}

export function useMixPlayback({ lines, plan, backgroundUrl, dubbing }: Options) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const backgroundRef = useRef<HTMLAudioElement>(null);
  /** 正在播的配音：行 id → { audio, url }。url 用来发现“这一句被重新配音了”。 */
  const active = useRef<Map<string, { audio: HTMLAudioElement; url: string }>>(new Map());
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);

  const mixEntries = useMemo(
    () => new Map<string, MixPlanSegment>((plan?.segments ?? []).map((e) => [e.id, e])),
    [plan],
  );

  const stopAll = useCallback(() => {
    active.current.forEach(({ audio }) => audio.pause());
    active.current.clear();
  }, []);

  // 视频静音状态：有背景轨时视频本身永远静音
  useEffect(() => {
    if (videoRef.current && backgroundUrl) videoRef.current.muted = true;
  }, [backgroundUrl]);

  // 卸载时停掉所有声音
  useEffect(() => stopAll, [stopAll]);

  const onTimeUpdate = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    const time = video.currentTime;
    setCurrentTime(time);

    // 只跳转（点脚本里的 00:02 – 00:05、或点时间线）也会派发 timeupdate。
    // 视频没在播时绝不能启动配音，否则用户只是想定位，却听到那句 TTS 单独响起来。
    if (video.paused) {
      stopAll();
      return;
    }

    const bg = backgroundRef.current;
    const hasBg = !!bg && !!backgroundUrl;
    if (hasBg) video.muted = true;

    let anyDubPlaying = false;
    let targetVideoRate = 1.0;

    if (dubbing) {
      for (const line of lines) {
        const url = line.audioUrl;
        const current = active.current.get(line.id);

        // 这一句没有（新鲜的）配音了，或 URL 变了（被重新配音）：丢掉旧的播放对象
        if (current && current.url !== url) {
          current.audio.pause();
          active.current.delete(line.id);
        }
        if (!url) continue;

        const shouldPlay = time >= line.start && time < line.end;
        const existing = active.current.get(line.id);

        if (shouldPlay) {
          anyDubPlaying = true;
          // 速率来自 plan（mix_service 按合成时实测的时长算好）：预览与导出同一组数、
          // 同一个夹取界。plan 未到时保持 1.0 —— 不猜，只是这段预览暂时不拉伸。
          const entry = mixEntries.get(line.id);
          const rate = entry?.atempo ?? 1;
          if (entry && entry.videoRate !== 1) targetVideoRate = entry.videoRate;
          const slot = Math.max(0.001, line.end - line.start);
          // 进度对位按"变速后实际占用"（plan.played）：速率≠1 时按自然时长对位会漂移。
          const played = entry?.played ?? line.audioDuration ?? slot;
          const progress = Math.max(0, Math.min(1, (time - line.start) / slot));

          if (!existing) {
            try {
              const audio = new Audio(url);
              setPreservesPitch(audio);     // 拉伸不能改音高；不依赖浏览器默认值
              audio.playbackRate = rate;
              audio.currentTime = progress * played;
              audio.play().catch((e) => console.warn('Audio play blocked:', e));
              active.current.set(line.id, { audio, url });
            } catch (e) {
              console.error('Failed to start dub audio:', e);
            }
          } else {
            if (existing.audio.playbackRate !== rate) existing.audio.playbackRate = rate;
            // 暂停 → 再播放之后这一句的 audio 还在 map 里但处于 paused：必须重新 play()
            if (existing.audio.paused) {
              existing.audio.currentTime = progress * played;
              existing.audio.play().catch((e) => console.warn('Audio resume blocked:', e));
            }
          }
        } else if (existing) {
          existing.audio.pause();
          active.current.delete(line.id);
        }
      }
    }

    video.playbackRate = targetVideoRate;
    if (bg && hasBg) {
      if (bg.playbackRate !== targetVideoRate) bg.playbackRate = targetVideoRate;
      const gain = bedGainAt(plan, time);
      bg.volume = gain !== null ? clamp01(gain) : 1.0;
    }
    // 未分离：原声里有原语言人声，按 plan 包络压到台词之下（与导出同一条曲线）。
    // plan 缺席时的 0.1/1.0 是降级，只影响预览的那几秒，成片由服务端按 plan 出。
    if (!hasBg) {
      const gain = dubbing ? bedGainAt(plan, time) : null;
      video.volume = gain !== null ? clamp01(gain) : dubbing && anyDubPlaying ? 0.1 : 1.0;
    }
  }, [lines, plan, mixEntries, backgroundUrl, dubbing, stopAll]);

  const seek = useCallback((time: number) => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = time;
    stopAll();
    // 拖动后的第一帧也按包络给，否则松手瞬间会漏出满音量的原声
    video.volume = backgroundUrl ? 0 : clamp01(dubbing ? (bedGainAt(plan, time) ?? 1.0) : 1.0);
    if (backgroundRef.current) backgroundRef.current.currentTime = time;
    setCurrentTime(time);
  }, [backgroundUrl, plan, dubbing, stopAll]);

  const videoHandlers = {
    onTimeUpdate,
    onLoadedMetadata: (e: React.SyntheticEvent<HTMLVideoElement>) => setDuration(e.currentTarget.duration),
    onPause: () => {
      stopAll();
      backgroundRef.current?.pause();
    },
    onPlay: () => {
      backgroundRef.current?.play().catch(() => {});
    },
    onSeeked: () => {
      if (backgroundRef.current && videoRef.current) backgroundRef.current.currentTime = videoRef.current.currentTime;
    },
  };

  const play = useCallback((from?: number) => {
    const video = videoRef.current;
    if (!video) return;
    if (from !== undefined) seek(from);
    void video.play().catch(() => {});
  }, [seek]);

  return { videoRef, backgroundRef, currentTime, duration, mixEntries, seek, play, videoHandlers };
}
