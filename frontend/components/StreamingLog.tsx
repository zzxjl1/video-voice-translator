
import React, { useEffect, useRef } from 'react';

interface StreamingLogProps {
  logs: string;
  isOpen: boolean;
  onClose: () => void; // Although we might not allow closing manually during process
  title?: string;
}

const StreamingLog: React.FC<StreamingLogProps> = ({ logs, isOpen, title = "Process Log" }) => {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (isOpen && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs, isOpen]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/90 backdrop-blur-sm p-4">
      <div className="w-full max-w-4xl h-[80vh] bg-gray-950 border border-gray-800 rounded-lg shadow-2xl flex flex-col overflow-hidden font-mono text-sm">
        <div className="bg-gray-900 px-4 py-2 border-b border-gray-800 flex items-center justify-between">
            <div className="flex items-center gap-2">
                <div className="flex gap-1.5">
                    <div className="w-3 h-3 rounded-full bg-red-500/20 border border-red-500/50"></div>
                    <div className="w-3 h-3 rounded-full bg-yellow-500/20 border border-yellow-500/50"></div>
                    <div className="w-3 h-3 rounded-full bg-green-500/20 border border-green-500/50 animate-pulse"></div>
                </div>
                <span className="text-gray-400 font-bold ml-2 text-xs uppercase tracking-wider">{title}</span>
            </div>
            <div className="text-[10px] text-gray-500 animate-pulse">
                Receiving Data Stream...
            </div>
        </div>
        <div className="flex-grow overflow-y-auto p-6 text-green-400 bg-black custom-scrollbar">
            <pre className="whitespace-pre-wrap break-words leading-relaxed font-mono">
                {logs}
                <span className="inline-block w-2 h-4 bg-green-500 ml-1 animate-pulse align-middle"></span>
            </pre>
            <div ref={bottomRef} />
        </div>
      </div>
    </div>
  );
};

export default StreamingLog;
