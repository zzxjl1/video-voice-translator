/**
 * 费用明细（用户选定的 A1「表格式」）。
 *
 * 结构：顶部一条余额摘要（余额 / 本月已用 / 次数，在工程页再加一行"本工程累计"），
 * 下面是四列表格：时间 / 项目 / 说明 / 积分。明细按时间倒序，最近的在最上面。
 *
 * 口径：只显示**积分**（1 积分 ≈ 0.01 元）；成本在服务端，接口不返回，这里也不可能出现。
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

/** micro 积分 → 显示串：<1 保留两位小数并去掉尾随 0，否则两位。 */
const pts = (micro: number): string => {
  const v = micro / 1_000_000;
  if (v === 0) return '0';
  const text = Math.abs(v) < 1 ? v.toFixed(2) : v.toFixed(2);
  return text.replace(/\.?0+$/, '');
};
const pts1 = (v: number): string => v.toFixed(2).replace(/\.?0+$/, '');

const timeLabel = (sec: number): string => {
  const d = new Date(sec * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

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
        api.entries({ limit: 60, project: projectId }),
      ]);
      setSummary(s);
      setEntries(e.entries);
      setProject(projectId ? await api.project(projectId) : null);
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
        className="flex max-h-[82vh] w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-claude-border bg-white shadow-xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        {/* 摘要 */}
        <div className="flex items-end justify-between border-b border-claude-border px-5 py-3.5">
          <div>
            <div className="text-[9px] font-bold uppercase tracking-[0.12em] text-gray-400">{t('积分余额')}</div>
            <div className="font-serif text-[26px] font-bold leading-tight text-claude-text">
              {summary ? pts1(summary.balance_points) : '—'}
            </div>
            {project && (
              <div className="mt-0.5 text-[10px] text-gray-400">
                {t('本工程累计')} <span className="font-bold text-gray-600">{pts1(project.total_points)}</span> {t('积分')}
              </div>
            )}
          </div>
          <div className="text-right">
            <div className="text-[10px] text-gray-400">
              {t('本月已用')}{' '}
              <span className="font-bold text-gray-600">{summary ? pts1(summary.month_points) : '—'}</span>
              {summary ? ` · ${summary.month_calls}×` : ''}
            </div>
            <button type="button" onClick={onClose} className="mt-1 text-[11px] text-gray-400 hover:text-gray-600">
              {t('关闭')}
            </button>
          </div>
        </div>

        {error && <p className="px-5 py-3 text-[12px] text-red-600">{error}</p>}
        {busy && !summary && <p className="px-5 py-3 text-[12px] text-gray-500">{t('读取中…')}</p>}

        {/* 表格 */}
        <div className="min-h-0 flex-1 overflow-y-auto">
          {entries.length === 0 && !busy ? (
            <p className="px-5 py-8 text-center text-[12px] text-gray-400">
              {t('还没有消耗记录。本地配音、人声分离与导出不消耗积分。')}
            </p>
          ) : (
            <table className="w-full text-[11px]">
              <thead className="sticky top-0 bg-[#FBFAF7]">
                <tr className="border-b border-claude-border text-left text-[9px] uppercase tracking-wider text-gray-400">
                  <th className="px-5 py-1.5 font-bold">{t('时间')}</th>
                  <th className="py-1.5 font-bold">{t('项目')}</th>
                  <th className="py-1.5 font-bold">{t('说明')}</th>
                  <th className="px-5 py-1.5 text-right font-bold">{t('积分')}</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => (
                  <tr key={e.id} className="border-b border-claude-border/50 last:border-0">
                    <td className="whitespace-nowrap px-5 py-1.5 tabular-nums text-gray-400">{timeLabel(e.at)}</td>
                    <td className="whitespace-nowrap py-1.5 text-claude-charcoal">{t(STEP_LABEL[e.step] ?? e.step)}</td>
                    <td className="max-w-[180px] truncate py-1.5 text-gray-500">{e.detail || '—'}</td>
                    <td className="whitespace-nowrap px-5 py-1.5 text-right font-bold tabular-nums text-claude-text">
                      {e.cost_source === 'free' ? t('免费') : `−${pts(e.points_micro)}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <p className="border-t border-claude-border px-5 py-2.5 text-[10px] leading-relaxed text-gray-400">
          {t('消耗以积分计（1 积分 ≈ 0.01 元，已含服务费）。本地配音、人声分离与导出不消耗积分。')}
        </p>
      </div>
    </div>
  );
};

export default BillingPanel;
