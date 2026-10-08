import React, { useState } from 'react';
import SeparationModeSelector from './SeparationModeSelector';
import type { SeparationMode, SeparationBackends } from '../services/apiService';

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
    cloneOffExtra?: React.ReactNode;
}

const ProcessingSettings: React.FC<ProcessingSettingsProps> = ({
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

    return (
        <div className="w-full space-y-3 text-left">
            <div className="py-2.5 px-3 bg-gray-50 rounded-xl">
                <div className="flex items-center mb-2">
                    <span className={`text-sm font-medium ${bgmSeparationLocked ? 'text-gray-400' : 'text-gray-700'}`}>BGM Separation</span>
                    <InfoTooltip text="Splits vocals from background music for cleaner transcription and voice cloning. Pick which backend does the work, or turn it off." />
                </div>
                <SeparationModeSelector
                    value={separationMode}
                    onChange={onSeparationModeChange}
                    backends={separationBackends}
                    locked={bgmSeparationLocked}
                />
            </div>

            {/* 多模态增强识别：omni 听原声 —— 纠正 ASR 错漏 + 标注语气标签，
                翻译基于纠错后的文本进行。与克隆正交，且先于克隆出现：
                它作用于转写与翻译，克隆只作用于声音。 */}
            <div className="flex items-center justify-between gap-3 py-1.5 px-3 bg-gray-50 rounded-xl">
                <div className="flex items-center">
                    <span className="text-sm font-medium text-gray-700">多模态增强识别</span>
                    <InfoTooltip text="纠正语音识别的错字漏字，并标注每句的语气（兴奋、耳语、大笑…），翻译基于纠错后的文本进行。会增加一些处理时间。" />
                </div>
                <Switch on={mmEnhance} onClick={() => onMmEnhanceChange(!mmEnhance)} />
            </div>

            {/* Voice Cloning 与它决定的内容同住一块灰底：开关、智能选材、
                音色池（或克隆说明/警告）不再分成三块各自带底色 —— 它们是
                同一个决策的开关与结果，视觉上就该是一体。内部以细分割线
                分隔，子行不再自带灰底。 */}
            <div className="px-3 py-1.5 bg-gray-50 rounded-xl">
                <div className="flex items-center justify-between gap-3 py-1.5">
                    <div className="flex items-center">
                        <span className="text-sm font-medium text-gray-700">Voice Cloning</span>
                        <InfoTooltip text="Clone the original speaker's voice for synthesis. The translated audio will sound like the original speaker. Requires more processing time." />
                    </div>
                    <Switch on={enableVoiceClone} onClick={() => onVoiceCloneChange(!enableVoiceClone)} />
                </div>

                {/* 智能选材：克隆的子选项，白底小卡表明从属。开关本身的说明
                    由 ⓘ 承担，不再重复一行文字。 */}
                {enableVoiceClone && (
                    <div className="mb-1.5 rounded-lg border border-gray-200/70 bg-white/80">
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

                {/* 克隆关闭时宿主想补的内容（落地页：音色池 / "该语言需要克隆"）。 */}
                {!enableVoiceClone && cloneOffExtra}
            </div>

            {/* 高级：自定义翻译要求。低频高级项的形态是折叠的一行 ——
                它改的是翻译模型的默认行为，绝大多数人不需要碰；展开后注入的是
                自然语言要求，所以不用开关、不用枚举，一个文本框就是全部。
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
    );
};

export default ProcessingSettings;
