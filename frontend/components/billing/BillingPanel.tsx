/**
 * 费用面板：账户余额 / 本月已用 / 按步骤明细 / 最近的分录。
 *
 * 口径（和 docs/billing.md 一致）：显示的都是**含服务费的用户价**，成本不在任何
 * 接口里返回，所以这里也不可能显示它。明细保留 4 位小数——单句常常不到一分钱，
 * 四舍五入到分会看起来像"免费"。
 */
import React, { useCallback, useEffect, useState } from 'react';
import { billing as api } from '../../net/client';
import type { BillingEntry, BillingSummary, ProjectBilling } from '../../net/types';
import { t } from '@/i18n';

const STEP_LABEL: Record<string, string> = {
  tts: '配音合成',
  asr: '语音识别',
  omni: '审听 / 聊天',
  llm: '翻译',
  clone: '声音克隆',
  browser_tts: '本地配音（免费）',
  separation: '人声分离（免费）',
  export: '导出（免费）',
};

const yuan = (micro: number, digits = 2) => (micro / 1_000_000).toFixed(digits);

const BillingPanel: React.FC<{ open: boolean; onClose: () => void; projectId?: string }> = ({ open, onClose, projectId }) => {
  const [summary, setSummary] = useState<BillingSummary | null>(null);
  const [project, setProject] = useState<ProjectBilling | null>(null);
  const [entries, setEntries] = useState<BillingEntry[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      const [s, e] = await Promise.all([
        api.summary(),
        api.entries({ limit: 30, project: projectId }),
      ]);
      setSummary(s);
      setEntries(e.entries);
      if (projectId) setProject(await api.project(projectId));
      else setProject(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30 p-4" onMouseDown={onClose}>
      <div
        className="w-full max-w-lg max-h-[80vh] overflow-y-auto rounded-2xl border border-claude-border bg-white shadow-xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-claude-border px-5 py-3">
          <h3 className="text-[14px] font-bold">{t('费用')}</h3>
          <button type="button" onClick={onClose} className="text-[12px] text-gray-400 hover:text-gray-600">
            {t('关闭')}
          </button>
        </div>

        {error && <p className="px-5 py-3 text-[12px] text-red-600">{error}</p>}
        {busy && !summary && <p className="px-5 py-3 text-[12px] text-gray-500">{t('读取中…')}</p>}

        {summary && (
          <div className="grid grid-cols-2 gap-3 px-5 py-4">
            <div className="rounded-xl bg-[#fbfbf9] px-3 py-2">
              <div className="text-[10px] text-gray-400">{t('余额')}</div>
              <div className="text-[18px] font-bold">¥{summary.balance_yuan.toFixed(2)}</div>
            </div>
            <div className="rounded-xl bg-[#fbfbf9] px-3 py-2">
              <div className="text-[10px] text-gray-400">{t('本月已用')}</div>
              <div className="text-[18px] font-bold">
                ¥{summary.month_spent_yuan.toFixed(2)}
                <span className="ml-1 text-[10px] font-normal text-gray-400">{summary.month_calls} {t('次调用')}</span>
              </div>
            </div>
            {project && (
              <div className="col-span-2 rounded-xl bg-[#fbfbf9] px-3 py-2">
                <div className="text-[10px] text-gray-400">{t('这个工程累计')}</div>
                <div className="text-[18px] font-bold">¥{yuan(project.total_micro)}</div>
              </div>
            )}
          </div>
        )}

        {summary && summary.by_step.length > 0 && (
          <div className="px-5 pb-2">
            <div className="mb-1 text-[10px] font-bold uppercase tracking-wider text-gray-400">{t('按步骤')}</div>
            {summary.by_step.map((s) => (
              <div key={s.step} className="flex items-center justify-between border-b border-claude-border/50 py-1.5 text-[12px]">
                <span>{t(STEP_LABEL[s.step] ?? s.step)}</span>
                <span className="text-gray-500">
                  {s.calls}× · <span className="font-medium text-claude-text">¥{yuan(s.spent_micro, 4)}</span>
                </span>
              </div>
            ))}
          </div>
        )}

        {entries.length > 0 && (
          <div className="px-5 pb-5">
            <div className="mb-1 mt-2 text-[10px] font-bold uppercase tracking-wider text-gray-400">{t('最近')}</div>
            {entries.map((e) => (
              <div key={e.id} className="flex items-center justify-between border-b border-claude-border/40 py-1 text-[11px] text-gray-500">
                <span className="truncate pr-2">
                  {t(STEP_LABEL[e.step] ?? e.step)}{e.detail ? ` · ${e.detail}` : ''}
                </span>
                <span className="shrink-0">
                  {e.cost_source === 'free' ? t('免费') : `¥${yuan(e.price_micro, 4)}`}
                </span>
              </div>
            ))}
          </div>
        )}

        <p className="px-5 pb-4 text-[10px] leading-relaxed text-gray-400">
          {t('金额为人民币，已含服务费。本地配音、人声分离与导出不收费。')}
        </p>
      </div>
    </div>
  );
};

export default BillingPanel;
