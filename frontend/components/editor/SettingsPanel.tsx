/**
 * 工程设置弹窗（编辑页里）。
 *
 * 和旧 `SettingsModal` 的本质区别：旧的是“改完点保存 → 整条管线重跑”；现在每一项都是一个
 * 命令，改动的影响由服务端按指纹算（哪些译文/配音因此过期），之后只补那些 —— 所以没有
 * “保存并重新处理”，改了就是改了，顶栏会提示还差多少、一键补齐。
 *
 * 唯一需要确认的是“推翻已有内容”的那两项（改翻译要求）。
 */
import React, { useEffect, useState } from 'react';
import type { Settings } from '../../net/types';
import { CHINESE_ACCENTS, accentLabel } from '../../utils/languages';

interface Props {
  open: boolean;
  onClose: () => void;
  settings: Settings;
  /** 返回是否成功。 */
  onChange: (patch: Record<string, unknown>) => Promise<boolean>;
  onCustomPrompt: (prompt: string) => Promise<boolean>;
  hasContent: boolean;
}

const Row: React.FC<{ title: string; hint: string; checked: boolean; disabled?: boolean; onChange: (v: boolean) => void }> = ({ title, hint, checked, disabled, onChange }) => (
  <label className={`flex items-start justify-between gap-4 py-3.5 ${disabled ? 'opacity-40' : 'cursor-pointer'}`}>
    <div className="min-w-0">
      <div className="text-[13px] font-bold text-claude-text">{title}</div>
      <div className="text-[11px] text-gray-400 mt-0.5 leading-relaxed">{hint}</div>
    </div>
    <span className={`relative shrink-0 mt-0.5 w-10 h-6 rounded-full transition-colors ${checked ? 'bg-claude-accent' : 'bg-gray-200'}`}>
      <input type="checkbox" className="sr-only" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${checked ? 'translate-x-4' : ''}`} />
    </span>
  </label>
);

const SettingsPanel: React.FC<Props> = ({ open, onClose, settings, onChange, onCustomPrompt, hasContent }) => {
  const [prompt, setPrompt] = useState(settings.customPrompt);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) setPrompt(settings.customPrompt); }, [open, settings.customPrompt]);
  if (!open) return null;

  const apply = async (patch: Record<string, unknown>) => {
    setBusy(true);
    await onChange(patch);
    setBusy(false);
  };

  const promptDirty = prompt.trim() !== settings.customPrompt.trim();
  const savePrompt = async () => {
    if (hasContent && !window.confirm('修改翻译要求会让所有译文过期，需要重新翻译。继续？')) return;
    setBusy(true);
    await onCustomPrompt(prompt.trim());
    setBusy(false);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/30 backdrop-blur-sm" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="w-full max-w-lg max-h-[88vh] overflow-y-auto bg-white rounded-3xl shadow-2xl border border-claude-border">
        <div className="px-7 py-5 border-b border-claude-border flex items-center justify-between sticky top-0 bg-white/95 backdrop-blur z-10">
          <h3 className="font-serif font-bold text-lg text-claude-text">项目设置</h3>
          <button onClick={onClose} className="p-1.5 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-claude-paper transition-colors">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>

        <div className={`px-7 py-3 divide-y divide-claude-border/60 ${busy ? 'pointer-events-none opacity-70' : ''}`}>
          <Row title="生成配音" hint="关闭后只出译文字幕，不合成配音，导出时原声原样保留。" checked={settings.enableDubbing} onChange={(v) => void apply({ enable_dubbing: v })} />
          <Row title="声音克隆" hint="用原说话人的声音配音。克隆需要几十秒到几分钟，会产生少量费用。" checked={settings.enableVoiceClone} disabled={!settings.enableDubbing} onChange={(v) => void apply({ enable_voice_clone: v })} />
          <Row title="智能选材" hint="克隆时让 AI 听着挑参考片段，比按时长挑更干净。" checked={settings.cloneSmartPick} disabled={!settings.enableDubbing || !settings.enableVoiceClone} onChange={(v) => void apply({ clone_smart_pick: v })} />
          <Row title="审听纠错" hint="下次初始化时让 AI 听原声核对转写、标注语气。已有的转写不会重做。" checked={settings.mmEnhance} onChange={(v) => void apply({ mm_enhance: v })} />

          {settings.targetLanguage === 'Chinese' && (
            <div className="py-3.5">
              <div className="text-[13px] font-bold text-claude-text mb-2">配音方言</div>
              <select
                value={settings.accent ?? ''}
                onChange={(e) => void apply({ accent: e.target.value })}
                className="w-full text-[13px] bg-white border border-claude-border rounded-xl px-3 py-2.5 outline-none focus:border-claude-accent/50"
              >
                <option value="">普通话</option>
                {CHINESE_ACCENTS.map((a) => <option key={a.value} value={a.value}>{accentLabel(a.value)}</option>)}
              </select>
              <p className="text-[11px] text-gray-400 mt-1.5">只影响之后合成的配音；已有配音会标为“需更新”。</p>
            </div>
          )}

          <div className="py-3.5">
            <div className="text-[13px] font-bold text-claude-text">翻译要求</div>
            <div className="text-[11px] text-gray-400 mt-0.5 mb-2.5">注入翻译模型的自定义要求，如「保留专有名词原文」「语气正式」。</div>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              maxLength={500}
              rows={3}
              className="w-full text-[13px] leading-relaxed bg-white border border-claude-border rounded-xl px-3.5 py-2.5 outline-none focus:border-claude-accent/50 focus:ring-2 focus:ring-claude-accent/10 resize-none"
              placeholder="（空 = 标准翻译）"
            />
            <div className="flex items-center justify-between mt-2">
              <span className="text-[10px] text-gray-300 tabular-nums">{prompt.length}/500</span>
              <button
                onClick={() => void savePrompt()}
                disabled={!promptDirty}
                className="px-4 py-2 rounded-xl text-[11px] font-bold bg-claude-accent text-white hover:bg-claude-accentHover disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              >
                保存
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default SettingsPanel;
