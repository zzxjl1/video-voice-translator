
import React, { useRef, useState } from 'react';

const LANGUAGES = [
  'English',
  'Chinese',
  'Japanese',
  'Korean',
  'French',
  'German',
  'Spanish',
];

interface VideoUploadProps {
  onVideoSelect: (file: File) => void;
  isLoading: boolean;
  targetLanguage: string;
  onLanguageChange: (lang: string) => void;
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

const VideoUpload: React.FC<VideoUploadProps> = ({ onVideoSelect, isLoading, targetLanguage, onLanguageChange, enableVoiceClone, onVoiceCloneChange, enableBgmSeparation, onBgmSeparationChange, bgmSeparationLocked }) => {
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    if (event.target.files && event.target.files[0]) {
      onVideoSelect(event.target.files[0]);
    }
  };

  const handleClick = () => {
    inputRef.current?.click();
  };

  // Generate repeated watermark items
  const watermarkRows = Array.from({ length: 12 }, (_, i) => i);

  return (
    <div className="relative flex flex-col items-center justify-center min-h-screen overflow-hidden bg-claude-bg">
      {/* Animated watermark background */}
      <div className="absolute inset-0 overflow-hidden pointer-events-none select-none" aria-hidden="true">
        <div className="absolute inset-0" style={{ transform: 'rotate(-18deg)', transformOrigin: 'center center' }}>
          {watermarkRows.map((row) => (
            <div
              key={row}
              className="whitespace-nowrap flex items-center gap-0"
              style={{
                animation: `marquee-${row % 2 === 0 ? 'left' : 'right'} ${50 + row * 3}s linear infinite`,
                marginTop: row === 0 ? '-80px' : '0',
                height: '100px',
              }}
            >
              {Array.from({ length: 20 }, (_, i) => (
                <span
                  key={i}
                  className="inline-flex items-center gap-4 mx-8 text-claude-accent/[0.04] font-serif font-bold select-none"
                  style={{ fontSize: '42px', letterSpacing: '0.02em' }}
                >
                  <svg className="w-8 h-8 flex-shrink-0 opacity-60" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 0 0 6-6v-1.5a6 6 0 0 0-6-6 6 6 0 0 0-6 6v1.5a6 6 0 0 0 6 6Z" />
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 12.75a3 3 0 0 0 3-3v-1.5a3 3 0 0 0-3-3 3 3 0 0 0-3 3v1.5a3 3 0 0 0 3 3Z" />
                  </svg>
                  Video Voice Translator
                </span>
              ))}
            </div>
          ))}
        </div>
      </div>

      {/* CSS animation */}
      <style>{`
        @keyframes marquee-left {
          0% { transform: translateX(0); }
          100% { transform: translateX(-50%); }
        }
        @keyframes marquee-right {
          0% { transform: translateX(-50%); }
          100% { transform: translateX(0); }
        }
      `}</style>

      {/* Main content */}
      <div className="relative z-10 flex flex-col items-center w-full max-w-lg px-6">
        {/* Logo */}
        <div className="flex items-center gap-4 mb-4">
          <div className="w-14 h-14 bg-claude-accent text-white rounded-2xl flex items-center justify-center shadow-lg shadow-claude-accent/20">
            <svg className="w-8 h-8" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={2.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 0 0 6-6v-1.5a6 6 0 0 0-6-6 6 6 0 0 0-6 6v1.5a6 6 0 0 0 6 6Z" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 12.75a3 3 0 0 0 3-3v-1.5a3 3 0 0 0-3-3 3 3 0 0 0-3 3v1.5a3 3 0 0 0 3 3Z" />
            </svg>
          </div>
          <div>
            <h1 className="text-2xl font-serif font-bold text-gray-900 tracking-tight">Video Voice Translator</h1>
            <p className="text-[10px] uppercase tracking-[0.2em] font-bold text-gray-400 mt-0.5">AI-Powered Translation</p>
          </div>
        </div>

        {/* Language Selector - Prominent */}
        <div className="w-full bg-white/80 backdrop-blur-xl rounded-2xl border border-gray-200/80 shadow-[0_8px_30px_rgba(0,0,0,0.06)] p-6 mb-6">
          <label className="block text-xs font-bold uppercase tracking-[0.15em] text-gray-500 mb-3 text-center">
            Translate video to
          </label>
          <div className="grid grid-cols-4 gap-2 mb-1">
            {LANGUAGES.map(lang => (
              <button
                key={lang}
                onClick={() => onLanguageChange(lang)}
                className={`
                  py-2.5 px-3 rounded-xl text-sm font-semibold transition-all duration-200
                  ${targetLanguage === lang
                    ? 'bg-claude-accent text-white shadow-md shadow-claude-accent/25 scale-[1.02]'
                    : 'bg-gray-50 text-gray-600 hover:bg-gray-100 hover:text-gray-800 border border-gray-100'
                  }
                `}
              >
                {lang}
              </button>
            ))}
          </div>
        </div>

        {/* Upload Card */}
        <div className="w-full bg-white/80 backdrop-blur-xl rounded-2xl border border-gray-200/80 shadow-[0_8px_30px_rgba(0,0,0,0.06)] p-8 flex flex-col items-center text-center">
          <div className="w-16 h-16 bg-claude-paper rounded-full flex items-center justify-center mb-5 text-claude-accent">
            <svg className="w-8 h-8" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="m15.75 10.5 4.72-4.72a.75.75 0 0 1 1.28.53v11.38a.75.75 0 0 1-1.28.53l-4.72-4.72M4.5 18.75h9a2.25 2.25 0 0 0 2.25-2.25v-9A2.25 2.25 0 0 0 13.5 5.25h-9A2.25 2.25 0 0 0 2.25 7.5v9A2.25 2.25 0 0 0 4.5 18.75Z" />
            </svg>
          </div>

          <h2 className="text-xl font-serif font-bold mb-2 text-claude-text">Upload Your Video</h2>
          <p className="text-sm text-gray-500 mb-5 leading-relaxed max-w-sm">
            We'll transcribe, translate to <span className="font-bold text-claude-accent">{targetLanguage}</span>, and re-voice your video with AI.
          </p>

          {/* Processing Options */}
          <div className="w-full space-y-3 mb-6">
            <div className="flex items-center justify-between py-2 px-3 bg-gray-50 rounded-xl">
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
            <div className="flex items-center justify-between py-2 px-3 bg-gray-50 rounded-xl">
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

          <input
            type="file"
            ref={inputRef}
            onChange={handleFileChange}
            className="hidden"
            accept="video/*"
            disabled={isLoading}
          />
          <button
            onClick={handleClick}
            disabled={isLoading}
            className="w-full px-8 py-4 bg-claude-accent text-white font-semibold rounded-xl hover:bg-claude-accentHover disabled:bg-gray-300 disabled:cursor-not-allowed transition-all duration-300 shadow-lg shadow-claude-accent/20 hover:shadow-xl hover:shadow-claude-accent/30 hover:-translate-y-0.5 active:translate-y-0 active:scale-[0.98] text-base"
          >
            {isLoading ? (
              <span className="flex items-center justify-center gap-2">
                <svg className="animate-spin w-5 h-5" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg>
                Processing Video...
              </span>
            ) : 'Select Video File'}
          </button>
        </div>
      </div>
    </div>
  );
};

export default VideoUpload;
