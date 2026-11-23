import React, { useEffect, useState } from 'react';
import SeparationModeSelector from './SeparationModeSelector';
import type { DubbingMode, SeparationMode, SeparationBackends } from '../net/types';
import type { Voice } from '@diffusionstudio/vits-web';
import {
  browserVoiceLabel,
  defaultVoiceForLanguage,
  voicesForLanguage,
} from '../features/browserTts';

/*
 * 「处理设置」这块开关的**唯一**定义。
 *
 * 为什么抽出来：落地页（VideoUpload）与工程内的 Processing Settings 弹窗
 * （SettingsModal）讲的是同一件事 —— 这些开关决定管线怎么做、改了就必须重跑。
 * 原先两处各写一份，必然是"哪边加了、哪边忘了"：
 *   弹窗只有 BGM 分离 + 声音复刻，**缺多模态增强识别、智能选材、自定义翻译要求**，
 *   而 App 的 `differFromProject` 与"保存并重做"早就把这五项都算作会改变结果的
 *   设置 —— 于是用户在工程里根本改不了那三项（只能回落地页重开一个工程）。
 *
 * 所以这里是一份实现，两个宿主：
 *   · 落地页：额外用 `cloneOffExtra` 塞进「预制音色池 / 该语言必须克隆」的提示
 *     （那是"还没开工、正在挑音色"才有的东西，弹窗里没有这回事）。
 *   · 弹窗：不传 `cloneOffExtra`，其余完全一致。
 */

/** 一个圈起来的 i。两个宿主共用这一份（原先各自定义了一份）。 */
export const InfoTooltip: React.FC<{ text: string }> = ({ text }) => {
    const [show, setShow] = useState(false);
    return (
        <span className="relative inline-flex items-center ml-1.5">
            <span
                className="w-4 h-4 rounded-full border border-gray-300 text-gray-400 text-[10px] font-bold flex items-center justify-center cursor-help hover:border-claude-accent hover:text-claude-accent transition-colors"
                onMouseEnter={() => setShow(true)}
                onMouseLeave={() => setShow(false)}
            >
                i
            </span>
            {show && (
                <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 z-50 pointer-events-none">
                    <div className="bg-gray-800 text-white text-[11px] leading-relaxed rounded-lg px-3 py-2 shadow-lg whitespace-normal w-52 text-center">
                        {text}
                        <div className="absolute left-1/2 -translate-x-1/2 top-full w-0 h-0 border-l-[5px] border-l-transparent border-r-[5px] border-r-transparent border-t-[5px] border-t-gray-800"></div>
                    </div>
                </div>
            )}
        </span>
    );
};

/**
 * 开关本体。这里四个开关形状完全一样，之前是四段复制的 class 串 —— 尺寸差一个
 * 像素都看不出，但改一次要改四处。
 */
const Switch: React.FC<{ on: boolean; onClick: () => void }> = ({ on, onClick }) => (
    <button
        type="button"
        role="switch"
        aria-checked={on}
        onClick={onClick}
        className={`relative w-10 h-[22px] shrink-0 rounded-full transition-colors duration-200 ${on ? 'bg-claude-accent' : 'bg-gray-300'}`}
    >
        <span
            className={`absolute top-[2px] left-[2px] w-[18px] h-[18px] bg-white rounded-full shadow transition-transform duration-200 ${on ? 'translate-x-[18px]' : ''}`}
        />
    </button>
);

export interface ProcessingSettingsProps {
    targetLanguage: string;
    dubbingMode: DubbingMode;
    onDubbingModeChange: (mode: DubbingMode) => void;
    browserVoice: string | null;
    onBrowserVoiceChange: (voice: string) => void;

    separationMode: SeparationMode;
    onSeparationModeChange: (mode: SeparationMode) => void;
    separationBackends: SeparationBackends;
    /** 克隆开着时分离被锁（克隆质量依赖人声轨）—— 判定在调用方，见 App.handleSeparationModeChange。 */
    bgmSeparationLocked?: boolean;

    /** 多模态增强识别：omni 听原声纠错 + 标注语气标签。 */
    mmEnhance: boolean;
    onMmEnhanceChange: (v: boolean) => void;

    enableVoiceClone: boolean;
    onVoiceCloneChange: (v: boolean) => void;
    /** 智能选材：克隆的子选项（依赖克隆开关）。 */
    cloneSmartPick: boolean;
    onCloneSmartPickChange: (v: boolean) => void;

    /** 注入翻译模型的自定义要求；空串 = 标准行为。 */
    customPrompt: string;
    onCustomPromptChange: (v: string) => void;

    /**
     * 克隆**关闭**时，克隆卡片里的附加内容。落地页塞「预制音色池 / 该语言必须
     * 开启克隆」的提示；弹窗不需要（工程里音色已经定了）。
     */
    /**
     * 「生成配音」总开关。不传 = 该宿主不渲染这张卡（本轮只在落地页提供，
     * 工程内的弹窗随后接）。关 = 仅字幕模式：跳过分离/克隆/合成（省大头
     * API 费），导出时原声比特级直通，只加翻译字幕。
     */
    cloneOffExtra?: React.ReactNode;
}

const ProcessingSettings: React.FC<ProcessingSettingsProps> = ({
    targetLanguage,
    dubbingMode,
    onDubbingModeChange,
    browserVoice,
    onBrowserVoiceChange,
    separationMode,
    onSeparationModeChange,
    separationBackends,
    bgmSeparationLocked,
    mmEnhance,
    onMmEnhanceChange,
    enableVoiceClone,
    onVoiceCloneChange,
    cloneSmartPick,
    onCloneSmartPickChange,
    customPrompt,
    onCustomPromptChange,
    cloneOffExtra,
}) => {
    /*
     * 高级项（自定义翻译要求）默认折叠：低频项不能常驻占高度。折叠状态属于
     * 这一份实例 —— 落地页与弹窗各自记忆，互不影响。
     */
    const [showAdvanced, setShowAdvanced] = useState(false);
    const [browserVoices, setBrowserVoices] = useState<Voice[]>([]);
    const [voicesLoading, setVoicesLoading] = useState(false);
    const [voicesError, setVoicesError] = useState<string | null>(null);

    const dubbingOn = dubbingMode !== 'off';

    useEffect(() => {
        if (!targetLanguage) return;
        let alive = true;
        setVoicesLoading(true);
        setVoicesError(null);
        voicesForLanguage(targetLanguage)
            .then((vs) => alive && setBrowserVoices(vs))
            .catch((e) => {
                if (!alive) return;
                setBrowserVoices([]);
                setVoicesError(e instanceof Error ? e.message : String(e));
            })
            .finally(() => alive && setVoicesLoading(false));
        return () => { alive = false; };
    }, [targetLanguage]);

    useEffect(() => {
        if (dubbingMode !== 'browser' || !browserVoices.length) return;
        if (browserVoice && browserVoices.some((v) => v.key === browserVoice)) return;
        onBrowserVoiceChange(defaultVoiceForLanguage(targetLanguage) ?? browserVoices[0].key);
    }, [dubbingMode, browserVoices, browserVoice, targetLanguage, onBrowserVoiceChange]);

    const browserAvailable = browserVoices.length > 0;
    const browserReason = voicesLoading
        ? '正在读取本地音色…'
        : voicesError
            ? `无法读取本地音色：${voicesError}`
            : browserAvailable
                ? null
                : `${targetLanguage} 暂无本地 Piper 音色`;

    return (
        <div className="w-full space-y-4 text-left">
            {/* ───────────────────────── 配音 ─────────────────────────
                配音方式三选一：远端 / 浏览器 Piper / 关闭。
                Off = 仅字幕：原声原样保留，不分离、不克隆、不合成。 */}
            <section>
                <header className="flex items-center justify-between px-3 pb-2">
                    <div className="flex items-center">
                        <span className="text-[11px] font-bold uppercase tracking-wider text-gray-400">配音</span>
                        <InfoTooltip text="Remote：远端 Qwen TTS，支持克隆和预制音色，按量计费；In-browser：本地 Piper，免费但不克隆；Off：只做转写/翻译/字幕，原声保持不变。" />
                    </div>
                </header>

                <div className={`px-3 py-2 rounded-xl border transition-colors ${dubbingOn ? 'bg-gray-50 border-transparent' : 'bg-amber-50/60 border-amber-200/70'}`}>
                    <div className="grid grid-cols-3 gap-1.5">
                        {([
                            { mode: 'remote' as const, label: 'Remote', hint: '远端 Qwen TTS，支持克隆和预制音色，按量计费' },
                            { mode: 'browser' as const, label: 'In-browser', hint: browserReason ?? '本地 Piper，免费，不克隆' },
                            { mode: 'off' as const, label: 'Off', hint: '只做转写/翻译/字幕，原声保持不变' },
                        ]).map(({ mode, label, hint }) => {
                            const disabled = mode === 'browser' && !browserAvailable;
                            const selected = dubbingMode === mode;
                            return (
                                <button
                                    key={mode}
                                    type="button"
                                    disabled={disabled}
                                    title={hint}
                                    onClick={() => !disabled && onDubbingModeChange(mode)}
                                    className={[
                                        'relative py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-200 border',
                                        selected
                                            ? 'bg-claude-accent text-white border-claude-accent shadow-sm shadow-claude-accent/25'
                                            : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50 hover:text-gray-800',
                                        disabled ? 'opacity-40 cursor-not-allowed hover:bg-white' : 'cursor-pointer',
                                    ].join(' ')}
                                >
                                    {label}
                                    {mode === 'browser' && (
                                        <span className="absolute -top-1.5 right-1 rounded-full bg-emerald-500 px-1.5 py-px text-[9px] font-bold leading-tight text-white shadow-sm">
                                            FREE
                                        </span>
                                    )}
                                </button>
                            );
                        })}
                    </div>

                    <p className="mt-2 text-[10px] leading-relaxed text-gray-400">
                        {dubbingMode === 'remote'
                            ? '远端合成配音，导出时替换原声；可开启声音克隆，按量计费。'
                            : dubbingMode === 'browser'
                                ? (browserReason ?? '本地 Piper 合成配音，免费；不支持声音克隆。')
                                : '仅翻译字幕：原声原样保留，不分离、不克隆、不合成。'}
                    </p>

                    {dubbingOn && (
                        <>
                            {/* BGM 分离：配音的子选项。白底小卡表明从属。 */}
                            <div className="mt-2 rounded-lg border border-gray-200/70 bg-white/80 px-2.5 py-2">
                                <div className="flex items-center mb-1.5">
                                    <span className="text-xs font-medium text-gray-700">BGM 分离</span>
                                    <InfoTooltip text="In-browser: splits vocals from background music locally; the first run downloads a 64 MB model. Off skips the download." />
                                </div>
                                <SeparationModeSelector
                                    value={separationMode}
                                    onChange={onSeparationModeChange}
                                    backends={separationBackends}
                                    locked={bgmSeparationLocked}
                                />
                            </div>

                            {dubbingMode === 'remote' ? (
                                /* 远端：声音克隆 / 智能选材 / 预制音色。 */
                                <div className="mt-2 rounded-lg border border-gray-200/70 bg-white/80">
                                    <div className="flex items-center justify-between gap-3 py-1.5 px-2.5">
                                        <div className="flex items-center">
                                            <span className="text-xs font-medium text-gray-700">声音克隆</span>
                                            <InfoTooltip text="Clones the original speaker's voice using the remote TTS. Requires BGM separation; turn it off to use built-in voices." />
                                        </div>
                                        <Switch on={enableVoiceClone} onClick={() => onVoiceCloneChange(!enableVoiceClone)} />
                                    </div>

                                    {enableVoiceClone && (
                                        <div className="mx-2.5 mb-1.5 rounded-lg border border-gray-200/70 bg-gray-50/80">
                                            <div className="flex items-center justify-between gap-3 py-1.5 px-2.5">
                                                <span className="text-xs font-medium text-gray-700">智能选材</span>
                                                <Switch
                                                    on={cloneSmartPick}
                                                    onClick={() => onCloneSmartPickChange(!cloneSmartPick)}
                                                />
                                            </div>
                                            <p className="px-2.5 pb-2 text-[10px] leading-relaxed text-gray-400">
                                                AI 试听候选片段，挑出无重叠、情绪中性的部分做克隆参考素材；每轮校验，不过关自动重选。
                                            </p>
                                        </div>
                                    )}

                                    {!enableVoiceClone && cloneOffExtra}
                                </div>
                            ) : dubbingMode === 'browser' ? (
                                /* 浏览器：只列 Piper 预制音色；没有克隆/智能选材。 */
                                <div className="mt-2 rounded-lg border border-gray-200/70 bg-white/80 px-2.5 py-2">
                                    <div className="mb-1.5 flex items-center">
                                        <span className="text-xs font-medium text-gray-700">
                                            预制音色 · {targetLanguage}
                                        </span>
                                        <span className="ml-auto rounded-full bg-emerald-50 px-1.5 py-px text-[9px] font-bold text-emerald-600">
                                            FREE
                                        </span>
                                    </div>
                                    <select
                                        value={browserVoice ?? ''}
                                        disabled={!browserAvailable}
                                        onChange={(e) => onBrowserVoiceChange(e.target.value)}
                                        className="w-full rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-700 disabled:cursor-not-allowed disabled:bg-gray-50 disabled:text-gray-400 focus:outline-none focus:border-claude-accent/50"
                                    >
                                        {!browserAvailable && <option value="">{browserReason ?? '暂无本地音色'}</option>}
                                        {browserVoices.map((v) => (
                                            <option key={v.key} value={v.key}>
                                                {browserVoiceLabel(v)}
                                            </option>
                                        ))}
                                    </select>
                                    <p className="mt-1 text-[10px] leading-relaxed text-gray-400">
                                        首次使用会下载该 voice 模型并缓存在浏览器；不克隆原说话人。
                                    </p>
                                </div>
                            ) : null}
                        </>
                    )}
                </div>
            </section>

            {/* ───────────────────────── 翻译 ─────────────────────────
                多模态增强与自定义要求都作用于转写与翻译 —— 与"要不要配音"
                正交，仅字幕模式下照样有意义，所以自成一组。 */}
            <section>
                <header className="px-3 pb-2">
                    <span className="text-[11px] font-bold uppercase tracking-wider text-gray-400">翻译</span>
                </header>

                <div className="space-y-3">
                    <div className="flex items-center justify-between gap-3 py-1.5 px-3 bg-gray-50 rounded-xl">
                        <div className="flex items-center">
                            <span className="text-sm font-medium text-gray-700">多模态增强识别</span>
                            <InfoTooltip text="默认开启：纠正语音识别的错字漏字，并标注每句的语气（兴奋、耳语、大笑…），翻译基于纠错后的文本进行。会增加一些处理时间；不需要可以关掉。" />
                        </div>
                        <Switch on={mmEnhance} onClick={() => onMmEnhanceChange(!mmEnhance)} />
                    </div>

                    {/* 高级：自定义翻译要求。低频高级项的形态是折叠的一行 ——
                        它改的是翻译模型的默认行为，绝大多数人不需要碰；展开后注入
                        的是自然语言要求，所以不用开关、不用枚举，一个文本框就是全部。
                        空串在后端完全不注入，默认行为零改动。
                        无边框、py-1.5：这一行是页面上最低优先级的可点物，视觉上
                        必须比开关行轻。 */}
                    <div>
                        <button
                            onClick={() => setShowAdvanced(v => !v)}
                            className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left cursor-pointer"
                        >
                            <svg
                                className={`h-3 w-3 text-gray-400 transition-transform duration-200 ${showAdvanced ? 'rotate-90' : ''}`}
                                fill="none" viewBox="0 0 24 24" strokeWidth={2.5} stroke="currentColor"
                            >
                                <path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" />
                            </svg>
                            <span className="text-[11px] font-bold uppercase tracking-wider text-gray-500">
                                高级 · 自定义翻译要求
                            </span>
                            {customPrompt.trim() && (
                                <span className="px-1.5 py-px rounded-full bg-claude-accent/10 text-[10px] font-bold text-claude-accent">
                                    已设置
                                </span>
                            )}
                        </button>
                        {showAdvanced && (
                            <div className="px-3 pb-3">
                                <textarea
                                    value={customPrompt}
                                    onChange={e => onCustomPromptChange(e.target.value.slice(0, 500))}
                                    rows={3}
                                    placeholder={'例：专有名词保留英文原文；语气调整为正式，面向企业客户…'}
                                    className="w-full resize-none rounded-lg border border-gray-200 bg-white px-2.5 py-2 text-xs leading-relaxed text-gray-700 placeholder:text-gray-300 focus:outline-none focus:border-claude-accent/50"
                                />
                                <div className="mt-1 flex items-baseline justify-between text-[10px] text-gray-400">
                                    <span>注入翻译模型，可覆盖默认行为；留空 = 标准翻译。</span>
                                    <span className="shrink-0 tabular-nums">{customPrompt.length}/500</span>
                                </div>
                            </div>
                        )}
                    </div>
                </div>
            </section>
        </div>
    );
};

export default ProcessingSettings;
