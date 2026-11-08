/**
 * 初始化视图：上传之后、进入编辑器之前。对应旧版的全屏终端弹窗，但：
 *   · 左边是**步骤清单**（每一步的状态、这一步在干什么）+ 总进度，一眼看出走到哪；
 *   · 右边是终端日志，保留原来的气质，细节都在这；
 *   · 不再是 z-[9999] 的全屏遮罩 —— 它就是一个页面，取消按钮永远可点。
 * 结束后（成功 / 失败）原地给出下一步，而不是弹窗自己关掉。
 */
import React, { useEffect, useRef } from 'react';
import { overallProgress, type InitState, type InitStep, type LogLine } from '../../store/init';

interface Props {
  filename: string;
  init: InitState;
  /** 服务端报告的失败原因（阶段 failed 时）。 */
  error?: string | null;
  /** 当前正在跑的步骤的文字（来自任务）。 */
  currentStep?: string | null;
  elapsedS?: number;
  cancelling?: boolean;
  onCancel: () => void;
  onRetry: () => void;
  onEnter: () => void;
  onBack: () => void;
}

const ICON: Record<InitStep['status'], React.ReactNode> = {
  pending: <span className="w-2 h-2 rounded-full bg-gray-200" />,
  running: <span className="w-3.5 h-3.5 rounded-full border-2 border-claude-accent/30 border-t-claude-accent animate-spin" />,
  done: (
    <svg className="w-3.5 h-3.5 text-emerald-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
    </svg>
  ),
  skipped: <span className="w-2 h-px bg-gray-300" />,
  failed: (
    <svg className="w-3.5 h-3.5 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M12 9v2m0 4h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z" />
    </svg>
  ),
};

const LOG_TONE: Record<LogLine['level'], string> = {
  info: 'text-gray-300',
  ok: 'text-green-400',
  warn: 'text-amber-300',
  error: 'text-red-400',
};

function fmtElapsed(s: number): string {
  const m = Math.floor(s / 60);
  return m ? `${m}分${Math.floor(s % 60)}秒` : `${Math.floor(s)}秒`;
}

const InitView: React.FC<Props> = ({ filename, init, error, currentStep, elapsedS = 0, cancelling, onCancel, onRetry, onEnter, onBack }) => {
  const logEnd = useRef<HTMLDivElement>(null);
  const percent = Math.round(overallProgress(init) * 100);
  const failed = !!error || !!init.failed;
  const done = init.finished && !failed;

  useEffect(() => {
    logEnd.current?.scrollIntoView({ block: 'end' });
  }, [init.log.length, init.log[init.log.length - 1]?.text]);

  return (
    <div className="min-h-screen bg-claude-bg flex items-center justify-center p-4 lg:p-8">
      <div className="w-full max-w-5xl grid lg:grid-cols-5 gap-5">
        {/* 左：步骤 */}
        <div className="lg:col-span-2 bg-white border border-claude-border rounded-3xl shadow-sm p-7 flex flex-col">
          <div className="mb-6">
            <p className="text-[10px] font-bold uppercase tracking-[0.2em] text-claude-accent mb-2">
              {failed ? '出了点问题' : done ? '准备好了' : '正在准备'}
            </p>
            <h2 className="font-serif font-bold text-xl text-claude-text leading-snug break-all">{filename}</h2>
          </div>

          <div className="mb-6">
            <div className="h-1.5 rounded-full bg-claude-paper overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-500 ${failed ? 'bg-amber-400' : 'bg-claude-accent'}`}
                style={{ width: `${failed ? Math.max(percent, 6) : percent}%` }}
              />
            </div>
            <div className="flex justify-between mt-2 text-[10px] font-bold uppercase tracking-widest text-gray-400">
              <span>{done ? '完成' : `${percent}%`}</span>
              {!done && !failed && elapsedS > 0 && <span className="tabular-nums">{fmtElapsed(elapsedS)}</span>}
            </div>
          </div>

          <ol className="space-y-1 flex-grow">
            {init.steps.map((s) => (
              <li
                key={s.id}
                className={`flex items-start gap-3 px-3 py-2.5 rounded-xl transition-colors ${s.status === 'running' ? 'bg-claude-accent/5' : ''}`}
              >
                <span className="shrink-0 w-5 h-5 flex items-center justify-center mt-px">{ICON[s.status]}</span>
                <div className="min-w-0 flex-grow">
                  <div className={`text-[13px] font-medium ${s.status === 'pending' || s.status === 'skipped' ? 'text-gray-400' : 'text-claude-text'}`}>
                    {s.label}
                  </div>
                  {(s.detail || s.status === 'skipped') && (
                    <div className={`text-[11px] mt-0.5 ${s.status === 'failed' ? 'text-amber-600' : 'text-gray-400'}`}>
                      {s.detail}
                      {s.progress && s.status === 'running' && s.progress.total > 0 && ` · ${s.progress.done}/${s.progress.total}`}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ol>

          <div className="mt-6 pt-5 border-t border-claude-border/70">
            {failed ? (
              <div className="space-y-3">
                <p className="text-[12px] text-red-500 leading-relaxed break-words">{error || init.failed}</p>
                <div className="flex gap-2">
                  <button onClick={onRetry} className="flex-1 py-2.5 rounded-xl bg-claude-accent hover:bg-claude-accentHover text-white text-xs font-bold shadow-sm shadow-claude-accent/20 transition-colors">
                    重试
                  </button>
                  <button onClick={onBack} className="px-4 py-2.5 rounded-xl border border-claude-border text-xs font-bold text-gray-500 hover:bg-claude-paper transition-colors">
                    返回
                  </button>
                </div>
              </div>
            ) : done ? (
              <button onClick={onEnter} className="w-full py-3 rounded-xl bg-claude-accent hover:bg-claude-accentHover text-white text-sm font-bold shadow-md shadow-claude-accent/20 transition-all active:scale-[0.99]">
                进入编辑器
              </button>
            ) : (
              <div className="flex items-center justify-between gap-3">
                <span className="text-[11px] text-gray-400 truncate">{currentStep ? `正在：${currentStep}` : '处理中…'}</span>
                <button
                  onClick={onCancel}
                  disabled={cancelling}
                  className="shrink-0 px-3.5 py-2 rounded-xl border border-claude-border text-[11px] font-bold text-gray-500 hover:text-red-500 hover:border-red-200 disabled:opacity-50 transition-colors"
                >
                  {cancelling ? '正在停止…' : '取消'}
                </button>
              </div>
            )}
          </div>
        </div>

        {/* 右：终端日志 */}
        <div className="lg:col-span-3 bg-gray-950 border border-gray-800 rounded-3xl shadow-2xl flex flex-col overflow-hidden font-mono text-[12px] min-h-[420px] lg:min-h-0">
          <div className="bg-gray-900 px-4 py-2.5 border-b border-gray-800 flex items-center gap-2">
            <div className="flex gap-1.5">
              <span className="w-3 h-3 rounded-full bg-red-500/30 border border-red-500/50" />
              <span className="w-3 h-3 rounded-full bg-yellow-500/30 border border-yellow-500/50" />
              <span className={`w-3 h-3 rounded-full border ${done ? 'bg-green-500/50 border-green-500/70' : 'bg-green-500/20 border-green-500/50 animate-pulse'}`} />
            </div>
            <span className="text-gray-500 font-bold ml-2 text-[10px] uppercase tracking-wider">Process Log</span>
          </div>
          <div className="flex-grow overflow-y-auto p-4 space-y-1">
            {init.log.length === 0 && <div className="text-gray-600">等待开始…</div>}
            {init.log.map((l) => (
              <div key={l.key} className={`${LOG_TONE[l.level]} leading-relaxed`}>
                <span className="text-gray-600 select-none">› </span>{l.text}
              </div>
            ))}
            {!done && !failed && <span className="inline-block w-2 h-4 bg-green-400/80 animate-pulse align-middle" />}
            <div ref={logEnd} />
          </div>
        </div>
      </div>
    </div>
  );
};

export default InitView;
