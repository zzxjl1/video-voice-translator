
import React from 'react';

interface LoaderProps {
  message: string;
}

const Loader: React.FC<LoaderProps> = ({ message }) => {
  return (
    <div className="absolute inset-0 bg-gray-900/80 flex flex-col items-center justify-center z-30 backdrop-blur-sm">
        <div className="w-16 h-16 border-4 border-blue-400 border-t-transparent rounded-full animate-spin"></div>
        <p className="mt-4 text-lg font-semibold text-white">{message}</p>
    </div>
  );
};

export default Loader;
