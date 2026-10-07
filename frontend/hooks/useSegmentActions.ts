import { useCallback } from 'react';
import type { Dispatch, SetStateAction } from 'react';

import {
  setSegmentHidden,
  setSegmentMuted,
  synthesizeSpeech,
  translateScript,
} from '../services/apiService';
import type { TranscriptionSegment } from '../types';

/**
 * 逐行的四个动作：重译一行、重生成一行的配音、重配以贴合时隙、静音/隐藏。
 *
 * 从 `App.tsx` 搬出来的。这一段本来是自洽的：只读 `videoId / targetLanguage /
 * targetAccent / customPrompt / segments`，只写 `segments` 与日志 —— 留在那个
 * 3000 行的组件里没有任何好处，搬出来还让"这些动作依赖什么"一眼可见（就是下
 * 面这 7 个参数，没有别的隐藏依赖）。
 *
 * 都是**乐观更新 + 失败回滚**：先改界面再发请求。逐行操作是"扫一遍随手点"的
 * 高频动作，等一个来回再变色会很难用。
 */
interface Params {
  videoId: string;
  targetLanguage: string;
  targetAccent: string;
  segments: TranscriptionSegment[];
  setSegments: Dispatch<SetStateAction<TranscriptionSegment[]>>;
  setRawLog: Dispatch<SetStateAction<string>>;
}

export function useSegmentActions({
  videoId,
  targetLanguage,
  targetAccent,
  segments,
  setSegments,
  setRawLog,
}: Params) {

  const handleTranslateSegmentImpl = useCallback(async (id: string, textToTranslate?: string) => {
    // Find the latest segment data from state
    const segmentToTranslate = segments.find(s => s.id === id);
    if (!segmentToTranslate || !videoId) return null;

    // Use the latest text passed from the update handler or fallback to current state
    const text = textToTranslate || segmentToTranslate.originalText;
    if (!text) return null;

    setSegments(prev => prev.map(s => s.id === id ? { ...s, isTranslating: true } : s));

    try {
      const results = await translateScript(
        videoId,
        [{
          id: segmentToTranslate.id,
          text: text,
          speaker_id: segmentToTranslate.speakerId,
          start_time: segmentToTranslate.startTime,
          end_time: segmentToTranslate.endTime,
        }],
        targetLanguage
      );

      if (results.length > 0) {
        const translatedText = results[0].translated_text;
        setSegments(prev => prev.map(s => s.id === id ? { ...s, translatedText, isTranslating: false } : s));
        return translatedText;
      }
    } catch (e) {
      console.error("Translation failed:", e);
    } finally {
      setSegments(prev => prev.map(s => s.id === id ? { ...s, isTranslating: false } : s));
    }
    return null;
  }, [videoId, targetLanguage, segments]);

  const handleSynthesizeSegment = useCallback(async (id: string, textToSynthesize?: string) => {
    const targetSegment = segments.find(s => s.id === id);
    if (!targetSegment || !videoId) return;

    const text = textToSynthesize || targetSegment.translatedText;
    if (!text) return;

    setSegments(prev => prev.map(s => s.id === id ? { ...s, isSynthesizing: true } : s));

    try {
      // Always aim at this line's own time slot. Every synthesis path has to
      // fit, not just the batch pipeline: the playback clamp was tightened on
      // the assumption that the server keeps lines close to their slot, so an
      // unfitted re-synthesis here would be the one case that gets cut short.
      const result = await synthesizeSpeech(videoId, targetSegment.id, text, undefined, {
        targetDuration: Math.max(0.5, targetSegment.endTime - targetSegment.startTime),
        targetLanguage,
        // Without this, re-recording ONE line would drop it back to Mandarin
        // while the rest of the video stayed in the chosen dialect.
        accent: targetAccent,
      });
      // Cache-buster: the file at this URL has just been replaced, and without
      // it the browser serves the previous take and `actualDuration` never
      // updates. Clearing actualDuration forces `onLoadedMetadata` to re-measure.
      setSegments(prev => prev.map(s => s.id === id
        ? { ...s, audioUrl: `${result.audio_url}?v=${Date.now()}`, actualDuration: undefined, isSynthesizing: false }
        : s));
    } catch (e) {
      console.error("Synthesis failed:", e);
    } finally {
      setSegments(prev => prev.map(s => s.id === id ? { ...s, isSynthesizing: false } : s));
    }
  }, [videoId, segments, targetLanguage, targetAccent]);

  /**
   * Re-synthesize one line so it fits its own time slot.
   *
   * Delegates to `handleSynthesizeSegment`, which already aims every synthesis
   * at the slot — this exists for the clearer log line the "Refit" action next
   * to a Duration Mismatch badge produces.
   */
  const handleRefitSegment = useCallback(async (id: string) => {
    setRawLog(prev => prev + `Refitting ${id} to fit its time slot...\n`);
    await handleSynthesizeSegment(id);
  }, [handleSynthesizeSegment]);

  /**
   * 只重译这一行：「长一点 / 短一点」调该行的长度预算，自定义要求注入一句话
   * 的要求。译完立刻重新配音 —— 这两个选项要改的本来就是"这行占多长"，
   * 只换文字不换音频等于没生效。静音行保持静音，不自动补音频。
   */
  const handleRetranslateLine = useCallback(async (
    segmentId: string,
    opts: { lengthHint?: 'longer' | 'shorter'; customPrompt?: string },
  ) => {
    const seg = segments.find(s => s.id === segmentId);
    if (!videoId || !seg) return;
    setSegments(prev => prev.map(s => (s.id === segmentId ? { ...s, isTranslating: true } : s)));
    try {
      const results = await translateScript(
        videoId,
        [{
          id: seg.id,
          text: seg.originalText,
          speaker_id: seg.speakerId,
          start_time: seg.startTime,
          end_time: seg.endTime,
        }],
        targetLanguage,
        opts.customPrompt ?? '',
        opts.lengthHint,
      );
      const match = results.find(r => r.id === segmentId);
      const newText = match?.translated_text;
      if (!newText) throw new Error('没有拿到新的译文');
      setSegments(prev => prev.map(s => (
        s.id === segmentId
          ? { ...s, translatedText: newText, isTranslating: false, audioUrl: undefined }
          : s
      )));
      if (!seg.muted) {
        await handleSynthesizeSegment(segmentId, newText);
      }
    } catch (e) {
      setSegments(prev => prev.map(s => (s.id === segmentId ? { ...s, isTranslating: false } : s)));
      setRawLog(prev => prev + `\n重译失败: ${e instanceof Error ? e.message : 'unknown'}\n`);
    }
  }, [segments, videoId, targetLanguage, handleSynthesizeSegment]);

  const handleSegmentUpdate = useCallback((id: string, updates: Partial<TranscriptionSegment>) => {
    setSegments(prev => prev.map(s => {
      if (s.id === id) {
        // Clear audio URL if text is changing (will be re-synthesized)
        if ((updates.originalText !== undefined || updates.translatedText !== undefined) && s.audioUrl) {
          return { ...s, ...updates, audioUrl: undefined };
        }
        return { ...s, ...updates };
      }
      return s;
    }));

    // Auto-trigger Processing Chain
    if (updates.originalText !== undefined) {
      // Chain: ASR -> Translation -> TTS
      handleTranslateSegmentImpl(id, updates.originalText).then(newTranslation => {
        if (newTranslation) {
          handleSynthesizeSegment(id, newTranslation);
        }
      });
    } else if (updates.translatedText !== undefined) {
      // Chain: Translation -> TTS
      handleSynthesizeSegment(id, updates.translatedText);
    }
  }, [handleTranslateSegmentImpl, handleSynthesizeSegment]);

  /**
   * 隐藏/取消隐藏一行的字幕。
   *
   * 乐观更新（先改界面再发请求，失败回滚），与静音一致 —— 这是个高频的
   * "扫一遍随手点"的操作，等一个来回再变色会很难用。
   */
  const handleToggleHide = useCallback(async (segmentId: string, hidden: boolean) => {
    if (!videoId) return;
    setSegments(prev => prev.map(s => (s.id === segmentId ? { ...s, hidden } : s)));
    try {
      await setSegmentHidden(videoId, segmentId, hidden);
    } catch (e) {
      console.error('Failed to toggle hidden:', e);
      setSegments(prev => prev.map(s => (s.id === segmentId ? { ...s, hidden: !hidden } : s)));
    }
  }, [videoId]);

  const handleToggleMute = useCallback(async (segmentId: string, muted: boolean) => {
    if (!videoId) return;
    setSegments(prev => prev.map(s => (
      s.id === segmentId ? { ...s, muted, audioUrl: muted ? undefined : s.audioUrl } : s
    )));
    try {
      await setSegmentMuted(videoId, segmentId, muted);
    } catch (e) {
      // 失败就把本地状态拨回去，别让界面显示一个服务端没记住的状态。
      setSegments(prev => prev.map(s => (s.id === segmentId ? { ...s, muted: !muted } : s)));
      setRawLog(prev => prev + `\n静音失败: ${e instanceof Error ? e.message : 'unknown'}\n`);
    }
  }, [videoId]);

  return {
    handleTranslateSegmentImpl,
    handleSynthesizeSegment,
    handleRefitSegment,
    handleRetranslateLine,
    handleSegmentUpdate,
    handleToggleHide,
    handleToggleMute,
  };
}
