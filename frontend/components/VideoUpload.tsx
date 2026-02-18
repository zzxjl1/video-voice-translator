
import React, { useRef } from 'react';

interface VideoUploadProps {
  onVideoSelect: (file: File) => void;
  isLoading: boolean;
}

const VideoUpload: React.FC<VideoUploadProps> = ({ onVideoSelect, isLoading }) => {
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    if (event.target.files && event.target.files[0]) {
      onVideoSelect(event.target.files[0]);
    }
  };

  const handleClick = () => {
    inputRef.current?.click();
  };

  return (
    <div className="flex flex-col items-center justify-center h-full p-12 text-center bg-white rounded-3xl border border-claude-border shadow-sm hover:shadow-md transition-shadow duration-300">
      <div className="w-20 h-20 bg-claude-paper rounded-full flex items-center justify-center mb-6 text-claude-accent">
        <svg className="w-10 h-10" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" d="m15.75 10.5 4.72-4.72a.75.75 0 0 1 1.28.53v11.38a.75.75 0 0 1-1.28.53l-4.72-4.72M4.5 18.75h9a2.25 2.25 0 0 0 2.25-2.25v-9A2.25 2.25 0 0 0 13.5 5.25h-9A2.25 2.25 0 0 0 2.25 7.5v9A2.25 2.25 0 0 0 4.5 18.75Z" />
        </svg>
      </div>

      <h2 className="text-3xl font-serif font-bold mb-3 text-claude-text">Upload Video</h2>
      <p className="text-gray-600 mb-8 max-w-md font-sans leading-relaxed">
        Begin your translation journey. We'll transcribe, translate, and re-voice your video content with AI.
      </p>

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
        className="px-8 py-3.5 bg-claude-accent text-white font-medium rounded-xl hover:bg-claude-accentHover disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors duration-300 shadow-lg shadow-claude-accent/20"
      >
        {isLoading ? 'Processing Video...' : 'Select Video File'}
      </button>
    </div>
  );
};

export default VideoUpload;
