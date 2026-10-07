
import React, { useEffect, useRef } from 'react';

interface StreamingLogProps {
  logs: string;
  isOpen: boolean;
  onClose: () => void;
  title?: string;
  isProcessing?: boolean;
  /**
   * 取消当前处理。
   *
   * 这个按钮必须在这里 —— 这个面板是 `fixed inset-0` 的全屏浮层，处理中它会
   * 盖住整个页面，包括主页面状态条上那个「取消运行」。没有它，用户在长任务
   * （一次多模态增强可能两分钟）中间根本无处可点，只能刷新页面。
   */
  onCancel?: () => void;
  /** 取消请求已发出，等当前 provider 请求跑完。 */
  isCancelling?: boolean;
  /** 当前阶段（人话），显示在标题右侧。 */
  stepLabel?: string | null;
}

const StreamingLog: React.FC<StreamingLogProps> = ({ logs, isOpen, onClose, title = "Process Log", isProcessing = true, onCancel, isCancelling = false, stepLabel = null }) => {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (isOpen && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs, isOpen]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/90 backdrop-blur-sm p-4">
      <div className="w-full max-w-4xl h-[80vh] bg-gray-950 border border-gray-800 rounded-lg shadow-2xl flex flex-col overflow-hidden font-mono text-sm">
        <div className="bg-gray-900 px-4 py-2 border-b border-gray-800 flex items-center justify-between">
            <div className="flex items-center gap-2">
                <div className="flex gap-1.5">
                    <div className={`w-3 h-3 rounded-full ${isProcessing ? 'bg-red-500/20 border-red-500/50' : 'bg-red-500/40 border-red-500/70 cursor-pointer hover:bg-red-500/80'} border`} onClick={!isProcessing ? onClose : undefined}></div>
                    <div className="w-3 h-3 rounded-full bg-yellow-500/20 border border-yellow-500/50"></div>
                    <div className={`w-3 h-3 rounded-full border ${isProcessing ? 'bg-green-500/20 border-green-500/50 animate-pulse' : 'bg-green-500/40 border-green-500/70'}`}></div>
                </div>
                <span className="text-gray-400 font-bold ml-2 text-xs uppercase tracking-wider">{title}</span>
            </div>
            <div className="flex items-center gap-3">
                {isProcessing ? (
                    <>
                        <div className="text-[10px] text-gray-500 animate-pulse">
                            {stepLabel ? `正在处理：${stepLabel}` : 'Receiving Data Stream...'}
                        </div>
                        {onCancel && (
                            <button
                                onClick={onCancel}
                                disabled={isCancelling}
                                className="text-[10px] font-bold px-2.5 py-1 rounded border transition-colors border-red-500/40 text-red-400 hover:border-red-400 hover:text-red-300 disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                                {isCancelling ? '正在取消…' : '取消运行'}
                            </button>
                        )}
                    </>
                ) : (
                    <button
                        onClick={onClose}
                        className="text-[10px] text-gray-400 hover:text-white px-2 py-1 rounded border border-gray-700 hover:border-gray-500 transition-colors"
                    >
                        Close
                    </button>
                )}
            </div>
        </div>
        <div className="flex-grow overflow-y-auto p-6 text-green-400 bg-black">
            <pre className="whitespace-pre-wrap break-words leading-relaxed font-mono">
                {logs}
                {isProcessing && <span className="inline-block w-2 h-4 bg-green-500 ml-1 animate-pulse align-middle"></span>}
            </pre>
            <div ref={bottomRef} />
        </div>
      </div>
    </div>
  );
};

export default StreamingLog;
