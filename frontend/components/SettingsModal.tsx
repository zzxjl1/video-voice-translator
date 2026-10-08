import React, { useEffect, useState } from 'react';
import ProcessingSettings from './ProcessingSettings';
import type { SeparationMode, SeparationBackends } from '../services/apiService';

interface SettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
    /**
     * 有改动时的「保存」：调用方负责触发重新处理。
     * 这些设置都改变管线的做法（分离后端 / 是否克隆 / 是否多模态增强 / 智能选材 /
     * 翻译要求），改了就必须重跑，光更新前端状态是不够的。
     *
     * 返回值表示用户在最外层确认里是否点了「继续」：false 代表放弃 ——
     * 弹窗会把设置撤回打开时的快照并留在原地，不能让「取消」留下一个
     * 已改设置但没重跑的状态。
     */
    onSave?: () => boolean | Promise<boolean | void> | void;

    separationMode: SeparationMode;
    onSeparationModeChange: (mode: SeparationMode) => void;
    separationBackends: SeparationBackends;
    bgmSeparationLocked?: boolean;

    enableVoiceClone: boolean;
    onVoiceCloneChange: (v: boolean) => void;
    /** 智能选材（克隆的子选项）。 */
    cloneSmartPick: boolean;
    onCloneSmartPickChange: (v: boolean) => void;

    /** 多模态增强识别。 */
    mmEnhance: boolean;
    onMmEnhanceChange: (v: boolean) => void;

    /** 自定义翻译要求。 */
    customPrompt: string;
    onCustomPromptChange: (v: string) => void;
}

/**
 * 打开那一刻的设置值，用来判断「有没有改动」。
 *
 * 快照只在打开时拍（deps 只有 isOpen）：改了之后再快照就没有比较基准了。
 * 这些设置都是即时生效的（一改就写进父状态），所以这里的 dirty 只用于决定
 * 「保存」是否点亮与是否触发重新处理，不是暂存草稿。
 *
 * **必须覆盖弹窗里的每一项**：少一项的表现是"改了那一项，保存按钮却是灰的"，
 * 用户于是以为它已经生效（实际上既没保存也没重跑）。这也是把这块 UI 收敛成
 * 一个共享组件（ProcessingSettings）的原因之一 —— 列表只有一个定义。
 */
interface Snapshot {
    separationMode: SeparationMode;
    enableVoiceClone: boolean;
    cloneSmartPick: boolean;
    mmEnhance: boolean;
    customPrompt: string;
}

const SettingsModal: React.FC<SettingsModalProps> = ({
    isOpen,
    onClose,
    onSave,
    separationMode,
    onSeparationModeChange,
    separationBackends,
    bgmSeparationLocked,
    enableVoiceClone,
    onVoiceCloneChange,
    cloneSmartPick,
    onCloneSmartPickChange,
    mmEnhance,
    onMmEnhanceChange,
    customPrompt,
    onCustomPromptChange,
}) => {
    const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
    useEffect(() => {
        if (isOpen) {
            setSnapshot({
                separationMode,
                enableVoiceClone,
                cloneSmartPick,
                mmEnhance,
                customPrompt,
            });
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [isOpen]);

    if (!isOpen) return null;

    const dirty =
        !!snapshot &&
        (separationMode !== snapshot.separationMode ||
            enableVoiceClone !== snapshot.enableVoiceClone ||
            cloneSmartPick !== snapshot.cloneSmartPick ||
            mmEnhance !== snapshot.mmEnhance ||
            customPrompt !== snapshot.customPrompt);

    /**
     * 放弃本次改动：把所有设置拨回打开时的值。
     *
     * 这些开关是即时生效的（一拨就写进父状态），所以「取消」必须显式回滚 ——
     * 否则会留下「设置显示为新值、配音仍是旧值」的状态，正是保存机制要防的
     * 那种不一致。
     *
     * **顺序：先克隆、后分离方式。** App 里克隆开着时 `handleSeparationModeChange('off')`
     * 会被挡掉，所以"先恢复分离方式"在快照是 Off、而用户刚打开克隆的那种组合下
     * 会被静默丢弃 —— 取消之后分离停在 In-browser，回不到原样。
     */
    const revertToSnapshot = () => {
        if (!snapshot) return;
        if (enableVoiceClone !== snapshot.enableVoiceClone) {
            onVoiceCloneChange(snapshot.enableVoiceClone);
        }
        if (separationMode !== snapshot.separationMode) {
            onSeparationModeChange(snapshot.separationMode);
        }
        if (cloneSmartPick !== snapshot.cloneSmartPick) {
            onCloneSmartPickChange(snapshot.cloneSmartPick);
        }
        if (mmEnhance !== snapshot.mmEnhance) onMmEnhanceChange(snapshot.mmEnhance);
        if (customPrompt !== snapshot.customPrompt) {
            onCustomPromptChange(snapshot.customPrompt);
        }
    };

    /** 保存：先问外层要不要真跑（确认对话框在那边），被拒则回滚并留在弹窗。 */
    const handleSave = async () => {
        if (!dirty) return;
        const accepted = await onSave?.();
        if (accepted === false) {
            revertToSnapshot();
            return;
        }
        onClose();
    };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-claude-text/40 backdrop-blur-sm p-4">
            <div className="bg-white border border-gray-200 rounded-2xl shadow-2xl w-full max-w-md overflow-hidden">
                <div className="px-8 py-6 border-b border-gray-100 flex justify-between items-center bg-claude-bg">
                    <h2 className="text-xl font-serif font-bold text-gray-800 flex items-center gap-3">
                        <span className="w-8 h-8 rounded-lg bg-claude-paper text-claude-accent flex items-center justify-center">
                            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="w-5 h-5">
                                <path fillRule="evenodd" d="M11.078 2.25c-.917 0-1.699.663-1.85 1.567L9.05 4.889c-.02.12-.115.26-.297.348a7.493 7.493 0 00-.986.57c-.166.115-.334.126-.45.083L6.3 5.508a1.875 1.875 0 00-2.282.819l-.922 1.597a1.875 1.875 0 00.432 2.385l.84.692c.095.078.17.229.154.43a7.598 7.598 0 000 1.139c.015.2-.059.352-.153.43l-.841.692a1.875 1.875 0 00-.432 2.385l.922 1.597a1.875 1.875 0 002.282.818l1.019-.382c.115-.043.283-.031.45.082.312.214.641.405.985.57.182.088.277.228.297.35l.178 1.071c.151.904.933 1.567 1.85 1.567h1.844c.916 0 1.699-.663 1.85-1.567l.178-1.072c.02-.12.114-.26.297-.349.344-.165.673-.356.985-.57.167-.114.335-.125.45-.082l1.02.382a1.875 1.875 0 002.28-.819l.922-1.597a1.875 1.875 0 00-.432-2.385l-.84-.692c-.095-.078-.17-.229-.154-.43a7.614 7.614 0 000-1.139c-.016-.2.059-.352.153-.43l.84-.692c.708-.582.891-1.59.433-2.385l-.922-1.597a1.875 1.875 0 00-2.282-.818l-1.02.382c-.114.043-.282.031-.449-.083a7.49 7.49 0 00-.985-.57c-.183-.087-.277-.227-.297-.348l-.179-1.072a1.875 1.875 0 00-1.85-1.567h-1.843zM12 15.75a3.75 3.75 0 100-7.5 3.75 3.75 0 000 7.5z" clipRule="evenodd" />
                            </svg>
                        </span>
                        Processing Settings
                    </h2>
                    {/* 右上角 X = 放弃本次改动（回滚到打开时的值），与「保存」互斥：
                        只有保存会提交。 */}
                    <button
                        onClick={() => { revertToSnapshot(); onClose(); }}
                        className="text-gray-400 hover:text-gray-600 transition p-2 rounded-full hover:bg-gray-100"
                    >
                        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor" className="w-5 h-5">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                        </svg>
                    </button>
                </div>

                {/* 与落地页同一个组件（ProcessingSettings）：那边加了开关这里自动有。
                    max-h + overflow：设置变多之后，矮窗口里要能滚到「保存」，而不是
                    被裁掉。 */}
                <div className="px-6 py-5 max-h-[65vh] overflow-y-auto">
                    <p className="text-xs text-gray-400 uppercase tracking-widest font-bold mb-3 px-1">
                        Audio Processing
                    </p>
                    <ProcessingSettings
                        separationMode={separationMode}
                        onSeparationModeChange={onSeparationModeChange}
                        separationBackends={separationBackends}
                        bgmSeparationLocked={bgmSeparationLocked}
                        mmEnhance={mmEnhance}
                        onMmEnhanceChange={onMmEnhanceChange}
                        enableVoiceClone={enableVoiceClone}
                        onVoiceCloneChange={onVoiceCloneChange}
                        cloneSmartPick={cloneSmartPick}
                        onCloneSmartPickChange={onCloneSmartPickChange}
                        customPrompt={customPrompt}
                        onCustomPromptChange={onCustomPromptChange}
                    />
                </div>

                <div className="px-8 py-5 border-t border-gray-100 bg-claude-bg/50 flex justify-end">
                    {/* 没改动就灰置禁用（关闭走右上角的 X）；一旦改动就点亮 ——
                        它现在是一次「保存并重做」：这些设置改了，旧的配音
                        结果就不再对应当前设置，必须重新处理。 */}
                    <button
                        onClick={handleSave}
                        disabled={!dirty}
                        className={`px-8 py-2.5 text-sm font-bold rounded-xl transition ${
                            dirty
                                ? 'bg-claude-accent text-white hover:bg-claude-accentHover shadow-lg shadow-claude-accent/20 cursor-pointer'
                                : 'bg-gray-200 text-gray-400 cursor-not-allowed'
                        }`}
                    >
                        保存
                    </button>
                </div>
            </div>
        </div>
    );
};

export default SettingsModal;
