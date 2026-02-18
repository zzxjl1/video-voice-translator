
import React, { useState, useEffect } from 'react';

interface SettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
    targetLanguage: string;
    onSave: (newLanguage: string) => void;
}

const LANGUAGES = [
    'English',
    'Chinese',
    'Japanese',
    'Korean',
    'French',
    'German',
    'Spanish',
];

const SettingsModal: React.FC<SettingsModalProps> = ({ isOpen, onClose, targetLanguage, onSave }) => {
    const [localLanguage, setLocalLanguage] = useState<string>(targetLanguage);

    useEffect(() => {
        setLocalLanguage(targetLanguage);
    }, [targetLanguage, isOpen]);

    if (!isOpen) return null;

    const handleSave = () => {
        onSave(localLanguage);
        onClose();
    };

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
                        Translation Language
                    </h2>
                    <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition p-2 rounded-full hover:bg-gray-100">
                        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor" className="w-5 h-5">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                        </svg>
                    </button>
                </div>

                <div className="p-8 space-y-6">
                    <div className="space-y-2">
                        <label className="text-xs font-bold uppercase tracking-wider text-gray-500">Target Language</label>
                        <div className="relative">
                            <select
                                value={localLanguage}
                                onChange={(e) => setLocalLanguage(e.target.value)}
                                className="w-full bg-white border border-gray-300 rounded-xl px-4 py-3 text-sm text-gray-800 focus:outline-none focus:border-claude-accent focus:ring-1 focus:ring-claude-accent transition appearance-none font-medium"
                            >
                                {LANGUAGES.map(l => <option key={l} value={l}>{l}</option>)}
                            </select>
                            <div className="pointer-events-none absolute inset-y-0 right-0 flex items-center px-4 text-gray-500">
                                <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7"></path></svg>
                            </div>
                        </div>
                        <p className="text-[11px] text-gray-400 pl-1">Selected language for script translation and voice synthesis.</p>
                    </div>
                </div>

                <div className="px-8 py-5 border-t border-gray-100 bg-claude-bg/50 flex justify-end gap-3">
                    <button
                        onClick={onClose}
                        className="px-5 py-2.5 text-sm font-bold text-gray-500 hover:text-gray-800 transition"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={handleSave}
                        className="px-6 py-2.5 bg-claude-accent hover:bg-claude-accentHover text-white text-sm font-bold rounded-xl shadow-lg shadow-claude-accent/20 transition"
                    >
                        Save Changes
                    </button>
                </div>
            </div>
        </div>
    );
};

export default SettingsModal;
