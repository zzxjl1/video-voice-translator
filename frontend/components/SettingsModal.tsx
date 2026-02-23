
import React, { useState } from 'react';

interface SettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
    enableVoiceClone: boolean;
    onVoiceCloneChange: (v: boolean) => void;
    enableBgmSeparation: boolean;
    onBgmSeparationChange: (v: boolean) => void;
    bgmSeparationLocked?: boolean;
}

const InfoTooltip: React.FC<{ text: string }> = ({ text }) => {
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

const SettingsModal: React.FC<SettingsModalProps> = ({ isOpen, onClose, enableVoiceClone, onVoiceCloneChange, enableBgmSeparation, onBgmSeparationChange, bgmSeparationLocked }) => {
    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-claude-text/40 backdrop-blur-sm p-4 animate-in fade-in duration-200">
            <div className="bg-white border border-gray-200 rounded-2xl shadow-2xl w-full max-w-md overflow-hidden animate-in zoom-in-95 duration-200">
                <div className="px-8 py-6 border-b border-gray-100 flex justify-between items-center bg-claude-bg">
                    <h2 className="text-xl font-serif font-bold text-gray-800 flex items-center gap-3">
                        <span className="w-8 h-8 rounded-lg bg-claude-paper text-claude-accent flex items-center justify-center">
                            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="w-5 h-5">
                                <path fillRule="evenodd" d="M11.078 2.25c-.917 0-1.699.663-1.85 1.567L9.05 4.889c-.02.12-.115.26-.297.348a7.493 7.493 0 00-.986.57c-.166.115-.334.126-.45.083L6.3 5.508a1.875 1.875 0 00-2.282.819l-.922 1.597a1.875 1.875 0 00.432 2.385l.84.692c.095.078.17.229.154.43a7.598 7.598 0 000 1.139c.015.2-.059.352-.153.43l-.841.692a1.875 1.875 0 00-.432 2.385l.922 1.597a1.875 1.875 0 002.282.818l1.019-.382c.115-.043.283-.031.45.082.312.214.641.405.985.57.182.088.277.228.297.35l.178 1.071c.151.904.933 1.567 1.85 1.567h1.844c.916 0 1.699-.663 1.85-1.567l.178-1.072c.02-.12.114-.26.297-.349.344-.165.673-.356.985-.57.167-.114.335-.125.45-.082l1.02.382a1.875 1.875 0 002.28-.819l.922-1.597a1.875 1.875 0 00-.432-2.385l-.84-.692c-.095-.078-.17-.229-.154-.43a7.614 7.614 0 000-1.139c-.016-.2.059-.352.153-.43l.84-.692c.708-.582.891-1.59.433-2.385l-.922-1.597a1.875 1.875 0 00-2.282-.818l-1.02.382c-.114.043-.282.031-.449-.083a7.49 7.49 0 00-.985-.57c-.183-.087-.277-.227-.297-.348l-.179-1.072a1.875 1.875 0 00-1.85-1.567h-1.843zM12 15.75a3.75 3.75 0 100-7.5 3.75 3.75 0 000 7.5z" clipRule="evenodd" />
                            </svg>
                        </span>
                        Processing Settings
                    </h2>
                    <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition p-2 rounded-full hover:bg-gray-100">
                        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor" className="w-5 h-5">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                        </svg>
                    </button>
                </div>

                <div className="px-8 py-6 space-y-4">
                    <p className="text-xs text-gray-400 uppercase tracking-widest font-bold mb-2">Audio Processing</p>

                    <div className="flex items-center justify-between py-3 px-4 bg-gray-50 rounded-xl">
                        <div className="flex items-center">
                            <span className={`text-sm font-medium ${bgmSeparationLocked ? 'text-gray-400' : 'text-gray-700'}`}>BGM Separation</span>
                            <InfoTooltip text={bgmSeparationLocked ? "BGM Separation is required when Voice Cloning is enabled." : "Separate background music from vocals before processing. Produces cleaner results but takes longer. Recommended for videos with music."} />
                        </div>
                        <button
                            onClick={() => !bgmSeparationLocked && onBgmSeparationChange(!enableBgmSeparation)}
                            className={`relative w-10 h-[22px] rounded-full transition-colors duration-200 ${bgmSeparationLocked ? 'bg-claude-accent/50 cursor-not-allowed' : enableBgmSeparation ? 'bg-claude-accent' : 'bg-gray-300'}`}
                        >
                            <span className={`absolute top-[2px] left-[2px] w-[18px] h-[18px] bg-white rounded-full shadow transition-transform duration-200 ${enableBgmSeparation ? 'translate-x-[18px]' : ''}`} />
                        </button>
                    </div>

                    <div className="flex items-center justify-between py-3 px-4 bg-gray-50 rounded-xl">
                        <div className="flex items-center">
                            <span className="text-sm font-medium text-gray-700">Voice Cloning</span>
                            <InfoTooltip text="Clone the original speaker's voice for synthesis. The translated audio will sound like the original speaker. Requires more processing time." />
                        </div>
                        <button
                            onClick={() => onVoiceCloneChange(!enableVoiceClone)}
                            className={`relative w-10 h-[22px] rounded-full transition-colors duration-200 ${enableVoiceClone ? 'bg-claude-accent' : 'bg-gray-300'}`}
                        >
                            <span className={`absolute top-[2px] left-[2px] w-[18px] h-[18px] bg-white rounded-full shadow transition-transform duration-200 ${enableVoiceClone ? 'translate-x-[18px]' : ''}`} />
                        </button>
                    </div>
                </div>

                <div className="px-8 py-5 border-t border-gray-100 bg-claude-bg/50 flex justify-end">
                    <button
                        onClick={onClose}
                        className="px-8 py-2.5 bg-gray-200 hover:bg-gray-300 text-gray-700 text-sm font-bold rounded-xl transition"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
};

export default SettingsModal;
